# 25Live -> BAS Schedule Sync — what discovery found, for the setup guide
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The rooms `--discover` found in 25Live, kept in state/discovery.json so the
web UI's setup guide can offer them for import, and the guesses that make an
import quick: which building each room is in, and an id for that building.

25Live says which building a room is in only on some instances. When it
doesn't, the building is guessed from the room names — "Science Hall 101"
and "Science Hall 204 (Chem lab)" are both in "Science Hall". A guess is only
a starting point: the setup guide shows it in an editable box.
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

FILE_NAME = "discovery.json"

# Words that end a building name rather than belonging to it: "Science Hall
# Room 101" is in Science Hall.
_ROOM_WORDS = {"room", "rm", "no", "number", "suite", "ste", "lab", "floor",
               "fl", "level", "lvl", "unit", "space", "bldg"}
# Too common to make two rooms the same building on their own.
_STOP_WORDS = {"the", "a", "an", "of", "and", "at", "main", "new", "old",
               "north", "south", "east", "west", "upper", "lower"}


def path_in(state_dir: Path) -> Path:
    return Path(state_dir) / FILE_NAME


# What a discovery's list is: every space 25Live let the account see; only
# those with bookings in the window, because that was asked for, or because
# listing every space failed (the job's log says why); or "booked", a list
# kept before discovery could list every space.
LISTINGS = ("every", "booked-only", "booked-fallback", "booked")


def save(state_dir: Path, days: int, spaces: list,
         now: Optional[datetime] = None, listing: str = "booked") -> Path:
    """Write what a discovery found, atomically, for the web UI to read."""
    path = path_in(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    when = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    body = json.dumps({"when": when, "days": days, "listing": listing,
                       "spaces": list(spaces)}, indent=1)
    fd, tmp = tempfile.mkstemp(prefix=".discovery.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def load(state_dir: Path) -> Optional[dict]:
    """{when, days, listing, spaces} from the last discovery, or None. Each
    space is cleaned to space_id, space_name, formal_name, building, capacity
    and bookings, whatever the file holds."""
    try:
        data = json.loads(path_in(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("spaces"), list):
        return None
    spaces = []
    for s in data["spaces"]:
        if not isinstance(s, dict) or str(s.get("space_id") or "").strip() == "":
            continue
        spaces.append({
            "space_id": str(s["space_id"]).strip(),
            "space_name": str(s.get("space_name") or s["space_id"]).strip(),
            "formal_name": str(s.get("formal_name") or "").strip(),
            "building": str(s.get("building") or "").strip(),
            "capacity": _int_or_none(s.get("capacity")),
            "bookings": _int_or_none(s.get("bookings")) or 0,
        })
    listing = data.get("listing") if data.get("listing") in LISTINGS else "booked"
    return {"when": str(data.get("when") or ""), "days": _int_or_none(data.get("days")),
            "listing": listing, "spaces": spaces}


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── guessing buildings ───────────────────────────────────────────────────────

def _words(name: str) -> list:
    """The name without anything in brackets, as words."""
    return re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name or "").split()


def _tidy(words: list) -> list:
    """Drop trailing room words and punctuation: "Science Hall, Room" ->
    "Science Hall"."""
    words = list(words)
    while words:
        last = words[-1].strip(".,:;-–—#/")
        if not last or last.lower() in _ROOM_WORDS:
            words.pop()
            continue
        words[-1] = last
        break
    return words


def guess_from_name(name: str) -> str:
    """The building a room name starts with: the words before its room
    number. "" when the name has no number after some words."""
    words = _words(name)
    for i, word in enumerate(words):
        if any(ch.isdigit() for ch in word):
            return " ".join(_tidy(words[:i]))
    return ""


def _significant(words: list) -> bool:
    return any(w.lower() not in _STOP_WORDS for w in words)


def guess_buildings(spaces: list) -> dict:
    """{space_id: building name} for every space: 25Live's own building
    when it gave one, else a guess from the names, else "".

    A name with a room number gives its building directly. A name without
    one ("Student Center Ballroom") takes the longest building already
    guessed that it starts with, or the longest start it shares with
    another such name ("Student Center Lounge")."""
    out: dict = {}
    pending: list = []
    for s in spaces:
        sid = str(s.get("space_id"))
        if s.get("building"):
            out[sid] = str(s["building"])
            continue
        name = s.get("formal_name") or s.get("space_name") or ""
        guess = guess_from_name(name)
        if guess:
            out[sid] = guess
        else:
            pending.append((sid, _words(name)))
    known = sorted({tuple(b.split()) for b in out.values() if b.split()},
                   key=len, reverse=True)
    for sid, words in pending:
        lowered = [w.lower() for w in words]
        match = next((" ".join(k) for k in known
                      if len(k) < len(words)
                      and [w.lower() for w in k] == lowered[:len(k)]), "")
        if not match:
            best: list = []
            for other_sid, other in pending:
                if other_sid == sid:
                    continue
                shared = []
                for a, b in zip(words, other, strict=False):
                    if a.lower() != b.lower():
                        break
                    shared.append(a)
                shared = _tidy(shared)
                if len(shared) < len(words) and _significant(shared) \
                        and len(shared) > len(best):
                    best = shared
            match = " ".join(best)
        out[sid] = match
    return out


def building_id(name: str, taken=()) -> str:
    """A room-map building id for a building name, in the style of the
    example map (science_hall), not clashing with any in `taken`."""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:40] or "building"
    used = {str(t) for t in taken}
    bid, n = base, 2
    while bid in used:
        bid, n = f"{base}_{n}", n + 1
    return bid
