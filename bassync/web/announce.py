# 25Live -> BAS Schedule Sync — announcements on the status page
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Announcements: a short message on the home page for everyone signed in —
"BAS maintenance Saturday 6–10; syncs may be late" — shown from a start time
until an end time (or until it's deleted). Posting them needs the `announce`
capability. They're kept in announcements.yaml beside web.yaml:

    announcements:
      - message: BAS maintenance Saturday 6–10 AM; syncs may be late.
        level: warning              # info (the default) or warning
        starts: 2026-10-03 06:00    # campus time
        ends: 2026-10-03 10:00      # optional
        added_by: Jane Doe
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from flask import abort, g, redirect, render_template, request, url_for

from ..config import ConfigError, read_yaml
from . import audit, campus_zone, ctx, notice, requires
from .views import files, svc, version_of

LEVELS = {"info": "Information", "warning": "Warning"}
KEYS = ("message", "level", "starts", "ends", "added_by")
MAX_MESSAGE = 600
HEADER = """\
# Announcements on the web UI's home page. Managed on its Announcements page,
# but plain YAML and safe to hand-edit. Times are campus time; `ends` is
# optional. See bassync/web/announce.py.
"""


class AnnouncementError(ValueError):
    """An announcement can't be used; the message says why."""


@dataclass(frozen=True)
class Announcement:
    index: int
    message: str
    level: str
    starts: datetime            # campus time, naive
    ends: Optional[datetime]
    added_by: str

    def showing(self, now: datetime) -> bool:
        return self.starts <= now and (self.ends is None or now < self.ends)

    def ended(self, now: datetime) -> bool:
        return self.ends is not None and self.ends <= now


def _when(value, what: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("T", " "))
    except ValueError:
        raise AnnouncementError(f"the {what} time must look like 2026-10-03 06:00, "
                                f"not {value!r}.")
    return parsed.replace(tzinfo=None, second=0, microsecond=0)


def parse(row, index: int = 0) -> Announcement:
    if not isinstance(row, dict):
        raise AnnouncementError("an entry is not a mapping.")
    message = str(row.get("message") or "").strip()
    if not message:
        raise AnnouncementError("write the message to show.")
    if len(message) > MAX_MESSAGE:
        raise AnnouncementError(f"keep the message under {MAX_MESSAGE} characters.")
    level = str(row.get("level") or "info").strip().lower()
    if level not in LEVELS:
        raise AnnouncementError("the level must be info or warning.")
    starts = _when(row.get("starts"), "start")
    if starts is None:
        raise AnnouncementError("give it a start time.")
    ends = _when(row.get("ends"), "end")
    if ends is not None and ends <= starts:
        raise AnnouncementError("it ends before it starts.")
    return Announcement(index, message, level, starts, ends, str(row.get("added_by") or ""))


def read(path) -> list:
    """The rows as written; [] if there's no file. Raises ConfigError."""
    if not path.exists():
        return []
    rows = read_yaml(path).get("announcements")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ConfigError(f"{path}: `announcements:` must be a list.")
    return rows


def dump(rows: list) -> str:
    import yaml
    clean = []
    for row in rows:
        if isinstance(row, dict):
            clean.append({k: str(row[k]) for k in KEYS if row.get(k) not in (None, "")})
    return HEADER + "\n" + yaml.safe_dump({"announcements": clean}, sort_keys=False,
                                          default_flow_style=False, allow_unicode=True,
                                          width=100)


def save(path, rows: list) -> bool:
    from ..mapedit import write_if_changed
    return write_if_changed(path, dump(rows))


def now_local() -> datetime:
    return datetime.now(campus_zone(svc())).replace(tzinfo=None)


def showing() -> list:
    """The announcements to show right now, warnings first. A file that
    can't be read shows nothing rather than breaking the home page."""
    try:
        rows = read(files().announcements_file)
    except ConfigError:
        return []
    now = now_local()
    out = []
    for index, row in enumerate(rows):
        try:
            a = parse(row, index)
        except AnnouncementError:
            continue
        if a.showing(now):
            out.append(a)
    return sorted(out, key=lambda a: (a.level != "warning", a.starts))


def _row_from_form(form) -> dict:
    row = {"message": (form.get("message") or "").strip().replace("\r\n", "\n"),
           "level": form.get("level") or "info",
           "starts": (form.get("starts") or "").replace("T", " "),
           "ends": (form.get("ends") or "").replace("T", " ")}
    return {k: v for k, v in row.items() if v}


def register(app) -> None:

    def read_rows() -> tuple:
        try:
            return read(files().announcements_file), ""
        except ConfigError as exc:
            return [], str(exc)

    @app.route("/announcements")
    @requires("announce")
    def announcements():
        rows, error = read_rows()
        now = now_local()
        items: dict = {"showing": [], "upcoming": [], "ended": [], "broken": []}
        for index, row in enumerate(rows):
            try:
                a = parse(row, index)
            except AnnouncementError as exc:
                items["broken"].append({"index": index, "row": row, "problem": str(exc)})
                continue
            key = "showing" if a.showing(now) else "ended" if a.ended(now) else "upcoming"
            items[key].append(a)
        for key in ("showing", "upcoming"):
            items[key].sort(key=lambda a: a.starts)
        return render_template("announcements.html", items=items, error=error,
                               levels=LEVELS, version=version_of(files().announcements_file))

    def _form(index=None, values=None, errors=None):
        values = dict(values or {})
        for key in ("starts", "ends"):
            if values.get(key):
                values[key] = str(values[key]).replace(" ", "T")[:16]
        if not values.get("starts"):
            values["starts"] = now_local().strftime("%Y-%m-%dT%H:%M")
        return render_template("announcement_form.html", index=index, values=values,
                               levels=LEVELS, errors=errors or [],
                               version=version_of(files().announcements_file),
                               max_message=MAX_MESSAGE), (422 if errors else 200)

    @app.route("/announcements/new")
    @app.route("/announcements/<int:index>/edit")
    @requires("announce")
    def announcement_form(index=None):
        values: dict = {}
        if index is not None:
            rows, error = read_rows()
            if error:
                abort(409, error)
            if not 0 <= index < len(rows) or not isinstance(rows[index], dict):
                abort(404)
            values = dict(rows[index])
        return _form(index, values)

    @app.route("/announcements/save", methods=["POST"])
    @requires("announce")
    def announcement_save():
        path = files().announcements_file
        form = request.form
        text_index = form.get("index", "")
        index = int(text_index) if text_index.isdigit() else None
        row = _row_from_form(form)
        try:
            parse(row)
        except AnnouncementError as exc:
            return _form(index, row, [str(exc)[0].upper() + str(exc)[1:]])
        with ctx()["write_lock"]:
            if form.get("version") != version_of(path):
                return _form(index, row, ["The announcements changed since this page was "
                                          "opened. Reload it and redo your change."])
            rows, error = read_rows()
            if error:
                abort(409, error)
            user = g.get("user") or {}
            if index is None:
                row["added_by"] = user.get("name") or user.get("username") or ""
                rows.append(row)
            elif 0 <= index < len(rows):
                old = rows[index] if isinstance(rows[index], dict) else {}
                if old.get("added_by"):
                    row["added_by"] = old["added_by"]
                rows[index] = row
            else:
                abort(404)
            try:
                save(path, rows)
            except OSError as exc:
                from .views import _write_error
                return _form(index, row, [_write_error(exc)])
        a = parse(row)
        audit("%s an announcement (%s, from %s): %r", "added" if index is None else "changed",
              a.level, a.starts.strftime("%Y-%m-%d %H:%M"), a.message[:80])
        notice("Saved. It shows on the home page "
               + ("now." if a.showing(now_local()) else f"from {a.starts:%a %d %b %H:%M}."))
        return redirect(url_for("announcements"))

    @app.route("/announcements/<int:index>/delete", methods=["POST"])
    @requires("announce")
    def announcement_delete(index):
        path = files().announcements_file
        with ctx()["write_lock"]:
            if request.form.get("version") != version_of(path):
                notice("The announcements changed since the page was opened; nothing was "
                       "deleted. Try again.", "error")
                return redirect(url_for("announcements"))
            rows, error = read_rows()
            if error or not 0 <= index < len(rows):
                abort(404)
            removed = rows.pop(index)
            try:
                save(path, rows)
            except OSError as exc:
                from .views import _write_error
                notice(_write_error(exc), "error")
                return redirect(url_for("announcements"))
        message = str(removed.get("message") if isinstance(removed, dict) else "")
        audit("deleted an announcement: %r", message[:80])
        notice("Deleted the announcement.")
        return redirect(url_for("announcements"))

    @app.route("/announcements/delete-ended", methods=["POST"])
    @requires("announce")
    def announcements_delete_ended():
        path = files().announcements_file
        now = now_local()
        with ctx()["write_lock"]:
            if request.form.get("version") != version_of(path):
                notice("The announcements changed since the page was opened; nothing was "
                       "deleted. Try again.", "error")
                return redirect(url_for("announcements"))
            rows, error = read_rows()
            if error:
                abort(409, error)
            keep = []
            for index, row in enumerate(rows):
                try:
                    if parse(row, index).ended(now):
                        continue
                except AnnouncementError:
                    pass
                keep.append(row)
            gone = len(rows) - len(keep)
            if gone:
                save(path, keep)
        if gone:
            audit("deleted %d ended announcement(s)", gone)
        notice(f"Deleted {gone} ended announcement(s)." if gone else "None had ended.")
        return redirect(url_for("announcements"))
