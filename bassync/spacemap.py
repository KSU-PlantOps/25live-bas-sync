# 25Live -> BAS Schedule Sync — room map loader
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Parses space_mapping.yaml — the cross-reference between 25Live spaces and BAS
schedules.

    buildings:  each building's roll-up schedule, defined ONCE.
    floors:     optional per-floor corridor schedules, keyed by building+level.
    spaces:     the rooms; each room names the building (and optionally floor)
                it belongs to.

Every room that names a building is automatically unioned into that building's
occupancy schedule, so if ANY room in the building is occupied the building
schedule (lobbies, common AHUs) runs too. A room never repeats its building's
target — it just names the building — which makes "all rooms in the building"
the default and removes the chance of forgetting to wire one up.

A room that also names a `floor` feeds that floor's corridor schedule as well,
so occupancy rolls up room -> floor -> building. That is what keeps a single
evening booking on the third floor from conditioning the whole tower while
still lighting and tempering the corridor someone has to walk down.

Inheritance, all with the same precedence — room > building > global:
    pre_condition_minutes   HVAC run-up before a booking
    post_buffer_minutes     run-down after it
    system                  which BAS this schedule lives on

A broken row is reported in `errors` and left out; the rest of the map still
loads. Whether the run then proceeds without it is the caller's policy
(`safety.on_map_errors`) — see bassync/sync.py. The floor and building
schedules a broken room would have fed are listed in `held`: writing them
without that room's bookings would be wrong, so a run leaves them untouched
until the row is fixed.
"""

import difflib
import logging
from pathlib import Path
from typing import Optional

from .config import ConfigError, read_yaml
from .model import Destination, SpaceConfig

# Largest buffer/merge gap accepted, in minutes. A day is already absurd for a
# run-up; anything above it is a typo (minutes vs seconds).
MAX_MINUTES = 1440

TOP_LEVEL_KEYS = ("buildings", "floors", "spaces")
BUILDING_KEYS = ("id", "name", "system", "target", "niagara_path",
                 "pre_condition_minutes", "post_buffer_minutes",
                 "merge_gap_minutes", "space_id", "note")
FLOOR_KEYS = ("building", "level", "name", "system", "target", "niagara_path",
              "note")
ROOM_KEYS = ("space_id", "space_name", "building", "floor", "system", "target",
             "niagara_path", "pre_condition_minutes", "post_buffer_minutes",
             "merge_gap_minutes", "note")


class RowError(ValueError):
    """A single row in the room map is unusable. Carries a message for the
    errors list so one bad room is reported, not raised — the other 400 rooms
    should still sync."""


def _space_id(value) -> str:
    """
    Normalise a 25Live space id to the string form the API returns.

    YAML reads an unquoted `1234` as int and `1234.0` as float; str() on the
    latter gives "1234.0", which would never match the "1234" 25Live sends, and
    the room would silently never sync. Integral floats are folded back to int.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _require_int(value, what: str, where: str) -> int:
    """int(value) or a RowError naming the field — a typo'd number is a
    reportable mapping mistake, not a traceback."""
    try:
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        raise RowError(f"{where}: `{what}` must be a whole number, got "
                       f"{value!r}.") from None


def _minutes(value, what: str, where: str) -> int:
    """A buffer or gap in minutes: a whole number from 0 to MAX_MINUTES.
    A negative run-up would shrink a booking — or invert it."""
    number = _require_int(value, what, where)
    if not 0 <= number <= MAX_MINUTES:
        raise RowError(f"{where}: `{what}` must be between 0 and {MAX_MINUTES} "
                       f"minutes, got {number}.")
    return number


def _minutes_or_default(value, default: int, what: str, where: str) -> int:
    """_minutes, falling back to `default` only when the value is truly
    absent (None) — an explicit 0 is a real setting that turns the buffer
    off, and must not be replaced by the global default."""
    return default if value is None else _minutes(value, what, where)


def _resolve(room_value, building_value, default):
    """room override > building override > global default.

    A value counts as "set" only when not None, so an explicit 0 (or an empty
    string for a system name) is honored at any level."""
    if room_value is not None:
        return room_value
    if building_value is not None:
        return building_value
    return default


def _value_source(row: dict, building: dict, key: str, where: str,
                  building_id: Optional[str]) -> str:
    """Where a room's buffer value came from, for its error message: a bad
    value inherited from the building is the building's row to fix."""
    if row.get(key) is None and building.get(key) is not None:
        return f"{where} (inherited from building {building_id})"
    return where


