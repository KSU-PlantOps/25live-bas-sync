# 25Live -> BAS Schedule Sync — who may do what in the web UI
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Roles, what each may do, and the web UI's access settings.

Three roles, each including the one before:

    basic     the status page and sync history; Sync now (every system)
    advanced  everything visible; the tools (dry run, validate, discover,
              test alert), a sync of one system, stopping a job; adding and
              editing rooms, buildings and floors
    admin     full control: connection, systems, passwords, alerts, defaults,
              schedule, the raw files, access and sign-in settings, and
              Force (overriding the mass-clear safety check)

Who gets which role is `web.yaml`, beside config.yaml, edited on the Access
page: Microsoft Entra ID sign-in, and which Entra groups may sign in with
which role. The local password (BAS_WEB_PASSWORD) is Admin, for setting SSO up
and for getting back in when it breaks; it can be switched off once SSO works.
"""

import re
from pathlib import Path
from typing import Optional

import yaml

from .. import mapedit

ROLES = ("basic", "advanced", "admin")
ROLE_LABELS = {"basic": "Basic", "advanced": "Advanced", "admin": "Admin"}

_BASIC = {"view_basic", "sync"}
_ADVANCED = _BASIC | {"view_all", "run_tools", "stop_job", "edit_map"}
_ADMIN = _ADVANCED | {"force", "admin"}
CAPABILITIES = {"basic": _BASIC, "advanced": _ADVANCED, "admin": _ADMIN}

AUTHORITY_HOSTS = {
    "login.microsoftonline.com": "Global (commercial and education tenants)",
    "login.microsoftonline.us": "US Government (GCC High, DoD)",
}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

WEB_HEADER = """\
# Who may use the 25Live -> BAS sync's web UI, and how they sign in. Managed on
# the web UI's Access page; plain YAML, safe to hand-edit. The SSO client
# secret is NOT here: it is stored in the service's state folder, or comes
# from BAS_WEB_SSO_CLIENT_SECRET.
"""


def can(role: Optional[str], capability: str) -> bool:
    return capability in CAPABILITIES.get(role or "", set())


def is_guid(value) -> bool:
    return bool(_GUID.match(str(value or "").strip()))


def empty() -> dict:
    return {"sso": {"enabled": False, "tenant_id": "", "client_id": "",
                    "authority_host": "login.microsoftonline.com",
                    "public_url": ""},
            "groups": [], "local_password": True}


def load(path: Path) -> tuple:
    """(settings, error). A missing file is the defaults; one that won't
    parse is the defaults with the error, and SSO stays off until it's fixed."""
    out = empty()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return out, ""
    except OSError as exc:
        return out, f"{path}: {exc}"
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return out, f"{path}: {exc}"
    if not isinstance(data, dict):
        return out, f"{path}: the top level is not a mapping"
    sso: dict = data["sso"] if isinstance(data.get("sso"), dict) else {}
    for key in ("tenant_id", "client_id", "public_url"):
        out["sso"][key] = str(sso.get(key) or "").strip()
    host = str(sso.get("authority_host") or "").strip()
    if host in AUTHORITY_HOSTS:
        out["sso"]["authority_host"] = host
    out["sso"]["enabled"] = sso.get("enabled") is True
    out["local_password"] = data.get("local_password", True) is not False
    for row in data.get("groups") or []:
        if not isinstance(row, dict) or row.get("role") not in ROLES:
            continue
        group = clean_group(row.get("group"))
        if group:
            out["groups"].append({"group": group, "role": row["role"],
                                  "note": str(row.get("note") or "").strip()})
    return out, ""


def clean_group(value) -> str:
    """A group as it appears in the token's `groups` claim: its object ID, or
    — when the app registration emits names — its sAMAccountName (e.g.
    BAS_Admins, or KSU\\BAS_Admins) or cloud display name."""
    text = str(value or "").strip()
    return text if 0 < len(text) <= 256 and "\n" not in text else ""


def save(path: Path, settings: dict) -> bool:
    body = yaml.safe_dump(settings, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    return mapedit.write_if_changed(path, WEB_HEADER + "\n" + body)


def sso_problems(settings: dict, has_secret: bool) -> list:
    """What stops SSO from working, as sentences; empty when it can."""
    sso = settings["sso"]
    out = []
    if not is_guid(sso["tenant_id"]):
        out.append("The tenant ID must be your directory's ID (a GUID), from "
                   "Entra ID → Overview.")
    if not is_guid(sso["client_id"]):
        out.append("The client ID must be the app registration's Application "
                   "(client) ID (a GUID).")
    if not has_secret:
        out.append("No client secret is set.")
    if not settings["groups"]:
        out.append("No groups may sign in yet.")
    return out


def matching(groups, settings: dict) -> list:
    """The configured groups (as configured) this user is in. Compared without
    regard to case: GUIDs and AD names are case-insensitive."""
    mine = {str(g).strip().lower() for g in groups or ()}
    return [row["group"] for row in settings["groups"] if row["group"].lower() in mine]


def role_for(groups, settings: dict) -> Optional[str]:
    """The highest role any of the user's groups has; None if none may sign in."""
    mine = {str(g).strip().lower() for g in groups or ()}
    best = None
    for row in settings["groups"]:
        if row["group"].lower() in mine and (
                best is None or ROLES.index(row["role"]) > ROLES.index(best)):
            best = row["role"]
    return best


def lockout_problem(settings: dict, sso_ready: bool) -> Optional[str]:
    """Why these settings would leave nobody able to administer the web UI."""
    if settings["local_password"]:
        return None
    if not (settings["sso"]["enabled"] and sso_ready):
        return ("The local password can only be turned off once single sign-on "
                "is on and working.")
    if not any(row["role"] == "admin" for row in settings["groups"]):
        return ("With the local password off, at least one group must have the "
                "Admin role, or nobody could change these settings again.")
    return None
