# 25Live -> BAS Schedule Sync — the long-running service (scheduler + web UI)
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Run the sync on a schedule and serve the web UI, until stopped.

    bas-sync-service             # what the Docker image runs (`serve`)
    bas-sync-service --health    # exit 0 if a service is alive (HEALTHCHECK)

The schedule is `schedule:` in config.yaml (editable in the web UI), in the
config's timezone. Each run, and each tool the web UI starts, is the ordinary
`bas-sync` command in a process of its own (see bassync/jobs.py), so it does
exactly what the same command would do from a shell.

Environment:

    BAS_WEB_PASSWORD        the web UI's password. Without it the web UI stays
                            off and only the schedule runs.
    BAS_WEB_AUTH=none       serve the web UI with no login — only behind a
                            reverse proxy that authenticates, or on localhost
    BAS_WEB_HOST            listen address (default 0.0.0.0)
    BAS_WEB_PORT            listen port (default 8080)
    BAS_WEB_TLS_CERT        serve HTTPS with this certificate (PEM, full chain)
    BAS_WEB_TLS_KEY         ...and this private key
    BAS_WEB_BEHIND_PROXY=1  trust X-Forwarded-For/-Proto from one reverse proxy
    SYNC_AT                 overrides schedule.times with one HH:MM time
    SYNC_ON_START=1         overrides schedule.run_on_start

Stopping (SIGTERM, i.e. `docker stop`) refuses new jobs and gives a running
sync up to BAS_STOP_GRACE seconds (default 90) to finish before interrupting
it; set the container's stop timeout at least that long.

