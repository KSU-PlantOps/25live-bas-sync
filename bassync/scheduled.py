# 25Live -> BAS Schedule Sync — what was last written, per space
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
What the sync last wrote, kept per schedule and per space, for the web UI's
Schedules pages — so the people who look after rooms (an events team, say)
can see what a room is scheduled to do without reading a whole run report.

Each live sync that gets as far as writing records it in state/scheduled.json:

    schedules   { "system:target": {system, target, label, kind, status,
                                     error, at, windows: [[start, end], ...]} }
    spaces      { space_id: {name, building, floor, at, schedules: [keys],
                             bookings: [{id, title, start, end, on, off,
                                         low_temp, extra}, ...]} }

`on`/`off` are when the space's schedules run for a booking (its run-up and
run-down included); `start`/`end` are the booking itself. A schedule whose
write failed, or that was held back, keeps the windows an earlier sync
wrote, with this run's status. A sync limited to some systems or buildings
updates only their part. Saving is best-effort, like the run history: a
full disk costs this page, never the run.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

FILE_NAME = "scheduled.json"
KIND_LABELS = {"room": "Its own schedule", "equipment": "Equipment",
               "floor": "Floor corridor", "building": "Building",
               "low_temp": "Low temp"}


def path_for(cfg: dict) -> Path:
    """state/scheduled.json, beside the safety state."""
    return Path(cfg["safety"]["state_file"]).with_name(FILE_NAME)


def load(path) -> dict:
    """The last record, or an empty one."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schedules", {})
    data.setdefault("spaces", {})
    return data


def _kinds(space_map) -> dict:
    kinds: dict = {}
    for dest in space_map.floors.values():
        kinds[dest] = "floor"
    for dest in space_map.equipment.values():
        kinds[dest] = "equipment"
    for info in space_map.buildings.values():
        kinds[info.destination] = "building"
    for dest in space_map.low_temp:
        kinds[dest] = "low_temp"
    for sc in space_map.spaces.values():
        if sc.destination is not None:
            kinds.setdefault(sc.destination, "room" if sc.space_type == "room" else "building")
    return kinds


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _booking(ev) -> dict:
    return {"id": str(ev.event_id), "title": ev.title,
            "start": _iso(ev.booked_start or ev.start), "end": _iso(ev.booked_end or ev.end),
            "on": _iso(ev.start), "off": _iso(ev.end), "low_temp": bool(ev.low_temp),
            "extra": str(ev.event_id).startswith("extra:")}


def record(path, report, space_map, events: list, in_scope=None) -> bool:
    """Fold this run's writes into the record. `in_scope` is the limited
    run's test on a destination (None for a full run)."""
    at = report.started.isoformat()
    previous = load(path)
    managed = {str(d) for d in space_map.destinations()} | {str(d) for d in space_map.held}
    kinds = {str(dest): kind for dest, kind in _kinds(space_map).items()}
    schedules = {k: v for k, v in previous["schedules"].items() if k in managed}
    for result in report.schedules:
        key = f"{result.system}:{result.target}"
        entry = dict(schedules.get(key) or {"windows": [], "at": None})
        entry.update(system=result.system, target=result.target, label=result.label,
                     status=result.status, error=result.error)
        entry["kind"] = kinds.get(key, entry.get("kind", "room"))
        if result.status in ("written", "preview"):
            entry["windows"] = [[w.start.isoformat(), w.end.isoformat()]
                                for w in result.windows]
            entry["at"] = at
        else:
            entry["tried"] = at
        schedules[key] = entry

    by_space: dict = {}
    for ev in events:
        by_space.setdefault(str(ev.space_id), []).append(ev)
    spaces = {k: v for k, v in previous["spaces"].items() if k in space_map.spaces}
    for sid, sc in space_map.spaces.items():
        dests = [*sc.all_destinations(), *sc.low_temp_destinations]
        if in_scope is not None and not any(in_scope(d) for d in dests):
            continue
        spaces[sid] = {
            "name": sc.space_name, "type": sc.space_type,
            "building": sc.building_id or "", "floor": sc.floor,
            "at": at, "schedules": [str(d) for d in dests],
            "bookings": [_booking(ev) for ev in
                         sorted(by_space.get(sid, []), key=lambda e: (e.start, e.event_id))],
        }
    data = {"updated": at, "full": in_scope is None, "schedules": schedules,
            "spaces": spaces}
    p = Path(path)
    tmp = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".scheduled.", suffix=".tmp", dir=p.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, p)
    except (OSError, TypeError, ValueError) as exc:
        logging.warning("Could not save what was scheduled to %s: %s", p, exc)
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
        return False
    return True
