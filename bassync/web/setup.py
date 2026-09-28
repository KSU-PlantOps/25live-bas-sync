# 25Live -> BAS Schedule Sync — the setup guide
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The setup guide: a first-run walk through what a new install needs, in
order — 25Live, the campus, a BAS system, the rooms (found in 25Live), each
building's schedule, and a check before the schedule goes on.

It is a guided view over the ordinary settings files, not a second store.
Each step reads and writes config.yaml, defaults.yaml and space_mapping.yaml
the way the other pages do, and a step is done when the files say so. So it
can be left and picked up again, and anything it does can be changed on the
other pages.

Two things keep a half-finished setup away from the BAS:

- config.yaml, when the guide creates it, has the schedule off. Finishing
  turns it on.
- Imported buildings wait on a `preview` system (STAGING) that writes
  nothing, with a placeholder target, until the Schedules step gives each
  one a real schedule on a real system. A dry run shows what they'd get.

The guide keeps one thing of its own, state/setup.json: whether it was
finished or skipped, so the status page stops sending admins here.
"""

import copy
import importlib.util
import ipaddress
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from flask import abort, g, redirect, render_template, request, url_for

from .. import mapedit, secretstore
from ..collegenet import CollegeNetClient
from ..config import ConfigError, load_config, parse_hhmm
from . import access, audit, ctx, importer, notice, requires
from .importer import STAGING
from .views import (
    _write_error,
    files,
    load_config_raw,
    load_defaults,
    load_map,
    mapedit_write_bytes,
    new_problems,
    svc,
)

PASSWORD_VAR = "BAS_25LIVE_PASSWORD"
STEPS = [("welcome", "Welcome"), ("25live", "25Live"), ("campus", "Campus"),
         ("bas", "BAS system"), ("rooms", "Rooms"), ("schedules", "Schedules"),
         ("finish", "Check and finish")]
SYSTEM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_DEVICE_ID = 4194302


# ── what the guide keeps, and how far along it is ────────────────────────────

def _state_file() -> Path:
    return files().state_dir / "setup.json"


def setup_state() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(**changes) -> None:
    data = {**setup_state(), **changes}
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    mapedit_write_bytes(path, (json.dumps(data, indent=1) + "\n").encode())


def _who() -> str:
    user = g.get("user") or {}
    return str(user.get("username") or user.get("name") or "")


def settled() -> bool:
    """The guide was finished or skipped."""
    state = setup_state()
    return bool(state.get("finished") or state.get("skipped"))


def wants_setup() -> bool:
    """Whether the status page sends this person to the guide first: someone
    who can change settings, on a new install (no config.yaml yet)."""
    return (access.can(g.get("caps"), "edit_settings")
            and not files().config.exists() and not settled())


def _staged(buildings: list, raw: dict) -> list:
    """The buildings whose schedule is on a preview system: nothing is
    written for them yet."""
    systems = mapedit.config_systems(raw)
    return [b for b in buildings
            if systems.get(mapedit.effective_system(b, buildings, raw)) == "preview"]


def _section(raw: dict, key: str) -> dict:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _as_list(value, default: list) -> list:
    return value if isinstance(value, list) else default


def progress(raw: dict, buildings: list, rooms: list) -> list:
    """Each step, whether the files say it's done, and a word on where it's at."""
    cn = _section(raw, "collegenet")
    systems = mapedit.config_systems(raw)
    real = sorted(n for n, d in systems.items() if d != "preview")
    staged = _staged(buildings, raw)
    where = str(cn.get("instance") or cn.get("base_url") or "")
    password = secretstore.source(PASSWORD_VAR, files().secrets_file)
    state = setup_state()
    status = {
        "welcome": (files().config.exists(), ""),
        "25live": (bool(where and cn.get("username") and password),
                   where or "not set up"),
        "campus": (bool(raw.get("timezone")), str(raw.get("timezone") or "")),
        "bas": (bool(real), ", ".join(real) or ("staging only" if systems else "")),
        "rooms": (bool(rooms), f"{len(rooms)} room{'' if len(rooms) == 1 else 's'}"
                  if rooms else ""),
        "schedules": (bool(buildings) and not staged,
                      f"{len(staged)} of {len(buildings)} to do" if staged else ""),
        "finish": (bool(state.get("finished")), ""),
    }
    return [{"id": sid, "title": title, "done": status[sid][0], "summary": status[sid][1],
             "url": url_for("setup_step", step=sid)} for sid, title in STEPS]


# ── helpers for the steps ────────────────────────────────────────────────────

def _writable(path: Path) -> bool:
    """Whether the service can write `path`, or create it."""
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return os.access(path, os.W_OK)


def _suggest_local_address() -> str:
    """This host's address on its main network, as a starting point for
    local_address. The /24 is a guess; the page says so."""
    from ..drivers.bacnet import _host_addresses
    found = _host_addresses()
    return f"{found[0]}/24" if len(found) == 1 else ""


def check_25live() -> tuple:
    """(ok, what happened): one small authenticated request with the saved
    settings and the stored or environment password. Nothing is retried, so
    a wrong address fails in seconds rather than minutes."""
    try:
        cfg = load_config(str(files().config), str(files().defaults))
    except ConfigError as exc:
        return False, str(exc)
    cn = dict(cfg["collegenet"])
    cn["password"] = (secretstore.get(PASSWORD_VAR, files().secrets_file)
                      or cn.get("password") or "")
    if not cn.get("base_url"):
        return False, "no instance name or base URL is set"
    client = CollegeNetClient(cn, ZoneInfo(cfg["timezone"]), None)
    try:
        return client.check_connection()
    finally:
        client.close()


def _save_config(new: dict) -> None:
    mapedit.save_config(files().config, new)


def _last_job(kind: str):
    """The summary of the newest job of this kind, or None."""
    return next((j for j in svc().jobs.recent(30) if j.get("kind") == kind), None)


# ── what each step shows ─────────────────────────────────────────────────────

def _welcome(raw, buildings, floors, rooms) -> dict:
    p = files()
    checks = [
        ("The settings folder can be written", _writable(p.config.parent),
         str(p.config.parent),
         "In Docker, the config folder must belong to the user the container runs as — "
         "see “The config folder” in docs/docker.md."),
        ("The state folder can be written", _writable(p.state_dir), str(p.state_dir),
         "It holds the run history, passwords set here, and what discovery found."),
        ("The BACnet driver is installed", importlib.util.find_spec("bacpypes3") is not None,
         "bacpypes3", "Only needed for bacnet systems; the Docker image has it."),
    ]
    return {"checks": checks}


def _25live(raw, buildings, floors, rooms) -> dict:
    cn = _section(raw, "collegenet")
    states = _as_list(cn.get("include_states"), [2])
    return {"form": {"instance": str(cn.get("instance") or ""),
                     "base_url": str(cn.get("base_url") or ""),
                     "username": str(cn.get("username") or ""),
                     "states": [int(s) for s in states if str(s).isdigit()]},
            "password_source": secretstore.source(PASSWORD_VAR, files().secrets_file),
            "state_labels": mapedit.INCLUDE_STATE_LABELS}


def _campus(raw, buildings, floors, rooms) -> dict:
    return {"form": {"timezone": str(raw.get("timezone") or ""), **load_defaults()},
            "zones": sorted(available_timezones()),
            "fields": mapedit.DEFAULTS_FIELDS}


def _bas(raw, buildings, floors, rooms) -> dict:
    systems = mapedit.config_systems(raw)
    return {"systems": sorted(systems.items()),
            "real": sorted(n for n, d in systems.items() if d != "preview"),
            "form": {"name": "campus" if "campus" not in systems else "",
                     "local_address": _suggest_local_address(),
                     "device_id": "599001", "bbmd_address": ""},
            "bacnet_installed": importlib.util.find_spec("bacpypes3") is not None}


def _rooms(raw, buildings, floors, rooms) -> dict:
    return {**importer.context(buildings, rooms), "back": "setup"}


def _schedules(raw, buildings, floors, rooms) -> dict:
    systems = mapedit.config_systems(raw)
    counts: dict = {}
    for r in rooms:
        counts[str(r.get("building"))] = counts.get(str(r.get("building")), 0) + 1
    staged = {id(b) for b in _staged(buildings, raw)}
    real = sorted(n for n, d in systems.items() if d != "preview")
    default = mapedit.effective_system({}, buildings, raw)
    # A staged building is offered the default system (or the only real one).
    fallback = default if default in real else (real[0] if real else "")
    rows = []
    for b in buildings:
        system = mapedit.effective_system(b, buildings, raw)
        rows.append({"id": str(b.get("id")), "name": str(b.get("name") or ""),
                     "rooms": counts.get(str(b.get("id")), 0),
                     "system": system if system in real else fallback,
                     "target": "" if id(b) in staged else str(b.get("target") or ""),
                     "staged": id(b) in staged})
    return {"rows": rows, "real": real, "floors": len(floors)}


def _finish(raw, buildings, floors, rooms) -> dict:
    schedule = _section(raw, "schedule")
    times = _as_list(schedule.get("times"), ["02:00"])
    shown = []
    for t in times:
        try:
            shown.append(parse_hhmm(t))
        except ValueError:
            shown.append(str(t))
    return {"validate": _last_job("validate"), "dry_run": _last_job("dry-run"),
            "schedule_on": schedule.get("enabled", True) is not False,
            "times": ", ".join(shown) or "02:00",
            "staged": len(_staged(buildings, raw)), "room_count": len(rooms),
            "sync_at": os.environ.get("SYNC_AT", "")}


_STEP_DATA = {"welcome": _welcome, "25live": _25live, "campus": _campus, "bas": _bas,
              "rooms": _rooms, "schedules": _schedules, "finish": _finish}


def render(step: str, errors=(), status: int = 200, **extra):
    raw, raw_error = load_config_raw()
    buildings, floors, rooms, map_version, map_error = load_map()
    data = {"step": step, "steps": progress(raw, buildings, rooms),
            "errors": list(errors), "raw_error": raw_error, "map_error": map_error,
            "map_version": map_version, "staging": STAGING,
            "config_exists": files().config.exists()}
    data.update(_STEP_DATA[step](raw, buildings, floors, rooms))
    data.update(extra)
    return render_template("setup.html", **data), status


def register(app) -> None:            # noqa: C901 — the guide's routes together

    @app.route("/setup")
    @app.route("/setup/<step>")
    @requires("edit_settings")
    def setup_step(step="welcome"):
        if step not in _STEP_DATA:
            abort(404)
        return render(step)

    @app.route("/setup/skip", methods=["POST"])
    @requires("edit_settings")
    def setup_skip():
        _record(skipped=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                skipped_by=_who())
        audit("skipped the setup guide")
        notice("The setup guide is under Settings whenever you want it.")
        return redirect(url_for("dashboard"))

    # ── 25Live ───────────────────────────────────────────────────────────────

    @app.route("/setup/25live", methods=["POST"])
    @requires("edit_settings")
    def setup_25live_save():
        form: dict = {
            "instance": (request.form.get("instance") or "").strip(),
            "base_url": (request.form.get("base_url") or "").strip().rstrip("/"),
            "username": (request.form.get("username") or "").strip(),
            "states": sorted({int(s) for s in request.form.getlist("state") if s.isdigit()})}
        password = request.form.get("password") or ""
        source = secretstore.source(PASSWORD_VAR, files().secrets_file)
        errors = []
        if not (form["instance"] or form["base_url"]):
            errors.append("Give your 25Live instance name — or, for a self-hosted "
                          "Series25, its WebServices base URL.")
        if form["instance"] and not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", form["instance"]):
            errors.append("An instance name is letters, digits, dots, dashes and underscores.")
        if form["base_url"] and not re.match(r"^https?://[^\s/]+", form["base_url"]):
            errors.append("The base URL starts with https:// and has no spaces.")
        if not form["username"]:
            errors.append("Give the service account's username.")
        if not form["states"]:
            errors.append("Pick at least one kind of booking to include.")
        if password and source != "environment" and not access.can(g.caps, "edit_passwords"):
            errors.append("Your role can't set passwords; ask someone who can, or set "
                          f"{PASSWORD_VAR} in the container's environment.")
        elif not password and not source:
            errors.append("Give the account's password.")
        raw, raw_error = load_config_raw()
        if raw_error:
            errors.append(f"config.yaml can't be read: {raw_error}. Fix it under Files first.")
        if errors:
            return render("25live", errors, 422, form=form, password_source=source)
        with ctx()["write_lock"]:
            raw, _e = load_config_raw()
            new = copy.deepcopy(raw)
            first = not files().config.exists()
            cn = new.get("collegenet") if isinstance(new.get("collegenet"), dict) else {}
            for key in ("instance", "base_url", "username"):
                if form[key]:
                    cn[key] = form[key]
                else:
                    cn.pop(key, None)
            cn["include_states"] = form["states"]
            new["collegenet"] = cn
            if first:
                # Nothing runs by itself until the guide is finished.
                new["schedule"] = {"enabled": False, "times": ["02:00"], "run_on_start": False}
            try:
                _save_config(new)
                if password and source != "environment":
                    secretstore.write(files().secrets_file, PASSWORD_VAR, password)
            except OSError as exc:
                return render("25live", [_write_error(exc)], 500, form=form)
        audit("saved the 25Live connection (setup guide)%s", ", and its password" if password else "")
        svc().tick()
        ok, detail = check_25live()
        if not ok:
            return render("25live", [f"Saved, but 25Live didn't accept the connection: {detail}"],
                          200, form=form)
        notice(f"Connected to 25Live ({detail}).")
        return redirect(url_for("setup_step", step="campus"))

    @app.route("/setup/25live/test", methods=["POST"])
    @requires("edit_settings")
    def setup_25live_test():
        ok, detail = check_25live()
        if ok:
            notice(f"Connected to 25Live ({detail}).")
            return redirect(url_for("setup_step", step="25live"))
        return render("25live", [f"25Live didn't accept the connection: {detail}"])

    # ── the campus ───────────────────────────────────────────────────────────

    @app.route("/setup/campus", methods=["POST"])
    @requires("edit_settings")
    def setup_campus_save():
        zone = (request.form.get("timezone") or "").strip()
        errors = []
        if zone not in available_timezones():
            errors.append("Pick the campus timezone from the list.")
        values = {}
        for key, label, fallback in mapedit.DEFAULTS_FIELDS:
            text = (request.form.get(key) or "").strip()
            try:
                number = int(text) if text else fallback
            except ValueError:
                errors.append(f"{label} must be a whole number.")
                continue
            low, high = (1, 366) if key == "lookahead_days" else (0, 1440)
            if not low <= number <= high:
                errors.append(f"{label} must be between {low} and {high}.")
            values[key] = number
        raw, raw_error = load_config_raw()
        if raw_error:
            errors.append(f"config.yaml can't be read: {raw_error}. Fix it under Files first.")
        if errors:
            form = {"timezone": zone, **{k: request.form.get(k, "")
                                         for k, *_ in mapedit.DEFAULTS_FIELDS}}
            return render("campus", errors, 422, form=form)
        with ctx()["write_lock"]:
            raw, _e = load_config_raw()
            new = copy.deepcopy(raw)
            new["timezone"] = zone
            try:
                _save_config(new)
                mapedit.save_defaults(files().defaults, {**load_defaults(), **values})
            except OSError as exc:
                return render("campus", [_write_error(exc)], 500)
        audit("saved the campus timezone and defaults (setup guide)")
        svc().tick()
        notice(f"Saved: {zone}, looking {values['lookahead_days']} days ahead.")
        return redirect(url_for("setup_step", step="bas"))

    # ── the BAS system ───────────────────────────────────────────────────────

    @app.route("/setup/bas", methods=["POST"])
    @requires("edit_settings")
    def setup_bas_save():
        later = request.form.get("action") == "later"
        form = {k: (request.form.get(k) or "").strip()
                for k in ("name", "local_address", "device_id", "bbmd_address")}
        raw, raw_error = load_config_raw()
        systems = mapedit.config_systems(raw)
        errors = []
        if raw_error:
            errors.append(f"config.yaml can't be read: {raw_error}. Fix it under Files first.")
        entry: dict = {}
        if not later:
            if not SYSTEM_NAME.fullmatch(form["name"]):
                errors.append("A system name is letters, digits, - and _, starting with a "
                              "letter or digit.")
            elif form["name"] in systems:
                errors.append(f"There is already a system called '{form['name']}' — "
                              "change it on the Connection page.")
            elif form["name"] == STAGING:
                errors.append(f"'{STAGING}' is the name kept for buildings not set up yet.")
            try:
                iface = ipaddress.ip_interface(form["local_address"])
                if "/" not in form["local_address"] or iface.version != 4:
                    raise ValueError
            except ValueError:
                errors.append("This host's address is an IPv4 address with its prefix "
                              "length, e.g. 10.4.1.55/24.")
            if not form["device_id"].isdigit() or not 0 <= int(form["device_id"]) <= MAX_DEVICE_ID:
                errors.append(f"The device ID is a whole number from 0 to {MAX_DEVICE_ID}.")
            if form["bbmd_address"] and not re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?",
                                                         form["bbmd_address"]):
                errors.append("The BBMD is an address, optionally with :port, e.g. 10.4.2.1.")
            if not errors:
                entry = {"driver": "bacnet", "local_address": form["local_address"],
                         "device_id": int(form["device_id"])}
                if form["bbmd_address"]:
                    entry["bbmd_address"] = form["bbmd_address"]
        if errors:
            return render("bas", errors, 422, form=form)
        with ctx()["write_lock"]:
            raw, _e = load_config_raw()
            new = copy.deepcopy(raw)
            if not isinstance(new.get("systems"), dict):
                new["systems"] = {}
            if entry:
                new["systems"][form["name"]] = entry
            elif STAGING not in new["systems"]:
                new["systems"][STAGING] = {"driver": "preview"}
            current = str(new.get("default_system") or "")
            if entry and current in ("", STAGING):
                new["default_system"] = form["name"]
            elif not current:
                new["default_system"] = STAGING
            try:
                _save_config(new)
            except OSError as exc:
                return render("bas", [_write_error(exc)], 500, form=form)
        if entry:
            audit("added BACnet system '%s' (setup guide)", form["name"])
            notice(f"Added the BACnet system '{form['name']}'. Its other settings are on the "
                   "Connection page; --validate checks it can reach the network.")
        else:
            audit("chose to stage every building for now (setup guide)")
            notice("Buildings will wait on a preview system that writes nothing until "
                   "you add a BAS system.")
        return redirect(url_for("setup_step", step="rooms"))

    # ── each building's schedule ─────────────────────────────────────────────

    @app.route("/setup/schedules", methods=["POST"])
    @requires("edit_settings")
    def setup_schedules_save():
        if not access.can(g.caps, "edit_map"):
            abort(403, "Changing buildings needs “Edit the room map”, which your role doesn't have.")
        with ctx()["write_lock"]:
            raw, raw_error = load_config_raw()
            buildings, floors, rooms, map_version, map_error = load_map()
            if raw_error or map_error:
                return render("schedules", [f"Can't save while a settings file can't be read: "
                                            f"{raw_error or map_error}"], 409)
            if request.form.get("version") != map_version:
                return render("schedules", ["The room map changed since this page was opened. "
                                            "Reload it and redo your change."], 409)
            systems = mapedit.config_systems(raw)
            default = mapedit.effective_system({}, buildings, raw)
            new_buildings = [dict(b) for b in buildings]
            errors, changed = [], []
            for b in new_buildings:
                bid = str(b.get("id"))
                target = (request.form.get(f"t.{bid}") or "").strip()
                system = (request.form.get(f"s.{bid}") or "").strip()
                if not target:
                    continue
                if systems.get(system) in (None, "preview"):
                    errors.append(f"{b.get('name') or bid}: pick the BAS system it's on.")
                    continue
                problem = mapedit.target_problem(target, system, raw)
                if problem:
                    errors.append(f"{b.get('name') or bid}: {problem}")
                    continue
                if b.get("target") == target and mapedit.effective_system(b, buildings, raw) == system:
                    continue
                b["target"] = target
                if system == default:
                    b.pop("system", None)
                else:
                    b["system"] = system
                changed.append(bid)
            if errors:
                typed = {k: v for k, v in request.form.items() if k[:2] in ("t.", "s.")}
                return render("schedules", errors, 422, typed=typed)
            if not changed:
                notice("Nothing had changed.")
                return redirect(url_for("setup_step", step="schedules"))
            defaults = load_defaults()
            _c, before, _w = mapedit.validate_before_save(buildings, floors, rooms, raw, defaults)
            after_cfg, after, _w = mapedit.validate_before_save(new_buildings, floors, rooms,
                                                                raw, defaults)
            problems = ([after_cfg] if after_cfg else []) + new_problems(before, after)
            if problems:
                typed = {k: v for k, v in request.form.items() if k[:2] in ("t.", "s.")}
                return render("schedules", ["Nothing was saved — the room map would have these "
                                            "problems:"] + problems[:10], 422, typed=typed)
            try:
                mapedit.save_mapping(files().space_map, new_buildings, floors, rooms)
            except OSError as exc:
                return render("schedules", [_write_error(exc)], 500)
        audit("set the schedule of %d building(s) (setup guide): %s", len(changed),
              ", ".join(changed))
        notice(f"Saved {len(changed)} building schedule{'' if len(changed) == 1 else 's'}.")
        staged = _staged(new_buildings, raw)
        return redirect(url_for("setup_step", step="schedules" if staged else "finish"))

    # ── check and finish ─────────────────────────────────────────────────────

    @app.route("/setup/finish", methods=["POST"])
    @requires("edit_settings")
    def setup_finish():
        turn_on = request.form.get("action") == "on"
        times: list = []
        errors = []
        if turn_on:
            for piece in re.split(r"[,\s]+", (request.form.get("times") or "").strip()):
                if not piece:
                    continue
                try:
                    times.append(parse_hhmm(piece))
                except ValueError as exc:
                    errors.append(f"{exc}; use 24-hour HH:MM, e.g. 02:00.")
            if not times and not errors:
                errors.append("Give at least one time for the sync to run.")
        raw, raw_error = load_config_raw()
        if raw_error:
            errors.append(f"config.yaml can't be read: {raw_error}. Fix it under Files first.")
        if errors:
            return render("finish", errors, 422, times=request.form.get("times", ""))
        if turn_on:
            with ctx()["write_lock"]:
                raw, _e = load_config_raw()
                new = copy.deepcopy(raw)
                schedule = new.get("schedule") if isinstance(new.get("schedule"), dict) else {}
                schedule.update({"enabled": True, "times": sorted(set(times))})
                schedule.setdefault("run_on_start", False)
                new["schedule"] = schedule
                try:
                    _save_config(new)
                except OSError as exc:
                    return render("finish", [_write_error(exc)], 500)
            svc().tick()
        _record(finished=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                finished_by=_who())
        audit("finished the setup guide%s", f"; schedule on at {', '.join(sorted(set(times)))}"
              if turn_on else "")
        notice("Setup is finished." + (f" The sync runs at {', '.join(sorted(set(times)))}."
                                       if turn_on else ""))
        return redirect(url_for("dashboard"))
