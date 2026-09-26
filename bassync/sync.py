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
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests
import yaml

from . import __version__, safety
from .collegenet import CollegeNetClient, CollegeNetError
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
                  "schedules are not touched this run — and the rest of the "
                  "campus syncs. Fix them to clear this alert.", count)
    return space_map, None, True


def run_sync(cfg: dict, dry_run: bool = False, force: bool = False,
             only_system: Optional[str] = None,
             report: Optional[RunReport] = None) -> int:
    """
    One full sync pass. Returns a process exit code, and records what it did
    in `report` (created if not given).

    dry_run fetches and builds, logs what *would* be written per driver
    (including whether the safety check would let a live run through), and
    contacts no BAS. It is the same code path as a live run right up to the
    write, so a clean dry run means the fetch, mapping and merge are all good.
    """
    tz = ZoneInfo(cfg["timezone"])
    if report is None:
        report = RunReport("DRY RUN" if dry_run else "SYNC", __version__, tz)
    report.only_system = only_system or ""

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
                            only_system, map_problem)

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
                            only_system, map_problem)
    finally:
        lock.release()


def _sync_locked(cfg: dict, tz: ZoneInfo, space_map: SpaceMap, report: RunReport,
                 dry_run: bool, force: bool, only_system: Optional[str],
                 map_problem: bool) -> int:
    try:
        events = _fetch(cfg, tz, space_map)
    except CollegeNetError as exc:
        logging.error("%s", exc)
        return _done(report, EXIT_FETCH_FAILED, "25Live fetch failed")
    report.event_count = len(events)

    builder = ScheduleBuilder(cfg["collegenet"]["merge_gap_minutes"])
    schedule = builder.build(events, space_map)

    # Every schedule this map owns — including roll-ups and rooms with no
    # bookings this week, which must be actively cleared rather than left
    # holding last week's occupancy.
    all_destinations = space_map.destinations()
    if only_system:
        all_destinations = {d for d in all_destinations if d.system == only_system}
        schedule = {d: w for d, w in schedule.items() if d.system == only_system}
        logging.info("Limited to system '%s': %d schedule(s)",
                     only_system, len(all_destinations))
        if not all_destinations:
            logging.error("No schedules belong to system '%s'.", only_system)
            return _done(report, EXIT_NO_MAP, f"no schedules on system {only_system}")

    for dest in all_destinations:
        schedule.setdefault(dest, [])

    verdict = safety.check(cfg, schedule, all_destinations, len(events))
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
        saved = safety.save_state(cfg["safety"]["state_file"],
                                  {d: w for d, w in schedule.items() if d in written},
                                  len(events), only_system=only_system,
                                  merge=code != EXIT_OK)
        if not saved:
            report.baseline += " — could NOT be saved for the next run"

    c = report.counts()
    if code != EXIT_OK:
        return _done(report, code, f"{c['failed']} write failure(s), "
                                   f"{c['written']} schedule(s) written")
    if map_problem:
        return _done(report, EXIT_NO_MAP,
                     f"{len(space_map.errors)} broken room-map row(s) left out; "
                     f"{c['written']} schedule(s) written")
    return _done(report, EXIT_OK, f"{c['written']} schedule(s) written")


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


def run_validate(cfg: dict, config_warnings: Optional[list] = None) -> int:
    """
    Pre-flight (no writes): config, room map, 25Live auth, that 25Live
    actually returns bookings for the mapped rooms, every BAS reachable, every
    schedule target readable (and what a live run would overwrite), and that
    the safety baseline can be kept. Logs a PASS/FAIL line per check and
    returns 0 only if all pass.
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


def run_discover(cfg: dict, days: int) -> int:
    """List 25Live spaces with events in the next `days` days, as a starter for
    space_mapping.yaml. Read-only."""
    tz = ZoneInfo(cfg["timezone"])
    if not cfg["collegenet"].get("base_url"):
        logging.error("25Live base_url is not set — configure "
                      "collegenet.instance or base_url in config.yaml.")
        return EXIT_FETCH_FAILED

    client = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
    try:
        spaces = client.discover_spaces(days)
    except (requests.RequestException, CollegeNetError) as exc:
        logging.error("Discovery failed: %s", exc)
        return EXIT_FETCH_FAILED
    finally:
        client.close()

    logging.info("Discovered %d space(s) with events in the next %d days:",
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
    return EXIT_OK
