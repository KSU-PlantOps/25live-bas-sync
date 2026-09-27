# 25Live -> BAS Schedule Sync — editing the room map and settings files
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Read, change, check and write the files an operator edits: space_mapping.yaml,
defaults.yaml and config.yaml.

Shared by both editors — the desktop one (editor.py, Tkinter) and the web UI
(bassync/web) — so a row one of them accepts, the other accepts too. Nothing
here imports a GUI toolkit or a web framework; tests/test_editor.py and
tests/test_mapedit.py exercise it headlessly.
"""

import copy
import os
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from . import paths

DEFAULT_MAP_FILE = Path(os.environ.get("BAS_SPACE_MAP") or paths.space_map_file())

# Written to the top of the file on save so a hand-editor knows the format.
FILE_HEADER = """\
# 25Live → BAS schedule cross-reference.
#
# This file is managed by the web UI and editor.py, but is plain YAML and
# safe to hand-edit. See README.md for the full field reference.
#
#   buildings:  each building's roll-up schedule, defined once.
#   floors:     optional per-floor corridor schedules (building + level).
#   spaces:     the rooms; each names its `building` and optionally its `floor`.
#   campus:     optional, on a building — a label for people; the sync ignores it.
#
# Occupancy rolls up room -> floor -> building: a room being booked runs its own
# zone, its floor's corridor, and its building's common areas.
#
#   system:  which BAS the schedule lives on — a key from `systems:` in
#            config.yaml. Omit to inherit (room/floor from its building,
#            building from default_system).
#   target:  the schedule's address in that system:
#              bacnet   "12001:5"        device instance : schedule instance
#                                        ("@10.4.2.30" pins the address)
#                                        (a Niagara station: its BACnet-
#                                        exported schedule, e.g. "2001:1")
#              niagara  "Bldg/Rm101_Occ" deprecated driver; ORD
#              rest     whatever your API path template expects
"""

# Field order we emit so the file reads cleanly and diffs stay stable.
BUILDING_KEY_ORDER = ["id", "name", "campus", "system", "target",
                      "pre_condition_minutes", "post_buffer_minutes",
                      "merge_gap_minutes", "space_id", "note"]
ROOM_KEY_ORDER = ["space_id", "space_name", "building", "floor", "system", "target",
                  "pre_condition_minutes", "post_buffer_minutes",
                  "merge_gap_minutes", "note"]
FLOOR_KEY_ORDER = ["building", "level", "name", "system", "target", "note"]

# Pre-1.0 key name. Read and migrated to `target` on load, so an existing map
# opens, edits and saves without anyone doing a find-and-replace.
LEGACY_TARGET_KEY = "niagara_path"

DEFAULT_DEFAULTS_FILE = Path(os.environ.get("BAS_DEFAULTS") or paths.defaults_file())

# Global scheduling defaults shown on the "Defaults" tab:
# (yaml key, label, built-in fallback).
DEFAULTS_FIELDS = [
    ("pre_condition_minutes", "Run-up (pre-condition) minutes", 30),
    ("post_buffer_minutes",   "Run-down (post-buffer) minutes", 15),
    ("merge_gap_minutes",     "Merge-gap minutes", 5),
    ("lookahead_days",        "Lookahead days", 7),
]

DEFAULTS_HEADER = """\
# Global scheduling defaults. Edit here, or on the "Defaults" page/tab of the
# web UI or editor.py.
# Rooms/buildings may override pre/post in space_mapping.yaml
# (precedence: room > building > these globals).
"""


# ─────────────────────────────────────────────────────────────────────────────
# YAML load / dump (no GUI — unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

def load_mapping(path) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Read space_mapping.yaml into (buildings, floors, rooms) lists of plain dicts.
    A missing or empty file yields three empty lists (fresh start).
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return [], [], []
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    buildings = [_migrate_row(b) for b in (data.get("buildings", []) or [])]
    floors = [_migrate_row(f) for f in (data.get("floors", []) or [])]
    rooms = [_migrate_row(r) for r in (data.get("spaces", []) or [])]
    return buildings, floors, rooms


def _migrate_row(row):
    """Rename a pre-1.0 `niagara_path:` to `target:`, preserving field order.

    Done on load rather than on save so the editor only ever deals in one key
    name, and an old map upgrades the first time someone saves it."""
    if not isinstance(row, dict) or LEGACY_TARGET_KEY not in row:
        return dict(row) if isinstance(row, dict) else row
    out: dict = {}
    for key, value in row.items():
        if key == LEGACY_TARGET_KEY:
            out.setdefault("target", value)
        else:
            out[key] = value
    return out


def _ordered(row: dict, key_order: list[str]) -> dict:
    """Return row's present keys in a stable order (others appended)."""
    out = {k: row[k] for k in key_order if k in row and row[k] not in (None, "")}
    for k, v in row.items():
        if k not in out and v not in (None, ""):
            out[k] = v
    return out


