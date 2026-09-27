# 25Live -> BAS Schedule Sync — web UI pages
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The pages. Each request reads the files fresh, and every write happens under
one lock after checking the file hasn't changed since the form was opened —
two people editing at once get told, rather than one silently undoing the
other.
"""

import copy
import hashlib
import io
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import available_timezones

import yaml
from flask import Response, abort, redirect, render_template, request, send_file, url_for

from .. import history, mapedit
from ..config import (
    STATE_PARAM_STYLES,
    ConfigError,
    _driver_name,
    load_config,
    parse_hhmm,
    system_password_env,
)
from ..drivers import driver_names
from ..jobs import KINDS, JobBusy
from ..scheduler import next_run_any
from . import audit, campus_zone, ctx, notice

# ─────────────────────────────────────────────────────────────────────────────
# Reading the files
# ─────────────────────────────────────────────────────────────────────────────


def svc():
    return ctx()["service"]


def files():
    return svc().paths


def version_of(path) -> str:
    """A short fingerprint of a file's content, to spot concurrent edits."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def load_map() -> tuple:
    """(buildings, floors, rooms, version, error). A file that won't parse
    comes back empty with the error, and the editors refuse to save over it."""
    path = files().space_map
    try:
        buildings, floors, rooms = mapedit.load_mapping(path)
        for rows in (buildings, floors, rooms):
            if not all(isinstance(r, dict) for r in rows):
                raise ValueError("a row is not a mapping of fields")
        error = ""
    except Exception as exc:                          # noqa: BLE001 — any parse failure
        buildings, floors, rooms, error = [], [], [], f"{path.name}: {exc}"
    return buildings, floors, rooms, version_of(path), error


def load_config_raw() -> tuple:
    raw, error = mapedit.read_config_checked(files().config)
    return raw, error or ""


def load_defaults() -> dict:
    return mapedit.load_defaults(files().defaults)


_HEALTH_CACHE: dict = {}


def health() -> dict:
    """What the sync would make of the current files: config problems and
    warnings, room-map problems, and counts. Cached until a file changes."""
    p = files()
    key = (version_of(p.config), version_of(p.defaults), version_of(p.space_map))
    if key in _HEALTH_CACHE:
        return _HEALTH_CACHE[key]
    buildings, floors, rooms, _v, map_read_error = load_map()
    raw, config_read_error = load_config_raw()
    warnings: list = []
    config_error = config_read_error
    if not config_error:
        try:
            load_config(str(p.config), str(p.defaults), warnings)
        except ConfigError as exc:
            config_error = str(exc)
    map_errors, map_warnings = [], []
    if map_read_error:
        map_errors = [map_read_error]
    elif not config_error:
        _err, map_errors, map_warnings = mapedit.validate_before_save(
            buildings, floors, rooms, raw, load_defaults())
    result = {
        "config_exists": p.config.exists(),
        "config_error": config_error,
        "config_warnings": warnings,
        "map_errors": map_errors,
        "map_warnings": map_warnings,
        "counts": {"rooms": len(rooms), "buildings": len(buildings),
                   "floors": len(floors),
                   "systems": len(mapedit.config_systems(raw))},
        "writable": os.access(p.config.parent if p.config.parent.exists() else ".", os.W_OK),
    }
    _HEALTH_CACHE.clear()
    _HEALTH_CACHE[key] = result
    return result


def secret_status(raw: dict) -> list:
    """(variable, what it's for, is it set) for each password the config needs.
    Only whether it is set — never the value."""
    out = [("BAS_25LIVE_PASSWORD", "25Live service account",
            bool(os.environ.get("BAS_25LIVE_PASSWORD")))]
    for name, sys_cfg in sorted((raw.get("systems") or {}).items()):
        if not isinstance(sys_cfg, dict):
            continue
        driver = _driver_name(sys_cfg.get("driver"))
        auth = sys_cfg.get("auth") if isinstance(sys_cfg.get("auth"), dict) else {}
        if driver in ("bacnet", "preview") or (auth or {}).get("mode") == "none":
            continue
        var = system_password_env(name)
        legacy = os.environ.get("BAS_NIAGARA_PASSWORD") if driver == "niagara" else None
        out.append((var, f"system '{name}' ({driver})",
                    bool(os.environ.get(var) or legacy)))
    email = ((raw.get("alerts") or {}).get("email") or {})
    if isinstance(email, dict) and email.get("enabled") and email.get("username"):
        out.append(("BAS_SMTP_PASSWORD", "alert email (SMTP)",
                    bool(os.environ.get("BAS_SMTP_PASSWORD"))))
    return out


