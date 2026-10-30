# 25Live -> BAS Schedule Sync — API tokens
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
API tokens: how a program — an MCP server giving an AI assistant access, a
script — signs in to the JSON API under /api/v1/ (see api.py).

A token is a name, a role and a secret. It may do what its role may, less
TOKEN_NEVER, and only through /api/v1/: it can't open a page of the web UI.
The role is one of web.yaml's, so the Access page decides what a token may
do just as it does for people, and changing the role changes every token
that has it at once. The built-in "AI assistant" role is the one meant for
this; it can read, run Dry run and Validate, add and change extra bookings,
and mark events low temp, but not sync.

Only a SHA-256 of each secret is kept, in state/api_tokens.json (owner-only,
like secrets.json): the secret is shown once, when the token is made, and
can't be shown again. A token can expire, and can be revoked at any time.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

FILE_NAME = "api_tokens.json"
PREFIX = "bsk_"
# What a token can never do, whatever its role: these are an administrator's,
# and a program acting on what it reads — an AI assistant reading 25Live event
# titles that anyone who books a room can write — must not be one prompt away
# from them. The API has no endpoint for any of them either; this is the
# second wall.
TOKEN_NEVER = frozenset({"force", "edit_settings", "edit_passwords", "manage_access",
                         "restart", "view_activity", "edit_map", "stop_job"})
EXPIRY_DAYS = (30, 90, 365, 0)           # 0 = never
MAX_TOKENS = 50
# How often a token's last use is written down: every request would be a
# write per call.
LAST_USED_EVERY = timedelta(minutes=10)
_NAME = re.compile(r"^[^\n\r]{1,60}$")
_ID = re.compile(r"^[0-9a-f]{8}$")
_lock = threading.Lock()


def path_for(state_dir: Path) -> Path:
    return Path(state_dir) / FILE_NAME


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def read(path: Path) -> list:
    """The tokens, each a dict; a missing or unreadable file is none."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logging.warning("[web] %s can't be read (%s); no API token works until it can",
                        path, exc)
        return []
    rows = data.get("tokens") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and _ID.match(str(r.get("id", "")))
            and isinstance(r.get("hash"), str)]


def _write(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tokens.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"tokens": rows}, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def expired(row: dict, now: Optional[datetime] = None) -> bool:
    expires = row.get("expires")
    if not expires:
        return False
    try:
        return datetime.fromisoformat(expires) <= (now or _now())
    except ValueError:
        return True                      # an expiry that can't be read has passed


def create(path: Path, name: str, role: str, days: int, by: str) -> tuple:
    """Make a token; (its row, the secret). The secret is not kept."""
    name = (name or "").strip()
    if not _NAME.match(name):
        raise ValueError("Give the token a name of up to 60 characters, so you can "
                         "tell later what uses it.")
    if days not in EXPIRY_DAYS:
        raise ValueError("Pick when the token expires.")
    secret = PREFIX + secrets.token_urlsafe(32)
    now = _now()
    with _lock:
        rows = read(path)
        if len(rows) >= MAX_TOKENS:
            raise ValueError(f"There are already {MAX_TOKENS} tokens; revoke some first.")
        if any(r.get("name") == name for r in rows):
            raise ValueError(f"There's already a token called “{name}”.")
        taken = {r["id"] for r in rows}
        token_id = secrets.token_hex(4)
        while token_id in taken:
            token_id = secrets.token_hex(4)
        row = {"id": token_id, "name": name, "role": role, "hash": _hash(secret),
               "hint": secret[-4:], "created": now.isoformat(timespec="seconds"),
               "created_by": by,
               "expires": (now + timedelta(days=days)).isoformat(timespec="seconds")
               if days else None,
               "last_used": None}
        rows.append(row)
        _write(path, rows)
    return row, secret


def revoke(path: Path, token_id: str) -> Optional[dict]:
    """Remove a token; the removed row, or None if there was none."""
    with _lock:
        rows = read(path)
        keep = [r for r in rows if r["id"] != token_id]
        if len(keep) == len(rows):
            return None
        _write(path, keep)
    return next(r for r in rows if r["id"] == token_id)


def verify(path: Path, secret: str) -> Optional[dict]:
    """The token this secret belongs to, if it's one and hasn't expired.
    Every stored hash is compared, in constant time, so the answer takes as
    long for a near miss as for nothing at all."""
    if not secret.startswith(PREFIX) or len(secret) > 200:
        return None
    wanted = _hash(secret)
    found = None
    for row in read(path):
        if hmac.compare_digest(row["hash"], wanted):
            found = row
    if found is None or expired(found):
        return None
    return found


def touch(path: Path, token_id: str) -> None:
    """Note that a token was just used, at most every LAST_USED_EVERY."""
    now = _now()
    with _lock:
        rows = read(path)
        for row in rows:
            if row["id"] != token_id:
                continue
            try:
                last = datetime.fromisoformat(row["last_used"]) if row.get("last_used") else None
            except ValueError:
                last = None
            if last is not None and now - last < LAST_USED_EVERY:
                return
            row["last_used"] = now.isoformat(timespec="seconds")
            try:
                _write(path, rows)
            except OSError as exc:
                logging.debug("[web] could not note an API token's use: %s", exc)
            return


def public(row: dict) -> dict:
    """A token as the Access page shows it: everything but the hash."""
    return {k: v for k, v in row.items() if k != "hash"}
