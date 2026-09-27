"""The release helper behind .github/workflows/release.yml."""

import importlib.util
from pathlib import Path

import pytest

import bassync

_PATH = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release_info.py"
_spec = importlib.util.spec_from_file_location("release_info", _PATH)
assert _spec and _spec.loader
release_info = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_info)

REPO = "KSU-PlantOps/25live-bas-sync"
IMAGE = "ghcr.io/ksu-plantops/25live-bas-sync"
OLD_TAGS = ["V1.0", "V1.0Beta2", "V1.0RC1", "V1.1", "release"]

CHANGELOG = """\
# Changelog

## [1.3.0] — 2026-10-01 — Faster roll-ups

Intro line using [BACpypes3].

### Added
- Something.

```text
## not a heading inside a code block
```

## [1.2.0] — 2026-09-27

### Fixed
- Another thing.

[BACpypes3]: https://github.com/JoelBender/BACpypes3
"""


def test_this_version_is_releasable():
    """The version in bassync is what gets released, and CHANGELOG describes it."""
    version = release_info.read_version()
    assert version == bassync.__version__
    release_info.parse_version(version)
    section = release_info.changelog_section(
        release_info.CHANGELOG.read_text(encoding="utf-8"), version)
    assert section.body


@pytest.mark.parametrize("text, number, pre", [
    ("1.2.0", (1, 2, 0), False),
    ("10.0.3", (10, 0, 3), False),
    ("1.3.0rc1", (1, 3, 0), True),
    ("1.3.0b2", (1, 3, 0), True),
    ("1.3.0a1", (1, 3, 0), True),
])
def test_parse_version(text, number, pre):
    v = release_info.parse_version(text)
    assert (v.number, v.prerelease) == (number, pre)


@pytest.mark.parametrize("text", ["1.2", "v1.2.0", "1.2.0.post1", "1.2.0-rc1", ""])
def test_unreleasable_versions_are_refused(text):
    with pytest.raises(release_info.ReleaseError):
        release_info.parse_version(text)


def test_section_body_title_and_boundaries():
    section = release_info.changelog_section(CHANGELOG, "1.3.0")
    assert section.title == "Faster roll-ups"
    assert section.body.startswith("Intro line")
    assert "## not a heading inside a code block" in section.body   # fenced, kept
    assert "Another thing" not in section.body                       # next section
    # A link defined elsewhere in the file comes along, so it still resolves.
    assert section.body.endswith("[BACpypes3]: https://github.com/JoelBender/BACpypes3")


def test_section_without_a_title():
    section = release_info.changelog_section(CHANGELOG, "1.2.0")
    assert section.title is None
    assert section.body.startswith("### Fixed")
    assert section.body.count("[BACpypes3]:") == 1    # already there; not duplicated


@pytest.mark.parametrize("rest, title", [
    ("", None),
    (" — 2026-09-27", None),
    (" - 2026-09-27", None),
    (" — Unreleased", None),
    (" — 2026-09-27 — Faster roll-ups", "Faster roll-ups"),
    (" – 2026-09-27 – A title - with a hyphen", "A title - with a hyphen"),
    (" — A title and no date", "A title and no date"),
])
def test_heading_titles(rest, title):
    assert release_info._heading_title(rest) == title


def test_missing_or_empty_section_is_an_error():
    with pytest.raises(release_info.ReleaseError, match=r"no '## \[9.9.9\]' section"):
        release_info.changelog_section(CHANGELOG, "9.9.9")
    with pytest.raises(release_info.ReleaseError, match="is empty"):
        release_info.changelog_section("## [1.0.0]\n\n## [0.9.0]\n- x\n", "1.0.0")


def test_existing_tag_matches_any_capitalisation():
    assert release_info.existing_tag(["V1.1", "v1.2.0"], "1.2.0") == "v1.2.0"
    assert release_info.existing_tag(["V1.2.0"], "1.2.0") == "V1.2.0"
    assert release_info.existing_tag(OLD_TAGS, "1.2.0") is None
    assert release_info.existing_tag(["v1.2.0rc1"], "1.2.0") is None