def new_problems(before: list, after: list) -> list:
    seen = set(before)
    return [p for p in after if p not in seen]


def _write_error(exc: OSError) -> str:
    folder = files().config.parent
    return (f"Couldn't write the file: {exc}. The folder {folder} must be "
            "writable by the service — in Docker, see \"Config folder\" in the "
            "README.")


# ─────────────────────────────────────────────────────────────────────────────
# The room map's three kinds of row
# ─────────────────────────────────────────────────────────────────────────────

# (key, label, kind, hint). Kinds: int, text, building, system, level.
ROOM_FIELDS = [
    ("space_id", "25Live space ID", "int", "required; the number 25Live gives the room"),
    ("space_name", "Name", "text", ""),
    ("building", "Building", "building", "the building it rolls up into"),
    ("floor", "Floor #", "level", "rolls up into that floor's corridor schedule"),
    ("system", "BAS system", "system", "blank = its building's"),
    ("target", "Target", "text", "its own schedule; blank = roll-up only"),
    ("pre_condition_minutes", "Pre-condition minutes", "int", "blank = building / default"),
    ("post_buffer_minutes", "Post-buffer minutes", "int", "blank = building / default"),
    ("merge_gap_minutes", "Merge-gap minutes", "int", "blank = default"),
    ("note", "Note", "text", ""),
]
BUILDING_FIELDS = [
    ("id", "Building ID", "text", "required; rooms and floors refer to it"),
    ("name", "Name", "text", ""),
    ("system", "BAS system", "system", "blank = the default system"),
    ("target", "Target", "text", "required; the building's common-area schedule"),
    ("pre_condition_minutes", "Pre-condition minutes (its rooms)", "int", "blank = default"),
    ("post_buffer_minutes", "Post-buffer minutes (its rooms)", "int", "blank = default"),
    ("space_id", "Bookable 25Live space ID", "int", "when the whole building is bookable"),
    ("merge_gap_minutes", "Merge-gap minutes (bookable space)", "int", "blank = default"),
    ("note", "Note", "text", ""),
]
FLOOR_FIELDS = [
    ("building", "Building", "building", "required"),
    ("level", "Floor #", "int", "required"),
    ("system", "BAS system", "system", "blank = its building's"),
    ("target", "Corridor target", "text", "required; the floor's corridor schedule"),
    ("note", "Note", "text", ""),
]

KINDS_OF_ROW: dict[str, dict] = {
    "rooms": {"title": "Rooms", "one": "room", "slot": 2, "fields": ROOM_FIELDS,
              "columns": [("space_id", "Space ID"), ("space_name", "Name"),
                          ("building", "Building"), ("floor", "Floor"),
                          ("system", "System"), ("target", "Target"),
                          ("pre_condition_minutes", "Pre"),
                          ("post_buffer_minutes", "Post"),
                          ("merge_gap_minutes", "Gap")],
              "key": "space_id"},
    "buildings": {"title": "Buildings", "one": "building", "slot": 0,
                  "fields": BUILDING_FIELDS,
                  "columns": [("id", "ID"), ("name", "Name"), ("system", "System"),
                              ("target", "Target"), ("pre_condition_minutes", "Pre"),
                              ("post_buffer_minutes", "Post"),
                              ("space_id", "Bookable ID")],
                  "key": "id"},
    "floors": {"title": "Floors", "one": "floor", "slot": 1, "fields": FLOOR_FIELDS,
               "columns": [("building", "Building"), ("level", "Floor"),
                           ("system", "System"), ("target", "Corridor target")],
               "key": "level"},
}


def row_label(kind: str, row: dict) -> str:
    if kind == "rooms":
        name = row.get("space_name")
        return f"room {row.get('space_id')}" + (f" ({name})" if name else "")
    if kind == "buildings":
        return f"building '{row.get('id')}'"
    return f"floor {row.get('level')} of '{row.get('building')}'"


def parse_row(form, fields) -> tuple:
    values: dict = {}
    errors: list = []
    for key, label, kind, _hint in fields:
        raw = (form.get(key) or "").strip()
        if raw == "":
            continue
        if kind in ("int", "level"):
            try:
                values[key] = int(raw)
            except ValueError:
                errors.append(f"{label} must be a whole number.")
            continue
        values[key] = raw
    return values, errors


def row_problem(kind: str, values: dict, lists: tuple, raw: dict,
                index: Optional[int]) -> Optional[str]:
    buildings, floors, rooms = lists
    if kind == "rooms":
        return mapedit.room_problem(values, rooms, buildings, raw, index)
    if kind == "buildings":
        return mapedit.building_problem(values, buildings, raw, index)
    return mapedit.floor_problem(values, floors, buildings, raw, index)


