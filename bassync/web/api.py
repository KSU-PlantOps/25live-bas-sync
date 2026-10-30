# 25Live -> BAS Schedule Sync — the JSON API
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
/api/v1/: what an MCP server (contrib/mcp-server/) or a script uses to read
the sync's state and make the few changes it's allowed to. docs/api.md
documents every endpoint.

It signs in with an API token (tokens.py), or with a signed-in browser
session (which then needs the CSRF token to change anything, like the rest of
the web UI). Every endpoint checks the caller's capabilities, and a sync
limited to some systems or buildings stays limited.

What it can change is deliberately small:

    jobs          Dry run, Validate, and — only if the role has Sync now —
                  a sync. Never Force, Discover or Test alert.
    bookings      add, change and delete extra bookings
    low temp      mark and unmark 25Live events

It can't touch the room map, settings, passwords, access or the service. The
data it returns includes 25Live event titles, which anyone who can book a
room writes: a program acting on them — an AI assistant above all — should
treat them as data, never as instructions.
"""

import functools
from datetime import datetime, timedelta
from typing import NoReturn

from flask import abort, g, jsonify, make_response, redirect, render_template, request, url_for

from .. import __version__, extras, history, lowtemp, mapedit
from ..config import ConfigError
from ..jobs import JobBusy
from . import (
    access,
    announce,
    audit,
    campus_zone,
    ctx,
    load_access,
    local_time,
    notice,
    parse_time,
    requires,
    tokens,
)
from .bookings import _places, _where_of
from .schedules import _low_temp_capable, _marks, _snapshot, _spaces
from .views import files, load_config_raw, load_map, svc, version_of

PREFIX = "/api/v1"
# The jobs the API may start, and the capability each needs. Sync needs its
# own check besides: the caller's sync limits.
API_JOBS = {"dry-run": "run_tools", "validate": "run_tools", "sync": "sync"}
BOOKING_KEYS = ("title", "space_id", "building", "floor", "date", "days", "from",
                "until", "start", "end", "exact", "low_temp", "note")
MAX_RUNS = 50


def fail(code: int, message: str) -> NoReturn:
    """Stop with a JSON error."""
    abort(make_response(jsonify(error=message), code))


def needs(*capabilities):
    """Refuse the request unless the caller has every one of these."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            missing = [c for c in capabilities if not access.can(g.get("caps"), c)]
            if missing:
                labels = " and ".join(f"“{access.CAPABILITY_LABELS.get(c, c)}”"
                                      for c in missing)
                fail(403, f"Your role ({g.get('role_label') or 'none'}) can't do that: "
                          f"it needs {labels}.")
            return fn(*args, **kwargs)
        inner.required = capabilities                     # type: ignore[attr-defined]
        return inner
    return wrap


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        fail(400, "Send a JSON object, with Content-Type: application/json.")
    return data


def _who() -> str:
    user = g.get("user") or {}
    return user.get("name") or user.get("username") or ""


def _today():
    return datetime.now(campus_zone(svc())).date()


# ── extra bookings ───────────────────────────────────────────────────────────

def _booking_rows() -> list:
    try:
        return extras.read(files().extras_file)
    except ConfigError as exc:
        fail(409, f"The extra bookings file can't be read: {exc}")


def _booking_item(index: int, row, labels: dict, today) -> dict:
    item: dict = {"index": index, "booking": row if isinstance(row, dict) else {},
                  "when": "", "where": "", "next": None, "ended": False, "problem": ""}
    try:
        b = extras.parse(row, index)
    except extras.BookingError as exc:
        item["problem"] = str(exc)
        return item
    item["when"] = b.when()
    item["where"] = labels.get(_where_of(row), "")
    if not item["where"]:
        item["problem"] = f"{b.where} isn't in the room map, so this drives nothing."
    starts = b.start_dates(today, today + timedelta(days=400))
    item["next"] = starts[0].isoformat() if starts else None
    item["ended"] = b.ended(today)
    return item


