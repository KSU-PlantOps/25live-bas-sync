# 25Live -> BAS Schedule Sync — command-line interface
# Copyright (C) 2026 Ryan Bibby and contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details <https://www.gnu.org/licenses/>.
"""
25Live -> BAS Schedule Sync
===========================
Pulls room bookings from CollegeNET 25Live and writes them into building
automation schedules, so HVAC and lighting pre-condition for booked rooms and
stand down when they're empty.

The BAS side is pluggable. One run can drive a mixed campus:

    bacnet    standard BACnet/IP Schedule objects — Automated Logic WebCTRL,
              Schneider EcoStruxure Building Operation, Tridium Niagara, and
              any other BTL-listed controller
    niagara   DEPRECATED: Niagara special events via a station REST service
              (not in stock N4). Niagara stations use `bacnet` against the
              station's BACnet schedule export.
    rest      a vendor REST API you describe in config.yaml
    preview   writes nothing; logs and optionally exports CSV

Runs ONCE per invocation — schedule it nightly (e.g. 2 AM) via Windows Task
Scheduler or cron. See docs/ for deployment details.

Entry points: `python main.py` from a checkout, or `bas-sync` once installed
with `pip install .`.
"""

import argparse
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bassync import __version__, paths
from bassync.config import (
    ConfigError,
    default_log_file,
    load_config,
    load_credentials,
    resolve_default_system,
)
from bassync.drivers import driver_names, load_driver_class
from bassync.history import runs_dir, save_report
from bassync.model import OccupancyWindow
from bassync.notify import ping_monitor, send_alert, send_run_report
from bassync.report import ReportLogHandler, RunReport
from bassync.sync import (
    EXIT_CODE_HELP,
    EXIT_ERROR,
    EXIT_OK,
    run_discover,
    run_sync,
    run_validate,
)

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"


def setup_logging(log_file: str, verbose: bool = False, max_mb: int = 10,
                  backups: int = 10) -> None:
    """
    Log to stdout (captured by Task Scheduler, cron mail, `docker logs`) and to
    a rotating file. The file rotates at `max_mb` and keeps `backups` old
    copies, so a nightly job doesn't grow one log forever; max_mb 0 turns
    rotation off.
    """
    handlers: list = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        if max_mb:
            handlers.append(logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_mb * 1024 * 1024, backupCount=backups,
                encoding="utf-8"))
        else:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        # If the log directory isn't writable, fall back to stdout only rather
        # than refusing to run — a nightly job that won't start is worse than
        # one that only logs to the scheduler's captured output.
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _sample_report(cfg: dict) -> RunReport:
    """A report in the real format, for --test-alert to send."""
    tz = ZoneInfo(cfg["timezone"])
    report = RunReport("TEST ALERT", __version__, tz)
    tomorrow = datetime.now(tz).replace(hour=0, minute=0, second=0,
                                        microsecond=0) + timedelta(days=1)
    windows = [OccupancyWindow(tomorrow.replace(hour=8, minute=30),
                               tomorrow.replace(hour=11, minute=15), ["TEST"]),
               OccupancyWindow(tomorrow.replace(hour=13),
                               tomorrow.replace(hour=17, minute=15), ["TEST"])]
    report.event_count = 2
    report.rooms = 1
    report.add_schedule("example", "12001:5", "Example Hall 101 (sample)",
                        windows, "preview")
    report.add_schedule("example", "12001:100", "Building Example Hall (sample)",
                        [], "preview")
    report.problems.append("INFO: this is a test — no sync ran and nothing was "
                           "written to any BAS.")
    report.finish(EXIT_OK, "test alert — this is what a nightly report looks like")
    return report


