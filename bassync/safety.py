# 25Live -> BAS Schedule Sync — mass-clear safety rail
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Refuses to turn the campus off by accident.

The failure that matters here is not a crash — it is a *successful-looking*
run that writes empty schedules everywhere. An expired 25Live service account,
a changed `state` query parameter, a Series25 version bump that renames an XML
element: each one returns HTTP 200 with zero events, and the sync then
faithfully clears every schedule it manages. Nobody notices until Monday
morning when the buildings are cold.

So a run compares itself to the last one and stops if too much occupancy
disappeared at once:

  * fewer than `min_events` assignments came back from 25Live at all, or
  * more than `max_cleared_fraction` of the schedules that had bookings last
    time would be emptied now.

Both are deliberately about *change*, not absolute counts — a genuinely quiet
week (spring break) still has last week's state to compare against, and a
first-ever run has nothing to compare so it is allowed through.

The comparison needs the previous run's state, so losing that file silently
disables half the rail. It therefore lives in its own `state/` directory (not
under logs/, which people clean out), is written atomically with the previous
copy kept as a fallback, and a run without a baseline says so at WARNING
level every time rather than quietly passing.

`--force` overrides, and is the right answer at the end of a semester when the
drop is real.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import paths


@dataclass
class SafetyVerdict:
    ok: bool
    reason: str = ""
    baseline: str = ""          # where the comparison state came from

    def __bool__(self) -> bool:
        return self.ok


