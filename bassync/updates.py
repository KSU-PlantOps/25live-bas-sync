# 25Live -> BAS Schedule Sync — is there a newer release?
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Whether a newer release is out, from GitHub's public releases API: asked at
most every twelve hours, in the background, so a page never waits on it and a
network with no route to GitHub only means "couldn't check".

Only telling: the container can't replace its own image (that would need the
Docker socket, which is root on the host). The web UI shows the commands, and
docs/docker.md has a host-side timer that updates automatically.

A pre-release is only offered to someone already running one.
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from . import __version__

REPOSITORY = "KSU-PlantOps/25live-bas-sync"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=20"
CHECK_EVERY = 12 * 3600
RETRY_AFTER = 3600                 # after a failed check
_VERSION = re.compile(r"^[vV]?(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?$")
_STAGE = {"a": 0, "b": 1, "rc": 2, None: 3}


def parse_version(text: str) -> Optional[tuple]:
    """"v1.3.0rc1" -> (1, 3, 0, 2, 1); None for anything else. Sorts the way
    PEP 440 does for these: 1.3.0a1 < 1.3.0rc1 < 1.3.0 < 1.3.1."""
    m = _VERSION.match(str(text or "").strip())
    if not m:
        return None
    return (int(m[1]), int(m[2]), int(m[3] or 0), _STAGE[m[4]], int(m[5] or 0))


def is_prerelease(text: str) -> bool:
    parsed = parse_version(text)
    return parsed is not None and parsed[3] < 3


def newest(releases: list, include_prereleases: bool) -> Optional[dict]:
    """The newest usable release in GitHub's list, by version number."""
    best, best_key = None, None
    for rel in releases if isinstance(releases, list) else []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        key = parse_version(rel.get("tag_name", ""))
        if key is None or (rel.get("prerelease") and not include_prereleases):
            continue
        if best_key is None or key > best_key:
            best, best_key = rel, key
    return best


def describe_error(exc: Exception) -> str:
    """A failed check in a few words, not a stack of wrapped exceptions."""
    if isinstance(exc, requests.exceptions.SSLError):
        return ("the TLS certificate api.github.com presented didn't verify — is "
                "there an intercepting proxy?")
    if isinstance(exc, requests.Timeout):
        return "api.github.com didn't answer in time"
    if isinstance(exc, requests.ConnectionError):
        return "no connection to api.github.com from here"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"GitHub answered HTTP {exc.response.status_code}"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "GitHub's answer didn't make sense"
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:160]


class UpdateChecker:
    def __init__(self, current: str = __version__, fetch: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time):
        self.current = current
        self._fetch = fetch or self._get
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self.checked: Optional[float] = None
        self.next_check = 0.0
        self.latest: Optional[dict] = None
        self.error = ""

    @staticmethod
    def _get() -> list:
        response = requests.get(API_URL, timeout=15, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"25live-bas-sync/{__version__}"})
        response.raise_for_status()
        return response.json()

    def refresh(self) -> None:
        """Ask GitHub now (blocking)."""
        try:
            releases = self._fetch()
            rel = newest(releases, is_prerelease(self.current))
            latest = None
            if rel is not None:
                latest = {"version": str(rel["tag_name"]).lstrip("vV"),
                          "name": rel.get("name") or rel["tag_name"],
                          "url": rel.get("html_url") or "",
                          "published": rel.get("published_at") or "",
                          "prerelease": bool(rel.get("prerelease"))}
            with self._lock:
                self.latest, self.error = latest, ""
                self.checked = self._clock()
                self.next_check = self.checked + CHECK_EVERY
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            logging.info("[updates] couldn't check for a newer release: %s", exc)
            with self._lock:
                self.error = describe_error(exc)
                self.checked = self._clock()
                self.next_check = self.checked + RETRY_AFTER
        finally:
            with self._lock:
                self._running = False

    def status(self, enabled: bool = True, background: bool = True) -> dict:
        """What's known, starting a background check when it's due."""
        if enabled:
            with self._lock:
                due = not self._running and self._clock() >= self.next_check
                if due:
                    self._running = True
            if due:
                if background:
                    threading.Thread(target=self.refresh, daemon=True, name="updates").start()
                else:
                    self.refresh()
        with self._lock:
            latest = self.latest
            available = latest is not None and (parse_version(latest["version"]) or ()) > (
                parse_version(self.current) or ())
            checked_at = (datetime.fromtimestamp(self.checked, timezone.utc).isoformat()
                          if self.checked else None)
            return {"enabled": enabled, "current": self.current, "checked": self.checked,
                    "checked_at": checked_at,
                    "latest": latest, "available": enabled and available,
                    "error": self.error, "checking": self._running}