def run_test_alert(cfg: dict) -> int:
    """
    Send a test notification and report each channel's outcome.

    Alerting is the one part of the system that only runs when something has
    already gone wrong — which is exactly when you find out it was never
    configured correctly. This exercises it on demand. Email gets a sample
    run report, so you can see what the nightly one will look like.
    """
    alerts = cfg.get("alerts") or {}
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    results = []
    if alerts.get("webhook_url"):
        results += send_alert(
            {**alerts, "email": {}},
            "25Live -> BAS sync: test alert",
            "This is a test notification from `--test-alert`.\n"
            f"Sent {stamp}. No sync ran and nothing was written to any BAS.\n\n"
            "If you are reading this, failure alerts will reach you.",
            force=True)
    email = alerts.get("email") or {}
    if email.get("enabled") or email.get("smtp_host"):
        forced = {**cfg, "alerts": {**alerts, "enabled": True, "webhook_url": "",
                                    "email": {**email, "enabled": True,
                                              "notify_on_success": True}}}
        results += send_run_report(forced, _sample_report(cfg))
    monitoring = cfg.get("monitoring") or {}
    if monitoring.get("ping_url"):
        logging.info("monitoring.ping_url is set; it is pinged after real runs "
                     "only (a test ping would reset the monitor's timer).")

    if not results:
        logging.error(
            "No alert channels are configured. Set alerts.webhook_url and/or "
            "alerts.email.smtp_host in config.yaml. (`alerts.enabled` does not "
            "need to be true for this test, but it does for real alerts.)")
        return EXIT_ERROR

    logging.info("=== Alert test results ===")
    for result in results:
        logging.info("  %s", result)
    if not alerts.get("enabled"):
        logging.warning("alerts.enabled is FALSE — real failures will NOT "
                        "notify. Set it to true once this test passes.")
    ok = all(r.ok for r in results)
    logging.info("=== Alert test %s ===", "PASSED" if ok else "FAILED")
    return EXIT_OK if ok else EXIT_ERROR


def print_drivers() -> int:
    print("Available BAS drivers (set as `driver:` under `systems:`):\n")
    for name in driver_names():
        cls = load_driver_class(name)
        print(f"  {name:<10} {cls.description}")
    print("\nFull setup notes for each are in docs/bas-setup.md and in the driver's "
          "own module docstring under bassync/drivers/.")
    return EXIT_OK


EXIT_EPILOG = ("Exit codes: 0 ok · 1 error/bad config · 2 room map · "
               "3 BAS unreachable · 4 25Live fetch · 5 write failures · "
               "6 validation · 7 safety abort · 8 another run in progress · "
               "130 interrupted.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bas-sync",
        description="25Live -> BAS schedule sync (single run).",
        epilog=EXIT_EPILOG)
    parser.add_argument("--version", action="version",
                        version=f"25live-bas-sync {__version__}")
    parser.add_argument("--config",
                        help="Path to config.yaml (default: config.yaml in the "
                             "install folder or $BAS_HOME, or $BAS_CONFIG)")
    parser.add_argument("--defaults",
                        help="Path to defaults.yaml (default: defaults.yaml in the "
                             "install folder or $BAS_HOME, or $BAS_DEFAULTS)")
    parser.add_argument("--space-map",
                        help="Override the path to space_mapping.yaml "
                             "(or set $BAS_SPACE_MAP)")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Fetch and build, print what WOULD be written and "
                           "whether the safety check would allow it; "
                           "contacts no BAS")
    mode.add_argument("--validate", action="store_true",
                      help="Pre-flight only: config, room map, auth, bookings "
                           "returned, reachability, schedule targets, safety "
                           "state; no writes")
    mode.add_argument("--discover", action="store_true",
                      help="List the 25Live spaces, with their upcoming bookings "
                           "(read-only)")
    mode.add_argument("--list-drivers", action="store_true",
                      help="Show the available BAS drivers and exit")
    mode.add_argument("--test-alert", action="store_true",
                      help="Send a test notification through every configured "
                           "alert channel (email gets a sample run report) and "
                           "report the result. Works even with alerts.enabled "
                           "false, so you can prove the plumbing first")
    parser.add_argument("--discover-days", type=int, default=30,
                        help="--discover counts bookings over this many days "
                             "(default 30)")
    parser.add_argument("--discover-booked-only", action="store_true",
                        help="--discover lists only the spaces with bookings in "
                             "the window, not every space 25Live has")

    parser.add_argument("--system", metavar="NAME",
                        help="Limit the run to one system from `systems:` — "
                             "use it to commission a building at a time")
    parser.add_argument("--force", action="store_true",
                        help="Override the mass-clear safety check. Needed at "
                             "semester break, when a big drop in bookings is real")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug-level logging")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.discover_days < 1:
        parser.error("--discover-days must be at least 1")

    if args.list_drivers:
        return print_drivers()

    cfg_path = (args.config or os.environ.get("BAS_CONFIG")
                or str(paths.config_file()))
    defaults_path = (args.defaults or os.environ.get("BAS_DEFAULTS")
                     or str(paths.defaults_file()))
    config_warnings: list = []
    try:
        cfg = load_config(cfg_path, defaults_path, config_warnings)
    except ConfigError as exc:
        # Logging isn't up yet; bring it up on the default path so this fatal
        # startup error still reaches the log a scheduled task leaves behind,
        # then exit cleanly rather than with a traceback.
        setup_logging(default_log_file())
        logging.error("Configuration error — %s", exc)
        return EXIT_ERROR

    # $BAS_SPACE_MAP lets the Docker image point at a mounted room map without
    # rewriting the command line.
    space_map_path = args.space_map or os.environ.get("BAS_SPACE_MAP")
    if space_map_path:
        cfg["space_map_file"] = space_map_path
    if os.environ.get("BAS_EXTRA_BOOKINGS"):
        cfg["extra_bookings_file"] = os.environ["BAS_EXTRA_BOOKINGS"]
    if cfg["log_file"] is None:
        cfg["log_file"] = default_log_file()

    setup_logging(cfg["log_file"], args.verbose, cfg.get("log_max_mb", 10),
                  cfg.get("log_backups", 10))
    if not Path(cfg_path).exists():
        logging.warning(
            "No config file at %s — using built-in defaults. Copy "
            "config.example.yaml to config.yaml and edit it for your site.",
            cfg_path)
    for warning in config_warnings:
        logging.warning("Config: %s", warning)
    load_credentials(cfg)   # after logging is up, so its warnings are captured

    if args.system and args.system not in (cfg.get("systems") or {}):
        logging.error("--system '%s' is not defined under `systems:`. Known: %s",
                      args.system,
                      ", ".join(sorted(cfg.get("systems") or {})) or "(none)")
        return EXIT_ERROR

    mode = ("VALIDATE" if args.validate else "DISCOVER" if args.discover
            else "TEST ALERT" if args.test_alert
            else "DRY RUN" if args.dry_run else "SYNC")
    systems = cfg.get("systems") or {}
    logging.info("=== 25Live -> BAS sync %s starting (%s, lookahead %d days) ===",
                 __version__, mode, cfg["collegenet"]["lookahead_days"])
    logging.info("Systems: %s | default: %s",
                 ", ".join(f"{n} ({c.get('driver', '?')})"
                           for n, c in sorted(systems.items())) or "(none)",
                 resolve_default_system(cfg) or "(none)")

    if not (args.test_alert or args.validate or args.discover or args.dry_run):
        return live_sync(cfg, args.force, args.system)
    try:
        if args.test_alert:
            code = run_test_alert(cfg)
        elif args.validate:
            code = run_validate(cfg, config_warnings)
        elif args.discover:
            code = run_discover(cfg, args.discover_days,
                                every_space=not args.discover_booked_only)
        else:
            code = run_sync(cfg, dry_run=True, only_system=args.system)
    except KeyboardInterrupt:
        return 130
    except Exception:                                # noqa: BLE001 — last-resort guard
        logging.exception("Unhandled error during %s", mode)
        code = EXIT_ERROR
    return _finish(code)