def dump_mapping(buildings: list[dict], floors: list[dict],
                 rooms: list[dict]) -> str:
    """Serialize (buildings, floors, rooms) back to YAML text with the header.
    The floors: section is only emitted when non-empty."""
    payload = {"buildings": [_ordered(b, BUILDING_KEY_ORDER) for b in buildings]}
    if floors:
        payload["floors"] = [_ordered(f, FLOOR_KEY_ORDER) for f in floors]
    payload["spaces"] = [_ordered(r, ROOM_KEY_ORDER) for r in rooms]
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    return FILE_HEADER + "\n" + body


def write_if_changed(path, text: str) -> bool:
    """
    Write `text` to `path` only if it differs from what is there, keeping the
    previous version as `<name>.bak`. Returns True if the file was written.

    Atomic: the new content goes to a temp file in the same folder and is
    renamed over the original, so the nightly sync can never read a
    half-written file, and a crash mid-save leaves the old file intact.
    Skipping unchanged files is what keeps the single .bak meaningful — a
    second Save with nothing changed no longer overwrites it with a copy of
    the current file.
    """
    p = Path(path)
    if p.exists():
        try:
            if p.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp makes the file owner-only; keep the old file's permissions (or
    # the usual rw-r--r--), or a sync running as another user — a cron job, a
    # service account — can no longer read the file after an edit.
    try:
        mode = p.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass                     # e.g. a filesystem without Unix modes
        if p.exists():
            backup = p.with_suffix(p.suffix + ".bak")
            backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def save_mapping(path, buildings: list[dict], floors: list[dict],
                 rooms: list[dict]) -> bool:
    """Write the mapping if it changed, keeping a .bak of the previous version."""
    return write_if_changed(path, dump_mapping(buildings, floors, rooms))


def unknown_building_refs(buildings: list[dict], rooms: list[dict]) -> list[str]:
    """space_ids of rooms whose `building` doesn't match any defined building."""
    known = {str(b.get("id")) for b in buildings}
    bad = []
    for r in rooms:
        b = r.get("building")
        if b is not None and str(b) not in known:
            bad.append(str(r.get("space_id")))
    return bad