def form_choices(lists: tuple, raw: dict) -> dict:
    buildings, floors, _rooms = lists
    levels = sorted({str(f.get("level")) for f in floors if f.get("level") not in (None, "")},
                    key=lambda s: (0, int(s)) if s.lstrip("-").isdigit() else (1, s))
    return {"buildings": [str(b.get("id")) for b in buildings],
            "systems": sorted(mapedit.config_systems(raw)),
            "levels": levels}


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

def register(app) -> None:            # noqa: C901 — one place for every route

    # ── dashboard ────────────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        service = svc()
        raw, _err = load_config_raw()
        runs = history.list_runs(files().runs_dir, limit=6)
        return render_template(
            "dashboard.html", health=health(), runs=runs,
            last_run=runs[0] if runs else None,
            jobs=service.jobs.recent(6), service=service,
            secrets=secret_status(raw),
            systems=sorted(mapedit.config_systems(raw)),
            paths=files())

    @app.route("/api/status")
    def api_status():
        service = svc()
        job = service.jobs.current
        runs = history.list_runs(files().runs_dir, limit=1)
        return {
            "job": job.summary() if job else None,
            "next_due": service.next_due.isoformat() if service.next_due else None,
            "last_run": ({k: runs[0].get(k) for k in ("id", "ok", "subject", "started")}
                         if runs else None),
        }

    # ── jobs: sync now and the tools ─────────────────────────────────────────

    @app.route("/jobs", methods=["GET", "POST"])
    def jobs():
        service = svc()
        if request.method == "GET":
            return render_template("jobs.html", jobs=service.jobs.recent(60),
                                   kinds=KINDS)
        kind = request.form.get("kind", "")
        if kind not in KINDS:
            abort(400, "Unknown job.")
        args: list = []
        system = (request.form.get("system") or "").strip()
        if system and kind in ("sync", "dry-run", "validate"):
            raw, _err = load_config_raw()
            if system not in mapedit.config_systems(raw):
                abort(400, f"No system named {system!r}.")
            args += ["--system", system]
        if kind == "sync" and request.form.get("force"):
            args.append("--force")
        if kind == "discover":
            try:
                days = max(1, min(int(request.form.get("days") or 30), 366))
            except ValueError:
                days = 30
            args += ["--discover-days", str(days)]
        try:
            job = service.jobs.start(kind, args, trigger=f"web ({request.remote_addr})")
        except JobBusy as exc:
            notice(f"{exc} is already running — wait for it to finish, or stop it.", "error")
            current = service.jobs.current
            return redirect(url_for("job", job_id=current.id) if current else url_for("jobs"))
        except RuntimeError as exc:
            notice(str(exc), "error")
            return redirect(url_for("dashboard"))
        audit("started %s %s", KINDS[kind][0], " ".join(args))
        return redirect(url_for("job", job_id=job.id))

    @app.route("/jobs/<job_id>")
    def job(job_id):
        found = svc().jobs.get(job_id)
        if found is None:
            abort(404)
        discovered = None
        if found.kind == "discover" and found.exit_code == 0:
            discovered = _discovered_spaces(list(found.lines))
        return render_template("job.html", job=found, discovered=discovered)

    @app.route("/api/jobs/<job_id>")
    def api_job(job_id):
        found = svc().jobs.get(job_id)
        if found is None:
            abort(404)
        try:
            since = int(request.args.get("since") or 0)
        except ValueError:
            since = 0
        lines, next_index = found.output_since(since)
        return {"lines": lines, "next": next_index, "running": found.running,
                "exit_code": found.exit_code, "stopped_by": found.stopped_by}

    @app.route("/jobs/<job_id>/stop", methods=["POST"])
    def job_stop(job_id):
        if svc().jobs.stop(job_id, who=f"web ({request.remote_addr})"):
            notice("Stop requested; the job is finishing what it was doing.")
        else:
            notice("That job isn't running.", "error")
        return redirect(url_for("job", job_id=job_id))

    # ── run history ──────────────────────────────────────────────────────────

    @app.route("/runs")
    def runs():
        return render_template("runs.html", runs=history.list_runs(files().runs_dir, 200))

    @app.route("/runs/<run_id>")
    def run(run_id):
        record = history.load_run(files().runs_dir, run_id)
        if record is None:
            abort(404)
        return render_template("run.html", run=record)

    @app.route("/runs/<run_id>/report")
    def run_report(run_id):
        record = history.load_run(files().runs_dir, run_id)
        if record is None:
            abort(404)
        response = Response(record.get("html") or "", mimetype="text/html")
        # The email's HTML, shown in a frame on the run page: inline styles
        # only, no script, nothing fetched, and only this site may frame it.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "frame-ancestors 'self'; sandbox")
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.route("/runs/<run_id>/schedules.csv")
    def run_csv(run_id):
        record = history.load_run(files().runs_dir, run_id)
        if record is None:
            abort(404)
        return Response(record.get("csv") or "", mimetype="text/csv", headers={
            "Content-Disposition": f"attachment; filename=schedules-{run_id}.csv"})

    # ── room map ─────────────────────────────────────────────────────────────

    @app.route("/map/<kind>")
    def map_list(kind):
        spec = KINDS_OF_ROW.get(kind) or abort(404)
        buildings, floors, rooms, version, error = load_map()
        rows = (buildings, floors, rooms)[spec["slot"]]
        return render_template("map_list.html", kind=kind, spec=spec, rows=rows,
                               version=version, error=error, health=health(),
                               kinds=KINDS_OF_ROW)

    @app.route("/map/<kind>/new")
    @app.route("/map/<kind>/<int:index>/edit")
    def map_form(kind, index=None):
        spec = KINDS_OF_ROW.get(kind) or abort(404)
        buildings, floors, rooms, version, error = load_map()
        if error:
            notice(f"The room map can't be edited until it reads cleanly: {error}", "error")
            return redirect(url_for("map_list", kind=kind))
        rows = (buildings, floors, rooms)[spec["slot"]]
        values: dict = {}
        if index is not None:
            if not 0 <= index < len(rows):
                abort(404)
            values = dict(rows[index])
        elif request.args.get("copy", "").isdigit():
            source = int(request.args["copy"])
            if 0 <= source < len(rows):
                values = dict(rows[source])
                values.pop(spec["key"], None)     # a copy needs its own id / floor #
        else:
            values = {key: request.args[key] for key, *_ in spec["fields"]
                      if request.args.get(key)}
        raw, _err = load_config_raw()
        return render_template(
            "map_form.html", kind=kind, spec=spec, index=index, values=values,
            version=version, errors=[], confirm=[],
            choices=form_choices((buildings, floors, rooms), raw))

    @app.route("/map/<kind>/save", methods=["POST"])
    def map_save(kind):
        spec = KINDS_OF_ROW.get(kind) or abort(404)
        index = request.form.get("index", "")
        index_n = int(index) if index.isdigit() else None
        with ctx()["write_lock"]:
            buildings, floors, rooms, version, error = load_map()
            raw, _cfg_err = load_config_raw()
            lists = (buildings, floors, rooms)
            rows = lists[spec["slot"]]
            values, errors = parse_row(request.form, spec["fields"])

            def again(errs, confirm=(), status=422):
                return render_template(
                    "map_form.html", kind=kind, spec=spec, index=index_n,
                    values=dict(request.form), version=request.form.get("version"),
                    errors=errs, confirm=list(confirm), choices=form_choices(lists, raw)), status

            if error:
                return again([f"The room map doesn't read cleanly: {error}"])
            if request.form.get("version") != version:
                return again(["The room map changed since this form was opened — "
                              "someone else saved, or the file was edited. Open "
                              "the list again and redo this change."], status=409)
            if index_n is not None and not 0 <= index_n < len(rows):
                return again(["That row no longer exists."])
            if errors:
                return again(errors)
            problem = row_problem(kind, values, lists, raw, index_n)
            if problem:
                return again([problem])

            original = rows[index_n] if index_n is not None else {}
            row = mapedit.merge_form_result(original, values,
                                            [key for key, *_ in spec["fields"]])
            new_lists = [list(buildings), [dict(f) for f in floors], [dict(r) for r in rooms]]
            new_rows = new_lists[spec["slot"]]
            if index_n is None:
                new_rows.append(row)
            else:
                new_rows[index_n] = row
            if kind == "buildings" and index_n is not None:
                mapedit.rename_building(original.get("id"), row.get("id"),
                                        new_lists[1], new_lists[2])

            defaults = load_defaults()
            _e, before, _w = mapedit.validate_before_save(buildings, floors, rooms, raw, defaults)
            cfg_error, after, warnings = mapedit.validate_before_save(
                new_lists[0], new_lists[1], new_lists[2], raw, defaults)
            added = new_problems(before, after)
            if added and not request.form.get("confirm"):
                return again([], added)
            try:
                mapedit.save_mapping(files().space_map, *new_lists)
            except OSError as exc:
                return again([_write_error(exc)])
        audit("saved %s", row_label(kind, row))
        message = f"Saved {row_label(kind, row)}."
        if added:
            message += f" {len(added)} problem(s) saved with it — see the list."
        notice(message, "warn" if added else "ok")
        if cfg_error:
            notice("config.yaml has a problem, so the whole map couldn't be "
                   "checked: " + cfg_error, "warn")
        return redirect(url_for("map_list", kind=kind))

    @app.route("/map/<kind>/<int:index>/delete", methods=["GET", "POST"])
    def map_delete(kind, index):
        spec = KINDS_OF_ROW.get(kind) or abort(404)
        with ctx()["write_lock"]:
            buildings, floors, rooms, version, error = load_map()
            rows = (buildings, floors, rooms)[spec["slot"]]
            if error or not 0 <= index < len(rows):
                abort(404)
            row = rows[index]
            blocked, consequences = None, ""
            if kind == "buildings":
                blocked, consequences = mapedit.building_delete_check(
                    row.get("id"), floors, rooms)
            if request.method == "GET" or blocked:
                return render_template("map_delete.html", kind=kind, spec=spec,
                                       index=index, row=row, label=row_label(kind, row),
                                       version=version, blocked=blocked,
                                       consequences=consequences)
            if request.form.get("version") != version:
                notice("The room map changed since you opened that page; nothing "
                       "was deleted. Check the row and try again.", "error")
                return redirect(url_for("map_list", kind=kind))
            if kind == "buildings":
                buildings, floors, rooms = mapedit.delete_building(
                    row.get("id"), buildings, floors, rooms)
            else:
                rows.pop(index)
            try:
                mapedit.save_mapping(files().space_map, buildings, floors, rooms)
            except OSError as exc:
                notice(_write_error(exc), "error")
                return redirect(url_for("map_list", kind=kind))
        audit("deleted %s", row_label(kind, row))
        notice(f"Deleted {row_label(kind, row)}.")
        return redirect(url_for("map_list", kind=kind))

    # ── settings: connection ─────────────────────────────────────────────────

    @app.route("/settings/connection")
    def connection():
        raw, error = load_config_raw()
        systems = mapedit.config_systems(raw)
        selected = request.args.get("system") or (sorted(systems)[0] if systems else "")
        if selected not in systems:
            selected = ""
        driver = request.args.get("driver") or systems.get(selected, "")
        if driver and driver not in driver_names():
            driver = systems.get(selected, "")
        return _connection_page(raw, error, systems, selected, driver,
                                mapedit.form_from_raw(raw, selected, driver), [], [])

    def _connection_page(raw, error, systems, selected, driver, form, errors,
                         confirm, status=200):
        states = ((raw.get("collegenet") or {}).get("include_states")
                  if isinstance(raw.get("collegenet"), dict) else None)
        states = [int(s) for s in states if str(s).lstrip("-").isdigit()] \
            if isinstance(states, list) else [2]
        known = {s for s, _ in mapedit.INCLUDE_STATE_LABELS}
        return render_template(
            "connection.html", raw=raw, error=error, systems=systems,
            selected=selected, driver=driver, drivers=driver_names(),
            sections=mapedit.config_sections(selected, driver), form=form,
            state_labels=mapedit.INCLUDE_STATE_LABELS, states=states,
            extra_states=[s for s in states if s not in known],
            default_system=str(raw.get("default_system") or ""),
            choices=_config_choices, errors=errors, confirm=confirm,
            version=version_of(files().config), secrets=secret_status(raw)), status

    @app.route("/settings/connection", methods=["POST"])
    def connection_save():
        system = request.form.get("system", "")
        driver = request.form.get("driver", "")
        with ctx()["write_lock"]:
            raw, error = load_config_raw()
            systems = mapedit.config_systems(raw)
            if system and system not in systems:
                abort(400, f"No system named {system!r}.")
            form = {}
            for path_t, _label, kind, _hint in [f for _s, fields in
                                                mapedit.config_sections(system, driver)
                                                for f in fields]:
                fid = ".".join(path_t)
                form[fid] = (fid in request.form) if kind == "bool" else request.form.get(fid, "")
            states = sorted({int(s) for s in request.form.getlist("state") if s.isdigit()})
            form["collegenet.include_states"] = ", ".join(str(s) for s in states)

            def again(errors, confirm=(), status=422):
                return _connection_page(raw, error, systems, system, driver, form,
                                        errors, list(confirm), status)

            if error:
                return again([f"config.yaml can't be read, so saving this form "
                              f"would erase the settings it doesn't show: {error}. "
                              "Fix it under Files first."])
            if request.form.get("version") != version_of(files().config):
                return again(["config.yaml changed since this page was opened. "
                              "Reload it and redo your change."], status=409)
            if not states:
                return again(["Pick at least one 25Live state to include."])
            try:
                new = mapedit.apply_config_form(raw, form, system, driver)
            except ValueError as exc:
                return again([f"Invalid setting — {exc}"])
            default = (request.form.get("default_system") or "").strip()
            if default:
                new["default_system"] = default
            else:
                new.pop("default_system", None)
            new_systems = new.get("systems") or {}
            if len(new_systems) == 1 and not new.get("default_system"):
                new["default_system"] = next(iter(new_systems))
            problems = _config_change_problems(raw, new)
            if problems and not request.form.get("confirm"):
                return again([], problems)
            try:
                mapedit.save_config(files().config, new)
            except OSError as exc:
                return again([_write_error(exc)])
        audit("saved the connection settings%s", f" for system '{system}'" if system else "")
        notice("Saved config.yaml." + (" Saved with the problems shown." if problems else ""),
               "warn" if problems else "ok")
        return redirect(url_for("connection", system=system))

    @app.route("/settings/systems", methods=["POST"])
    def system_add():
        name = (request.form.get("name") or "").strip()
        driver = request.form.get("driver") or "bacnet"
        with ctx()["write_lock"]:
            raw, error = load_config_raw()
            systems = mapedit.config_systems(raw)
            problem = None
            if error:
                problem = f"config.yaml can't be read: {error}"
            elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
                problem = ("A system name is letters, digits, - and _, starting "
                           "with a letter or digit — it becomes part of the "
                           f"{system_password_env('NAME')} variable.")
            elif name in systems:
                problem = f"There is already a system called '{name}'."
            elif driver not in driver_names():
                problem = f"Unknown driver {driver!r}."
            if problem:
                notice(problem, "error")
                return redirect(url_for("connection"))
            new = copy.deepcopy(raw)
            if not isinstance(new.get("systems"), dict):
                new["systems"] = {}
            new["systems"][name] = {"driver": driver}
            try:
                mapedit.save_config(files().config, new)
            except OSError as exc:
                notice(_write_error(exc), "error")
                return redirect(url_for("connection"))
        audit("added system '%s' (%s)", name, driver)
        notice(f"Added system '{name}'. Fill in its settings below and save.")
        return redirect(url_for("connection", system=name))

    @app.route("/settings/systems/<name>/delete", methods=["POST"])
    def system_delete(name):
        with ctx()["write_lock"]:
            raw, error = load_config_raw()
            systems = mapedit.config_systems(raw)
            if error or name not in (raw.get("systems") or {}):
                abort(404)
            buildings, floors, rooms, _v, _e = load_map()
            users = [row_label(kind, r) for kind, rows in
                     (("buildings", buildings), ("floors", floors), ("rooms", rooms))
                     for r in rows if str(r.get("system") or "") == name]
            is_default = (str(raw.get("default_system") or "") == name
                          or (len(systems) == 1 and (buildings or rooms)))
            if users or is_default:
                why = (f"{len(users)} row(s) name it: {', '.join(users[:8])}"
                       + ("…" if len(users) > 8 else "")) if users else \
                    "it is the default system that rows without their own use"
                notice(f"'{name}' is in use — {why}. Move them to another system first.",
                       "error")
                return redirect(url_for("connection", system=name))
            new = copy.deepcopy(raw)
            del new["systems"][name]
            try:
                mapedit.save_config(files().config, new)
            except OSError as exc:
                notice(_write_error(exc), "error")
                return redirect(url_for("connection", system=name))
        audit("removed system '%s'", name)
        notice(f"Removed system '{name}'.")
        return redirect(url_for("connection"))

    # ── settings: defaults and schedule ──────────────────────────────────────

    @app.route("/settings/defaults", methods=["GET", "POST"])
    def defaults():
        values = load_defaults()
        errors: list = []
        if request.method == "POST":
            new = {}
            for key, label, fallback in mapedit.DEFAULTS_FIELDS:
                text = (request.form.get(key) or "").strip()
                try:
                    number = int(text) if text else fallback
                except ValueError:
                    errors.append(f"{label} must be a whole number.")
                    continue
                low = 1 if key == "lookahead_days" else 0
                high = 366 if key == "lookahead_days" else 1440
                if not low <= number <= high:
                    errors.append(f"{label} must be between {low} and {high}.")
                new[key] = number
            if not errors:
                try:
                    with ctx()["write_lock"]:
                        written = mapedit.save_defaults(files().defaults, new)
                except OSError as exc:
                    errors.append(_write_error(exc))
                else:
                    audit("saved the scheduling defaults")
                    notice("Saved defaults.yaml." if written else "Nothing had changed.")
                    return redirect(url_for("defaults"))
            values = {**values, **{k: request.form.get(k, "") for k, *_ in mapedit.DEFAULTS_FIELDS}}
        return render_template("defaults.html", values=values,
                               fields=mapedit.DEFAULTS_FIELDS, errors=errors)

    @app.route("/settings/schedule", methods=["GET", "POST"])
    def schedule():
        service = svc()
        raw, error = load_config_raw()
        current = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
        form = {"enabled": current.get("enabled", True),
                "times": ", ".join(_times_text(current.get("times", ["02:00"]))),
                "run_on_start": bool(current.get("run_on_start", False))}
        errors: list = []
        if request.method == "POST":
            form = {"enabled": "enabled" in request.form,
                    "times": request.form.get("times", ""),
                    "run_on_start": "run_on_start" in request.form}
            times = []
            for piece in re.split(r"[,\s]+", form["times"].strip()):
                if not piece:
                    continue
                try:
                    times.append(parse_hhmm(piece))
                except ValueError as exc:
                    errors.append(f"{exc}; use 24-hour HH:MM, e.g. 02:00.")
            if error:
                errors.append(f"config.yaml can't be read: {error}. Fix it under Files first.")
            if form["enabled"] and not times and not errors:
                errors.append("Give at least one time, or turn the schedule off.")
            if not errors:
                new = copy.deepcopy(raw)
                new["schedule"] = {"enabled": form["enabled"],
                                   "times": sorted(set(times)),
                                   "run_on_start": form["run_on_start"]}
                try:
                    with ctx()["write_lock"]:
                        mapedit.save_config(files().config, new)
                except OSError as exc:
                    errors.append(_write_error(exc))
                else:
                    audit("set the schedule to %s", ", ".join(sorted(set(times))) or "off")
                    service.tick()          # pick it up now, not in 15 seconds
                    notice("Saved the schedule.")
                    return redirect(url_for("schedule"))
        upcoming = []
        if service.schedule.enabled and service.schedule.times:
            zone = campus_zone(service)
            moment = datetime.now(zone)
            for _ in range(5):
                nxt = next_run_any(service.schedule.times, moment)
                if nxt is None:
                    break
                upcoming.append(nxt)
                moment = nxt
        return render_template("schedule.html", form=form, errors=errors,
                               service=service, upcoming=upcoming,
                               sync_at=os.environ.get("SYNC_AT", ""))

    # ── files ────────────────────────────────────────────────────────────────

    FILES = {"config": "config.yaml", "defaults": "defaults.yaml",
             "map": "space_mapping.yaml"}

    def _file_path(name: str) -> Path:
        p = files()
        return {"config": p.config, "defaults": p.defaults, "map": p.space_map}[name]

    @app.route("/files")
    def files_list():
        rows = []
        for name, title in FILES.items():
            path = _file_path(name)
            try:
                stat = path.stat()
                size, modified = stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            except OSError:
                size, modified = None, None
            rows.append({"name": name, "title": title, "path": path,
                         "size": size, "modified": modified})
        return render_template("files.html", files=rows, health=health())

    @app.route("/files/<name>", methods=["GET", "POST"])
    def file_edit(name):
        if name not in FILES:
            abort(404)
        path = _file_path(name)
        errors: list = []
        confirm: list = []
        text = ""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            errors.append(f"Couldn't read {path}: {exc}")
        version = version_of(path)
        if request.method == "POST":
            text = request.form.get("text", "").replace("\r\n", "\n")
            with ctx()["write_lock"]:
                if request.form.get("version") != version_of(path):
                    errors.append(f"{FILES[name]} changed since this page was opened. "
                                  "Copy your text somewhere safe, reload, and redo "
                                  "the change.")
                else:
                    errors, confirm = _check_file(name, text)
                    if not errors and (not confirm or request.form.get("confirm")):
                        try:
                            written = mapedit.write_if_changed(path, text)
                        except OSError as exc:
                            errors.append(_write_error(exc))
                        else:
                            audit("edited %s directly", FILES[name])
                            notice(f"Saved {FILES[name]} (the previous version is "
                                   f"{path.name}.bak)." if written else "Nothing had changed.",
                                   "warn" if confirm else "ok")
                            return redirect(url_for("files_list"))
            version = request.form.get("version") or version
        return render_template("file_edit.html", name=name, title=FILES[name],
                               path=path, text=text, version=version,
                               errors=errors, confirm=confirm), (422 if errors or confirm else 200)

    def _check_file(name: str, text: str) -> tuple:
        """(errors that block saving, problems to confirm)."""
        try:
            data = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as exc:
            return [f"That isn't valid YAML: {exc}"], []
        if data is not None and not isinstance(data, dict):
            return ["The top level must be a mapping (key: value lines)."], []
        p = files()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / FILES[name]
            tmp_path.write_text(text, encoding="utf-8")
            config_path = tmp_path if name == "config" else p.config
            defaults_path = tmp_path if name == "defaults" else p.defaults
            try:
                load_config(str(config_path), str(defaults_path))
            except ConfigError as exc:
                return [], [f"The sync would refuse to start: {exc}"]
            if name != "map":
                return [], []
            try:
                buildings, floors, rooms = mapedit.load_mapping(tmp_path)
            except Exception as exc:                  # noqa: BLE001
                return [f"The room map doesn't read: {exc}"], []
            raw, _err = load_config_raw()
            _e, errs, _w = mapedit.validate_before_save(buildings, floors, rooms,
                                                        raw, load_defaults())
            return [], [f"Room map: {e}" for e in errs]

    @app.route("/files/<name>/download")
    def file_download(name):
        if name not in FILES:
            abort(404)
        path = _file_path(name)
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="text/yaml", as_attachment=True,
                         download_name=FILES[name])

    @app.route("/files/backup.zip")
    def files_backup():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, title in FILES.items():
                path = _file_path(name)
                if path.exists():
                    zf.write(path, title)
        buf.seek(0)
        stamp = datetime.now(campus_zone(svc())).strftime("%Y%m%d-%H%M")
        audit("downloaded a backup of the settings")
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name=f"25live-bas-sync-config-{stamp}.zip")

    # ── logs ─────────────────────────────────────────────────────────────────

    def _log_path(which: str) -> Path:
        log = files().log_file
        return log if which == "sync" else log.parent / "service.log"

    @app.route("/logs")
    def logs():
        which = request.args.get("which", "sync")
        if which not in ("sync", "service"):
            abort(404)
        try:
            lines = max(50, min(int(request.args.get("lines") or 400), 5000))
        except ValueError:
            lines = 400
        return render_template("logs.html", which=which, lines=lines,
                               text=_tail(_log_path(which), lines),
                               path=_log_path(which))

    @app.route("/logs/<which>/download")
    def log_download(which):
        if which not in ("sync", "service"):
            abort(404)
        path = _log_path(which)
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="text/plain", as_attachment=True,
                         download_name=path.name)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers the routes share