def _finish(code: int) -> int:
    logging.info("=== Exited with code %d ===", code)
    return code


def live_sync(cfg: dict, force: bool, only_system) -> int:
    """
    A live run: sync, then report. The only mode that notifies — the others
    are interactive and just return a code to whoever ran them.

    Every WARNING/ERROR logged during the run is copied into the report, so
    the alert says what went wrong. A crash still produces a report and an
    alert (exactly one — not a "crashed" and a "failed" for the same event).
    """
    report = RunReport("SYNC", __version__, ZoneInfo(cfg["timezone"]))
    handler = ReportLogHandler(report)
    logging.getLogger().addHandler(handler)
    try:
        try:
            code = run_sync(cfg, dry_run=False, force=force,
                            only_system=only_system, report=report)
        except KeyboardInterrupt:
            logging.warning("Interrupted — some schedules may be partially "
                            "written. Re-run to bring everything back into "
                            "agreement.")
            return 130
        except Exception as exc:                     # noqa: BLE001 — last-resort guard
            logging.exception("Unhandled error during run")
            code = EXIT_ERROR
            report.finish(code, f"crashed: {type(exc).__name__}: {exc}")
        if report.exit_code is None:
            report.finish(code, EXIT_CODE_HELP.get(code, ""))
    finally:
        logging.getLogger().removeHandler(handler)

    for line in report.summary_lines():
        logging.info("%s", line)
    save_report(report, runs_dir(cfg))
    send_run_report(cfg, report)
    ping_monitor(cfg.get("monitoring") or {}, report.ok)
    return _finish(code)


if __name__ == "__main__":
    sys.exit(main())