def _target_of(row: dict) -> Optional[str]:
    """
    The schedule address for a row, or None when it has none.

    `target:` is the current key. `niagara_path:` is the pre-1.0 name and is
    still accepted verbatim, so an existing campus map keeps working after an
    upgrade without a mass edit.

    Returning None is meaningful rather than an error: a room in a building
    that can only be scheduled at the floor or air-handler level has no
    schedule of its own, and still contributes its bookings to its roll-ups.
    Buildings and floors, which exist *only* to be a schedule, are checked by
    their callers.
    """
    target = row.get("target")
    if target in (None, ""):
        target = row.get("niagara_path")
    if target in (None, ""):
        return None
    return str(target).strip() or None


def _unknown_keys(row: dict, known: tuple, where: str, warnings: list) -> None:
    """
    Warn about keys the sync doesn't read. Since `target:` became optional, a
    misspelt `tagret:` produces a perfectly valid roll-up-only room — the
    room's own schedule just silently stops being written. Say so.
    """
    for key in row:
        if key in known:
            continue
        hint = difflib.get_close_matches(str(key), known, n=1)
        suggestion = f" Did you mean `{hint[0]}`?" if hint else ""
        warnings.append(f"{where}: unknown key `{key}` — ignored.{suggestion}")


class SpaceMap:
    """The loaded room map, plus whatever was wrong with it."""

    def __init__(self, spaces: dict, errors: list, warnings: list,
                 building_count: int = 0, floor_count: int = 0,
                 labels: Optional[dict] = None, fatal: bool = False,
                 held: Optional[set] = None):
        self.spaces = spaces              # { space_id: SpaceConfig }
        self.errors = errors              # rows left out, or the whole file
        self.warnings = warnings          # worth saying, not worth stopping for
        self.building_count = building_count
        self.floor_count = floor_count
        # Human-readable name for each destination ("Science Hall 1021",
        # "Floor 2 of liberal_arts"), for reports.
        self.labels = labels or {}
        # True when the file itself couldn't be read — nothing loaded, so
        # there is nothing a "skip the bad rows" policy could still sync.
        self.fatal = fatal
        # Roll-ups a broken row feeds. They are still managed schedules, but
        # a run must not rewrite them without that row's bookings.
        self.held = held or set()

    def __bool__(self) -> bool:
        return bool(self.spaces)

    def __len__(self) -> int:
        return len(self.spaces)

    def destinations(self) -> set:
        """
        Every distinct schedule the sync manages — rooms, floor corridors and
        building roll-ups.

        This is the set that gets cleared when a space has no bookings, so a
        roll-up missing from here is a schedule that would silently keep running
        last week's occupancy forever.
        """
        out = set()
        for sc in self.spaces.values():
            out.update(sc.all_destinations())
        return out

    def systems_used(self) -> set:
        return {d.system for d in self.destinations()}


