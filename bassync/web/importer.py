# 25Live -> BAS Schedule Sync — adding rooms found in 25Live
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Room map → From 25Live, and the setup guide's Rooms step: the spaces the last
discovery found (state/discovery.json, see bassync/discovery.py), grouped by
building, to add to the room map a building at a time or all at once.

Each room's building is 25Live's where the instance gives one, else a guess
from its name, and editable. Rooms go into the room-map building of that name
(or id); a name the map doesn't have yet becomes a new building. New
buildings wait on the `preview` system STAGING, which writes nothing, with a
placeholder target, until someone gives each its real schedule — so adding
rooms never, by itself, writes to a BAS.

Seeing the page needs view_all; looking in 25Live, run_tools; adding rooms,
edit_map. Adding the staging system to config.yaml, when a new building needs
it, is part of adding rooms: it writes nothing and names nothing else.
"""

import copy

from flask import abort, g, redirect, render_template, request, url_for

from .. import discovery, mapedit
from ..jobs import JobBusy
from . import access, audit, ctx, notice, requires
from .views import (
    _write_error,
    files,
    load_config_raw,
    load_defaults,
    load_map,
    new_problems,
    svc,
)

STAGING = "staging"
DISCOVER_DAYS = (30, 60, 90, 180)
NO_BUILDING = ""


def _discover_jobs() -> tuple:
    """(the discovery running now, the summary of the last one that finished)."""
    current = svc().jobs.current
    running = current if current is not None and current.kind == "discover" else None
    last = next((j for j in svc().jobs.recent(30)
                 if j.get("kind") == "discover" and not j.get("running")), None)
    return running, last


def _building_ids_by_name(buildings: list) -> dict:
    """{lower-cased name or id: building id} for the room map's buildings."""
    out: dict = {}
    for b in buildings:
        for key in ("name", "id"):
            if b.get(key) not in (None, ""):
                out.setdefault(str(b[key]).strip().lower(), str(b["id"]))
    return out


def context(buildings: list, rooms: list) -> dict:
    """What the import table shows: the last discovery's spaces, grouped by
    building (25Live's, or a guess), each marked if the room map has it."""
    running, last = _discover_jobs()
    found = discovery.load(files().state_dir)
    groups: dict = {}
    existing = _building_ids_by_name(buildings)
    if found:
        guesses = discovery.guess_buildings(found["spaces"])
        mapped = {str(r.get("space_id")) for r in rooms}
        for s in sorted(found["spaces"], key=lambda s: s["space_name"].lower()):
            name = s["building"] or guesses.get(s["space_id"], "")
            group = groups.setdefault(name.lower(), {
                "name": name, "rows": [], "new": 0,
                "building_id": existing.get(name.lower()) if name else None})
            row = {**s, "guess": name, "mapped": s["space_id"] in mapped}
            group["rows"].append(row)
            group["new"] += 0 if row["mapped"] else 1
    ordered = sorted(groups.values(), key=lambda gr: (gr["name"] == NO_BUILDING,
                                                      gr["name"].lower()))
    names = sorted({str(b.get("name") or b.get("id")) for b in buildings}
                   | {gr["name"] for gr in ordered if gr["name"]}, key=str.lower)
    return {"running": running, "last": last, "found": found, "groups": ordered,
            "building_names": names, "days": DISCOVER_DAYS,
            "new_count": sum(gr["new"] for gr in ordered),
            "space_count": len(found["spaces"]) if found else 0}


def _page(back: str, errors=(), status: int = 200):
    """The page the import was made from, again, with what went wrong."""
    if back == "setup":
        from .setup import render
        return render("rooms", errors, status)
    buildings, _floors, rooms, map_version, _e = load_map()
    return render_template("map_import.html", errors=list(errors), back="map",
                           map_version=map_version, staging=STAGING,
                           **context(buildings, rooms)), status


