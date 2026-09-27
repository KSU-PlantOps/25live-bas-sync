# 25Live -> BAS Schedule Sync — passwords set from the web UI
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Passwords entered in the web UI, kept next to the safety state.

Passwords still never go in the settings files (config.yaml and friends are
the ones people back up, diff and paste into tickets). The web UI can store
one here instead: `state/secrets.json`, readable by its owner only, holding
the same variables the environment would — BAS_25LIVE_PASSWORD,
BAS_SYS_<NAME>_PASSWORD, BAS_SMTP_PASSWORD, BAS_WEB_SSO_CLIENT_SECRET.

The environment always wins: a variable set in .env (or the scheduled task's
environment) is used as it is, and the web UI shows it as set there.
load_credentials() fills in any that the environment lacks from this file, so
every way of running the sync — the service, a one-shot `sync`, cron — sees
the same passwords.

BAS_WEB_PASSWORD is deliberately not storable: it is the way back in when
single sign-on is misconfigured, so it lives outside anything the web UI can
change.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from . import paths

STORE_NAME = "secrets.json"
_ALLOWED = re.compile(r"^(BAS_25LIVE_PASSWORD|BAS_SMTP_PASSWORD|BAS_NIAGARA_PASSWORD|"
                      r"BAS_WEB_SSO_CLIENT_SECRET|BAS_SYS_[A-Z0-9_]+_PASSWORD)$")


def allowed(name: str) -> bool:
    return bool(_ALLOWED.match(name or ""))


def store_file(cfg: Optional[dict] = None, state_dir: Optional[Path] = None) -> Path:
    """state/secrets.json, beside wherever the safety state lives."""
    if state_dir is not None:
        return Path(state_dir) / STORE_NAME
    state_file = ((cfg or {}).get("safety") or {}).get("state_file")
    base = Path(state_file).parent if state_file else paths.state_dir()
    return base / STORE_NAME


def read(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if allowed(k) and isinstance(v, str) and v}


def write(path: Path, name: str, value: Optional[str]) -> None:
    """Set `name` (or remove it, for a blank value). Owner-only, atomic."""
    if not allowed(name):
        raise ValueError(f"{name} can't be stored here")
    data = read(path)
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".secrets.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fill_environ(path: Path, environ=None) -> list:
    """Copy stored secrets into the environment where it has none; returns
    the names filled in."""
    environ = os.environ if environ is None else environ
    filled = []
    for name, value in read(path).items():
        if not environ.get(name):
            environ[name] = value
            filled.append(name)
    return filled


def source(name: str, path: Path, environ=None) -> Optional[str]:
    """Where a secret comes from: "environment", "stored", or None."""
    environ = os.environ if environ is None else environ
    if environ.get(name):
        return "environment"
    if name in read(path):
        return "stored"
    return None


def get(name: str, path: Path, environ=None) -> Optional[str]:
    environ = os.environ if environ is None else environ
    return environ.get(name) or read(path).get(name)
