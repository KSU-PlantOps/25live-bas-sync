# 25Live -> BAS Schedule Sync — run history
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
A record of every live run, for the web UI's dashboard and history page.

Each live sync saves its RunReport as one small JSON file in `state/runs/`,
next to the safety baseline: the summary the history table needs, plus the
report already rendered as text and HTML (the same ones the email carries),
so a past run reads exactly as it did on the night.

Saving is best-effort. A full disk or an unwritable folder costs the history
entry, never the run.
"""

import json
import logging
import os
import re
import tempfile
import threading
from datetime import timezone
from pathlib import Path
from typing import Optional

from . import paths
from .report import RunReport

KEEP_RUNS = 200
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z?-[a-z-]+(?:-\d+)?$")


def runs_dir(cfg: Optional[dict] = None) -> Path:
    """state/runs, beside wherever the safety state lives."""
    state_file = ((cfg or {}).get("safety") or {}).get("state_file")
    base = Path(state_file).parent if state_file else paths.state_dir()
    return base / "runs"


def report_record(report: RunReport) -> dict:
    counts = report.counts()
    return {
        "mode": report.mode,
        "version": report.version,
        "started": report.started.isoformat(),
        "finished": report.finished.isoformat() if report.finished else None,
        "exit_code": report.exit_code,
        "ok": report.ok,
        "outcome": report.outcome,
        "subject": report.subject(),
        "event_count": report.event_count,
        "rooms": report.rooms,
        "only_system": report.only_system,
        "only_buildings": list(report.only_buildings),
        "safety_ok": report.safety_ok,
        "safety_forced": report.safety_forced,
        "safety_reason": report.safety_reason,
        "counts": counts,
        "problem_count": len(report.problems),
        "systems": {name: {"driver": s.driver, "reachable": s.reachable,
                           "detail": s.detail}
                    for name, s in report.systems.items()},
        "text": report.to_text(),
        "html": report.to_html(),
        "csv": report.to_csv(),
    }


def save_report(report: RunReport, directory: Path, keep: int = KEEP_RUNS) -> Optional[str]:
    """Write the report as the newest history entry and prune the oldest.
    Returns the run id, or None if it couldn't be saved."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # UTC, so the names sort in run order across a DST change.
        stamp = report.started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        mode = re.sub(r"[^a-z]+", "-", report.mode.lower()).strip("-") or "run"
        run_id = f"{stamp}-{mode}"
        n = 1
        while (directory / f"{run_id}.json").exists():
            n += 1
            run_id = f"{stamp}-{mode}-{n}"
        fd, tmp = tempfile.mkstemp(prefix=".run.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(report_record(report), fh)
            os.replace(tmp, directory / f"{run_id}.json")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)            # a half-written record isn't left behind
        for old in sorted(directory.glob("*.json"))[:-keep or None]:
            old.unlink(missing_ok=True)
        return run_id
    except (OSError, ValueError, TypeError) as exc:
        logging.warning("Could not save this run to the history in %s: %s",
                        directory, exc)
        return None


# {path: ((mtime_ns, size), summary)}: a record's summary, kept while its file
# is unchanged. The full records carry the rendered reports, a few MB each on
# a big campus, and the status page asks for the latest every few seconds.
_summaries: dict = {}
_summaries_lock = threading.Lock()


def list_runs(directory: Path, limit: int = 50) -> list:
    """The newest runs first: each record's summary fields plus its `id`,
    without the rendered report bodies."""
    out: list = []
    try:
        files = sorted(directory.glob("*.json"), reverse=True)
    except OSError:
        return out
    for path in files[:limit]:
        summary = _summary(path)
        if summary is not None:
            out.append(dict(summary))
    return out


def _summary(path: Path) -> Optional[dict]:
    try:
        stat = path.stat()
    except OSError:
        return None
    key, stamp = str(path), (stat.st_mtime_ns, stat.st_size)
    with _summaries_lock:
        cached = _summaries.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    record = _read(path)
    if record is None:
        return None
    summary = {k: v for k, v in record.items() if k not in ("text", "html", "csv")}
    summary["id"] = path.stem
    with _summaries_lock:
        if len(_summaries) > 4 * KEEP_RUNS:
            # Pruned runs' entries: start again rather than track them.
            _summaries.clear()
        _summaries[key] = (stamp, summary)
    return summary


def load_run(directory: Path, run_id: str) -> Optional[dict]:
    """One run's full record, or None. The id is checked against the file
    names this module writes, so it can't reach outside the folder."""
    if not _RUN_ID.match(run_id or ""):
        return None
    record = _read(directory / f"{run_id}.json")
    if record is not None:
        record["id"] = run_id
    return record


def _read(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None
