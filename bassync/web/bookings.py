# 25Live -> BAS Schedule Sync — the Extra bookings page
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Extra bookings: occupancy the sync schedules that isn't in 25Live, kept in
extra_bookings.yaml (see bassync/extras.py). Seeing them needs view_all;
adding, changing and deleting them needs edit_bookings.
"""

from datetime import datetime, timedelta

from flask import abort, g, redirect, render_template, request, url_for

from .. import extras
from ..config import ConfigError
from . import access, audit, campus_zone, ctx, notice, requires
from .views import files, load_map, svc, version_of


def _places(buildings: list, floors: list, rooms: list) -> tuple:
    """(grouped options for the Where list, {value: label}). Values are
    "b:<building>", "f:<building>:<level>" and "r:<space_id>"."""
    names = {str(b.get("id")): str(b.get("name") or b.get("id"))
             for b in buildings if isinstance(b, dict) and b.get("id") not in (None, "")}
    groups: list = [("Buildings", [(f"b:{bid}", name) for bid, name in names.items()])]
    floor_opts = []
    for f in floors:
        if isinstance(f, dict) and f.get("building") not in (None, "") and f.get("level") not in (None, ""):
            bid = str(f["building"])
            floor_opts.append((f"f:{bid}:{f['level']}",
                               f.get("name") or f"Floor {f['level']} of {names.get(bid, bid)}"))
    groups.append(("Floors", floor_opts))
    room_opts = []
    for r in rooms:
        if isinstance(r, dict) and r.get("space_id") not in (None, ""):
            sid = str(r["space_id"])
            room_opts.append((f"r:{sid}", f"{r.get('space_name') or sid} ({sid})"))
    groups.append(("Rooms", sorted(room_opts, key=lambda o: o[1].lower())))
    labels = {value: label for _heading, opts in groups for value, label in opts}
    return [grp for grp in groups if grp[1]], labels


def _where_of(row: dict) -> str:
    if row.get("space_id") not in (None, ""):
        return f"r:{row['space_id']}"
    if row.get("floor") not in (None, ""):
        return f"f:{row.get('building')}:{row['floor']}"
    return f"b:{row.get('building')}"


def _row_from_form(form) -> dict:
    row: dict = {"title": (form.get("title") or "").strip()[:120]}
    kind, _, rest = (form.get("where") or "").partition(":")
    if kind == "r":
        row["space_id"] = int(rest) if rest.isdigit() else rest
    elif kind == "f":
        building, _, level = rest.rpartition(":")
        row["building"] = building
        row["floor"] = int(level) if level.lstrip("-").isdigit() else level
    elif kind == "b":
        row["building"] = rest
    if form.get("repeat") == "weekly":
        row["days"] = [d for d in extras.DAYS if d in form.getlist("days")]
        for key in ("from", "until"):
            if form.get(key):
                row[key] = form[key]
    else:
        row["date"] = form.get("date") or ""
    row["start"] = form.get("start") or ""
    row["end"] = form.get("end") or ""
    if form.get("exact"):
        row["exact"] = True
    if form.get("low_temp"):
        row["low_temp"] = True
    note = (form.get("note") or "").strip()[:500]
    if note:
        row["note"] = note
    return row


def register(app) -> None:

    def read_rows() -> tuple:
        path = files().extras_file
        try:
            return extras.read(path), ""
        except ConfigError as exc:
            return [], str(exc)

    def today():
        return datetime.now(campus_zone(svc())).date()

    @app.route("/bookings")
    @requires("view_all")
    def bookings():
        rows, error = read_rows()
        buildings, floors, rooms, _v, _e = load_map()
        _groups, labels = _places(buildings, floors, rooms)
        now = today()
        upcoming: list = []
        ended: list = []
        for index, row in enumerate(rows):
            item = {"index": index, "row": row if isinstance(row, dict) else {},
                    "where": "", "when": "", "problem": "", "next": None}
            try:
                b = extras.parse(row, index)
            except extras.BookingError as exc:
                item["problem"] = str(exc)
                upcoming.append(item)
                continue
            where = _where_of(row)
            item["where"] = labels.get(where) or ""
            if not item["where"]:
                item["problem"] = (f"{b.where} isn't in the room map, so this drives nothing.")
            item["when"] = b.when()
            item["exact"] = b.exact
            starts = b.start_dates(now, now + timedelta(days=400))
            item["next"] = starts[0] if starts else None
            (ended if b.ended(now) else upcoming).append(item)
        upcoming.sort(key=lambda i: (i["next"] is None, i["next"] or now))
        return render_template("bookings.html", upcoming=upcoming, ended=ended, error=error,
                               path=files().extras_file, version=version_of(files().extras_file))

    def _form(index=None, values=None, errors=None, copy=False):
        buildings, floors, rooms, _v, map_error = load_map()
        groups, _labels = _places(buildings, floors, rooms)
        values = dict(values or {})
        values.setdefault("repeat", "weekly" if values.get("days") else "once")
        if not values.get("where") and (values.get("space_id") not in (None, "")
                                        or values.get("building")):
            values["where"] = _where_of(values)
        if isinstance(values.get("days"), str):
            values["days"] = [d.strip() for d in values["days"].split(",")]
        values["days"] = [str(d).strip().lower()[:3] for d in values.get("days") or []]
        for key in ("date", "from", "until", "start", "end"):
            if values.get(key) is not None and not isinstance(values[key], str):
                values[key] = str(values[key])[:10] if key in ("date", "from", "until") else values[key]
        for key in ("start", "end"):
            if isinstance(values.get(key), int):
                values[key] = f"{values[key] // 60:02d}:{values[key] % 60:02d}"
        return render_template("booking_form.html", index=None if copy else index,
                               values=values, groups=groups, errors=errors or [],
                               days=list(zip(extras.DAYS, extras.DAY_LABELS, strict=True)),
                               version=version_of(files().extras_file),
                               map_error=map_error), (422 if errors else 200)

    @app.route("/bookings/new")
    @app.route("/bookings/<int:index>/edit")
    @app.route("/bookings/<int:index>/copy")
    @requires("edit_bookings")
    def booking_form(index=None):
        values: dict = {}
        if index is not None:
            rows, error = read_rows()
            if error:
                abort(409, error)
            if not 0 <= index < len(rows) or not isinstance(rows[index], dict):
                abort(404)
            values = dict(rows[index])
        copy = request.path.endswith("/copy")
        if copy:
            values["title"] = f"{values.get('title') or 'Extra booking'} (copy)"
        return _form(index, values, copy=copy)

    @app.route("/bookings/save", methods=["POST"])
    @requires("edit_bookings")
    def booking_save():
        path = files().extras_file
        form = request.form
        text_index = form.get("index", "")
        index = int(text_index) if text_index.isdigit() else None
        row = _row_from_form(form)
        if not access.can(g.caps, "low_temp"):
            # Only a role that may mark low temp changes it; keep what it was.
            row.pop("low_temp", None)
            rows_now, _err = read_rows()
            if index is not None and 0 <= index < len(rows_now) and \
                    isinstance(rows_now[index], dict) and rows_now[index].get("low_temp"):
                row["low_temp"] = True
        errors = []
        if not row["title"]:
            errors.append("Give it a title, so people can tell what it is.")
        if not form.get("where"):
            errors.append("Pick where it is: a building, a floor or a room.")
        try:
            extras.parse(row)
        except extras.BookingError as exc:
            errors.append(str(exc)[0].upper() + str(exc)[1:])
        if errors:
            return _form(index, dict(row, where=form.get("where"), repeat=form.get("repeat")),
                         errors)
        with ctx()["write_lock"]:
            if form.get("version") != version_of(path):
                return _form(index, dict(row, where=form.get("where"), repeat=form.get("repeat")),
                             ["The extra bookings changed since this page was opened. Reload "
                              "it and redo your change."])
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
                extras.save(path, rows)
            except OSError as exc:
                from .views import _write_error
                return _form(index, dict(row, where=form.get("where")), [_write_error(exc)])
        audit("%s the extra booking %r (%s)", "added" if index is None else "changed",
              row["title"], extras.parse(row).when())
        notice(f"Saved “{row['title']}”. The next sync writes it"
               + (" — or use Sync now." if not svc().jobs.busy else "."))
        return redirect(url_for("bookings"))

    @app.route("/bookings/<int:index>/delete", methods=["POST"])
    @requires("edit_bookings")
    def booking_delete(index):
        path = files().extras_file
        with ctx()["write_lock"]:
            if request.form.get("version") != version_of(path):
                notice("The extra bookings changed since the page was opened; nothing was "
                       "deleted. Try again.", "error")
                return redirect(url_for("bookings"))
            rows, error = read_rows()
            if error or not 0 <= index < len(rows):
                abort(404)
            removed = rows.pop(index)
            try:
                extras.save(path, rows)
            except OSError as exc:
                from .views import _write_error
                notice(_write_error(exc), "error")
                return redirect(url_for("bookings"))
        title = removed.get("title") if isinstance(removed, dict) else "?"
        audit("deleted the extra booking %r", title)
        notice(f"Deleted “{title}”. The next sync takes it off the schedule.")
        return redirect(url_for("bookings"))

    @app.route("/bookings/delete-ended", methods=["POST"])
    @requires("edit_bookings")
    def bookings_delete_ended():
        path = files().extras_file
        now = today()
        with ctx()["write_lock"]:
            if request.form.get("version") != version_of(path):
                notice("The extra bookings changed since the page was opened; nothing was "
                       "deleted. Try again.", "error")
                return redirect(url_for("bookings"))
            rows, error = read_rows()
            if error:
                abort(409, error)
            keep = []
            for index, row in enumerate(rows):
                try:
                    if extras.parse(row, index).ended(now):
                        continue
                except extras.BookingError:
                    pass
                keep.append(row)
            gone = len(rows) - len(keep)
            if gone:
                extras.save(path, keep)
        if gone:
            audit("deleted %d ended extra booking(s)", gone)
        notice(f"Deleted {gone} ended booking(s)." if gone else "None had ended.")
        return redirect(url_for("bookings"))
