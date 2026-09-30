# 25Live -> BAS Schedule Sync — run orchestration
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The three things a run can do — sync, validate, discover — and the exit codes
that let monitoring tell them apart.

A sync pass:
    load room map -> take the run lock -> fetch 25Live -> apply buffers
    -> merge -> roll up -> safety check -> fan out to each BAS driver
    -> heartbeat -> save state

Every step records what it did in a RunReport (bassync/report.py), which is
what the email and webhook notifications are built from.
"""

import logging
import os
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests
import yaml

from . import __version__, discovery, extras, lowtemp, safety, scheduled
from .collegenet import CollegeNetClient, CollegeNetError
from .config import ConfigError
from .drivers import build_driver
from .lock import RunLock
from .model import Destination
from .report import RunReport
from .schedule import ScheduleBuilder
from .spacemap import SpaceMap, load_space_map

# Exit codes, for cron/Task Scheduler monitoring.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_MAP = 2
EXIT_BAS_UNREACHABLE = 3
EXIT_FETCH_FAILED = 4
EXIT_WRITE_FAILURES = 5
EXIT_VALIDATION_FAILED = 6
EXIT_SAFETY_ABORT = 7
EXIT_LOCKED = 8

EXIT_CODE_HELP = {
    EXIT_ERROR: "Unhandled error or unusable configuration (see the log).",
    EXIT_NO_MAP: ("Room map problem — the map is missing or unusable, or some "
                  "rows were broken and left out (the rest of the campus "
                  "synced)."),
    EXIT_BAS_UNREACHABLE: "A BAS system was unreachable and nothing was written.",
    EXIT_FETCH_FAILED: "25Live fetch failed (auth, network, or config).",
    EXIT_WRITE_FAILURES: "One or more BAS writes failed.",
    EXIT_VALIDATION_FAILED: "Validation failed.",
    EXIT_SAFETY_ABORT: ("Refused to clear a large share of schedules at once — "
                        "check 25Live, then re-run with --force if the drop is real."),
    EXIT_LOCKED: "Another sync was already running; this one did nothing.",
}


def _fetch(cfg: dict, tz: ZoneInfo, space_map):
    """Pull events, translating transport failures into CollegeNetError."""
    client = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
    try:
        return client.fetch_events(space_map)
    except requests.RequestException as exc:
        raise CollegeNetError(f"Failed to fetch from 25Live: {exc}") from exc
    finally:
        client.close()


def _done(report: RunReport, code: int, outcome: str = "") -> int:
    report.finish(code, outcome)
    return code


def _lock_path(cfg: dict) -> Path:
    return Path(cfg["safety"]["state_file"]).with_name("run.lock")


def load_map_for_run(cfg: dict, report: RunReport) -> tuple:
    """
    Load the room map and apply `safety.on_map_errors`.

    Returns (space_map, exit code to stop with or None, map_problem). With the
    default `skip` policy a broken row is reported and left out — its
    schedules are simply not touched this run — and the rest of the campus
    still syncs; the run then exits 2 so the alert still fires. A map that
    can't be read at all, or that has nothing left in it, always stops.
    """
    space_map = load_space_map(cfg["space_map_file"], cfg)
    report.rooms = len(space_map)
    report.map_errors = list(space_map.errors)
    report.map_warnings = list(space_map.warnings)
    if not space_map.errors:
        if not space_map:
            logging.error("Room map is empty — nothing to sync.")
            return space_map, _done(report, EXIT_NO_MAP, "room map is empty"), False
        return space_map, None, False

    for err in space_map.errors:
        logging.error("Room map: %s", err)
    count = len(space_map.errors)
    if space_map.fatal or not space_map:
        logging.error("Room map is unusable (%d problem(s)) — nothing to sync.", count)
        return space_map, _done(report, EXIT_NO_MAP, "room map unusable"), False
    policy = (cfg.get("safety") or {}).get("on_map_errors", "skip")
    if policy == "abort":
        logging.error("Room map has %d problem(s) and safety.on_map_errors is "
                      "'abort' — nothing will be written until they are fixed.",
                      count)
        return space_map, _done(report, EXIT_NO_MAP,
                                f"{count} room map problem(s); nothing written"), False
    logging.error("Room map has %d problem(s). Those rows are left out — their "
                  "schedules, and any floor or building schedule they roll up "
                  "into, are not touched this run — and the rest of the "
                  "campus syncs. Fix them to clear this alert.", count)
    return space_map, None, True


def run_sync(cfg: dict, dry_run: bool = False, force: bool = False,
             only_system: Optional[str] = None,
             report: Optional[RunReport] = None,
             only_buildings: Optional[list] = None) -> int:
    """
    One full sync pass. Returns a process exit code, and records what it did
    in `report` (created if not given).

    `only_system` or `only_buildings` limit what is written — to one BAS, or
    to the schedules of some buildings (their own, their floors', their
    equipment's and their rooms'). Everything is still fetched and built, so
    a schedule shared with rooms elsewhere still gets all of its bookings;
    only the writes, the safety check and the saved baseline are limited.

    dry_run fetches and builds, logs what *would* be written per driver
    (including whether the safety check would let a live run through), and
    contacts no BAS. It is the same code path as a live run right up to the
    write, so a clean dry run means the fetch, mapping and merge are all good.
    """
    tz = ZoneInfo(cfg["timezone"])
    if report is None:
        report = RunReport("DRY RUN" if dry_run else "SYNC", __version__, tz)
    report.only_system = only_system or ""
    report.only_buildings = list(only_buildings or [])

    space_map, stop, map_problem = load_map_for_run(cfg, report)
    if stop is not None:
        return stop

    if not cfg["collegenet"].get("base_url"):
        logging.error(
            "25Live base_url is not set — set collegenet.instance (for "
            "CollegeNET-hosted sites) or collegenet.base_url in config.yaml. "
            "Did you copy config.example.yaml to config.yaml?")
        return _done(report, EXIT_FETCH_FAILED, "25Live base_url not configured")

    if dry_run:
        return _sync_locked(cfg, tz, space_map, report, dry_run, force,
                            only_system, map_problem, only_buildings)

    lock = RunLock(_lock_path(cfg))
    try:
        locked = lock.acquire()
    except OSError as exc:
        logging.warning("Could not create the run lock %s (%s) — continuing "
                        "without protection against an overlapping run.",
                        lock.path, exc)
        locked = None
    if locked is False:
        logging.error("Another sync is already running (pid %s, lock %s) — "
                      "exiting without writing anything.", lock.holder(), lock.path)
        return _done(report, EXIT_LOCKED, "another sync was already running")
    try:
        return _sync_locked(cfg, tz, space_map, report, dry_run, force,
                            only_system, map_problem, only_buildings)
    finally:
        lock.release()


def _sync_locked(cfg: dict, tz: ZoneInfo, space_map: SpaceMap, report: RunReport,
                 dry_run: bool, force: bool, only_system: Optional[str],
                 map_problem: bool, only_buildings: Optional[list] = None) -> int:
    if only_buildings:
        unknown = [b for b in only_buildings if b not in space_map.building_ids]
        if unknown:
            logging.error("No building %s in the room map.",
                          ", ".join(f"'{b}'" for b in unknown))
            return _done(report, EXIT_NO_MAP, f"no building {', '.join(unknown)}")
    try:
        events = _fetch(cfg, tz, space_map)
    except CollegeNetError as exc:
        logging.error("%s", exc)
        return _done(report, EXIT_FETCH_FAILED, "25Live fetch failed")
    report.event_count = len(events)

    try:
        extra = extras.expand(cfg.get("extra_bookings_file"), space_map, tz,
                              cfg["collegenet"]["lookahead_days"])
    except ConfigError as exc:
        # Which schedules its rows drive is unknowable, so nothing is safe to
        # write: leaving them out would stand those rooms down.
        logging.error("Extra bookings can't be read — %s. Nothing will be written "
                      "until the file is fixed.", exc)
        return _done(report, EXIT_NO_MAP, "extra bookings file unreadable; nothing written")
    extras.log_expansion(extra)
    report.extra_bookings = extra.occurrences
    report.extra_errors = list(extra.errors)
    report.map_warnings.extend(extra.warnings)
    map_problem = map_problem or bool(extra.errors)

    # Events marked low temp also drive their rooms' low-temp schedules. A
    # marks file that can't be read leaves every low-temp schedule as it is:
    # which ones it would change can't be known.
    low_temp_held: set = set()
    try:
        marked = lowtemp.event_ids(cfg.get("low_temp_file"))
    except ConfigError as exc:
        marked = set()
        report.low_temp_error = str(exc)
        low_temp_held = set(space_map.low_temp)
        map_problem = True
        logging.error("Low-temp events can't be read — %s. The low-temp schedules are "
                      "left as they are until the file is fixed.", exc)
    if marked:
        events = [replace(e, low_temp=True) if e.event_id in marked else e for e in events]
    bookings = events + extra.events
    _note_low_temp(bookings, space_map, report)

    builder = ScheduleBuilder(cfg["collegenet"]["merge_gap_minutes"])
    schedule = builder.build(bookings, space_map, extra.windows)

    # Every schedule this map owns — including roll-ups and rooms with no
    # bookings this week, which must be actively cleared rather than left
    # holding last week's occupancy.
    all_destinations = space_map.destinations()
    in_scope = _scope(space_map, only_system, only_buildings)
    if in_scope is not None:
        what = (f"system '{only_system}'" if only_system else
                "building" + ("s " if len(only_buildings or []) > 1 else " ")
                + ", ".join(f"'{b}'" for b in only_buildings or []))
        all_destinations = {d for d in all_destinations if in_scope(d)}
        schedule = {d: w for d, w in schedule.items() if in_scope(d)}
        logging.info("Limited to %s: %d schedule(s)", what, len(all_destinations))
        if not all_destinations:
            logging.error("No schedules belong to %s.", what)
            return _done(report, EXIT_NO_MAP, f"no schedules for {what}")

    # Roll-ups a broken room row feeds keep their current schedule: writing
    # them now would drop that room's bookings from its corridor and building.
    # Reported even when no healthy room still feeds one, so the email says
    # which buildings were left alone.
    causes: dict = {}
    for why, dests in (("a broken room-map row feeds it", space_map.held),
                       ("a broken extra booking feeds it", extra.held),
                       ("the low-temp events file can't be read", low_temp_held)):
        for d in dests:
            if in_scope is None or in_scope(d):
                causes.setdefault(d, []).append(why)
    held = set(causes)
    if held:
        logging.warning(
            "Not writing %d schedule(s) this run because a broken room-map row, "
            "extra booking or low-temp events file feeds them; they keep their "
            "current schedule: %s",
            len(held),
            ", ".join(sorted(str(d) for d in held)[:10])
            + (f" (+{len(held) - 10} more)" if len(held) > 10 else ""))
        for dest in sorted(held, key=str):
            report.add_schedule(
                dest.system, dest.target, space_map.labels.get(dest, dest.target),
                schedule.get(dest, []), "not written",
                f"held: {' and '.join(causes[dest])} (see Problems); its current "
                "schedule is left as it is")
        all_destinations = all_destinations - held
        schedule = {d: w for d, w in schedule.items() if d not in held}

    for dest in all_destinations:
        schedule.setdefault(dest, [])

    # Low-temp schedules only ever hold a few marked events, so one ending is
    # no sign of 25Live going quiet: they're left out of the comparison.
    low = space_map.low_temp
    verdict = safety.check(cfg, {d: w for d, w in schedule.items() if d not in low},
                           all_destinations - low, len(events))
    report.safety_ok = bool(verdict)
    report.safety_reason = verdict.reason
    report.baseline = verdict.baseline

    if dry_run:
        logging.info("Safety check — a live run would %s: %s",
                     "PROCEED" if verdict else "ABORT", verdict.reason)
        _preview(cfg, tz, schedule, space_map, report)
        return _done(report, EXIT_OK, "dry run")

    if not verdict:
        if force:
            report.safety_forced = True
            logging.warning("SAFETY OVERRIDE (--force): %s", verdict.reason)
        else:
            logging.error("ABORTING before any write. %s", verdict.reason)
            return _done(report, EXIT_SAFETY_ABORT,
                         "safety check blocked the run; nothing was written")
    else:
        logging.info("Safety check passed: %s", verdict.reason)

    code, written = _write_all(cfg, tz, schedule, space_map.labels, report)
    # Record what actually landed, not what was intended. A destination whose
    # write failed keeps its previous baseline, so the next run compares
    # against reality; recording the intent would quietly assert that a
    # building is scheduled when it isn't. Saving on a partial failure also
    # keeps the baseline fresh — otherwise one persistently broken system
    # freezes it, and weeks of legitimate drift eventually reads as a mass
    # clear.
    if written:
        # Merge, not replace, whenever part of the campus was left alone —
        # failed writes, skipped rows, held roll-ups — so those schedules keep
        # their baseline for the next comparison.
        saved = safety.save_state(cfg["safety"]["state_file"],
                                  {d: w for d, w in schedule.items() if d in written},
                                  len(events), only_system=only_system,
                                  merge=code != EXIT_OK or map_problem or bool(only_buildings))
        if not saved:
            report.baseline += " — could NOT be saved for the next run"
    scheduled.record(scheduled.path_for(cfg), report, space_map, bookings, in_scope)

    c = report.counts()
    if code != EXIT_OK:
        return _done(report, code, f"{c['failed']} write failure(s), "
                                   f"{c['written']} schedule(s) written")
    if map_problem:
        broken = []
        if space_map.errors:
            broken.append(f"{len(space_map.errors)} broken room-map row(s)")
        if extra.errors:
            broken.append(f"{len(extra.errors)} broken extra booking(s)")
        if report.low_temp_error:
            broken.append("the unreadable low-temp events file")
        return _done(report, EXIT_NO_MAP, f"{' and '.join(broken)} left out; "
                                          f"{c['written']} schedule(s) written")
    return _done(report, EXIT_OK, f"{c['written']} schedule(s) written")


def _note_low_temp(bookings: list, space_map: SpaceMap, report: RunReport) -> None:
    """Count the low-temp bookings, and say which land in a room with no
    low-temp schedule — marked, but nothing to make colder."""
    low = [b for b in bookings if b.low_temp]
    report.low_temp_bookings = len(low)
    if not low:
        return
    logging.info("Low temp: %d booking(s) marked low temp in this run's window", len(low))
    missing: dict = {}
    for b in low:
        sc = space_map.spaces.get(b.space_id)
        if sc is not None and not sc.low_temp_destinations:
            missing.setdefault(sc.space_name, set()).add(b.title)
    for room, titles in sorted(missing.items()):
        logging.warning("Low temp: %s is marked low temp in %s, which has no low-temp "
                        "schedule (low_temp_target) in the room map, so nothing runs "
                        "colder there.", ", ".join(sorted(titles)), room)


def _scope(space_map: SpaceMap, only_system: Optional[str],
           only_buildings: Optional[list]):
    """Which schedules a limited run writes, as a test on a destination; None
    for all of them."""
    tests = []
    if only_system:
        tests.append(lambda d: d.system == only_system)
    if only_buildings:
        wanted = space_map.building_destinations(only_buildings)
        tests.append(lambda d: d in wanted)
    if not tests:
        return None
    return lambda d: all(test(d) for test in tests)


def _group_by_system(schedule: dict) -> dict:
    by_system: dict = defaultdict(dict)
    for dest, windows in schedule.items():
        by_system[dest.system][dest.target] = windows
    return by_system


def _preview(cfg: dict, tz: ZoneInfo, schedule: dict, space_map: SpaceMap,
             report: RunReport) -> None:
    """Log what a live run would write, using each driver's own encoding so
    the preview reflects what actually goes on the wire."""
    logging.info("DRY RUN — no BAS was contacted. Schedules that WOULD be written:")
    systems = cfg.get("systems") or {}
    for system_name, targets in sorted(_group_by_system(schedule).items()):
        sys_cfg = systems.get(system_name)
        driver = None
        if sys_cfg:
            try:
                driver = build_driver(system_name, sys_cfg, tz, cfg.get("retry"))
            except Exception as exc:                      # noqa: BLE001
                logging.error("System '%s': %s", system_name, exc)
        logging.info("--- system '%s' (%s) — %d schedule(s) ---", system_name,
                     (sys_cfg or {}).get("driver", "unconfigured"), len(targets))
        for target, windows in sorted(targets.items()):
            label = space_map.labels.get(Destination(system_name, target), target)
            report.add_schedule(system_name, target, label, windows, "preview")
            if driver is not None:
                # describe() is pure formatting in every shipped driver, so a
                # dry run stays offline even for BACnet.
                logging.info("  %s", driver.describe(target, windows))
            else:
                logging.info("  %s: %d window(s)", target, len(windows))
        if driver is not None:
            try:
                driver.close()
            except Exception as exc:                      # noqa: BLE001
                logging.debug("closing %s: %s", system_name, exc)


def _write_all(cfg: dict, tz: ZoneInfo, schedule: dict, labels: dict,
               report: RunReport) -> tuple:
    """
    Fan out to each system's driver. One unreachable BAS fails that system's
    schedules, not the whole campus — whatever the driver raises.

    Returns (exit_code, set_of_destinations_actually_written).
    """
    systems = cfg.get("systems") or {}
    failures = 0
    written: set = set()
    unreachable = 0

    def label_of(system_name: str, target: str) -> str:
        return labels.get(Destination(system_name, target), target)

    def fail_all(system_name: str, targets: dict, reason: str) -> None:
        for target, windows in sorted(targets.items()):
            report.add_schedule(system_name, target, label_of(system_name, target),
                                windows, "failed", reason)

    for system_name, targets in sorted(_group_by_system(schedule).items()):
        sys_cfg = systems.get(system_name)
        result = report.system(system_name, (sys_cfg or {}).get("driver", "?"))
        if not sys_cfg:
            reason = "system not defined under `systems:` in config.yaml"
            logging.error("System '%s' is referenced by %d schedule(s) but not "
                          "defined under `systems:` in config.yaml.",
                          system_name, len(targets))
            result.reachable, result.detail = False, reason
            fail_all(system_name, targets, reason)
            failures += len(targets)
            continue

        try:
            driver = build_driver(system_name, sys_cfg, tz, cfg.get("retry"))
        except Exception as exc:                          # noqa: BLE001
            logging.error("System '%s': %s", system_name, exc)
            result.reachable, result.detail = False, str(exc)
            fail_all(system_name, targets, str(exc))
            failures += len(targets)
            continue

        try:
            try:
                driver.connect()
                ok, detail = driver.health_check()
            except Exception as exc:                      # noqa: BLE001
                # A driver that cannot even start (bad NIC address, failed
                # login) fails ITS schedules. The other systems still get
                # their night's write.
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            result.reachable, result.detail = ok, detail
            if not ok:
                logging.error("System '%s' unreachable (%s) — skipping its %d "
                              "schedule(s).", system_name, detail, len(targets))
                unreachable += 1
                failures += len(targets)
                fail_all(system_name, targets, f"system unreachable: {detail}")
                continue
            logging.info("--- system '%s' (%s): %s ---", system_name,
                         sys_cfg.get("driver"), detail)

            status = "preview" if driver.name == "preview" else "written"
            for target, windows in sorted(targets.items()):
                label = label_of(system_name, target)
                try:
                    driver.write_schedule(target, windows)
                    written.add(Destination(system_name, target))
                    report.add_schedule(system_name, target, label, windows, status)
                except Exception as exc:                  # noqa: BLE001
                    logging.error("Error writing %s:%s — %s",
                                  system_name, target, exc)
                    report.add_schedule(system_name, target, label, windows,
                                        "failed", str(exc))
                    failures += 1

            if driver.has_heartbeat:
                try:
                    driver.write_heartbeat(datetime.now(tz))
                    result.heartbeat = "written"
                except Exception as exc:                  # noqa: BLE001
                    result.heartbeat = f"FAILED ({exc})"
                    logging.warning("System '%s': heartbeat failed: %s",
                                    system_name, exc)
        finally:
            try:
                driver.close()
            except Exception as exc:                      # noqa: BLE001
                logging.warning("System '%s': error while disconnecting: %s",
                                system_name, exc)

    if failures:
        logging.error("Sync finished with %d write failure(s); %d schedule(s) "
                      "written successfully.", failures, len(written))
        # An unreachable system is a different operational problem from a
        # rejected write, and worth its own exit code for monitoring.
        code = (EXIT_BAS_UNREACHABLE
                if unreachable and not written else EXIT_WRITE_FAILURES)
        return code, written

    logging.info("Sync completed successfully — %d schedule(s) written across "
                 "%d system(s).", len(written), len(_group_by_system(schedule)))
    return EXIT_OK, written


# ─────────────────────────────────────────────────────────────────────────────
# --validate
# ─────────────────────────────────────────────────────────────────────────────

def _state_dir_check(cfg: dict) -> tuple:
    """Can this run keep the safety baseline? A read-only or unmounted
    state directory silently disables the mass-clear comparison."""
    state_file = Path(cfg["safety"]["state_file"])
    directory = state_file.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-test-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, (f"{directory} is not writable ({exc}) — the safety "
                       "baseline cannot be kept. In Docker, mount a volume at "
                       "/app/state.")
    return True, str(directory)


def run_validate(cfg: dict, config_warnings: Optional[list] = None,
                 only_system: Optional[str] = None) -> int:
    """
    Pre-flight (no writes): config, room map, 25Live auth, that 25Live
    actually returns bookings for the mapped rooms, every BAS reachable, every
    schedule target readable (and what a live run would overwrite), and that
    the safety baseline can be kept. With `only_system`, only that BAS is
    checked. Logs a PASS/FAIL line per check and returns 0 only if all pass.
    """
    tz = ZoneInfo(cfg["timezone"])
    checks: list = []
    notes: list = list(config_warnings or [])

    space_map = load_space_map(cfg["space_map_file"], cfg)
    map_ok = not space_map.errors and bool(space_map)
    if map_ok:
        detail = f"{len(space_map)} space(s), {space_map.building_count} building(s)"
    else:
        shown = space_map.errors[:5]
        detail = "; ".join(shown) or "empty"
        if len(space_map.errors) > 5:
            detail += f" (+{len(space_map.errors) - 5} more)"
    checks.append(("Room map loads", map_ok, detail))
    notes.extend(space_map.warnings)

    extra_path = cfg.get("extra_bookings_file")
    if extra_path and Path(extra_path).exists():
        try:
            extra = extras.expand(extra_path, space_map, tz,
                                  cfg["collegenet"]["lookahead_days"])
        except ConfigError as exc:
            checks.append(("Extra bookings load", False, str(exc)))
        else:
            detail = (f"{extra.occurrences} occurrence(s) in the next "
                      f"{cfg['collegenet']['lookahead_days']} day(s)")
            if extra.errors:
                detail = "; ".join(extra.errors[:5])
            checks.append(("Extra bookings load", not extra.errors, detail))
            notes.extend(extra.warnings)

    low_path = cfg.get("low_temp_file")
    if low_path and Path(low_path).exists():
        try:
            marked = lowtemp.event_ids(low_path)
        except ConfigError as exc:
            checks.append(("Low-temp events load", False,
                           f"{exc} — a live run leaves every low-temp schedule as it is"))
        else:
            checks.append(("Low-temp events load", True, f"{len(marked)} event(s) marked"))
            if marked and space_map and not space_map.low_temp:
                notes.append("Events are marked low temp, but no room in the room map "
                             "has a low-temp schedule (low_temp_target), so they "
                             "drive nothing.")

    base_url = cfg["collegenet"].get("base_url")
    checks.append(("25Live base_url configured", bool(base_url),
                   base_url or "set collegenet.instance or base_url"))

    if base_url:
        client = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
        try:
            ok, detail = client.check_connection()
            checks.append(("25Live reachable + authenticated", ok, detail))
            if ok and space_map:
                checks.append(_bookings_check(cfg, client, space_map))
        finally:
            client.close()

    systems = cfg.get("systems") or {}
    if not systems:
        checks.append(("BAS systems configured", False,
                       "no `systems:` block in config.yaml"))

    used = space_map.systems_used() if space_map else set(systems)
    if only_system:
        used = {only_system}
    by_system: dict = defaultdict(list)
    for dest in (space_map.destinations() if space_map else set()):
        by_system[dest.system].append(dest.target)

    for system_name in sorted(used):
        sys_cfg = systems.get(system_name)
        if not sys_cfg:
            checks.append((f"System '{system_name}' defined", False,
                           "referenced by the room map but not in `systems:`"))
            continue
        try:
            driver = build_driver(system_name, sys_cfg, tz, cfg.get("retry"))
        except Exception as exc:                          # noqa: BLE001
            checks.append((f"System '{system_name}' driver", False, str(exc)))
            continue
        try:
            try:
                driver.connect()
                ok, detail = driver.health_check()
            except Exception as exc:                      # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            checks.append((f"System '{system_name}' reachable", ok, detail))
            if ok:
                missing = []
                for target in sorted(set(by_system.get(system_name, []))):
                    try:
                        exists, why, target_notes = driver.inspect_target(target)
                    except Exception as exc:              # noqa: BLE001
                        exists, why, target_notes = False, f"{type(exc).__name__}: {exc}", []
                    notes.extend(target_notes)
                    if not exists:
                        missing.append(f"{target} ({why})")
                checks.append((f"System '{system_name}' schedules exist",
                               not missing,
                               "all present" if not missing
                               else f"{len(missing)} missing: "
                                    f"{', '.join(missing[:5])}"
                                    + (f" (+{len(missing) - 5} more)"
                                       if len(missing) > 5 else "")))
        finally:
            try:
                driver.close()
            except Exception as exc:                      # noqa: BLE001
                logging.debug("closing %s: %s", system_name, exc)

    ok, detail = _state_dir_check(cfg)
    checks.append(("Safety state directory writable", ok, detail))
    if ok:
        _state, status = safety.load_state_with_status(cfg["safety"]["state_file"])
        if status in ("missing", "corrupt"):
            notes.append(f"No safety baseline yet ({status}) — normal before the "
                         "first live run; the mass-clear comparison starts "
                         "after it.")

    alerts = cfg.get("alerts") or {}
    if alerts.get("enabled") and not (alerts.get("webhook_url")
                                      or (alerts.get("email") or {}).get("enabled")):
        notes.append("alerts.enabled is true but no channel is configured "
                     "(webhook_url or email.enabled).")
    if not (cfg.get("monitoring") or {}).get("ping_url"):
        notes.append("monitoring.ping_url is not set — nothing will notice if "
                     "the nightly job stops running altogether.")

    logging.info("=== Validation results ===")
    for name, ok, detail in checks:
        logging.info("  [%-4s] %s — %s", "PASS" if ok else "FAIL", name, detail)
    for note in notes:
        logging.info("  [WARN] %s", note)

    all_ok = all(ok for _, ok, _ in checks)
    logging.info("=== Validation %s ===", "PASSED" if all_ok else "FAILED")
    return EXIT_OK if all_ok else EXIT_VALIDATION_FAILED


def _bookings_check(cfg: dict, client: CollegeNetClient, space_map) -> tuple:
    """
    Does 25Live actually return bookings for the mapped rooms?

    A wrong `state_param_style`, a service account without read access to
    these locations, or an API change all answer HTTP 200 with zero events —
    the one failure a connection check can't see. When the configured style
    returns nothing, the others are tried and any that work are named.
    """
    name = (f"25Live returns bookings for mapped rooms "
            f"(next {cfg['collegenet']['lookahead_days']} days)")
    minimum = max(int((cfg.get("safety") or {}).get("min_events", 1)), 1)
    try:
        count = len(client.fetch_events(space_map))
    except (requests.RequestException, CollegeNetError) as exc:
        return name, False, str(exc)
    if count >= minimum:
        return name, True, f"{count} booking(s)"
    detail = f"{count} booking(s) with state_param_style '{client.state_param_style}'"
    probe = client.probe_state_styles(space_map)
    better = [f"'{style}' returns {n}" for style, n in probe.items()
              if isinstance(n, int) and n > count]
    if better:
        detail += ("; " + ", ".join(better) + " — set collegenet.state_param_style "
                   "to one of those")
    else:
        detail += ("; no other state encoding returned more. Check the service "
                   "account can read these locations, and that they really have "
                   "bookings in the window")
    return name, False, detail


def run_discover(cfg: dict, days: int, every_space: bool = True) -> int:
    """List the 25Live spaces — every one the account can see, or only those
    with bookings in the next `days` days — as a starter for
    space_mapping.yaml, and keep them in state/discovery.json for the web UI.
    Read-only as far as 25Live and the BAS are concerned."""
    tz = ZoneInfo(cfg["timezone"])
    if not cfg["collegenet"].get("base_url"):
        logging.error("25Live base_url is not set — configure "
                      "collegenet.instance or base_url in config.yaml.")
        return EXIT_FETCH_FAILED

    client = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
    try:
        spaces, listed_every = client.discover_spaces(days, every_space=every_space)
    except (requests.RequestException, CollegeNetError) as exc:
        logging.error("Discovery failed: %s", exc)
        return EXIT_FETCH_FAILED
    finally:
        client.close()

    listing = ("every" if listed_every else
               "booked-fallback" if every_space else "booked-only")
    if listed_every:
        logging.info("Discovered %d space(s), %d with bookings in the next %d days:",
                     len(spaces), sum(1 for s in spaces if s["bookings"]), days)
    else:
        logging.info("Discovered %d space(s) with bookings in the next %d days:",
                     len(spaces), days)
    rows = [{"space_id": int(s["space_id"]) if str(s["space_id"]).isdigit()
             else s["space_id"],
             "space_name": s["space_name"],
             "building": "",
             "target": ""} for s in spaces]
    # safe_dump quotes names containing quotes, colons or backslashes; the
    # hand-built f-strings this replaced produced YAML that would not load.
    print("\n# --- discovered spaces: fill in building (and floor) and/or a "
          "target for each ---")
    print(yaml.safe_dump({"spaces": rows}, sort_keys=False, allow_unicode=True,
                         default_flow_style=False), end="")
    # Kept for the web UI's setup guide, which offers them for import.
    try:
        discovery.save(Path(cfg["safety"]["state_file"]).parent, days, spaces,
                       listing=listing)
    except OSError as exc:
        logging.warning("Couldn't keep the discovered spaces for the web UI: %s", exc)
    return EXIT_OK