def _booking_from(data: dict, old=None) -> dict:
    """A booking row from a JSON body, checked; fails 422 with every problem."""
    unknown = sorted(set(data) - set(BOOKING_KEYS) - {"version"})
    row = {k: data[k] for k in BOOKING_KEYS if data.get(k) not in (None, "", [])}
    if isinstance(row.get("title"), str):
        row["title"] = row["title"].strip()[:120]
    if isinstance(row.get("note"), str):
        row["note"] = row["note"].strip()[:500]
    for key in ("exact", "low_temp"):
        if key in row:
            if row[key] is not True:
                row.pop(key)
    if not access.can(g.caps, "low_temp"):
        # Only a role that may mark low temp changes it; keep what it was.
        row.pop("low_temp", None)
        if isinstance(old, dict) and old.get("low_temp"):
            row["low_temp"] = True
    problems = [f"Unknown field {k!r}." for k in unknown]
    if not row.get("title") or not isinstance(row["title"], str):
        problems.append("Give it a title, so people can tell what it is.")
    if row.get("space_id") in (None, "") and not row.get("building"):
        problems.append("Say where it is: a space_id, a building, or a building and floor.")
    try:
        extras.parse(row)
    except extras.BookingError as exc:
        problems.append(str(exc)[0].upper() + str(exc)[1:])
    if problems:
        fail(422, " ".join(problems))
    return row


def _check_version(data: dict) -> None:
    sent = str(data.get("version") or request.args.get("version") or "")
    if sent != version_of(files().extras_file):
        fail(409, "The extra bookings changed since you read them (or no version was "
                  "sent). GET /api/v1/bookings again and use its version.")


