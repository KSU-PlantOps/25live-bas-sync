# 25Live -> BAS Schedule Sync — who may do what in the web UI
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Roles, what each may do, and the web UI's access settings.

A role is a name and a set of capabilities (CAPABILITY_LIST). Three come
built in, and are what a site gets until it changes them on the Access page:

    basic     the status page and sync history; Sync now (every system)
    advanced  everything visible; the tools (dry run, validate, discover,
              test alert), a sync of one system, stopping a job; adding and
              editing rooms, buildings, floors and extra bookings
    admin     everything, including capabilities added in later versions

A role that can sync can be limited to some systems and buildings
(`sync_only:`): its Sync now then covers only those, one at a time. The
scheduled sync always covers everything.

Who gets which role is `web.yaml`, beside config.yaml, edited on the Access
page: the roles, Microsoft Entra ID sign-in, and which Entra groups may sign
in with which role. Someone in several groups gets every capability of every
role they're in. The local password (BAS_WEB_PASSWORD) has every capability,
for setting SSO up and for getting back in when it breaks; it can be switched
off once SSO works.
"""

import re
from pathlib import Path
from typing import Optional

import yaml

from .. import mapedit

# (capability, what it allows, heading), in the order the Access page shows.
CAPABILITY_LIST = (
    ("view_basic", "See the status page and the sync history", "See"),
    ("view_all", "See everything else: jobs, the room map, extra bookings, "
                 "settings and logs (read-only)", "See"),
    ("sync", "Sync now — everything, or one system or building (a role can be "
             "limited to some, below)", "Run"),
    ("run_tools", "Dry run, Validate, Discover and Test alert", "Run"),
    ("stop_job", "Stop a running job", "Run"),
    ("force", "Force a sync past the mass-clear safety check", "Run"),
    ("edit_map", "Add and edit rooms, buildings and floors", "Change"),
    ("edit_bookings", "Add and edit extra bookings", "Change"),
    ("edit_settings", "Change the connection, systems, alerts, schedule, defaults, "
                      "safety limits and the settings files", "Administer"),
    ("edit_passwords", "Set and clear stored passwords", "Administer"),
    ("view_activity", "See the activity log: who did what", "Administer"),
    ("restart", "Restart the service, and check for updates", "Administer"),
    ("manage_access", "Change sign-in, roles and appearance — which can grant "
                      "any capability, so give it only to administrators", "Administer"),
)
ALL_CAPABILITIES = tuple(c for c, _label, _heading in CAPABILITY_LIST)
CAPABILITY_LABELS = {c: label for c, label, _heading in CAPABILITY_LIST}
# What a capability is no use without, added whenever a role is saved: every
# page but the status page needs view_all, and Force is a kind of sync.
NEEDS: dict = {c: ("view_basic", "view_all") for c in ALL_CAPABILITIES}
NEEDS.update({"view_basic": (), "view_all": ("view_basic",),
              "sync": ("view_basic",), "force": ("view_basic", "sync")})

DEFAULT_ROLES = (
    {"id": "basic", "name": "Basic", "capabilities": ["view_basic", "sync"]},
    {"id": "advanced", "name": "Advanced",
     "capabilities": ["view_basic", "view_all", "sync", "run_tools", "stop_job",
                      "edit_map", "edit_bookings"]},
    # "all" also grants capabilities added by later versions.
    {"id": "admin", "name": "Admin", "capabilities": "all"},
)
EVERYTHING = frozenset(ALL_CAPABILITIES)
_ROLE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

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


def can(capabilities, capability: str) -> bool:
    return capability in (capabilities or ())


def complete(capabilities) -> list:
    """The capabilities, with what each needs, in CAPABILITY_LIST order."""
    have = {c for c in capabilities if c in CAPABILITY_LABELS}
    for c in list(have):
        have.update(NEEDS[c])
    return [c for c in ALL_CAPABILITIES if c in have]


def role_capabilities(role: dict) -> frozenset:
    caps = role.get("capabilities")
    return EVERYTHING if caps == "all" else frozenset(caps or ())


def default_roles() -> list:
    return [dict(r, capabilities=r["capabilities"] if r["capabilities"] == "all"
                 else list(r["capabilities"])) for r in DEFAULT_ROLES]


def clean_roles(data) -> list:
    """The roles section, checked: unique ids, a name each, known capabilities
    (plus what they need). Missing or unusable, it's the built-in three."""
    if not isinstance(data, list):
        return default_roles()
    out: list = []
    for row in data:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip().lower()
        if not _ROLE_ID.match(rid) or any(r["id"] == rid for r in out):
            continue
        name = str(row.get("name") or rid).strip()[:40] or rid
        caps = row.get("capabilities")
        role: dict = {"id": rid, "name": name,
                      "capabilities": "all" if caps == "all" else complete(
                          caps if isinstance(caps, list) else [])}
        limits = clean_sync_only(row.get("sync_only"))
        if limits:
            role["sync_only"] = limits
        out.append(role)
    return out or default_roles()


