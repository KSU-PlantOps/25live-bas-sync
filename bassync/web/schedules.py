# 25Live -> BAS Schedule Sync — what's scheduled, per space
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The Schedules pages: what the sync last wrote for each space — its bookings,
and the times written to every schedule it drives — read from
state/scheduled.json (bassync/scheduled.py). Seeing them needs
`view_schedules`, which works on its own, so an events team can have just
this. Marking an event low temp (bassync/lowtemp.py) needs `low_temp` too.
"""

from datetime import datetime

from flask import abort, g, redirect, render_template, request, url_for

from .. import lowtemp, scheduled
from ..config import ConfigError
from . import access, audit, campus_zone, ctx, notice, parse_time, requires
from .views import files, load_map, svc


def _snapshot() -> dict:
    return scheduled.load(files().scheduled_file)


def _marks() -> tuple:
    """(rows, error) from the low-temp events file."""
    try:
        return lowtemp.read(files().low_temp_file), ""
    except ConfigError as exc:
        return [], str(exc)


def _spaces(buildings: list, rooms: list) -> list:
    """Every space in the room map — rooms, and buildings bookable as a
    whole — as (space id, name, building id, building name, campus, floor)."""
    names = {str(b.get("id")): b for b in buildings
             if isinstance(b, dict) and b.get("id") not in (None, "")}
    out = []
    for r in rooms:
        if not isinstance(r, dict) or r.get("space_id") in (None, ""):
            continue
        b = names.get(str(r.get("building") or ""), {})
        out.append({"id": str(r["space_id"]), "name": str(r.get("space_name") or r["space_id"]),
                    "building": str(r.get("building") or ""),
                    "building_name": str(b.get("name") or r.get("building") or ""),
                    "campus": str(b.get("campus") or ""), "floor": r.get("floor")})
    for bid, b in names.items():
        if b.get("space_id") not in (None, ""):
            name = str(b.get("name") or bid)
            out.append({"id": str(b["space_id"]), "name": f"{name} (whole building)",
                        "building": bid, "building_name": name,
                        "campus": str(b.get("campus") or ""), "floor": None})
    return sorted(out, key=lambda s: (s["building_name"].lower(), s["name"].lower()))


def _low_temp_capable(entry: dict, snapshot: dict) -> bool:
    return any((snapshot["schedules"].get(key) or {}).get("kind") == "low_temp"
               for key in entry.get("schedules") or [])


def _by_day(items: list, start_key, zone) -> list:
    """[(date, [items])] in order, by each item's local start date."""
    days: dict = {}
    for item in items:
        when = parse_time(start_key(item))
        if when is None:
            continue
        days.setdefault(when.astimezone(zone).date(), []).append(item)
    return sorted(days.items())