def register(app) -> None:

    @app.route(PREFIX + "/")
    @needs()                                  # any caller may ask who they are
    def api_index():
        """Who the caller is, what they may do, and the API's version."""
        return {"api": "v1", "version": __version__,
                "caller": {"name": _who(), "via": (g.get("user") or {}).get("via", ""),
                           "role": g.get("role_label", ""),
                           "capabilities": sorted(g.get("caps") or ()),
                           "may_sync": (access.describe_scope(g.get("sync_scope"))
                                        if access.can(g.get("caps"), "sync") else "nothing")},
                "timezone": svc().schedule.timezone or "UTC"}

    # ── status, jobs and run history ─────────────────────────────────────────

    @app.route(PREFIX + "/status")
    @needs("view_basic")
    def api_v1_status():
        service = svc()
        job = service.jobs.current
        runs = history.list_runs(files().runs_dir, limit=1)
        return {"job": job.summary() if job else None,
                "next_due": service.next_due.isoformat() if service.next_due else None,
                "last_run": runs[0] if runs else None,
                "announcements": [{"message": a.message, "level": a.level,
                                   "starts": a.starts.isoformat(),
                                   "ends": a.ends.isoformat() if a.ends else None}
                                  for a in announce.showing()]}

    @app.route(PREFIX + "/runs")
    @needs("view_basic")
    def api_v1_runs():
        try:
            limit = max(1, min(int(request.args.get("limit") or 10), MAX_RUNS))
        except ValueError:
            limit = 10
        return {"runs": history.list_runs(files().runs_dir, limit=limit)}

    @app.route(PREFIX + "/runs/<run_id>")
    @needs("view_basic")
    def api_v1_run(run_id):
        record = history.load_run(files().runs_dir, run_id)
        if record is None:
            fail(404, f"No run {run_id!r}.")
        # The report as text; the HTML and CSV are the same thing again.
        return {k: v for k, v in record.items() if k not in ("html", "csv")}

    @app.route(PREFIX + "/jobs", methods=["POST"])
    @needs("view_basic")
    def api_v1_job_start():
        data = _body()
        kind = str(data.get("kind") or "")
        if kind not in API_JOBS:
            fail(400, f"kind must be one of {', '.join(API_JOBS)}. Force, Discover and "
                      "Test alert aren't available through the API.")
        needed = API_JOBS[kind]
        if not access.can(g.caps, needed):
            fail(403, f"Your role ({g.role_label or 'none'}) can't start that: it needs "
                      f"“{access.CAPABILITY_LABELS[needed]}”.")
        system = str(data.get("system") or "").strip()
        building = str(data.get("building") or "").strip()
        if system and building:
            fail(400, "Give a system or a building, not both.")
        if building and kind == "validate":
            fail(400, "Validate can be limited to a system, not a building.")
        if kind == "sync" and not access.may_sync(g.sync_scope, system or None,
                                                  building or None):
            fail(403, f"Your role ({g.role_label or 'none'}) may sync only "
                      f"{access.describe_scope(g.sync_scope)}, one at a time.")
        args: list = []
        if system:
            raw, _err = load_config_raw()
            if system not in mapedit.config_systems(raw):
                fail(400, f"No system named {system!r}.")
            args += ["--system", system]
        if building:
            if building not in {str(b.get("id")) for b in load_map()[0]}:
                fail(400, f"No building {building!r} in the room map.")
            args += ["--building", building]
        try:
            job = svc().jobs.start(kind, args, trigger=f"api ({_who()}, {g.addr})")
        except JobBusy as exc:
            fail(409, f"{exc} is already running; wait for it to finish.")
        except RuntimeError as exc:
            fail(503, str(exc))
        audit("started %s%s through the API", kind, "".join(f" {a}" for a in args))
        return {"job": job.summary(), "url": f"{PREFIX}/jobs/{job.id}"}, 202

    @app.route(PREFIX + "/jobs/<job_id>")
    @needs("view_basic")
    def api_v1_job(job_id):
        found = svc().jobs.get(job_id)
        if found is None:
            fail(404, f"No job {job_id!r}.")
        try:
            since = max(0, int(request.args.get("since") or 0))
        except ValueError:
            since = 0
        lines, next_index = found.output_since(since)
        return {"job": found.summary(), "lines": lines, "next": next_index,
                "running": found.running, "exit_code": found.exit_code}

    # ── what's scheduled ─────────────────────────────────────────────────────

    @app.route(PREFIX + "/spaces")
    @needs("view_schedules")
    def api_v1_spaces():
        buildings, _floors, rooms, _v, map_error = load_map()
        snapshot = _snapshot()
        now = datetime.now(campus_zone(svc()))
        out = []
        for space in _spaces(buildings, rooms):
            entry = snapshot["spaces"].get(space["id"])
            upcoming = [b for b in (entry or {}).get("bookings", [])
                        if (parse_time(b.get("off")) or now) > now]
            out.append(dict(space, synced=entry is not None, upcoming=len(upcoming),
                            next=upcoming[0] if upcoming else None,
                            low_temp_capable=bool(entry) and _low_temp_capable(entry, snapshot)))
        return {"spaces": out, "map_error": map_error}

    @app.route(PREFIX + "/spaces/<space_id>")
    @needs("view_schedules")
    def api_v1_space(space_id):
        buildings, _floors, rooms, _v, _e = load_map()
        space = next((s for s in _spaces(buildings, rooms) if s["id"] == space_id), None)
        snapshot = _snapshot()
        entry = snapshot["spaces"].get(space_id)
        if space is None and entry is None:
            fail(404, f"No space {space_id!r} in the room map.")
        zone = campus_zone(svc())
        now = datetime.now(zone)
        today = datetime.combine(now.date(), datetime.min.time(), tzinfo=zone)
        marked = {lowtemp.event_id_of(r) for r in _marks()[0]}
        bookings = [dict(b, marked_low_temp=b.get("id") in marked)
                    for b in (entry or {}).get("bookings", [])
                    if (parse_time(b.get("end")) or now) >= today]
        schedules = []
        for key in (entry or {}).get("schedules", []):
            info = dict(snapshot["schedules"].get(key) or {"label": key, "windows": []})
            info["key"] = key
            info["windows"] = [w for w in info.get("windows") or []
                               if (parse_time(w[1]) or now) > today]
            schedules.append(info)
        return {"space": space or {"id": space_id, "name": entry.get("name") or space_id},
                "synced_at": (entry or {}).get("at"), "bookings": bookings,
                "schedules": schedules}

    @app.route(PREFIX + "/map")
    @needs("view_all")
    def api_v1_map():
        buildings, floors, rooms, version, error = load_map()
        return {"buildings": buildings, "floors": floors, "rooms": rooms,
                "version": version, "error": error}

    # ── extra bookings ───────────────────────────────────────────────────────

    @app.route(PREFIX + "/bookings")
    @needs("view_all")
    def api_v1_bookings():
        buildings, floors, rooms, _v, _e = load_map()
        _groups, labels = _places(buildings, floors, rooms)
        today = _today()
        rows = _booking_rows()
        return {"bookings": [_booking_item(i, r, labels, today) for i, r in enumerate(rows)],
                "version": version_of(files().extras_file)}

    def _save_booking(index):
        data = _body()
        path = files().extras_file
        with ctx()["write_lock"]:
            if index is not None:
                _check_version(data)
            rows = _booking_rows()
            if index is not None and not 0 <= index < len(rows):
                fail(404, f"No extra booking {index}.")
            old = rows[index] if index is not None else None
            row = _booking_from(data, old)
            if index is None:
                row["added_by"] = _who()
                rows.append(row)
                index = len(rows) - 1
            else:
                if isinstance(old, dict) and old.get("added_by"):
                    row["added_by"] = old["added_by"]
                rows[index] = row
            try:
                extras.save(path, rows)
            except OSError as exc:
                fail(500, f"Couldn't write the extra bookings: {exc}")
        audit("%s the extra booking %r (%s) through the API",
              "added" if old is None else "changed", row["title"], extras.parse(row).when())
        buildings, floors, rooms, _v, _e = load_map()
        labels = _places(buildings, floors, rooms)[1]
        return {"booking": _booking_item(index, row, labels, _today()),
                "version": version_of(path),
                "note": "The next sync writes it."}, 201 if old is None else 200

    @app.route(PREFIX + "/bookings", methods=["POST"])
    @needs("view_all", "edit_bookings")
    def api_v1_booking_add():
        return _save_booking(None)

    @app.route(PREFIX + "/bookings/<int:index>", methods=["PUT"])
    @needs("view_all", "edit_bookings")
    def api_v1_booking_change(index):
        return _save_booking(index)

    @app.route(PREFIX + "/bookings/<int:index>", methods=["DELETE"])
    @needs("view_all", "edit_bookings")
    def api_v1_booking_delete(index):
        path = files().extras_file
        with ctx()["write_lock"]:
            _check_version(request.get_json(silent=True) or {})
            rows = _booking_rows()
            if not 0 <= index < len(rows):
                fail(404, f"No extra booking {index}.")
            removed = rows.pop(index)
            try:
                extras.save(path, rows)
            except OSError as exc:
                fail(500, f"Couldn't write the extra bookings: {exc}")
        title = removed.get("title") if isinstance(removed, dict) else "?"
        audit("deleted the extra booking %r through the API", title)
        return {"deleted": removed, "version": version_of(path),
                "note": "The next sync takes it off the schedule. Later bookings' "
                        "indexes moved down by one."}

    # ── low temp ─────────────────────────────────────────────────────────────

    @app.route(PREFIX + "/low-temp")
    @needs("view_schedules")
    def api_v1_low_temp():
        rows, error = _marks()
        if error:
            fail(409, f"The low-temp events file can't be read: {error}")
        return {"events": [dict(r, event_id=lowtemp.event_id_of(r)) for r in rows]}

    def _set_low_temp(event_id, mark: bool, data: dict):
        event_id = str(event_id or "").strip()
        if not event_id or len(event_id) > 64:
            fail(400, "Give the 25Live event ID.")
        path = files().low_temp_file
        name = str(data.get("name") or "").strip()[:200]
        with ctx()["write_lock"]:
            try:
                rows = lowtemp.read(path)
            except ConfigError as exc:
                fail(409, str(exc))
            if mark:
                changed = lowtemp.mark(rows, event_id, name, _who(),
                                       local_time(svc(), datetime.now().astimezone()),
                                       str(data.get("note") or "")[:500])
            else:
                changed = lowtemp.unmark(rows, event_id)
            if changed:
                try:
                    lowtemp.save(path, rows)
                except OSError as exc:
                    fail(500, f"Couldn't write the low-temp events: {exc}")
        if changed:
            audit("%s event %s%s low temp through the API",
                  "marked" if mark else "unmarked", event_id, f" ({name})" if name else "")
        return {"event_id": event_id, "low_temp": mark, "changed": changed,
                "note": "The next sync writes it." if changed else
                        ("It was already marked." if mark else "It wasn't marked.")}

    @app.route(PREFIX + "/low-temp", methods=["POST"])
    @needs("view_schedules", "low_temp")
    def api_v1_low_temp_mark():
        data = _body()
        return _set_low_temp(data.get("event_id"), True, data)

    @app.route(PREFIX + "/low-temp/<event_id>", methods=["DELETE"])
    @needs("view_schedules", "low_temp")
    def api_v1_low_temp_unmark(event_id):
        return _set_low_temp(event_id, False, {})

    # ── the API tokens page (Settings) ───────────────────────────────────────

    def _tokens_page(new=None, errors=None, values=None):
        conf = load_access()
        rows = [tokens.public(r) for r in tokens.read(tokens.path_for(files().state_dir))]
        names = access.role_names(conf)
        for row in rows:
            row["role_name"] = names.get(row.get("role"), "")
            row["expired"] = tokens.expired(row)
        caps = {r["id"]: [access.CAPABILITY_LABELS[c] for c in access.ALL_CAPABILITIES
                          if c in access.role_capabilities(r) - tokens.TOKEN_NEVER]
                for r in conf["roles"]}
        return render_template(
            "tokens.html", tokens=rows, roles=conf["roles"], role_caps=caps,
            never=[access.CAPABILITY_LABELS[c] for c in access.ALL_CAPABILITIES
                   if c in tokens.TOKEN_NEVER],
            expiry_days=tokens.EXPIRY_DAYS, new=new, errors=errors or [],
            values=values or {"role": "ai" if "ai" in names else "", "days": 90},
            api_url=request.url_root.rstrip("/") + PREFIX + "/"), (422 if errors else 200)

    @app.route("/settings/access/tokens")
    @requires("manage_access")
    def api_tokens():
        return _tokens_page()

    @app.route("/settings/access/tokens", methods=["POST"])
    @requires("manage_access")
    def api_token_create():
        form = request.form
        values = {"name": (form.get("name") or "").strip(), "role": form.get("role") or "",
                  "days": form.get("days") or ""}
        errors = []
        if values["role"] not in access.role_names(load_access()):
            errors.append("Pick the role the token signs in as.")
        try:
            days = int(values["days"])
        except ValueError:
            days = -1
        if not errors:
            try:
                row, secret = tokens.create(tokens.path_for(files().state_dir),
                                            values["name"], values["role"], days, _who())
            except ValueError as exc:
                errors.append(str(exc))
            except OSError as exc:
                errors.append(f"Couldn't store the token: {exc}")
        if errors:
            return _tokens_page(errors=errors, values=values)
        audit("made the API token %r (role %s, %s)", row["name"], row["role"],
              f"expires {row['expires'][:10]}" if row["expires"] else "never expires")
        # Shown here, once, and never stored in the session or a redirect.
        return _tokens_page(new={"name": row["name"], "secret": secret})

    @app.route("/settings/access/tokens/<token_id>/revoke", methods=["POST"])
    @requires("manage_access")
    def api_token_revoke(token_id):
        try:
            removed = tokens.revoke(tokens.path_for(files().state_dir), token_id)
        except OSError as exc:
            notice(f"Couldn't revoke it: {exc}", "error")
            return redirect(url_for("api_tokens"))
        if removed is None:
            notice("That token was already gone.", "error")
        else:
            audit("revoked the API token %r", removed.get("name"))
            notice(f"Revoked “{removed.get('name')}”. Anything using it is refused from now on.")
        return redirect(url_for("api_tokens"))
