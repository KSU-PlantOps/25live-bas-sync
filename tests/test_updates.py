"""Checking GitHub for a newer release."""

import pytest
import requests

from bassync import updates


def _rel(tag, prerelease=False, draft=False):
    return {"tag_name": tag, "name": f"{tag} — title", "prerelease": prerelease,
            "draft": draft, "html_url": f"https://example/{tag}",
            "published_at": "2026-10-01T12:00:00Z"}


@pytest.mark.parametrize("older, newer", [
    ("1.2.0", "1.3.0rc1"), ("1.3.0a1", "1.3.0b1"), ("1.3.0rc1", "1.3.0rc2"),
    ("1.3.0rc2", "1.3.0"), ("1.3.0", "1.3.1"), ("V1.1", "v1.2.0"), ("1.9.0", "1.10.0"),
])
def test_versions_sort_like_pep_440(older, newer):
    assert updates.parse_version(older) < updates.parse_version(newer)


def test_unparseable_versions_are_ignored():
    assert updates.parse_version("release") is None
    assert updates.parse_version("V1.0Beta2") is None
    assert updates.is_prerelease("1.3.0rc1") and not updates.is_prerelease("1.3.0")


def test_the_newest_release_by_number_skipping_drafts_and_prereleases():
    releases = [_rel("v1.3.0rc1", prerelease=True), _rel("v1.2.0"), _rel("V1.1"),
                _rel("v1.4.0", draft=True), _rel("release")]
    assert updates.newest(releases, include_prereleases=False)["tag_name"] == "v1.2.0"
    assert updates.newest(releases, include_prereleases=True)["tag_name"] == "v1.3.0rc1"
    assert updates.newest([], True) is None and updates.newest("junk", True) is None


def test_a_newer_release_is_available():
    checker = updates.UpdateChecker("1.2.0", fetch=lambda: [_rel("v1.2.1"), _rel("v1.2.0")])
    status = checker.status(background=False)
    assert status["available"] and status["latest"]["version"] == "1.2.1"
    assert status["checked_at"] and not status["error"]


def test_prereleases_are_offered_only_to_prerelease_users():
    releases = [_rel("v1.3.0rc2", prerelease=True), _rel("v1.2.0")]
    stable = updates.UpdateChecker("1.2.0", fetch=lambda: releases).status(background=False)
    assert not stable["available"]
    rc = updates.UpdateChecker("1.3.0rc1", fetch=lambda: releases).status(background=False)
    assert rc["available"] and rc["latest"]["version"] == "1.3.0rc2"


def test_checks_are_spaced_out_and_failures_retry_sooner():
    now = [1000.0]
    calls = []

    def fetch():
        calls.append(now[0])
        if len(calls) == 1:
            raise requests.ConnectionError("no route to host")
        return [_rel("v1.2.0")]

    checker = updates.UpdateChecker("1.2.0", fetch=fetch, clock=lambda: now[0])
    status = checker.status(background=False)
    assert status["error"] == "no connection to api.github.com from here"
    assert not status["available"]
    checker.status(background=False)
    assert len(calls) == 1                                  # not again straight away
    now[0] += updates.RETRY_AFTER
    assert not checker.status(background=False)["error"]
    now[0] += updates.CHECK_EVERY - 1
    checker.status(background=False)
    assert len(calls) == 2


def test_turned_off_it_never_asks():
    def fetch():
        raise AssertionError("asked GitHub")
    status = updates.UpdateChecker("1.2.0", fetch=fetch).status(enabled=False, background=False)
    assert not status["available"] and status["checked"] is None


@pytest.mark.parametrize("exc, words", [
    (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED ..."), "intercepting proxy"),
    (requests.Timeout("read timed out"), "didn't answer in time"),
    (ValueError("Expecting value"), "didn't make sense"),
])
def test_failures_are_described_briefly(exc, words):
    assert words in updates.describe_error(exc)