def _read(path) -> Optional[dict]:
    """The state at `path`, or None if missing, unreadable or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _backup_path(path) -> Path:
    p = Path(path)
    return p.with_name(p.name + ".prev")


def load_state_with_status(path: str) -> tuple:
    """
    (previous run's state, where it came from).

    Tries, in order: the state file; its `.prev` copy (a run interrupted
    mid-save, or a corrupted file); and — for the default location only —
    the 1.x location under logs/, so an upgrade keeps its baseline.

    The status is one of "ok", "backup", "legacy", "corrupt" (a file exists
    but nothing usable could be read) or "missing".
    """
    exists = bool(path) and Path(path).exists()
    data = _read(path) if path else None
    if data is not None:
        return data, "ok"
    if path:
        backup = _read(_backup_path(path))
        if backup is not None:
            logging.warning("Safety state %s is unreadable; using the previous "
                            "copy %s.", path, _backup_path(path))
            return backup, "backup"
    if path and Path(path) == paths.state_file():
        legacy = _read(paths.legacy_state_file())
        if legacy is not None:
            logging.info("Using the 1.x safety state at %s as the baseline; it "
                         "moves to %s after this run.",
                         paths.legacy_state_file(), path)
            return legacy, "legacy"
    return {}, ("corrupt" if exists else "missing")


def load_state(path: str) -> dict:
    """Previous run's per-destination window counts ({} if none)."""
    return load_state_with_status(path)[0]


def save_state(path: str, schedule: dict, event_count: int,
               only_system: Optional[str] = None, merge: bool = False) -> bool:
    """
    Record what this run wrote, for the next run to compare against.

    Merges into the existing state rather than replacing it when the run only
    covered part of the campus:

      * `only_system` — a `--system` run knows nothing about the other systems'
        schedules, and overwriting their baseline with silence would make the
        NEXT full run look like a campus-wide clear.
      * `merge` — a partially failed run. Only the destinations that actually
        got written are passed in; everything else keeps its prior baseline.

    Written atomically (temp file + rename) so a crash mid-save can't leave a
    truncated file, and the previous file is kept as `.prev`. Returns False
    (after logging why) if the state could not be saved.
    """
    windows = {str(dest): len(w) for dest, w in schedule.items()}
    if only_system or merge:
        merged = (load_state(path).get("windows") or {})
        merged.update(windows)
        windows = merged
    payload = {
        "written_at": datetime.now().astimezone().isoformat(),
        "event_count": event_count,
        "windows": windows,
    }
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            if p.exists():
                os.replace(p, _backup_path(p))
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:
        logging.error("Could not save the safety state to %s: %s — the "
                      "mass-clear comparison will have NO baseline next run. "
                      "Make that directory writable (in Docker, mount a volume "
                      "at /app/state).", path, exc)
        return False
    return True


def check(cfg: dict, schedule: dict, all_destinations: set,
          event_count: int, previous: Optional[dict] = None) -> SafetyVerdict:
    """
    Decide whether this run is safe to write.

    `schedule` is what was built (destinations with bookings); anything in
    `all_destinations` and not in `schedule` gets cleared.

    The comparison is scoped to `all_destinations`, which handles both a
    `--system` run (where it holds only that system's schedules) and rooms
    removed from the map since the last run.
    """
    safety = cfg.get("safety") or {}
    if not safety.get("enabled", True):
        return SafetyVerdict(True, "safety checks disabled in config", "disabled")

    min_events = int(safety.get("min_events", 1))
    if event_count < min_events:
        return SafetyVerdict(False, (
            f"25Live returned {event_count} space assignment(s), below "
            f"min_events={min_events}. That usually means an auth or query "
            "problem rather than an empty campus — writing now would clear "
            f"all {len(all_destinations)} schedule(s). Check the 25Live "
            "credentials and the `state` parameter, or re-run with --force if "
            "the campus really is empty."))

    status = "provided"
    if previous is None:
        previous, status = load_state_with_status(safety.get("state_file") or "")
    prior_windows = previous.get("windows") or {}
    if not prior_windows:
        where = safety.get("state_file") or "(no state_file)"
        if status == "corrupt":
            note = (f"the safety state {where} is corrupt and has no usable "
                    "backup")
        else:
            note = f"no safety state at {where}"
        logging.warning(
            "No baseline for the mass-clear check (%s), so only min_events "
            "guards this run. That is expected on the very first run. If it "
            "happens every night, the state directory is not persisting — in "
            "Docker, mount a volume at /app/state.", note)
        return SafetyVerdict(True, "no previous run to compare against", status)

    # Only schedules this run still manages can be "cleared" by it. A room
    # taken out of the map — decommissioned, handed to a contractor, moved to
    # another system — is no longer written at all, so counting it as cleared
    # would block the next sync with a false alarm about buildings nobody is
    # touching.
    managed = {str(d) for d in all_destinations}
    prior_windows = {k: v for k, v in prior_windows.items() if k in managed}
    previously_occupied = {k for k, v in prior_windows.items() if v}
    written_at = previous.get("written_at", "?")
    if not previously_occupied:
        return SafetyVerdict(True, "no previously-occupied schedules to compare",
                             f"{status}, from {written_at}")

    now_occupied = {str(dest) for dest, windows in schedule.items() if windows}
    cleared = previously_occupied - now_occupied
    fraction = len(cleared) / len(previously_occupied)
    limit = float(safety.get("max_cleared_fraction", 0.34))
    baseline = f"{status}, from {written_at}"

    if fraction > limit:
        sample = ", ".join(sorted(cleared)[:5])
        more = f" (+{len(cleared) - 5} more)" if len(cleared) > 5 else ""
        return SafetyVerdict(False, (
            f"{len(cleared)} of {len(previously_occupied)} previously-occupied "
            f"schedules ({fraction:.0%}) would be cleared this run, above "
            f"max_cleared_fraction={limit:.0%}. Affected: {sample}{more}. If "
            "this drop is real — semester break, a building taken offline — "
            "re-run with --force. Otherwise check 25Live before writing."),
            baseline)

    return SafetyVerdict(True, (
        f"{len(cleared)}/{len(previously_occupied)} previously-occupied "
        f"schedules clearing ({fraction:.0%}), within the "
        f"{limit:.0%} limit"), baseline)