# ─────────────────────────────────────────────────────────────────────────────

def _config_choices(path_t) -> list:
    if path_t[-1] == "verify_tls":
        return ["true", "false"]
    if path_t == ("collegenet", "state_param_style"):
        return list(STATE_PARAM_STYLES)
    if path_t == ("timezone",):
        try:
            return sorted(available_timezones())
        except Exception:                               # noqa: BLE001
            return []
    return []


def _config_change_problems(raw: dict, new: dict) -> list:
    """What a config change breaks: a config the sync would refuse, or room
    map rows that were fine and now aren't (a changed driver, a removed
    default). Problems that were already there aren't repeated."""
    buildings, floors, rooms, _v, _e = load_map()
    defaults = load_defaults()
    before_cfg, before, _w = mapedit.validate_before_save(buildings, floors, rooms, raw, defaults)
    after_cfg, after, _w = mapedit.validate_before_save(buildings, floors, rooms, new, defaults)
    problems = []
    if after_cfg and after_cfg != before_cfg:
        problems.append(f"The sync would refuse to start: {after_cfg}")
    problems += [f"Room map: {p}" for p in new_problems(before, after)]
    return problems


def _times_text(times) -> list:
    out = []
    for t in times if isinstance(times, list) else [times]:
        try:
            out.append(parse_hhmm(t))
        except ValueError:
            out.append(str(t))
    return out


def _tail(path: Path, lines: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - lines * 400))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


_DISCOVER_MARK = "# --- discovered spaces"


def _discovered_spaces(lines: list) -> Optional[list]:
    """The spaces a finished --discover printed, marked with whether the room
    map has them already."""
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(_DISCOVER_MARK))
    except StopIteration:
        return None
    try:
        data = yaml.safe_load("\n".join(lines[start + 1:])) or {}
    except yaml.YAMLError:
        return None
    spaces = data.get("spaces") if isinstance(data, dict) else None
    if not isinstance(spaces, list):
        return None
    _b, _f, rooms, _v, _e = load_map()
    mapped = {str(r.get("space_id")) for r in rooms}
    return [{"space_id": s.get("space_id"), "space_name": s.get("space_name", ""),
             "mapped": str(s.get("space_id")) in mapped}
            for s in spaces if isinstance(s, dict)]

