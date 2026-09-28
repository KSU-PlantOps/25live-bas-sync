# 25Live -> BAS Schedule Sync — background jobs for the service
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Run the sync and its read-only tools in the background, one at a time, and
keep what they printed.

Every job is the ordinary command line — `python -m bassync.cli --validate`,
and so on — in a process of its own. That is deliberate:

- A process can hold BACnet's UDP port 47808 only while it runs, and the live
  sync takes the run lock in state/. A job does exactly what the same command
  would do from a shell, so the web UI can't bypass a check the CLI makes.
- A crash or a hung network call in a job can't take the web server with it,
  and a stuck job can be stopped.

One job runs at a time. A second request while one runs is refused, naming
the one running, rather than queued: pressing "Sync now" twice should not
sync twice. The scheduler waits for a running job to finish instead.

Each job's output is kept in memory for the page that is watching it, and in
state/jobs/ so it survives a restart of the service.
"""

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

KEEP_JOBS = 100
MAX_LINES = 20000

# kind -> (label, CLI arguments, whether it writes to a BAS)
KINDS: dict = {
    "sync": ("Sync", [], True),
    "dry-run": ("Dry run", ["--dry-run"], False),
    "validate": ("Validate", ["--validate"], False),
    "discover": ("Discover spaces", ["--discover"], False),
    "test-alert": ("Test alert", ["--test-alert"], False),
}

_JOB_ID = re.compile(r"^\d{8}T\d{6}Z-[a-z-]+-[0-9a-f]{6}$")


class JobBusy(RuntimeError):
    """Another job is running; its label is the message."""


@dataclass
class Job:
    id: str
    kind: str
    label: str
    args: list
    trigger: str                          # "schedule", "start", "web (10.0.0.9)"
    started: str
    finished: Optional[str] = None
    exit_code: Optional[int] = None
    stopped_by: str = ""
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES), repr=False)
    dropped: int = 0                      # lines that fell off the front
    process: Optional[subprocess.Popen] = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.finished is None

    def summary(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)
             if f.name not in ("lines", "process", "dropped")}
        d["args"] = list(self.args)
        d["running"] = self.running
        return d

    def output_since(self, index: int) -> tuple:
        """(new lines, the index to ask from next time). `index` counts every
        line the job ever printed, so a page that fell behind the in-memory
        window just skips ahead."""
        lines = list(self.lines)
        first = self.dropped
        start = max(index - first, 0)
        return lines[start:], first + len(lines)


class JobManager:
    def __init__(self, jobs_dir: Path, command: Optional[list] = None,
                 env: Optional[dict] = None):
        self.dir = Path(jobs_dir)
        self.command = command or [sys.executable, "-m", "bassync.cli"]
        self.env = env
        self._lock = threading.Lock()
        self._current: Optional[Job] = None
        self._recent: deque = deque(maxlen=20)       # finished, newest last
        self._stopping = False
        self._recover_interrupted()

    # ── starting and stopping ────────────────────────────────────────────────

    def start(self, kind: str, extra_args: Optional[list] = None,
              trigger: str = "") -> Job:
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        label, base_args, _writes = KINDS[kind]
        args = list(base_args) + [str(a) for a in (extra_args or [])]
        with self._lock:
            if self._stopping:
                raise JobBusy("the service is shutting down")
            if self._current is not None:
                raise JobBusy(self._current.label)
            now = datetime.now(timezone.utc)
            job = Job(id=f"{now:%Y%m%dT%H%M%SZ}-{kind}-{secrets.token_hex(3)}",
                      kind=kind, label=label, args=args, trigger=trigger,
                      started=now.isoformat())
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass                    # the job still runs; only its record is lost
            env = dict(os.environ if self.env is None else self.env)
            env["PYTHONUNBUFFERED"] = "1"
            try:
                job.process = subprocess.Popen(
                    self.command + args, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    env=env)
            except OSError as exc:
                raise RuntimeError(f"could not start {label}: {exc}")
            self._current = job
            # The reader starts before anything else can fail, so a running
            # process always has someone to collect it and free the slot.
            threading.Thread(target=self._pump, args=(job,), daemon=True,
                             name=f"job-{kind}").start()
            self._write_meta(job)
        logging.info("[jobs] %s started (%s): %s", label, trigger or "?",
                     " ".join(args) or "(live sync)")
        return job

    def stop(self, job_id: str, who: str = "") -> bool:
        """Interrupt a running job the way Ctrl+C would. The sync catches it
        and says what state it left things in."""
        job = self._current
        if job is None or job.id != job_id or job.process is None:
            return False
        job.stopped_by = who or "stopped"
        logging.warning("[jobs] %s stopped by %s", job.label, job.stopped_by)
        try:
            job.process.send_signal(signal.SIGINT)
        except OSError:
            return False
        return True

    def shutdown(self, grace: float = 60.0) -> None:
        """Refuse new jobs, give a running one `grace` seconds to finish, then
        interrupt it — a live sync stopped half-way leaves schedules
        partially written, so it gets the chance to finish first."""
        with self._lock:
            self._stopping = True
            job = self._current
        if job is None or job.process is None:
            return
        logging.info("[jobs] waiting up to %ds for %s to finish", int(grace), job.label)
        try:
            job.process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        job.stopped_by = "service shutdown"
        for sig, wait in ((signal.SIGINT, 10), (signal.SIGTERM, 5)):
            try:
                job.process.send_signal(sig)
                job.process.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                return
        job.process.kill()

    def close_if_idle(self) -> Optional[str]:
        """Refuse new jobs from now on — if none is running. Returns the
        running job's label instead, changing nothing, if one is."""
        with self._lock:
            if self._current is not None:
                return self._current.label
            self._stopping = True
            return None

    # ── reading ──────────────────────────────────────────────────────────────

    @property
    def current(self) -> Optional[Job]:
        return self._current

    @property
    def busy(self) -> bool:
        return self._current is not None

    def get(self, job_id: str) -> Optional[Job]:
        """A running or recent job, from memory or from state/jobs/."""
        for job in [self._current, *reversed(self._recent)]:
            if job is not None and job.id == job_id:
                return job
        if not _JOB_ID.match(job_id or ""):
            return None
        meta = _read_json(self.dir / f"{job_id}.json")
        if meta is None:
            return None
        job = Job(id=str(meta.get("id") or job_id), kind=str(meta.get("kind") or ""),
                  label=str(meta.get("label") or ""), args=list(meta.get("args") or []),
                  trigger=str(meta.get("trigger") or ""),
                  started=str(meta.get("started") or ""), finished=meta.get("finished"),
                  exit_code=meta.get("exit_code"),
                  stopped_by=str(meta.get("stopped_by") or ""))
        try:
            text = (self.dir / f"{job_id}.log").read_text(encoding="utf-8",
                                                          errors="replace")
            job.lines.extend(text.splitlines())
        except OSError:
            pass
        return job

    def recent(self, limit: int = 30) -> list:
        """Summaries of the newest jobs first, the running one included."""
        out = []
        try:
            files = sorted(self.dir.glob("*.json"), reverse=True)[:limit]
        except OSError:
            files = []
        for path in files:
            meta = _read_json(path)
            if meta:
                meta["running"] = meta.get("finished") is None
                out.append(meta)
        return out

    # ── internals ────────────────────────────────────────────────────────────

    def _pump(self, job: Job) -> None:
        log_path = self.dir / f"{job.id}.log"
        try:
            log = open(log_path, "a", encoding="utf-8")
        except OSError:
            log = None
        assert job.process is not None and job.process.stdout is not None
        try:
            for line in job.process.stdout:
                line = line.rstrip("\n")
                if len(job.lines) == job.lines.maxlen:
                    job.dropped += 1
                job.lines.append(line)
                if log is not None:
                    log.write(line + "\n")
                    log.flush()
            code = job.process.wait()
        finally:
            if log is not None:
                log.close()
        # Recorded and tidied up under the lock, and only then shown as
        # finished: nothing can start, or see this job done, half-way through.
        with self._lock:
            finished = datetime.now(timezone.utc).isoformat()
            self._write_meta(job, dict(job.summary(), exit_code=code, finished=finished,
                                       running=False))
            self._prune(keep=job.id)
            self._recent.append(job)
            job.exit_code = code
            job.finished = finished
            self._current = None
        logging.info("[jobs] %s finished with exit code %d", job.label, code)

    def _write_meta(self, job: Job, summary: Optional[dict] = None) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f".{job.id}.json.tmp"
            tmp.write_text(json.dumps(summary or job.summary()), encoding="utf-8")
            os.replace(tmp, self.dir / f"{job.id}.json")
        except (OSError, TypeError, ValueError) as exc:
            logging.warning("[jobs] could not record job %s in %s: %s",
                            job.id, self.dir, exc)

    def _prune(self, keep: str = "") -> None:
        """Keep the newest KEEP_JOBS records and their output, and `keep`'s.
        Oldest first by when each was last written: ids only go down to the
        second, and the rest of an id is random."""
        try:
            records = []
            for path in self.dir.glob("*.json"):
                try:
                    records.append((path.stat().st_mtime, path.name, path))
                except FileNotFoundError:
                    continue
            logs = list(self.dir.glob("*.log"))
        except OSError:
            return
        records.sort()
        kept = {path.stem for _mtime, _name, path in records[-KEEP_JOBS:]} | {keep}
        for _mtime, _name, path in records:
            if path.stem not in kept:
                path.unlink(missing_ok=True)
        for path in logs:                      # and output whose record is gone
            if path.stem not in kept:
                path.unlink(missing_ok=True)

    def _recover_interrupted(self) -> None:
        """A job still marked running from before a restart didn't finish."""
        try:
            files = list(self.dir.glob("*.json"))
        except OSError:
            return
        for path in files:
            meta = _read_json(path)
            if meta and meta.get("finished") is None:
                meta["finished"] = meta.get("started")
                meta["stopped_by"] = "service restarted while it ran"
                meta.pop("running", None)
                try:
                    path.write_text(json.dumps(meta), encoding="utf-8")
                except OSError:
                    pass


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
