# 25Live -> BAS Schedule Sync — run report
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
What a run did, in a form a person can read.

A live sync fills one RunReport as it goes — how many bookings came back, the
safety verdict, each system's reachability, and every schedule with the exact
windows written to it (or why it wasn't). The notifier turns it into the
email and webhook messages; everything is also in the log.

Warnings and errors logged during the run are captured too (see
ReportLogHandler), so a failure alert carries the reason instead of "see the
log" — the log is on a server the person reading the email may not have
access to at 6 AM.
"""

import csv
import html
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Captured log lines are capped so a run with thousands of failures still
# produces an email a mail relay will accept.
MAX_PROBLEMS = 200


@dataclass
class ScheduleResult:
    """One schedule this run was responsible for."""
    system: str
    target: str
    label: str
    windows: list
    status: str                 # written | failed | not written | preview
    error: str = ""


@dataclass
class SystemResult:
    name: str
    driver: str
    reachable: Optional[bool] = None
    detail: str = ""
    heartbeat: str = ""


@dataclass
class RunReport:
    mode: str
    version: str
    tz: object = None
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())
    finished: Optional[datetime] = None
    exit_code: Optional[int] = None
    outcome: str = ""                       # one line, set with the exit code
    event_count: Optional[int] = None
    rooms: int = 0
    map_errors: list = field(default_factory=list)
    map_warnings: list = field(default_factory=list)
    safety_ok: Optional[bool] = None
    safety_reason: str = ""
    safety_forced: bool = False
    baseline: str = ""
    only_system: str = ""
    systems: dict = field(default_factory=dict)
    schedules: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    def __post_init__(self):
        if self.tz is not None:
            self.started = datetime.now(self.tz)  # type: ignore[arg-type]

    # ── recording ────────────────────────────────────────────────────────────

    def system(self, name: str, driver: str = "") -> SystemResult:
        if name not in self.systems:
            self.systems[name] = SystemResult(name, driver)
        return self.systems[name]

    def add_schedule(self, system: str, target: str, label: str, windows: list,
                     status: str, error: str = "") -> None:
        self.schedules.append(ScheduleResult(system, target, label or target,
                                             list(windows), status, error))

    def finish(self, exit_code: int, outcome: str = "") -> None:
        self.exit_code = exit_code
        self.outcome = outcome
        self.finished = (datetime.now(self.tz) if self.tz is not None  # type: ignore[arg-type]
                         else datetime.now().astimezone())

    # ── derived ──────────────────────────────────────────────────────────────

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def counts(self) -> dict:
        written = [s for s in self.schedules if s.status in ("written", "preview")]
        return {
            "written": len(written),
            "occupied": sum(1 for s in written if s.windows),
            "cleared": sum(1 for s in written if not s.windows),
            "failed": sum(1 for s in self.schedules if s.status == "failed"),
            "not_written": sum(1 for s in self.schedules if s.status == "not written"),
            "windows": sum(len(s.windows) for s in written),
        }

    def subject(self, prefix: str = "") -> str:
        c = self.counts()
        day = self.started.strftime("%a %b %d")
        if self.ok:
            body = (f"OK — {c['written']} schedule(s) written, "
                    f"{c['windows']} booking window(s) ({day})")
        else:
            body = f"FAILED (exit {self.exit_code}) — {self.outcome or 'see details'} ({day})"
        return f"{prefix} {body}".strip()

    def summary_lines(self) -> list:
        c = self.counts()
        lines = [f"Result: {'OK' if self.ok else 'FAILED'}"
                 + (f" (exit {self.exit_code})" if not self.ok else "")
                 + (f" — {self.outcome}" if self.outcome else "")]
        when = self.started.strftime("%Y-%m-%d %H:%M %Z")
        took = ""
        if self.finished:
            took = f", {int((self.finished - self.started).total_seconds())}s"
        lines.append(f"Run: {self.mode} started {when}{took} (25live-bas-sync {self.version})")
        if self.only_system:
            lines.append(f"Limited to system: {self.only_system}")
        if self.event_count is not None:
            lines.append(f"25Live: {self.event_count} booking(s) for {self.rooms} mapped room(s)")
        if self.schedules:
            lines.append(f"Schedules: {c['written']} written ({c['occupied']} with "
                         f"bookings, {c['cleared']} cleared), {c['failed']} failed"
                         + (f", {c['not_written']} not written" if c["not_written"] else ""))
        if self.safety_ok is not None:
            verdict = "passed" if self.safety_ok else (
                "OVERRIDDEN with --force" if self.safety_forced else "BLOCKED the run")
            lines.append(f"Safety check: {verdict} — {self.safety_reason}")
        if self.baseline:
            lines.append(f"Safety baseline: {self.baseline}")
        if self.map_errors:
            lines.append(f"Room map: {len(self.map_errors)} broken row(s) left out")
        for s in self.systems.values():
            state = ("reachable" if s.reachable else "UNREACHABLE"
                     if s.reachable is False else "not contacted")
            line = f"System {s.name} ({s.driver}): {state}"
            if s.detail:
                line += f" — {s.detail}"
            if s.heartbeat:
                line += f"; heartbeat {s.heartbeat}"
            lines.append(line)
        return lines

    # ── rendering ────────────────────────────────────────────────────────────

    @staticmethod
    def _day_lines(windows: list) -> list:
        """Windows grouped by local day: ['Thu 10/01  08:30–11:15, 13:00–17:15']."""
        by_day: dict = {}
        for w in windows:
            by_day.setdefault(w.start.date(), []).append(w)
        out = []
        for day in sorted(by_day):
            spans = []
            for w in by_day[day]:
                end = w.end.strftime("%H:%M")
                if w.end.date() != w.start.date():
                    end = w.end.strftime("%a %H:%M")
                spans.append(f"{w.start.strftime('%H:%M')}–{end}")
            out.append(f"{by_day[day][0].start.strftime('%a %m/%d')}  {', '.join(spans)}")
        return out

    def _sorted_schedules(self) -> list:
        order = {"failed": 0, "not written": 1, "written": 2, "preview": 3}
        return sorted(self.schedules,
                      key=lambda s: (order.get(s.status, 9), s.system, s.label.lower()))

    def to_text(self, full: bool = True) -> str:
        parts = ["\n".join(self.summary_lines())]
        problems = self.problems
        if problems:
            parts.append("Problems:\n" + "\n".join(f"  - {p}" for p in problems[:MAX_PROBLEMS]))
        if full and self.schedules:
            rows = []
            for s in self._sorted_schedules():
                head = f"[{s.status.upper()}] {s.label}  ({s.system}:{s.target})"
                if s.error:
                    rows.append(f"{head}\n      error: {s.error}")
                elif not s.windows:
                    rows.append(f"{head}\n      no bookings — cleared")
                else:
                    rows.append(head + "\n" + "\n".join(
                        f"      {line}" for line in self._day_lines(s.windows)))
            parts.append("What was scheduled:\n" + "\n".join(rows))
        return "\n\n".join(parts) + "\n"

    def to_html(self, full: bool = True) -> str:
        e = html.escape
        colour = "#1a7f37" if self.ok else "#cf222e"
        out = [
            "<html><body style=\"font-family:-apple-system,Segoe UI,Arial,"
            "sans-serif;font-size:14px;color:#1f2328\">",
            f"<h2 style=\"color:{colour};margin:0 0 8px\">"
            f"{'Sync OK' if self.ok else 'Sync FAILED'}</h2>",
            "<ul style=\"padding-left:18px\">",
            *[f"<li>{e(line)}</li>" for line in self.summary_lines()],
            "</ul>",
        ]
        problems = self.problems
        if problems:
            out.append("<h3>Problems</h3><ul style=\"padding-left:18px\">")
            out.extend(f"<li><code>{e(p)}</code></li>" for p in problems[:MAX_PROBLEMS])
            out.append("</ul>")
        if full and self.schedules:
            out.append("<h3>What was scheduled</h3>")
            out.append("<table cellpadding=\"6\" style=\"border-collapse:collapse;"
                       "border:1px solid #d0d7de\">"
                       "<tr style=\"background:#f6f8fa;text-align:left\">"
                       "<th>Status</th><th>Space / schedule</th><th>System</th>"
                       "<th>Target</th><th>Bookings</th></tr>")
            for s in self._sorted_schedules():
                if s.error:
                    detail = f"<span style=\"color:#cf222e\">{e(s.error)}</span>"
                elif not s.windows:
                    detail = "<em>no bookings — cleared</em>"
                else:
                    detail = "<br>".join(e(line) for line in self._day_lines(s.windows))
                status_colour = {"failed": "#cf222e", "not written": "#9a6700"}.get(
                    s.status, "#1a7f37")
                out.append(
                    "<tr style=\"border-top:1px solid #d0d7de;vertical-align:top\">"
                    f"<td style=\"color:{status_colour};white-space:nowrap\">"
                    f"{e(s.status)}</td><td>{e(s.label)}</td><td>{e(s.system)}</td>"
                    f"<td><code>{e(s.target)}</code></td><td>{detail}</td></tr>")
            out.append("</table>")
        out.append("</body></html>")
        return "\n".join(out)

    def to_csv(self) -> str:
        """Every schedule and window, one row per window, for a spreadsheet."""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["status", "system", "target", "label", "start", "end",
                         "hours", "event_ids", "error"])
        for s in self._sorted_schedules():
            if not s.windows:
                writer.writerow([s.status, s.system, s.target, s.label, "", "",
                                 0, "", s.error])
            for w in s.windows:
                writer.writerow([
                    s.status, s.system, s.target, s.label,
                    w.start.isoformat(), w.end.isoformat(),
                    round(w.duration.total_seconds() / 3600, 2),
                    " ".join(str(i) for i in getattr(w, "source_event_ids", [])),
                    s.error])
        return buf.getvalue()


class ReportLogHandler(logging.Handler):
    """Copies WARNING-and-above log lines into a RunReport, so the alert says
    what went wrong instead of pointing at a log file."""

    def __init__(self, report: RunReport, level: int = logging.WARNING):
        super().__init__(level)
        self.report = report

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.report.problems) >= MAX_PROBLEMS:
            return
        try:
            message = record.getMessage()
        except Exception:                                  # noqa: BLE001
            message = str(record.msg)
        self.report.problems.append(f"{record.levelname}: {message}")