def test_latest_ignores_old_prereleases_and_junk_tags():
    v = release_info.parse_version
    assert release_info.is_latest(v("1.2.0"), OLD_TAGS)
    assert not release_info.is_latest(v("1.1.1"), OLD_TAGS + ["v1.2.0"])   # a backport
    assert not release_info.is_latest(v("1.3.0rc1"), OLD_TAGS)
    assert release_info.is_latest(v("1.2.0"), ["v1.2.0rc1"])   # newer than its own RC


def test_docker_tags():
    v = release_info.parse_version
    assert release_info.docker_tags(IMAGE, v("1.2.0"), True) == [
        f"{IMAGE}:1.2.0", f"{IMAGE}:1.2", f"{IMAGE}:latest"]
    assert release_info.docker_tags(IMAGE, v("1.1.1"), False) == [
        f"{IMAGE}:1.1.1", f"{IMAGE}:1.1"]
    assert release_info.docker_tags(IMAGE, v("1.3.0rc1"), False) == [f"{IMAGE}:1.3.0rc1"]


def _plan(event="push", ref="refs/heads/main", tags=OLD_TAGS, version="1.3.0"):
    return release_info.make_plan(version, CHANGELOG, list(tags), REPO, event, ref)


def test_a_new_version_on_main_is_published():
    plan = _plan()
    assert plan.publish and plan.build
    assert plan.tag == "v1.3.0"
    assert plan.title == "v1.3.0 — Faster roll-ups"
    assert plan.image == IMAGE
    outputs = dict(line.split("=", 1) for line in release_info.plan_outputs(plan))
    assert outputs["new"] == "true" and outputs["latest"] == "true"
    assert outputs["docker_tags"] == f"{IMAGE}:1.3.0,{IMAGE}:1.3,{IMAGE}:latest"
    assert _plan(event="workflow_dispatch").publish


def test_released_versions_and_other_branches_do_not_publish():
    released = _plan(tags=OLD_TAGS + ["v1.3.0"])
    assert not released.publish and not released.build     # an ordinary push: nothing
    assert released.existing_tag == "v1.3.0"
    # Pull requests and manual runs elsewhere build a dry run but never publish.
    for plan in (_plan(event="pull_request", ref="refs/pull/9/merge"),
                 _plan(event="workflow_dispatch", ref="refs/heads/feature"),
                 _plan(event="workflow_dispatch", tags=OLD_TAGS + ["v1.3.0"])):
        assert plan.build and not plan.publish


def test_untitled_section_gives_a_plain_title():
    assert _plan(version="1.2.0").title == "v1.2.0"


def test_release_notes_add_install_lines():
    section = release_info.changelog_section(CHANGELOG, "1.3.0")
    url = f"https://github.com/{REPO}"
    notes = release_info.release_notes(section, "v1.3.0", "1.3.0", IMAGE, url,
                                       "25live_bas_sync-1.3.0-py3-none-any.whl")
    assert notes.startswith(section.body)
    assert f"docker pull {IMAGE}:1.3.0" in notes
    assert 'pip install "./25live_bas_sync-1.3.0-py3-none-any.whl[bacnet]"' in notes
    assert f"{url}/blob/v1.3.0/CHANGELOG.md" in notes
    # The docs they point at are the ones for this release, and they exist.
    for page in ("docker.md", "upgrading.md"):
        assert f"{url}/blob/v1.3.0/docs/{page})" in notes
        assert (release_info.ROOT / "docs" / page).is_file()
    assert "pip install" not in release_info.release_notes(
        section, "v1.3.0", "1.3.0", IMAGE, url)


def test_cli_check_and_notes(tmp_path, capsys):
    assert release_info.main(["check"]) == 0
    assert bassync.__version__ in capsys.readouterr().out
    out = tmp_path / "notes.md"
    assert release_info.main(["notes", "--repository", REPO, "--out", str(out)]) == 0
    assert f"docker pull {IMAGE}:{bassync.__version__}" in out.read_text(encoding="utf-8")


def test_cli_plan_with_given_tags(capsys):
    assert release_info.main(["plan", "--repository", REPO, "--event", "push",
                              "--ref", "refs/heads/main", "--tags", *OLD_TAGS]) == 0
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert outputs["version"] == bassync.__version__
    assert outputs["publish"] == "true"


def test_cli_reports_problems_without_a_traceback(capsys):
    assert release_info.main(["plan", "--repository", "", "--tags"]) == 1
    assert "--repository" in capsys.readouterr().err