def load_defaults(path) -> dict:
    """Read defaults.yaml, filling any missing or invalid key with its built-in
    fallback so the form always shows real numbers. A malformed, unreadable, or
    non-mapping file degrades to the built-in defaults rather than stopping the
    editor from opening."""
    data = {}
    p = Path(path)
    if p.exists() and p.stat().st_size:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (yaml.YAMLError, OSError):
            data = {}

    def _as_int(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    return {key: _as_int(data.get(key, fb), fb)
            for key, _label, fb in DEFAULTS_FIELDS}


def dump_defaults(values: dict) -> str:
    """Serialize the global defaults back to YAML text with the header."""
    payload = {key: int(values.get(key, fb)) for key, _label, fb in DEFAULTS_FIELDS}
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    return DEFAULTS_HEADER + "\n" + body


def save_defaults(path, values: dict) -> bool:
    """Write defaults.yaml if it changed, keeping a .bak of the previous one."""
    return write_if_changed(path, dump_defaults(values))


def building_dependents(building_id: str, floors: list[dict],
                        rooms: list[dict]) -> tuple:
    """
    What deleting a building would break: (its floors, rooms that roll up
    ONLY into it, rooms that also have their own target).

    A roll-up-only room left without its building drives nothing, which the
    sync reports as a broken row — so those have to be reassigned first.
    """
    bid = str(building_id)
    its_floors = [f for f in floors if str(f.get("building")) == bid]
    rollup_only: list = []
    with_target: list = []
    for r in rooms:
        if str(r.get("building")) != bid:
            continue
        (with_target if r.get("target") else rollup_only).append(r)
    return its_floors, rollup_only, with_target


def merge_form_result(original: dict, result: dict, form_keys) -> dict:
    """
    The edited row: the form's values, plus every key of the original row the
    form doesn't show. A form only knows its own fields; rebuilding the row
    from the form alone silently dropped anything else (a hand-added note, a
    building's merge_gap_minutes).
    """
    keep = {k: v for k, v in original.items() if k not in set(form_keys)}
    return {**keep, **result}


def target_problem(target: str, system_name: str, config_raw: dict) -> Optional[str]:
    """
    Why `target` is not a valid address for `system_name`'s driver, or None.
    Unknown systems/drivers are not the form's business — the loader reports
    those on Save.
    """
    if not target:
        return None
    sys_cfg = ((config_raw or {}).get("systems") or {}).get(system_name)
    if not isinstance(sys_cfg, dict) or not sys_cfg.get("driver"):
        return None
    from .drivers import DriverError, load_driver_class
    try:
        cls = load_driver_class(str(sys_cfg["driver"]))
    except DriverError:
        return None
    _canon, errors = cls.normalize_targets(sys_cfg, [target])
    return errors.get(target)


# ── row checks, shared by both editors ───────────────────────────────────────
# A row's `system` is optional: blank means "inherit" (a room or floor takes its
# building's) or "default" (a building takes default_system). The editors pass
# blank for both; the desktop editor maps its "(inherit)" labels to blank first.

def effective_system(values: dict, buildings: list[dict], config_raw: dict,
                     building_id=None) -> str:
    """The system a row would resolve to: its own, else its building's, else
    config.yaml's default_system (or the only system there is)."""
    name = values.get("system") or None
    if not name and building_id not in (None, ""):
        for b in buildings:
            if str(b.get("id")) == str(building_id):
                name = b.get("system")
                break
    if not name:
        raw = config_raw or {}
        systems = raw.get("systems") or {}
        name = (str(raw.get("default_system") or "").strip()
                or (next(iter(systems)) if len(systems) == 1 else ""))
    return str(name or "")


def target_error(values: dict, buildings: list[dict], config_raw: dict,
                 building_id=None) -> Optional[str]:
    """Why the row's target isn't valid for the system it resolves to."""
    target = values.get("target")
    if not target:
        return None
    system = effective_system(values, buildings, config_raw, building_id)
    problem = target_problem(str(target), system, config_raw or {})
    return f"Target for system '{system}':\n\n{problem}" if problem else None


def room_problem(values: dict, rooms: list[dict], buildings: list[dict],
                 config_raw: dict, index: Optional[int] = None) -> Optional[str]:
    """Why a room row can't be saved as it is, or None. `index` is the row
    being edited (None for a new one), so it doesn't clash with itself."""
    if values.get("space_id") in (None, ""):
        return "Space ID is required."
    taken = {str(r.get("space_id")) for j, r in enumerate(rooms) if j != index}
    if str(values["space_id"]) in taken:
        return f"Space ID {values['space_id']} is already used by another room."
    # A blank target is normal for a building that can only be scheduled per
    # floor or per air handler — the room still feeds its roll-ups. What it
    # must not do is drive nothing at all.
    building = values.get("building") or None
    if not values.get("target") and not building:
        return ("This room has no Target and no Building, so its bookings would "
                "drive nothing.\n\nEither give it a Target (its own schedule — "
                "e.g. \"12001:5\" for BACnet), or a Building to roll up into "
                "(optionally with a Floor).")
    return target_error(values, buildings, config_raw, building)


def building_problem(values: dict, buildings: list[dict], config_raw: dict,
                     index: Optional[int] = None) -> Optional[str]:
    """Why a building row can't be saved as it is, or None."""
    if not values.get("id"):
        return "Building ID is required."
    taken = {str(b.get("id")) for j, b in enumerate(buildings) if j != index}
    if str(values["id"]) in taken:
        return f"Building ID '{values['id']}' is already in use."
    if not values.get("target"):
        return ("Target is required — the schedule's address in its BAS (e.g. "
                "\"12001:5\" for BACnet).")
    return target_error(values, buildings, config_raw)


def floor_problem(values: dict, floors: list[dict], buildings: list[dict],
                  config_raw: dict, index: Optional[int] = None) -> Optional[str]:
    """Why a floor row can't be saved as it is, or None."""
    if not values.get("building"):
        return "Building is required."
    if values.get("level") in (None, ""):
        return "Floor # is required."
    if not values.get("target"):
        return "Corridor Target is required."
    taken = {(str(f.get("building")), str(f.get("level")))
             for j, f in enumerate(floors) if j != index}
    if (str(values["building"]), str(values["level"])) in taken:
        return (f"Floor {values['level']} of '{values['building']}' is already "
                "defined.")
    return target_error(values, buildings, config_raw, values.get("building"))


def rename_building(old_id, new_id, floors: list[dict], rooms: list[dict]) -> None:
    """Repoint the floors and rooms that name a building whose id changed."""
    old, new = str(old_id), str(new_id)
    if old == new:
        return
    for row in [*floors, *rooms]:
        if str(row.get("building")) == old:
            row["building"] = new


def building_delete_check(building_id, floors: list[dict],
                          rooms: list[dict]) -> tuple:
    """(reason it can't be deleted or None, what deleting it also does).

    A room that rolls up only into this building would be left driving
    nothing, so those have to be moved or given a target first. Its floors go
    with it; rooms with their own target stay, detached from it."""
    bid = str(building_id)
    its_floors, rollup_only, with_target = building_dependents(bid, floors, rooms)
    if rollup_only:
        ids = ", ".join(str(r.get("space_id")) for r in rollup_only[:20])
        more = f" (+{len(rollup_only) - 20} more)" if len(rollup_only) > 20 else ""
        return (f"These rooms roll up only into '{bid}' and have no target of "
                "their own, so without the building their bookings would drive "
                f"nothing:\n\n  {ids}{more}\n\nGive them a target, move them to "
                "another building, or delete them first."), ""
    notes = []
    if its_floors:
        notes.append(f"Its {len(its_floors)} floor schedule(s) are deleted with "
                     "it — a floor can't exist without its building.")
    if with_target:
        ids = ", ".join(str(r.get("space_id")) for r in with_target[:20])
        notes.append("These rooms keep their own schedules but no longer roll "
                     f"up into a building:\n  {ids}")
    return None, "\n\n".join(notes)


def delete_building(building_id, buildings: list[dict], floors: list[dict],
                    rooms: list[dict]) -> tuple:
    """(buildings, floors, rooms) without the building and its floors, and
    with rooms that had their own target detached from it. Call
    building_delete_check first: this doesn't refuse anything."""
    bid = str(building_id)
    kept_rooms = []
    for r in rooms:
        if str(r.get("building")) == bid:
            r = {k: v for k, v in r.items() if k not in ("building", "floor")}
        kept_rooms.append(r)
    return ([b for b in buildings if str(b.get("id")) != bid],
            [f for f in floors if str(f.get("building")) != bid],
            kept_rooms)


def validate_before_save(buildings: list[dict], floors: list[dict],
                         rooms: list[dict], config_raw: dict,
                         defaults: dict) -> tuple:
    """
    Run the files about to be saved through the sync's own loaders.

    Returns (config_error, map_errors, map_warnings). A config error means the
    nightly sync would refuse to start; map errors are rows it would leave out
    (or, with on_map_errors: abort, stop on). The editor's own form checks
    can't see cross-row problems — a floor whose building was deleted, two
    targets pinning one BACnet device to different addresses — and these can.
    """
    from .config import ConfigError, load_config
    from .spacemap import load_space_map
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(config_raw or {}, sort_keys=False),
                            encoding="utf-8")
        defaults_path = Path(tmp) / "defaults.yaml"
        defaults_path.write_text(dump_defaults(defaults), encoding="utf-8")
        map_path = Path(tmp) / "space_mapping.yaml"
        map_path.write_text(dump_mapping(buildings, floors, rooms), encoding="utf-8")
        try:
            cfg = load_config(str(cfg_path), str(defaults_path))
        except ConfigError as exc:
            return str(exc).replace(str(cfg_path), "config.yaml"), [], []
        import logging
        previous = logging.root.manager.disable
        logging.disable(logging.WARNING)       # the dialog shows them instead
        try:
            space_map = load_space_map(str(map_path), cfg)
        finally:
            logging.disable(previous)
        return None, list(space_map.errors), list(space_map.warnings)