def register(app) -> None:            # noqa: C901 — the import's routes together

    @app.route("/map/import")
    @requires("view_all")
    def map_import():
        return _page("map")

    @app.route("/map/import/find", methods=["POST"])
    @requires("run_tools")
    def map_import_find():
        back = "setup" if request.form.get("back") == "setup" else "map"
        try:
            days = int(request.form.get("days") or 60)
        except ValueError:
            days = 60
        days = max(1, min(days, 366))
        try:
            svc().jobs.start("discover", ["--discover-days", str(days)],
                             trigger=f"web ({request.remote_addr})")
        except JobBusy as exc:
            notice(f"{exc} is already running — wait for it to finish, then try again.", "error")
        except RuntimeError as exc:
            notice(str(exc), "error")
        else:
            audit("started Discover spaces --discover-days %d (to add rooms)", days)
        return redirect(url_for("setup_step", step="rooms") if back == "setup"
                        else url_for("map_import"))

    @app.route("/map/import", methods=["POST"])
    @requires("view_all", "edit_map")
    def map_import_save():
        back = "setup" if request.form.get("back") == "setup" else "map"
        if back == "setup" and not access.can(g.caps, "edit_settings"):
            abort(403)
        found = discovery.load(files().state_dir)
        if not found:
            notice("Find the rooms in 25Live first.", "error")
            return _page(back)
        spaces = {s["space_id"]: s for s in found["spaces"]}
        picked = [sid for sid in dict.fromkeys(request.form.getlist("pick")) if sid in spaces]
        # "Add this building": only the ticked rooms whose building is that one.
        only = " ".join((request.form.get("only") or "").split()).lower()
        with ctx()["write_lock"]:
            raw, raw_error = load_config_raw()
            buildings, floors, rooms, map_version, map_error = load_map()
            if raw_error or map_error:
                return _page(back, ["Can't add rooms while a settings file can't be read: "
                                    f"{raw_error or map_error}"], 409)
            if request.form.get("version") != map_version:
                return _page(back, ["The room map changed since this page was opened. "
                                    "Check the list and add them again."], 409)
            staging_cfg = (raw.get("systems") or {}).get(STAGING)
            if isinstance(staging_cfg, dict) and staging_cfg.get("driver") != "preview":
                return _page(back, [f"New buildings wait on a preview system called "
                                    f"'{STAGING}', but that name is taken by a "
                                    f"{staging_cfg.get('driver') or '?'} system. Rename "
                                    "that system on the Connection page first."], 409)
            new_buildings = [dict(b) for b in buildings]
            new_rooms = [dict(r) for r in rooms]
            mapped = {str(r.get("space_id")) for r in rooms}
            by_name = _building_ids_by_name(new_buildings)
            taken = {str(b.get("id")) for b in new_buildings}
            added_buildings: list = []
            added: list = []
            skipped: list = []
            for sid in picked:
                if sid in mapped:
                    continue
                name = " ".join((request.form.get(f"b.{sid}") or "").split())[:120]
                if only and name.lower() != only:
                    continue
                if not name:
                    skipped.append(spaces[sid]["space_name"])
                    continue
                bid = by_name.get(name.lower())
                if bid is None:
                    bid = discovery.building_id(name, taken)
                    taken.add(bid)
                    by_name[name.lower()] = bid
                    new_buildings.append({"id": bid, "name": name, "system": STAGING,
                                          "target": bid})
                    added_buildings.append(bid)
                new_rooms.append({"space_id": int(sid) if sid.isdigit() else sid,
                                  "space_name": spaces[sid]["space_name"], "building": bid})
                mapped.add(sid)
                added.append(bid)
            if not added:
                errors = (["These have no building, so their bookings would drive nothing: "
                           + ", ".join(skipped[:10]) + ("…" if len(skipped) > 10 else "")]
                          if skipped else ["Tick the rooms to add."])
                return _page(back, errors, 422)
            new_raw = copy.deepcopy(raw)
            if added_buildings:
                systems = new_raw.get("systems") if isinstance(new_raw.get("systems"), dict) else {}
                systems.setdefault(STAGING, {"driver": "preview"})
                new_raw["systems"] = systems
                if not new_raw.get("default_system"):
                    real = [n for n, c in systems.items()
                            if isinstance(c, dict) and c.get("driver") != "preview"]
                    new_raw["default_system"] = real[0] if len(real) == 1 else STAGING
            defaults = load_defaults()
            before_cfg, before, _w = mapedit.validate_before_save(buildings, floors, rooms,
                                                                  raw, defaults)
            after_cfg, after, _w = mapedit.validate_before_save(new_buildings, floors, new_rooms,
                                                                new_raw, defaults)
            problems = ([after_cfg] if after_cfg and after_cfg != before_cfg else []) \
                + new_problems(before, after)
            if problems:
                return _page(back, ["Nothing was added — the room map would have these "
                                    "problems:"] + problems[:10], 422)
            try:
                if new_raw != raw:
                    mapedit.save_config(files().config, new_raw)
                mapedit.save_mapping(files().space_map, new_buildings, floors, new_rooms)
            except OSError as exc:
                return _page(back, [_write_error(exc)], 500)
        into = sorted(set(added))
        audit("added %d room(s) from 25Live to %s%s", len(added), ", ".join(into),
              f" (new: {', '.join(added_buildings)})" if added_buildings else "")
        message = (f"Added {len(added)} room{'' if len(added) == 1 else 's'}"
                   + (f" and {len(added_buildings)} new building"
                      f"{'' if len(added_buildings) == 1 else 's'}" if added_buildings else "")
                   + ".")
        if skipped:
            message += f" Left out {len(skipped)} with no building."
        if added_buildings and back == "map":
            message += (f" New buildings wait on the '{STAGING}' system, which writes "
                        "nothing, until you give each its schedule.")
        notice(message, "warn" if skipped else "ok")
        if back == "setup":
            return redirect(url_for("setup_step", step="schedules" if added_buildings else "rooms"))
        if len(added_buildings) == 1:
            # One building at a time: straight on to its schedule.
            index = [str(b.get("id")) for b in new_buildings].index(added_buildings[0])
            return redirect(url_for("map_form", kind="buildings", index=index))
        return redirect(url_for("map_import"))
