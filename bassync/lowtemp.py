# 25Live -> BAS Schedule Sync — events marked low temp
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Events marked low temp: 25Live events whose rooms should run colder than
usual — a blood drive, a crowded exam. They're marked on the web UI's
Schedules pages (the `low_temp` capability) and kept in low_temp_events.yaml,
beside config.yaml:

    events:
      - event_id: "48213"
        name: Red Cross blood drive      # for people: 25Live's name when marked
        marked_by: Jane Doe
        marked_at: 2026-09-30T10:12

A marked event drives the low-temp schedule (`low_temp_target:` in the room
map) of every room it books, and of those rooms' equipment, for each of its
bookings — with the room's run-up and run-down — from the next sync on. What
"colder" means is set in the BAS; the sync only says when.

A room's extra booking can be low temp too (`low_temp: true` in
extra_bookings.yaml, bassync/extras.py).
"""

from pathlib import Path
from typing import Optional

from .config import ConfigError, read_yaml

FILE_NAME = "low_temp_events.yaml"
KEYS = ("event_id", "name", "marked_by", "marked_at", "note")
HEADER = """\
# 25Live events whose rooms run colder: each drives the low-temp schedule
# (`low_temp_target:` in space_mapping.yaml) of every room it books. Managed on
# the web UI's Schedules pages, but plain YAML and safe to hand-edit. See
# bassync/lowtemp.py.
"""


def default_path(config_path) -> Path:
    return Path(config_path).parent / FILE_NAME


def read(path) -> list:
    """The marks as written (dicts), [] if there is no file. Raises
    ConfigError if the file exists but can't be read."""
    if not path or not Path(path).exists():
        return []
    rows = read_yaml(path).get("events")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ConfigError(f"{path}: `events:` must be a list.")
    return [r for r in rows if isinstance(r, dict)]


def event_id_of(row: dict) -> str:
    return str(row.get("event_id") if row.get("event_id") is not None else "").strip()


def event_ids(path) -> set:
    """Every marked event id. Raises ConfigError if the file can't be read."""
    return {eid for eid in (event_id_of(r) for r in read(path)) if eid}


def find(rows: list, event_id) -> Optional[dict]:
    wanted = str(event_id).strip()
    return next((r for r in rows if event_id_of(r) == wanted), None)


def mark(rows: list, event_id, name: str = "", by: str = "", when: str = "",
         note: str = "") -> bool:
    """Add a mark; False if the event was already marked."""
    event_id = str(event_id).strip()
    if not event_id or find(rows, event_id) is not None:
        return False
    row = {"event_id": event_id, "name": name.strip()[:200], "marked_by": by,
           "marked_at": when, "note": note.strip()[:500]}
    rows.append({k: v for k, v in row.items() if v})
    return True


def unmark(rows: list, event_id) -> bool:
    """Remove a mark; False if the event wasn't marked."""
    row = find(rows, event_id)
    if row is None:
        return False
    rows.remove(row)
    return True


def dump(rows: list) -> str:
    import yaml
    clean = []
    for row in rows:
        out = {k: str(row[k]) for k in KEYS if row.get(k) not in (None, "")}
        if out.get("event_id"):
            clean.append(out)
    body = yaml.safe_dump({"events": clean}, sort_keys=False, default_flow_style=False,
                          allow_unicode=True, width=100)
    return HEADER + "\n" + body


def save(path, rows: list) -> bool:
    from .mapedit import write_if_changed
    return write_if_changed(path, dump(rows))