# ─────────────────────────────────────────────────────────────────────────────
# config.yaml (connection settings) — load / apply / save (no GUI, testable)
#
# The Connection tab edits a curated set of connection fields. Passwords are
# deliberately NOT part of the form — they come from environment variables only
# (BAS_25LIVE_PASSWORD / BAS_SYS_<SYSTEM>_PASSWORD). Any sections already in
# config.yaml that the form doesn't expose (retry, alerts, …) are preserved
# untouched on save.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG_FILE = paths.config_file()

CONFIG_HEADER = """\
# 25Live → BAS connection settings. Managed by the web UI and editor.py
# (Connection), but safe to hand-edit. Passwords are NOT stored here — set them as environment
# variables: BAS_25LIVE_PASSWORD and BAS_SYS_<SYSTEM>_PASSWORD. See
# config.example.yaml and README.md for the full reference.
"""

# Fields shown for a BAS system, per driver. The Connection tab edits one
# system at a time and swaps this group when you pick a different one, so a
# BACnet system never shows Niagara's ORD boxes and vice versa.
# (key-within-the-system, label, kind, hint).
DRIVER_CONFIG_FIELDS = {
    "bacnet": [
        ("local_address", "Local NIC address", "text",
         "THIS host's address and prefix, e.g. 10.4.1.55/24"),
        ("device_id", "BACnet device ID", "int",
         "must be free campus-wide"),
        ("device_name", "Device name", "text", "how this sync identifies itself"),
        ("bbmd_address", "BBMD address", "text",
         "needed when this host is not on the controllers' subnet"),
        ("event_priority", "Exception priority", "int",
         "1-16, lower wins; 16 lets hand-entered exceptions override"),
        ("verify_writes", "Verify writes", "bool",
         "read back after writing to confirm it took"),
        ("max_special_events", "Max special events", "int",
         "0 = no cap; set to your controllers' limit"),
    ],
    "niagara": [
        ("host", "Host", "text", "station hostname or IP"),
        ("port", "Port", "int", "often 443 or 8443"),
        ("https", "Use HTTPS", "bool", ""),
        ("username", "Username", "text", ""),
        ("verify_tls", "Verify TLS", "combo",
         "true, false, or a path to a CA bundle"),
        ("schedule_base_path", "Schedule base ORD", "text",
         "default slot:/Schedules"),
        ("heartbeat_path", "Heartbeat ORD", "text",
         "optional; blank to disable"),
    ],
    "rest": [
        ("base_url", "Base URL", "text", "e.g. https://ebo.example.edu"),
        ("username", "Username", "text", ""),
        ("verify_tls", "Verify TLS", "combo",
         "true, false, or a path to a CA bundle"),
    ],
    "preview": [
        ("csv_file", "CSV export path", "text", "blank = log only, no file"),
    ],
}