def load_space_map(path: str, cfg: dict) -> SpaceMap:
    """Parse space_mapping.yaml into a SpaceMap. Never raises on bad content —
    problems come back as `errors` so --validate can report them all at once
    instead of failing on the first one."""
    from .config import resolve_default_system

    cn = cfg["collegenet"]
    default_pre = cn["default_pre_condition_minutes"]
    default_post = cn["default_post_buffer_minutes"]
    default_gap = cn["merge_gap_minutes"]
    default_system = resolve_default_system(cfg)
    known_systems = set(cfg.get("systems") or {})

    errors: list = []
    warnings: list = []
    labels: dict = {}
    held: set = set()

    p = Path(path)
    if not p.exists():
        errors.append(
            f"Room map not found: {path} — copy space_mapping.example.yaml to "
            "space_mapping.yaml (or run editor.py) and add your rooms.")
        return SpaceMap({}, errors, warnings, fatal=True)

    try:
        data = read_yaml(p)
    except ConfigError as exc:
        # Reported rather than raised: --validate should list every problem it
        # can find in one pass, not stop at the first.
        errors.append(f"Room map {exc}")
        return SpaceMap({}, errors, warnings, fatal=True)

    for key in data:
        if key not in TOP_LEVEL_KEYS:
            warnings.append(f"Room map: unknown top-level section `{key}` — "
                            f"ignored (expected {', '.join(TOP_LEVEL_KEYS)}).")

    def _label(dest: Optional[Destination], text: str) -> None:
        if dest is not None:
            labels.setdefault(dest, text)

    # ── 1) Index the building definitions by id ──────────────────────────────
    buildings: dict = {}
    building_dest: dict = {}
    for b in (data.get("buildings") or []):
        if not isinstance(b, dict):
            errors.append(f"A `buildings:` entry is not a mapping: {b!r}")
            continue
        bid = str(b.get("id") or "").strip()
        if not bid:
            errors.append("A building has no `id:`.")
            continue
        if bid in buildings:
            errors.append(f"Building id '{bid}' is defined more than once.")
            continue
        _unknown_keys(b, BUILDING_KEYS, f"Building {bid}", warnings)
        buildings[bid] = b
        target = _target_of(b)
        system = str(_resolve(b.get("system"), None, default_system) or "")
        if target is None:
            errors.append(
                f"Building {bid}: no `target:` — a building entry exists to "
                "name its roll-up schedule, so it needs one. Rooms may omit "
                "`target:`; buildings may not.")
        else:
            if not system:
                errors.append(
                    f"Building {bid}: no `system:` and no default. Set "
                    "`default_system:` in config.yaml or name one per building.")
            elif known_systems and system not in known_systems:
                errors.append(
                    f"Building {bid}: system '{system}' is not defined under "
                    f"`systems:` in config.yaml. Known: "
                    f"{', '.join(sorted(known_systems)) or '(none)'}.")
            else:
                building_dest[bid] = Destination(system=system, target=target)
                _label(building_dest[bid], f"Building {b.get('name') or bid}")

    # ── 1b) Index floor corridor schedules by (building id, level) ───────────
    floor_dest: dict = {}
    for f in (data.get("floors") or []):
        if not isinstance(f, dict):
            errors.append(f"A floors: entry is not a mapping: {f!r}")
            continue
        raw_building = f.get("building")
        raw_level = f.get("level")
        if raw_building in (None, "") or raw_level in (None, ""):
            errors.append(
                f"Floor entry {f!r} needs both `building:` and `level:`.")
            continue
        building_id = str(raw_building).strip()
        try:
            level = _require_int(raw_level, "level", f"Floor of '{building_id}'")
        except RowError as exc:
            errors.append(str(exc))
            continue
        _unknown_keys(f, FLOOR_KEYS, f"Floor {level} of '{building_id}'", warnings)
        if building_id not in buildings:
            errors.append(
                f"Floor {level} references unknown building '{building_id}'.")
            continue

        target = _target_of(f)
        if target is None:
            errors.append(f"Floor {level} of '{building_id}': no `target:` — a "
                          "floor entry exists to name its corridor schedule.")
            continue
        # A floor inherits its building's system unless it says otherwise —
        # a corridor is served by the same panel as the rooms off it far more
        # often than not.
        bld = buildings[building_id]
        system = str(_resolve(f.get("system"), bld.get("system"),
                              default_system) or "")
        if not system:
            errors.append(
                f"Floor {level} of '{building_id}': no `system:` and no default.")
            continue
        if known_systems and system not in known_systems:
            errors.append(
                f"Floor {level} of '{building_id}': system '{system}' is not "
                f"defined under `systems:` in config.yaml.")
            continue
        key = (building_id, level)
        if key in floor_dest:
            errors.append(
                f"Floor {level} of building '{building_id}' is defined twice.")
            continue
        floor_dest[key] = Destination(system=system, target=target)
        _label(floor_dest[key], f.get("name") or
               f"Floor {level} of {bld.get('name') or building_id}")

    space_map: dict = {}

    def _register(space_id: str, sc: SpaceConfig, label: str) -> bool:
        if space_id in space_map:
            kept = space_map[space_id]
            errors.append(
                f"25Live space_id {space_id} is mapped twice ({label} and "
                f"{kept.space_name}). Each space may appear once; the second "
                "entry was left out.")
            # Roll-ups only the dropped entry fed would lose its bookings.
            held.update(set(sc.rollup_destinations()) - set(kept.all_destinations()))
            return False
        space_map[space_id] = sc
        return True

    # ── 2) Rooms ─────────────────────────────────────────────────────────────
    for row in (data.get("spaces") or []):
        if not isinstance(row, dict):
            errors.append(f"A `spaces:` entry is not a mapping: {row!r}")
            continue
        raw_id = row.get("space_id")
        if raw_id in (None, ""):
            errors.append(f"A room has no `space_id:` ({row.get('space_name', '?')}).")
            continue
        space_id = _space_id(raw_id)
        where = f"Room {space_id}"
        _unknown_keys(row, ROOM_KEYS, where, warnings)

        building = None
        bdest = None
        raw_building = row.get("building")
        room_building = None if raw_building in (None, "") else str(raw_building).strip()
        if room_building:
            building = buildings.get(room_building)
            if building is None:
                warnings.append(
                    f"{where} references unknown building '{room_building}' — it "
                    "will NOT roll up. Add it under buildings: or fix the name.")
            else:
                bdest = building_dest.get(room_building)

        # Optional per-floor corridor schedule (room -> floor -> building).
        # Only meaningful for a room that belongs to a building, since floors
        # are keyed by building.
        floor = None
        fdest = None
        if row.get("floor") not in (None, ""):
            try:
                floor = _require_int(row["floor"], "floor", where)
            except RowError as exc:
                warnings.append(f"{exc} Ignoring the floor.")
            if floor is not None:
                if room_building in (None, ""):
                    warnings.append(
                        f"{where} names floor {floor} but no building, so there "
                        "is nothing to look the floor up against — it will NOT "
                        "drive a corridor schedule.")
                else:
                    fdest = floor_dest.get((str(room_building), floor))
                    if fdest is None:
                        warnings.append(
                            f"{where} references floor {floor} of building "
                            f"'{room_building}' with no matching floors: entry — "
                            "it will NOT drive a corridor schedule.")

        bld = building or {}
        # From here on the row's roll-ups are known. If the row turns out to
        # be broken, those schedules must not be rewritten without it.
        rollups = [d for d in (fdest, bdest) if d is not None]
        system = str(_resolve(row.get("system"), bld.get("system"),
                              default_system) or "")
        if not system:
            errors.append(
                f"{where}: no `system:` and no default. Set `default_system:` "
                "in config.yaml or name one per room.")
            held.update(rollups)
            continue
        if known_systems and system not in known_systems:
            errors.append(
                f"{where}: system '{system}' is not defined under `systems:` in "
                f"config.yaml. Known: "
                f"{', '.join(sorted(known_systems)) or '(none)'}.")
            held.update(rollups)
            continue

        # A room without its own `target:` is normal: plenty of buildings can
        # only be scheduled at the floor or air-handler level. It still feeds
        # its roll-ups. What is NOT useful is a room that drives nothing at
        # all — that room's bookings would vanish silently, so say so.
        target = _target_of(row)
        rdest = Destination(system=system, target=target) if target else None
        if rdest is None and bdest is None and fdest is None:
            errors.append(
                f"{where}: no `target:` and no roll-up to contribute to, so its "
                "bookings would drive nothing. Give it a `target:`, or a "
                "`building:` (and optionally `floor:`) to roll up into.")
            continue

        name = str(row.get("space_name") or space_id)
        try:
            space = SpaceConfig(
                space_id=space_id,
                space_name=name,
                space_type="room",
                destination=rdest,
                building_destination=bdest,
                pre_condition_minutes=_minutes(
                    _resolve(row.get("pre_condition_minutes"),
                             bld.get("pre_condition_minutes"), default_pre),
                    "pre_condition_minutes",
                    _value_source(row, bld, "pre_condition_minutes", where,
                                  room_building)),
                post_buffer_minutes=_minutes(
                    _resolve(row.get("post_buffer_minutes"),
                             bld.get("post_buffer_minutes"), default_post),
                    "post_buffer_minutes",
                    _value_source(row, bld, "post_buffer_minutes", where,
                                  room_building)),
                merge_gap_minutes=_minutes_or_default(
                    row.get("merge_gap_minutes"), default_gap,
                    "merge_gap_minutes", where),
                floor=floor,
                floor_destination=fdest,
            )
        except RowError as exc:
            errors.append(str(exc))
            held.update(rollups)
            continue

        if _register(space_id, space, f"room {row.get('space_name', space_id)}"):
            _label(rdest, name)

    # ── 3) Buildings that are themselves bookable in 25Live ──────────────────
    #     e.g. an atrium with its own 25Live space. Its own events then count
    #     toward the building schedule alongside the room roll-up.
    for building_id, b in buildings.items():
        if b.get("space_id") in (None, ""):
            continue
        dest = building_dest.get(building_id)
        if dest is None:
            continue
        space_id = _space_id(b["space_id"])
        where = f"Building {building_id}"
        try:
            space = SpaceConfig(
                space_id=space_id,
                space_name=str(b.get("name") or building_id),
                space_type="building",
                destination=dest,
                building_destination=None,
                pre_condition_minutes=_minutes_or_default(
                    b.get("pre_condition_minutes"), default_pre,
                    "pre_condition_minutes", where),
                post_buffer_minutes=_minutes_or_default(
                    b.get("post_buffer_minutes"), default_post,
                    "post_buffer_minutes", where),
                merge_gap_minutes=_minutes_or_default(
                    b.get("merge_gap_minutes"), default_gap,
                    "merge_gap_minutes", where),
            )
        except RowError as exc:
            errors.append(str(exc))
            held.add(dest)           # its own bookings would be missing
            continue
        _register(space_id, space, f"building {building_id}")

    # ── 4) One canonical spelling per schedule ───────────────────────────────
    #     Drivers know when two strings name the same object (a BACnet target
    #     with and without its pinned address). Canonicalising here means the
    #     builder unions their bookings and the schedule is written once —
    #     instead of twice, with the second write erasing the first. It is
    #     also where malformed targets are caught, before any run.
    space_map, held = _canonicalize(space_map, cfg, labels, errors, held)

    # ── 5) Two rooms pointing at one schedule ────────────────────────────────
    #     Legal and sometimes intentional (an air-wall room split into A/B in
    #     25Live but served by one AHU). The builder unions them rather than
    #     letting one overwrite the other, but say so — more often it is a
    #     copy-paste slip.
    seen: dict = {}
    for sc in space_map.values():
        if sc.space_type != "room" or sc.destination is None:
            continue
        seen.setdefault(sc.destination, []).append(sc.space_id)
    for dest, ids in seen.items():
        if len(ids) > 1:
            warnings.append(
                f"Schedule {dest} is the target of {len(ids)} rooms "
                f"({', '.join(sorted(ids))}). Their bookings are unioned — "
                "intended for a divisible room, a mistake otherwise.")

    rooms = [s for s in space_map.values() if s.space_type == "room"]
    rollup_only = sum(1 for s in rooms if s.destination is None)
    logging.info(
        "Loaded %d rooms (%d roll-up only) across %d buildings, %d floor "
        "schedules, from %s",
        len(rooms), rollup_only, len(buildings), len(floor_dest), path)
    for w in warnings:
        logging.warning("%s", w)
    return SpaceMap(space_map, errors, warnings,
                    building_count=len(buildings), floor_count=len(floor_dest),
                    labels=labels, held=held)