# ── what a role may sync ─────────────────────────────────────────────────────

def _names(value) -> list:
    if not isinstance(value, list):
        return []
    out: list = []
    for item in value:
        text = str(item or "").strip()[:64]
        if text and "\n" not in text and text not in out:
            out.append(text)
    return out[:500]


def clean_sync_only(data) -> dict:
    """A role's sync limits: the systems and building ids it may sync. Empty
    (no key at all) means no limit."""
    if not isinstance(data, dict):
        return {}
    out = {key: _names(data.get(key)) for key in ("systems", "buildings")}
    return {k: v for k, v in out.items() if v}


def sync_scope(role_ids, settings: dict) -> Optional[dict]:
    """What someone with these roles may sync: None for everything (any of
    their roles that can sync has no limits), else {"systems", "buildings"}:
    every one any of their syncing roles is limited to."""
    scope: dict = {"systems": set(), "buildings": set()}
    for role in settings["roles"]:
        if role["id"] not in role_ids or "sync" not in role_capabilities(role):
            continue
        limits = role.get("sync_only") or {}
        if not limits:
            return None
        scope["systems"] |= set(limits.get("systems") or ())
        scope["buildings"] |= set(limits.get("buildings") or ())
    return scope


def may_sync(scope: Optional[dict], system: Optional[str] = None,
             building: Optional[str] = None) -> bool:
    """Whether a Sync now of everything (neither given), one system, or one
    building is within `scope`."""
    if scope is None:
        return True
    if system:
        return system in scope["systems"]
    if building:
        return building in scope["buildings"]
    return False


def limits_scope(limits: dict) -> dict:
    """A role's `sync_only` as a scope, for describe_scope."""
    return {"systems": set(limits.get("systems") or ()),
            "buildings": set(limits.get("buildings") or ())}


def describe_scope(scope: Optional[dict]) -> str:
    if scope is None:
        return "everything"
    parts = []
    if scope["systems"]:
        parts.append("the system" + ("s " if len(scope["systems"]) > 1 else " ")
                     + ", ".join(sorted(scope["systems"])))
    if scope["buildings"]:
        parts.append("the building" + ("s " if len(scope["buildings"]) > 1 else " ")
                     + ", ".join(sorted(scope["buildings"])))
    return " and ".join(parts) or "nothing"


def rename_building(settings: dict, old: str, new: str) -> bool:
    """Follow a renamed building in every role's sync limits; True if any
    changed."""
    changed = False
    for role in settings["roles"]:
        buildings = (role.get("sync_only") or {}).get("buildings")
        if buildings and old in buildings:
            role["sync_only"]["buildings"] = [new if b == old else b for b in buildings]
            changed = True
    return changed


def role_names(settings: dict) -> dict:
    return {r["id"]: r["name"] for r in settings["roles"]}


def new_role_id(name: str, settings: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:30] or "role"
    taken = {r["id"] for r in settings["roles"]}
    rid, n = base, 2
    while rid in taken:
        rid, n = f"{base}_{n}", n + 1
    return rid


def is_guid(value) -> bool:
    return bool(_GUID.match(str(value or "").strip()))


def empty() -> dict:
    return {"sso": {"enabled": False, "tenant_id": "", "client_id": "",
                    "authority_host": "login.microsoftonline.com",
                    "public_url": ""},
            "groups": [], "local_password": True, "roles": default_roles(),
            "updates": {"check": True}, "branding": empty_branding()}


# ── branding (the Appearance page) ───────────────────────────────────────────