DRIVER_CHOICES = sorted(DRIVER_CONFIG_FIELDS)

# Sections that are the same whatever BAS you are on.
GLOBAL_CONFIG_SECTIONS = [
    ("25Live (CollegeNET)", [
        (("collegenet", "instance"), "Instance name", "text",
         "CollegeNET-hosted instance; the base URL is derived from it"),
        (("collegenet", "base_url"), "Base URL", "text",
         "self-hosted only; blank = derive from the instance"),
        (("collegenet", "username"), "Username", "text",
         "a LOCAL 25Live account (not SSO)"),
        (("collegenet", "include_states"), "Include states", "text",
         "other states already in the file are preserved"),
        (("collegenet", "state_param_style"), "State parameter style", "combo",
         "plus | comma | repeat | none — instances differ; wrong = 0 events"),
    ]),
    ("General", [
        (("timezone",), "Timezone", "combo", "IANA name, e.g. America/New_York"),
        (("log_file",), "Log file", "text", "blank = logs/25live_sync.log"),
    ]),
]


def system_config_fields(system_name: str, driver: str) -> list:
    """(path, label, kind, hint) for one BAS system's settings."""
    fields = DRIVER_CONFIG_FIELDS.get(driver, [])
    return [(("systems", system_name, key), label, kind, hint)
            for key, label, kind, hint in fields]


def config_sections(system_name: str, driver: str) -> list:
    """The Connection tab's sections for the currently selected system."""
    sections = list(GLOBAL_CONFIG_SECTIONS[:1])
    if system_name:
        title = f"BAS system: {system_name}  ({driver or 'no driver'})"
        if driver == "niagara":
            title += "  — deprecated: use bacnet with the station's schedule export"
        sections.append((title, system_config_fields(system_name, driver)))
    sections.extend(GLOBAL_CONFIG_SECTIONS[1:])
    return sections

# 25Live event states this project documents (numeric -> label), offered as
# checkboxes on the Connection tab. Any other states already in config.yaml are
# preserved untouched.
INCLUDE_STATE_LABELS = [(2, "Confirmed"), (4, "Tentative")]


def _config_fields(system_name: str = "", driver: str = ""):
    """Flatten the sections to a list of (path, label, kind, hint)."""
    return [f for _section, fields in config_sections(system_name, driver)
            for f in fields]


# Checkbox fields that default to ON when config.yaml doesn't set them. Keyed
# by the last path segment, since the system name in the middle varies.
_BOOL_DEFAULT_ON = {"https", "verify_writes"}