Restarting from the web UI stops the same way (it isn't offered while a job
runs or a scheduled sync is minutes away), then replaces the process with a
fresh copy of itself: same PID, arguments and environment, so Docker, systemd
or a terminal sees one process that never exited. It re-reads everything read
only at start — the TLS certificate, the log and state locations — but not
the container's environment (.env); that needs `docker compose up -d`.
"""

import argparse
import logging
import logging.handlers
import os
import secrets
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import __version__, paths
from .config import ConfigError, load_config, parse_hhmm
from .jobs import JobBusy, JobManager
from .scheduler import next_run_any
from .updates import UpdateChecker

RESTART_QUIET = 120                # no restart this close to a scheduled sync (seconds)
HEARTBEAT_FILE = "service.alive"
HEARTBEAT_MAX_AGE = 120            # seconds; the loop touches it every ~15 s
TICK_SECONDS = 15


@dataclass
class Schedule:
    enabled: bool = True
    times: list = field(default_factory=list)
    timezone: str = "UTC"
    run_on_start: bool = False
    source: str = "config.yaml"        # or "SYNC_AT"
    error: str = ""                    # why config.yaml couldn't be read


@dataclass
class Paths:
    config: Path
    defaults: Path
    space_map: Path
    state_dir: Path
    log_file: Path
    web: Optional[Path] = None             # web.yaml; default beside config.yaml
    extras: Optional[Path] = None          # extra_bookings.yaml; ditto
    low_temp: Optional[Path] = None        # low_temp_events.yaml; ditto

    @property
    def web_file(self) -> Path:
        return self.web or self.config.parent / "web.yaml"

    @property
    def extras_file(self) -> Path:
        return self.extras or self.config.parent / "extra_bookings.yaml"

    @property
    def low_temp_file(self) -> Path:
        return self.low_temp or self.config.parent / "low_temp_events.yaml"

    @property
    def announcements_file(self) -> Path:
        return self.web_file.parent / "announcements.yaml"

    @property
    def scheduled_file(self) -> Path:
        return self.state_dir / "scheduled.json"

    @property
    def secrets_file(self) -> Path:
        return self.state_dir / "secrets.json"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"


def resolve_paths() -> Paths:
    """The files the sync itself will use, by the same rules as the CLI:
    $BAS_CONFIG / $BAS_DEFAULTS / $BAS_SPACE_MAP, then config.yaml's own
    settings, then the install folder (or $BAS_HOME)."""
    config = Path(os.environ.get("BAS_CONFIG") or paths.config_file())
    defaults = Path(os.environ.get("BAS_DEFAULTS") or paths.defaults_file())
    space_map = os.environ.get("BAS_SPACE_MAP") or ""
    state_dir = paths.state_dir()
    log_file = Path(paths.log_file())
    extras = os.environ.get("BAS_EXTRA_BOOKINGS") or ""
    low_temp = ""
    try:
        cfg = load_config(str(config), str(defaults))
    except ConfigError:
        cfg = None
    if cfg is not None:
        space_map = space_map or cfg.get("space_map_file") or ""
        extras = extras or cfg.get("extra_bookings_file") or ""
        low_temp = cfg.get("low_temp_file") or ""
        if (cfg.get("safety") or {}).get("state_file"):
            state_dir = Path(cfg["safety"]["state_file"]).parent
        if cfg.get("log_file"):
            log_file = Path(cfg["log_file"])
    web = os.environ.get("BAS_WEB_CONFIG") or ""
    return Paths(config, defaults, Path(space_map or paths.space_map_file()),
                 state_dir, log_file, Path(web) if web else None,
                 Path(extras) if extras else None,
                 Path(low_temp) if low_temp else None)


class Service:
    """The scheduler's state and one step of its loop; the web UI reads it."""

    def __init__(self, files: Paths, jobs: Optional[JobManager] = None,
                 environ: Optional[dict] = None):
        self.paths = files
        self.jobs = jobs or JobManager(files.jobs_dir)
        self.environ = os.environ if environ is None else environ
        self.started = datetime.now(timezone.utc)
        self.schedule = Schedule()
        self.next_due: Optional[datetime] = None
        self.pending_since: Optional[datetime] = None    # due while a job ran
        self.last_tick: Optional[datetime] = None
        self.web_enabled = False
        self.web: dict = {}                  # where the web UI listens (no secrets)
        self.stop_event = threading.Event()
        self.restart_requested = False
        self.boot = secrets.token_hex(6)     # changes with every start, restarts included
        self.updates = UpdateChecker()
        self._signature: Optional[tuple] = None
        self._tick_lock = threading.Lock()

    # ── the schedule ─────────────────────────────────────────────────────────

    def read_schedule(self) -> Schedule:
        """schedule: from config.yaml, with SYNC_AT / SYNC_ON_START applied.
        A config.yaml that doesn't load keeps the last good schedule, so a
        half-finished edit can't silently stop the nightly run."""
        previous = self.schedule
        if not self.paths.config.exists():
            # A fresh install: nothing to sync until it has been set up, and a
            # nightly failure until then would only be noise.
            return Schedule(enabled=False, times=previous.times,
                            timezone=previous.timezone,
                            error="No config.yaml yet — set up the Connection "
                                  "page, and scheduled syncs start.")
        try:
            cfg = load_config(str(self.paths.config), str(self.paths.defaults))
        except ConfigError as exc:
            return Schedule(previous.enabled, previous.times, previous.timezone,
                            previous.run_on_start, previous.source, str(exc))
        sched = cfg.get("schedule") or {}
        out = Schedule(enabled=bool(sched.get("enabled", True)),
                       times=list(sched.get("times") or []),
                       timezone=cfg.get("timezone") or "UTC",
                       run_on_start=bool(sched.get("run_on_start")))
        sync_at = (self.environ.get("SYNC_AT") or "").strip()
        if sync_at:
            try:
                out.times = [parse_hhmm(sync_at)]
                out.enabled = True
                out.source = "SYNC_AT"
            except ValueError as exc:
                out.error = f"SYNC_AT {exc}; using config.yaml's schedule"
        if self.environ.get("SYNC_ON_START", "").strip() in ("1", "true", "yes"):
            out.run_on_start = True
        return out

    def _compute_next(self, now: datetime) -> Optional[datetime]:
        if not (self.schedule.enabled and self.schedule.times):
            return None
        tz = ZoneInfo(self.schedule.timezone)
        return next_run_any(self.schedule.times, now.astimezone(tz))

    def tick(self, now: Optional[datetime] = None) -> Optional[str]:
        """One pass of the loop. Returns the id of a sync it started, if any.
        The web UI calls it too, to apply a schedule change at once."""
        with self._tick_lock:
            return self._tick(now)

    def _tick(self, now: Optional[datetime] = None) -> Optional[str]:
        now = now or datetime.now(timezone.utc)
        self.last_tick = now
        self.schedule = self.read_schedule()
        signature = (self.schedule.enabled, tuple(self.schedule.times),
                     self.schedule.timezone)
        if signature != self._signature:
            if self._signature is not None or self.schedule.times:
                logging.info("[scheduler] schedule: %s", self.describe())
            self._signature = signature
            self.next_due = self._compute_next(now)
            if self.next_due is None:
                self.pending_since = None
        due = self.pending_since is not None or (
            self.next_due is not None and now >= self.next_due)
        if not due:
            return None
        if self.next_due is not None and now >= self.next_due:
            self.next_due = self._compute_next(now)
        trigger = "schedule"
        if self.pending_since is not None:
            waited = int((now - self.pending_since).total_seconds() // 60)
            trigger = f"schedule (waited {waited} min for another job)"
        try:
            job = self.jobs.start("sync", trigger=trigger)
        except JobBusy as exc:
            if self.pending_since is None:
                logging.info("[scheduler] sync due, but %s is running; it "
                             "will start when that finishes", exc)
                self.pending_since = now
            return None
        except RuntimeError as exc:
            logging.error("[scheduler] could not start the sync: %s", exc)
            self.pending_since = None
            return None
        self.pending_since = None
        return job.id

    def describe(self) -> str:
        s = self.schedule
        if not s.enabled or not s.times:
            return "off (runs only when started from the web UI or the CLI)"
        text = f"daily at {', '.join(s.times)} ({s.timezone})"
        if s.source != "config.yaml":
            text += f", from {s.source}"
        return text

    # ── restarting ───────────────────────────────────────────────────────────

    def restart_blocker(self, now: Optional[datetime] = None) -> Optional[str]:
        """Why a restart would be a bad idea right now, or None."""
        now = now or datetime.now(timezone.utc)
        job = self.jobs.current
        if job is not None:
            return f"{job.label} is running. Restart when it has finished, or stop it first."
        if self.pending_since is not None:
            return "A scheduled sync is waiting to start. Restart after it has run."
        if self.next_due is not None and 0 <= (self.next_due - now).total_seconds() < RESTART_QUIET:
            return "A scheduled sync starts in the next two minutes. Restart after it has run."
        if self.stop_event.is_set():
            return "The service is already stopping."
        return None

    def request_restart(self) -> Optional[str]:
        """Stop, then start again in place (see main). Returns why not, or
        None when the restart is on its way."""
        blocker = self.restart_blocker()
        if blocker:
            return blocker
        closer = getattr(self.jobs, "close_if_idle", None)
        running = closer() if closer else None
        if running:
            return f"{running} has just started. Restart when it has finished."
        self.restart_requested = True
        logging.info("[service] restart requested")
        # A moment's grace, so the page saying so reaches the browser first.
        timer = threading.Timer(0.5, self.stop_event.set)
        timer.daemon = True
        timer.start()
        return None

    # ── the loop ─────────────────────────────────────────────────────────────

    def run_scheduler(self, stop: threading.Event) -> None:
        self.schedule = self.read_schedule()
        if self.schedule.run_on_start:
            logging.info("[scheduler] running once on start")
            try:
                self.jobs.start("sync", trigger="start")
            except (JobBusy, RuntimeError) as exc:
                logging.warning("[scheduler] start-up sync not started: %s", exc)
        while not stop.is_set():
            try:
                self.tick()
            except Exception:                      # noqa: BLE001 — keep scheduling
                logging.exception("[scheduler] unexpected error; carrying on")
            self._heartbeat()
            wait: float = TICK_SECONDS
            if self.next_due is not None:
                until = (self.next_due - datetime.now(timezone.utc)).total_seconds()
                wait = max(1.0, min(wait, until))
            stop.wait(wait)

    def _heartbeat(self) -> None:
        try:
            self.paths.state_dir.mkdir(parents=True, exist_ok=True)
            (self.paths.state_dir / HEARTBEAT_FILE).write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _setup_logging(log_file: Path) -> None:
    handlers: list = [logging.StreamHandler(sys.stdout)]
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file.parent / "service.log", maxBytes=5 * 1024 * 1024,
            backupCount=5, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")


def health(files: Paths) -> int:
    """0 if a service touched its heartbeat recently, else 1."""
    beat = files.state_dir / HEARTBEAT_FILE
    try:
        age = time.time() - beat.stat().st_mtime
    except OSError:
        print(f"unhealthy: no heartbeat at {beat}")
        return 1
    if age > HEARTBEAT_MAX_AGE:
        print(f"unhealthy: last heartbeat {int(age)}s ago")
        return 1
    print(f"ok: heartbeat {int(age)}s ago")
    return 0


def sso_ready(files: Paths, environ) -> bool:
    """Whether Entra sign-in is switched on and complete (see bassync/web)."""
    from . import secretstore
    from .web import access
    settings, error = access.load(files.web_file)
    secret = secretstore.get("BAS_WEB_SSO_CLIENT_SECRET", files.secrets_file, environ)
    return (not error and settings["sso"]["enabled"]
            and not access.sso_problems(settings, bool(secret)))


def web_settings(environ, sso: bool = False) -> dict:
    """What the web UI should do, from the environment. `enabled` is False
    (with a reason) when nobody could sign in: no password, no working
    single sign-on, and auth not turned off."""
    auth = (environ.get("BAS_WEB_AUTH") or "password").strip().lower()
    password = environ.get("BAS_WEB_PASSWORD") or ""
    cert = environ.get("BAS_WEB_TLS_CERT") or ""
    key = environ.get("BAS_WEB_TLS_KEY") or ""
    try:
        port = int(environ.get("BAS_WEB_PORT") or 8080)
    except ValueError:
        port = -1
    out = {"auth": auth, "password": password, "host": environ.get("BAS_WEB_HOST") or "0.0.0.0",
           "port": port, "cert": cert, "key": key,
           "behind_proxy": (environ.get("BAS_WEB_BEHIND_PROXY") or "").strip() in ("1", "true", "yes"),
           "enabled": True, "reason": ""}
    if auth not in ("password", "none"):
        out.update(enabled=False, reason=f"BAS_WEB_AUTH={auth!r} is not `password` or `none`")
    elif auth == "password" and not password and not sso:
        out.update(enabled=False, reason="BAS_WEB_PASSWORD is not set, and single "
                                         "sign-on isn't set up")
    elif not 0 < port < 65536:
        out.update(enabled=False, reason=f"BAS_WEB_PORT={environ.get('BAS_WEB_PORT')!r} is not a port")
    elif bool(cert) != bool(key):
        out.update(enabled=False, reason="set both BAS_WEB_TLS_CERT and BAS_WEB_TLS_KEY, or neither")
    return out


def _start_web(service: Service, settings: dict):
    """Build the app and start the server on a thread; returns the server."""
    from cheroot import wsgi

    from .web import create_app
    app = create_app(service, settings)
    server = wsgi.Server((settings["host"], settings["port"]), app,
                         numthreads=8, server_name="bas-sync")
    if settings["cert"]:
        from cheroot.ssl.builtin import BuiltinSSLAdapter
        server.ssl_adapter = BuiltinSSLAdapter(settings["cert"], settings["key"])
    server.prepare()                       # binds now, so a busy port fails here
    threading.Thread(target=server.serve, daemon=True, name="web").start()
    return server


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bas-sync-service",
        description="Run the 25Live -> BAS sync on its schedule and serve the web UI.")
    parser.add_argument("--version", action="version",
                        version=f"25live-bas-sync {__version__}")
    parser.add_argument("--health", action="store_true",
                        help="exit 0 if a running service is healthy (for HEALTHCHECK)")
    parser.add_argument("--no-web", action="store_true",
                        help="run the schedule only, without the web UI")
    args = parser.parse_args(argv)
    files = resolve_paths()
    if args.health:
        return health(files)

    _setup_logging(files.log_file)
    sync_at = (os.environ.get("SYNC_AT") or "").strip()
    if sync_at:
        try:
            parse_hhmm(sync_at)
        except ValueError as exc:
            # Set on purpose, so don't quietly fall back to another schedule.
            logging.error("[service] SYNC_AT %s — fix it or remove it (the "
                          "schedule can be set in the web UI instead). Exiting.", exc)
            return 2
    service = Service(files)
    logging.info("=== 25live-bas-sync %s service starting ===", __version__)
    logging.info("[service] config %s, room map %s, state %s",
                 files.config, files.space_map, files.state_dir)
    if not files.config.exists():
        logging.warning("[service] no config.yaml at %s yet — set it up in the "
                        "web UI (Settings) or copy config.example.yaml there",
                        files.config)

    stop = service.stop_event

    def _on_signal(signum, _frame):
        logging.info("[service] %s received; stopping", signal.Signals(signum).name)
        service.restart_requested = False          # a stop wins over a restart
        stop.set()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    server = None
    try:
        sso = sso_ready(files, os.environ)
    except Exception:                              # noqa: BLE001 — flask missing, etc.
        sso = False
    settings = web_settings(os.environ, sso)
    if args.no_web:
        logging.info("[service] web UI off (--no-web)")
    elif not settings["enabled"]:
        logging.warning("[service] web UI off: %s. The schedule still runs.",
                        settings["reason"])
    else:
        try:
            server = _start_web(service, settings)
        except Exception as exc:                   # noqa: BLE001 — say why, keep scheduling
            logging.error("[service] web UI could not start on %s:%s — %s. The "
                          "schedule still runs.", settings["host"], settings["port"], exc)
        else:
            service.web_enabled = True
            service.web = {k: settings[k] for k in ("host", "port", "cert", "behind_proxy", "auth")}
            scheme = "https" if settings["cert"] else "http"
            logging.info("[service] web UI on %s://%s:%d%s", scheme, settings["host"],
                         settings["port"],
                         " — NO LOGIN (BAS_WEB_AUTH=none)" if settings["auth"] == "none" else "")
            if settings["password"] and len(settings["password"]) < 12:
                logging.warning("[service] BAS_WEB_PASSWORD is shorter than 12 "
                                "characters; anyone who reaches this port can "
                                "start a sync")

    scheduler = threading.Thread(target=service.run_scheduler, args=(stop,),
                                 daemon=True, name="scheduler")
    scheduler.start()
    while not stop.is_set():
        stop.wait(1)
    if server is not None:
        server.stop()
    try:
        grace = float(os.environ.get("BAS_STOP_GRACE") or 90)
    except ValueError:
        grace = 90.0
    service.jobs.shutdown(grace)
    scheduler.join(timeout=5)
    if service.restart_requested:
        return restart_in_place()
    logging.info("=== service stopped ===")
    return 0


def restart_in_place() -> int:
    """Replace this process with a fresh copy of itself. Returns only if that
    failed, with an exit code a restart policy will act on."""
    logging.info("=== service restarting ===")
    for handler in logging.getLogger().handlers:
        handler.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    try:
        os.execv(sys.executable, argv)
    except OSError as exc:
        logging.error("[service] couldn't restart in place (%s); exiting so the "
                      "container's restart policy starts it again", exc)
    return 3


if __name__ == "__main__":
    sys.exit(main())