def register(app) -> None:

    @app.route("/schedules")
    @requires("view_schedules")
    def schedules():
        from .announce import showing
        buildings, _floors, rooms, _v, map_error = load_map()
        snapshot = _snapshot()
        now = datetime.now(campus_zone(svc()))
        spaces = _spaces(buildings, rooms)
        marked = {lowtemp.event_id_of(r) for r in _marks()[0]}
        for space in spaces:
            entry = snapshot["spaces"].get(space["id"])
            space["synced"] = entry is not None
            upcoming = [b for b in (entry or {}).get("bookings", [])
                        if (parse_time(b.get("off")) or now) > now]
            space["upcoming"] = len(upcoming)
            space["next"] = upcoming[0] if upcoming else None
            space["low_temp"] = bool(entry) and _low_temp_capable(entry, snapshot)
            space["low_marked"] = sum(1 for b in upcoming if b.get("low_temp")
                                      or b.get("id") in marked)
        campuses = sorted({s["campus"] for s in spaces if s["campus"]})
        # For a role without the status page, this is the home page.
        banner = showing() if not access.can(g.caps, "view_basic") else []
        return render_template("schedules.html", spaces=spaces, snapshot=snapshot,
                               campuses=campuses, map_error=map_error,
                               announcements=banner)

    @app.route("/schedules/space/<space_id>")
    @requires("view_schedules")
    def space_schedule(space_id):
        buildings, _floors, rooms, _v, _e = load_map()
        space = next((s for s in _spaces(buildings, rooms) if s["id"] == space_id), None)
        snapshot = _snapshot()
        entry = snapshot["spaces"].get(space_id)
        if space is None and entry is None:
            abort(404, f"No space {space_id} in the room map.")
        if space is None:
            space = {"id": space_id, "name": entry.get("name") or space_id,
                     "building": entry.get("building") or "", "building_name": "",
                     "campus": "", "floor": entry.get("floor")}
        zone = campus_zone(svc())
        now = datetime.now(zone)
        today = datetime.combine(now.date(), datetime.min.time(), tzinfo=zone)
        rows, marks_error = _marks()
        marked = {lowtemp.event_id_of(r): r for r in rows}
        bookings = [b for b in (entry or {}).get("bookings", [])
                    if (parse_time(b.get("end")) or now) >= today]
        for b in bookings:
            b["marked"] = b.get("id") in marked
        schedules = []
        for key in (entry or {}).get("schedules", []):
            info = dict(snapshot["schedules"].get(key) or {"label": key, "kind": "",
                                                            "windows": [], "status": ""})
            info["key"] = key
            info["kind_label"] = scheduled.KIND_LABELS.get(info.get("kind", ""), "")
            windows = [w for w in info.get("windows") or []
                       if (parse_time(w[1]) or now) > today]
            info["days"] = _by_day(windows, lambda w: w[0], zone)
            schedules.append(info)
        low_capable = any(s.get("kind") == "low_temp" for s in schedules)
        return render_template(
            "space_schedule.html", space=space, entry=entry, snapshot=snapshot,
            days=_by_day(bookings, lambda b: b.get("start"), zone),
            schedules=schedules, low_capable=low_capable, marks_error=marks_error,
            next_due=svc().next_due, zone=zone)

    @app.route("/schedules/low-temp")
    @requires("view_schedules")
    def low_temp_list():
        rows, error = _marks()
        snapshot = _snapshot()
        now = datetime.now(campus_zone(svc()))
        seen: dict = {}
        for sid, entry in snapshot["spaces"].items():
            for b in entry.get("bookings", []):
                seen.setdefault(b.get("id"), []).append((sid, entry.get("name") or sid, b))
        marks = []
        for row in rows:
            eid = lowtemp.event_id_of(row)
            found = seen.get(eid, [])
            upcoming = [b for _sid, _name, b in found if (parse_time(b.get("off")) or now) > now]
            marks.append({"row": row, "id": eid,
                          "spaces": sorted({(sid, name) for sid, name, _b in found}),
                          "next": min((b.get("start") for b in upcoming), default=None),
                          "seen": bool(found)})
        return render_template("low_temp.html", marks=marks, error=error,
                               snapshot=snapshot)

    @app.route("/schedules/low-temp", methods=["POST"])
    @requires("view_schedules", "low_temp")
    def low_temp_mark():
        form = request.form
        event_id = (form.get("event_id") or "").strip()
        action = form.get("action")
        back = form.get("back") or ""
        target = (url_for("space_schedule", space_id=back) if back and back != "list"
                  else url_for("low_temp_list"))
        if not event_id or len(event_id) > 64 or action not in ("mark", "unmark"):
            abort(400, "Give the 25Live event ID, and whether to mark it or not.")
        path = files().low_temp_file
        user = g.get("user") or {}
        who = user.get("name") or user.get("username") or ""
        name = (form.get("name") or "").strip()[:200]
        with ctx()["write_lock"]:
            try:
                rows = lowtemp.read(path)
            except ConfigError as exc:
                abort(409, str(exc))
            if action == "mark":
                changed = lowtemp.mark(rows, event_id, name, who,
                                       datetime.now(campus_zone(svc())).strftime("%Y-%m-%d %H:%M"),
                                       form.get("note") or "")
            else:
                changed = lowtemp.unmark(rows, event_id)
            if changed:
                try:
                    lowtemp.save(path, rows)
                except OSError as exc:
                    from .views import _write_error
                    notice(_write_error(exc), "error")
                    return redirect(target)
        label = f"“{name}” ({event_id})" if name else f"event {event_id}"
        if not changed:
            notice(f"{label} was {'already' if action == 'mark' else 'not'} marked low temp.")
            return redirect(target)
        audit("%s event %s%s low temp", "marked" if action == "mark" else "unmarked",
              event_id, f" ({name})" if name else "")
        due = svc().next_due
        when = (f"the next sync ({due.astimezone(campus_zone(svc())):%a %H:%M})"
                if due else "the next sync")
        if action == "mark":
            notice(f"Marked {label} low temp: its rooms' low-temp schedules run for it "
                   f"from {when}.")
        else:
            notice(f"{label} is no longer low temp; its rooms go back to normal at {when}.")
        return redirect(target)