def _config_default_bool(path_t) -> bool:
    """Default for a checkbox field when config.yaml doesn't set it."""
    return path_t[-1] in _BOOL_DEFAULT_ON


def _dig(node, path):
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _set_path(node, path, value):
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _del_path(node, path):
    stack = []
    cur = node
    for key in path[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return
        stack.append((cur, key))
        cur = cur[key]
    if isinstance(cur, dict):
        cur.pop(path[-1], None)
    for parent, key in reversed(stack):        # prune now-empty parents
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]


def read_config_checked(path) -> tuple:
    """
    (config.yaml as a plain dict, error message or None).

    A missing or empty file is simply {} with no error. A file that exists
    but can't be parsed returns {} AND the error — and the editor then
    refuses to save config.yaml, because saving the form over it would
    replace every section it doesn't show (alerts, safety, other systems)
    with nothing.
    """
    p = Path(path)
    if not (p.exists() and p.stat().st_size):
        return {}, None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError) as exc:
        return {}, f"{p}: {exc}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, f"{p}: the top level is not a mapping"
    return data, None


def read_config_raw(path) -> dict:
    """Load config.yaml as a plain dict (or {} if missing/empty/malformed), so
    sections the form doesn't expose survive a round-trip through the editor."""
    return read_config_checked(path)[0]


def config_systems(raw: dict) -> dict:
    """
    {system name: driver} from a raw config, migrating a pre-1.0 `niagara:`
    block so an older file opens with its station already listed.
    """
    systems = raw.get("systems")
    out = {}
    if isinstance(systems, dict):
        for name, cfg in systems.items():
            if isinstance(cfg, dict):
                out[str(name)] = str(cfg.get("driver") or "")
    if not out and isinstance(raw.get("niagara"), dict):
        out["niagara"] = "niagara"
    return out


def form_from_raw(raw: dict, system_name: str = "", driver: str = "") -> dict:
    """Flat {dotted_path: value} for the Connection tab, from a raw config.
    bool fields come back as bools; everything else as strings ('' if unset)."""
    out: dict = {}
    for path_t, _label, kind, _hint in _config_fields(system_name, driver):
        val = _dig(raw, path_t)
        fid = ".".join(path_t)
        if kind == "bool":
            out[fid] = bool(val) if val is not None else _config_default_bool(path_t)
        elif isinstance(val, bool):
            out[fid] = "true" if val else "false"     # e.g. verify_tls: false
        elif isinstance(val, list):
            out[fid] = ", ".join(str(x) for x in val)
        else:
            out[fid] = "" if val is None else str(val)
    return out


def load_config_form(path, system_name: str = "", driver: str = "") -> dict:
    """Read config.yaml into the Connection tab's flat form."""
    return form_from_raw(read_config_raw(path), system_name, driver)


def _coerce_config_value(s: str, kind: str, path_t):
    if path_t == ("collegenet", "include_states"):
        return [int(x) for x in s.replace(",", " ").split()]
    if path_t[-1] == "verify_tls":
        low = s.lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
        return s                               # otherwise a CA-bundle path
    if kind == "int":
        return int(s)
    return s


def apply_config_form(raw: dict, form: dict, system_name: str = "",
                      driver: str = "") -> dict:
    """Return a deep copy of `raw` with the Connection-tab fields applied. Blank
    text/int fields remove the key (so the built-in defaults apply); bool fields
    are always written. Raises ValueError on a non-numeric int / state value."""
    out = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    if system_name:
        # The driver is what decides which fields mean anything, so it is
        # written even though it is not one of the form's own fields.
        _set_path(out, ("systems", system_name, "driver"), driver)
    for path_t, label, kind, _hint in _config_fields(system_name, driver):
        fid = ".".join(path_t)
        raw_val = form.get(fid)
        if kind == "bool":
            _set_path(out, path_t, bool(raw_val))
            continue
        s = ("" if raw_val is None else str(raw_val)).strip()
        if s == "":
            _del_path(out, path_t)
            continue
        try:
            _set_path(out, path_t, _coerce_config_value(s, kind, path_t))
        except ValueError:
            raise ValueError(f"'{label}' — could not read {s!r}")
    return out


def save_config(path, config: dict) -> bool:
    """Write config.yaml, keeping a .bak of the previous version. Rewriting
    drops the file's comments, which is why the editor only calls this when a
    connection setting actually changed."""
    body = yaml.safe_dump(config, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    return write_if_changed(path, CONFIG_HEADER + "\n" + body)