DEFAULT_SITE_NAME = "25Live → BAS sync"
LOGO_MAX_BYTES = 512 * 1024
_ACCENT = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOGO_FILE = re.compile(r"^web-logo\.(png|jpg|webp)$")
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def empty_branding() -> dict:
    return {"site_name": "", "accent": "", "logo": "", "notice": "",
            "contact": {"name": "", "email": "", "phone": ""}}


def branding_from(data) -> dict:
    """The branding settings, cleaned: every value text of a sane length,
    the accent a #rrggbb colour, the logo a file name this module writes."""
    out = empty_branding()
    if not isinstance(data, dict):
        return out
    out["site_name"] = str(data.get("site_name") or "").strip()[:80]
    out["notice"] = str(data.get("notice") or "").strip()[:1000]
    accent = str(data.get("accent") or "").strip()
    out["accent"] = accent.lower() if _ACCENT.match(accent) else ""
    logo = str(data.get("logo") or "").strip()
    out["logo"] = logo if _LOGO_FILE.match(logo) else ""
    contact: dict = data["contact"] if isinstance(data.get("contact"), dict) else {}
    for key, limit in (("name", 120), ("email", 200), ("phone", 60)):
        out["contact"][key] = str(contact.get(key) or "").strip()[:limit]
    return out


def sniff_image(data: bytes) -> Optional[str]:
    """png / jpg / webp from the file's own first bytes, or None. The name
    and the browser's content type are not trusted; SVG is refused on purpose
    (it can carry script)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def accent_css(accent: str) -> str:
    """CSS custom properties for a brand colour, with black or white text on
    it — whichever reads better (WCAG relative luminance)."""
    if not _ACCENT.match(accent or ""):
        return ""
    r, g, b = (int(accent[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    luminance = 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    text = "#111111" if luminance > 0.179 else "#ffffff"
    return (f":root {{ --accent: {accent}; --accent-hi: {accent}; "
            f"--accent-text: {text}; }}\n")


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
    out["branding"] = branding_from(data.get("branding"))
    out["roles"] = clean_roles(data.get("roles"))
    updates = data["updates"] if isinstance(data.get("updates"), dict) else {}
    out["updates"]["check"] = updates.get("check", True) is not False
    for row in data.get("groups") or []:
        # A group whose role doesn't exist is kept (and shown as such on the
        # Access page) but grants nothing.
        if not isinstance(row, dict) or not str(row.get("role") or "").strip():
            continue
        group = clean_group(row.get("group"))
        if group:
            out["groups"].append({"group": group, "role": str(row["role"]).strip().lower(),
                                  "note": str(row.get("note") or "").strip()})
    return out, ""


def clean_group(value) -> str:
    """A group as it appears in the token's `groups` claim: its object ID, or
    — when the app registration emits names — its sAMAccountName (e.g.
    BAS_Admins, or KSU\\BAS_Admins) or cloud display name."""
    text = str(value or "").strip()
    return text if 0 < len(text) <= 256 and "\n" not in text else ""


def save(path: Path, settings: dict) -> bool:
    settings = dict(settings)
    # The built-in roles aren't written out while they're unchanged, so a
    # later version's new capabilities reach them.
    if settings.get("roles") == default_roles():
        settings.pop("roles")
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


def roles_for(groups, settings: dict) -> list:
    """The roles (ids, in the roles' order) the user's groups give them."""
    mine = {str(g).strip().lower() for g in groups or ()}
    wanted = {row["role"] for row in settings["groups"] if row["group"].lower() in mine}
    return [r["id"] for r in settings["roles"] if r["id"] in wanted]


def capabilities_of(role_ids, settings: dict) -> frozenset:
    """Every capability of every one of the roles."""
    out: set = set()
    for role in settings["roles"]:
        if role["id"] in role_ids:
            out |= role_capabilities(role)
    return frozenset(out)


def lockout_problem(settings: dict, sso_ready: bool) -> Optional[str]:
    """Why these settings would leave nobody able to administer the web UI."""
    if settings["local_password"]:
        return None
    if not (settings["sso"]["enabled"] and sso_ready):
        return ("The local password can only be turned off once single sign-on "
                "is on and working.")
    managers = {r["id"] for r in settings["roles"]
                if "manage_access" in role_capabilities(r)}
    if not any(row["role"] in managers for row in settings["groups"]):
        return ("With the local password off, at least one group must have a role "
                "that can change sign-in and roles, or nobody could change these "
                "settings again.")
    return None