def _canonicalize(space_map: dict, cfg: dict, labels: dict, errors: list,
                  held: set) -> tuple:
    """
    Rewrite every destination to its driver's canonical form, and drop what a
    driver says is unusable. Returns (space map, held destinations), both
    canonical.

    Every unusable target is reported once. A room whose own target is bad
    still feeds its floor and building — only its own schedule is left alone;
    a bad floor or building target is removed from the rooms that roll up into
    it. A room left driving nothing at all is dropped.
    """
    from .drivers import DriverError, load_driver_class

    systems = cfg.get("systems") or {}
    by_system: dict = {}
    for sc in space_map.values():
        for dest in sc.all_destinations():
            by_system.setdefault(dest.system, set()).add(dest.target)

    canon: dict = {}
    invalid: dict = {}
    for system, targets in by_system.items():
        sys_cfg = systems.get(system)
        driver = sys_cfg.get("driver") if isinstance(sys_cfg, dict) else None
        if not driver:
            continue
        try:
            cls = load_driver_class(driver)
        except DriverError:
            continue
        mapping, bad = cls.normalize_targets(sys_cfg, sorted(targets))
        for original, canonical in mapping.items():
            canon[Destination(system, original)] = Destination(system, canonical)
        for original, message in bad.items():
            invalid[Destination(system, original)] = message

    for dest, message in sorted(invalid.items(), key=lambda kv: str(kv[0])):
        errors.append(f"{labels.get(dest, dest)} ({dest.system}): {message}")

    def _fix(dest: Optional[Destination]) -> Optional[Destination]:
        if dest is None or dest in invalid:
            return None
        return canon.get(dest, dest)

    out: dict = {}
    for space_id, sc in space_map.items():
        fixed = SpaceConfig(
            space_id=sc.space_id, space_name=sc.space_name,
            space_type=sc.space_type,
            destination=_fix(sc.destination),
            building_destination=_fix(sc.building_destination),
            pre_condition_minutes=sc.pre_condition_minutes,
            post_buffer_minutes=sc.post_buffer_minutes,
            merge_gap_minutes=sc.merge_gap_minutes,
            floor=sc.floor,
            floor_destination=_fix(sc.floor_destination),
        )
        if not fixed.all_destinations():
            errors.append(f"Room {space_id}: every schedule it would write or "
                          "roll up into is invalid (see above), so its bookings "
                          "would drive nothing.")
            continue
        out[space_id] = fixed

    for original, canonical in canon.items():
        if original in labels and canonical not in labels:
            labels[canonical] = labels[original]
    held_out = {canon.get(d, d) for d in held if d not in invalid}
    return out, held_out
