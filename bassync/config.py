# 25Live -> BAS Schedule Sync — configuration
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Configuration loading, validation and secret handling.

Three files, deliberately separated by who owns them:

    config.yaml     IT/controls: 25Live endpoint, BAS systems, credentials,
                    retries, alerting, safety limits.
    defaults.yaml   Operators: run-up / run-down / merge-gap / lookahead.
    space_mapping.yaml  The room -> schedule cross-reference.

Passwords never live in any of them; see load_credentials().

Everything is validated when it is loaded. A wrong type or an impossible value
(`timezone: America/NewYork`, `smtp_port: 587x`, a negative buffer) is a
ConfigError naming the key, raised before anything talks to 25Live or a BAS —
not a traceback from deep inside the run. A key the sync does not recognise is
a warning, because a typo (`notify_on_sucess`) otherwise just silently does
nothing.
"""

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import paths

# Sentinel left in the defaults so a forgotten password is obvious (and warned
# about) rather than silently sent as the literal string "CHANGE_ME".
PLACEHOLDER_PASSWORD = "CHANGE_ME"

# For CollegeNET-hosted 25Live the WebServices base URL is built from the
# instance name. Self-hosted sites set collegenet.base_url directly instead.
COLLEGENET_URL_TEMPLATE = "https://webservices.collegenet.com/r25ws/wrd/{instance}/run"

STATE_PARAM_STYLES = ("plus", "space", "comma", "repeat", "none")
MAP_ERROR_POLICIES = ("skip", "abort")
WEBHOOK_FORMATS = ("slack", "teams", "generic")
EMAIL_REPORT_LEVELS = ("full", "summary")
SMTP_SECURITY = ("starttls", "ssl", "tls", "smtps", "none", "plain")


DEFAULTS: dict[str, Any] = {
    # ── CollegeNET 25Live Series25 WebServices (XML API) ──
    "collegenet": {
        "instance": "",                       # your 25Live instance name
        "base_url": "",                       # blank -> built from `instance`
        "username": "",                       # a LOCAL 25Live account (not SSO)
        "password": PLACEHOLDER_PASSWORD,     # set env var BAS_25LIVE_PASSWORD
        "lookahead_days": 7,
        "include_states": [2],                # 2=confirmed (add 4 for tentative)
        # How the `state` filter is encoded in the query string. Series25
        # instances differ; `--validate` probes the alternatives when the
        # configured one returns nothing. See bassync/collegenet.py.
        "state_param_style": "plus",          # plus | space | comma | repeat | none
        # Individual occurrences of a recurring event can be cancelled while
        # the event itself stays confirmed. 99 is Series25's "cancelled"
        # reservation state.
        "exclude_reservation_states": [99],
        "default_pre_condition_minutes": 30,
        "default_post_buffer_minutes": 15,
        "merge_gap_minutes": 5,
    },

    # ── BAS systems this campus writes to ──
    # Each key is a system name that space_mapping.yaml can reference. The
    # `driver` picks the integration; every other key is passed to that driver.
    # A pre-1.0 config's top-level `niagara:` block is folded in here by
    # migrate_legacy_systems(), onto the (deprecated) niagara driver.
    "systems": {},

    # System used by any building/room that doesn't name one. Blank means "the
    # only system defined", which keeps single-BAS configs free of boilerplate.
    "default_system": "",

    "timezone": "America/New_York",           # campus timezone (IANA name)

    # ── Resilience: retry transient 25Live/BAS errors (timeouts, 5xx) ──
    # Applied to reads and idempotent clears; writes are not auto-retried, so a
    # retry can never duplicate a schedule entry.
    "retry": {
        "attempts": 3,
        "backoff_seconds": 2.0,
    },

    # ── Safety rails ──
    # A sync that writes empty schedules everywhere turns off HVAC campus-wide.
    # That is exactly what a bad credential, a changed `state` parameter, or an
    # API version bump looks like, so the run refuses to do it unless the drop
    # is small or an operator passes --force. See bassync/safety.py.
    "safety": {
        "enabled": True,
        "min_events": 1,                # abort if 25Live returned fewer events
        "max_cleared_fraction": 0.34,   # abort if more than this share of
                                        #   previously-occupied schedules would
                                        #   be emptied in one run
        "state_file": "",               # blank -> state/last_run.json
        # What a broken row in space_mapping.yaml does to the run:
        #   skip   report it, leave that row's schedule — and the floor and
        #          building schedules it rolls up into — untouched, sync the
        #          rest of the campus, and exit non-zero so it alerts
        #   abort  write nothing anywhere until the map is fixed
        "on_map_errors": "skip",
    },

    # ── Alerting and reports ──
    "alerts": {
        "enabled": False,
        "notify_on_success": False,           # every channel, unless it overrides
        "webhook_url": "",                    # or $BAS_ALERT_WEBHOOK_URL
        "webhook_format": "slack",            # slack | teams | generic
        "webhook_notify_on_success": None,    # None -> notify_on_success
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "security": "",                   # starttls | ssl | none; blank ->
                                              #   by port (465 ssl, else starttls)
            "use_tls": None,                  # pre-1.0 boolean, still honored
            "username": "",                   # SMTP user (password: BAS_SMTP_PASSWORD)
            "from_addr": "",
            "to_addrs": [],
            "notify_on_success": None,        # None -> alerts.notify_on_success
            "report": "full",                 # full | summary
            "attach_csv": True,               # every scheduled window as a CSV
            "subject_prefix": "[25Live sync]",
        },
    },

    # ── Dead-man's switch ──
    # Alerts only fire when the job runs. These are pinged by the job itself,
    # so a monitoring service (healthchecks.io, Uptime Kuma push monitors,
    # Cronitor, ...) can alarm when the pings STOP — an expired service
    # password, a rebuilt host, a disabled scheduled task.
    "monitoring": {
        "ping_url": "",                       # GET after every successful live run
        "ping_fail_url": "",                  # GET after a failed live run
    },

    # ── When the service runs the sync ──
    # Used by `bas-sync-service` — what the Docker container runs, with the web
    # UI. Local times in `timezone`, 24-hour. The one-shot CLI ignores this;
    # schedule it with cron or Task Scheduler instead. SYNC_AT in the
    # environment, when set, overrides `times`.
    "schedule": {
        "enabled": True,
        "times": ["02:00"],
        "run_on_start": False,                # also run once when it starts
    },

    "space_map_file": None,                   # None -> paths.space_map_file()
    "extra_bookings_file": None,              # None -> extra_bookings.yaml beside
                                              #   config.yaml (bassync/extras.py)
    "low_temp_file": None,                    # None -> low_temp_events.yaml beside
                                              #   config.yaml (bassync/lowtemp.py)
    "log_file": None,                         # None -> default_log_file()
    "log_max_mb": 10,                         # rotate the log at this size
    "log_backups": 10,                        # ...keeping this many old files
}


def default_log_file() -> str:
    """Default log path: a logs/ folder next to the project (portable across
    OSes). setup_logging() falls back to stdout if it isn't writable, so this
    never blocks a run."""
    return str(paths.log_file())


def default_state_file() -> str:
    """Where the safety rail remembers the previous run's schedule sizes."""
    return str(paths.state_file())


class ConfigError(Exception):
    """A YAML file (config / defaults / room map) is unreadable, malformed, not
    a mapping, or holds a value the sync cannot use. Carries a human-readable,
    file-named message so callers can report one clean line instead of a raw
    traceback."""


def read_yaml(path) -> dict:
    """
    Load a YAML file into a dict. A missing or empty file yields {}. A parse
    error, an unreadable file, or a top level that isn't a mapping raises
    ConfigError naming the file — so a stray tab in config.yaml fails with one
    clear line instead of a stack trace at 2 AM.
    """
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"{p}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{p}: expected a YAML mapping at the top level, got "
            f"{type(data).__name__}.")
    return data


def _deep_merge(base: dict, override: dict) -> None:
    """
    Recursively merge `override` into `base` in place (nested dicts merged,
    scalars/lists replaced).

    A section left empty in YAML (`alerts:` with nothing under it) parses as
    None. That means "nothing to override", not "replace the whole section
    with None" — the latter used to crash the run on the first lookup.
    """
    for key, val in override.items():
        if val is None and isinstance(base.get(key), dict):
            continue
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


# Global scheduling defaults live in defaults.yaml — a small, GUI-editable file.
# These flat keys map onto the internal config so the rest of the code is
# unchanged.
DEFAULTS_FILE_MAP = {
    "pre_condition_minutes": "default_pre_condition_minutes",
    "post_buffer_minutes":   "default_post_buffer_minutes",
    "merge_gap_minutes":     "merge_gap_minutes",
    "lookahead_days":        "lookahead_days",
}


# Keys of the pre-1.0 top-level `niagara:` block. When a config has that and no
# `systems:`, it is folded into a system named "niagara" so existing
# deployments upgrade without touching their YAML.
LEGACY_NIAGARA_KEYS = {
    "host", "port", "https", "username", "password", "verify_tls",
    "schedule_base_path", "heartbeat_path", "rest_base",
}


def migrate_legacy_systems(cfg: dict, errors: Optional[list] = None) -> None:
    """
    Fold a pre-1.0 top-level `niagara:` block into `systems:` in place.

    Before 1.0 there was exactly one BAS and its settings lived under
    `niagara:`. Rather than force every existing site to rewrite config.yaml,
    that block is promoted to `systems: {niagara: {driver: niagara, ...}}` and
    becomes the default system. An explicit `systems:` block always wins; the
    two can coexist while a site migrates.
    """
    legacy = cfg.pop("niagara", None)
    if not legacy:
        return
    systems = cfg.setdefault("systems", {})
    if not isinstance(legacy, dict) or not isinstance(systems, dict):
        # Reported, not raised: validate_config names a non-mapping systems:.
        if errors is not None and not isinstance(legacy, dict):
            errors.append("`niagara:` (the pre-1.0 block) must be a mapping of "
                          "settings.")
        return
    if "niagara" in systems:
        # An explicit systems: entry of the same name takes precedence; the
        # legacy block only fills gaps it didn't specify.
        merged = {"driver": "niagara", **legacy}
        merged.update(systems["niagara"])
        systems["niagara"] = merged
    else:
        systems["niagara"] = {"driver": "niagara", **legacy}
    if not cfg.get("default_system"):
        cfg["default_system"] = "niagara"


def resolve_default_system(cfg: dict) -> str:
    """
    The system a building/room gets when it doesn't name one.

    An explicit `default_system` wins. Otherwise, if exactly one system is
    defined, that is unambiguous and becomes the default — so single-BAS sites
    never have to name it. With several systems and no default, spaces must say
    which one they mean; that is reported as a mapping error rather than
    guessed at, because guessing writes occupancy to the wrong building.
    """
    explicit = (cfg.get("default_system") or "").strip()
    if explicit:
        return explicit
    systems = cfg.get("systems") or {}
    if len(systems) == 1:
        return next(iter(systems))
    return ""


def load_config(path: str, defaults_path: Optional[str] = None,
                warnings: Optional[list] = None) -> dict:
    """
    Build the runtime config: a deep copy of DEFAULTS, with config.yaml merged
    over it, then defaults.yaml applied on top of the scheduling knobs.

    A missing file just leaves the built-ins in place. Raises ConfigError if a
    file exists but is unreadable or malformed, or if any value fails
    validation — better a clean refusal than a run that silently falls back to
    defaults and writes the wrong schedules. Secrets are applied separately by
    load_credentials().

    Non-fatal findings (unknown keys, mostly typos) are appended to
    `warnings` when a list is passed, so the caller can log them once logging
    is up.
    """
    found: list = []
    cfg = copy.deepcopy(DEFAULTS)
    user = read_yaml(path)
    if user:
        found.extend(_unknown_keys(user, CONFIG_SCHEMA, str(path)))
        _deep_merge(cfg, user)

    errors: list = []
    if defaults_path:
        gd = read_yaml(defaults_path)
        for key in gd:
            if key not in DEFAULTS_FILE_MAP:
                found.append(f"{defaults_path}: unknown key `{key}` — ignored "
                             f"(known: {', '.join(DEFAULTS_FILE_MAP)}).")
        # Checked here, under the names the operator actually typed, so a bad
        # value in defaults.yaml isn't reported as an internal config key.
        where = f"{defaults_path}: "
        for file_key, cfg_key in DEFAULTS_FILE_MAP.items():
            if gd.get(file_key) is None:
                continue
            checked = {file_key: gd[file_key]}
            problems: list = []
            _as_int(checked, file_key, where, problems,
                    minimum=1 if file_key == "lookahead_days" else 0,
                    maximum=366 if file_key == "lookahead_days" else 1440)
            if problems:
                errors.extend(problems)
            else:
                cfg["collegenet"][cfg_key] = checked[file_key]

    migrate_legacy_systems(cfg, errors)

    errors.extend(validate_config(cfg, found))
    if errors:
        raise ConfigError(
            f"{path}: {len(errors)} problem(s):\n  - " + "\n  - ".join(errors))

    cn = cfg["collegenet"]
    if not cn.get("base_url"):
        instance = (cn.get("instance") or "").strip()
        if instance:
            cn["base_url"] = COLLEGENET_URL_TEMPLATE.format(instance=instance)

    if not cfg["safety"].get("state_file"):
        cfg["safety"]["state_file"] = default_state_file()
    if not cfg.get("space_map_file"):
        cfg["space_map_file"] = str(paths.space_map_file())
    if not cfg.get("extra_bookings_file"):
        cfg["extra_bookings_file"] = str(Path(path).parent / "extra_bookings.yaml")
    if not cfg.get("low_temp_file"):
        cfg["low_temp_file"] = str(Path(path).parent / "low_temp_events.yaml")
    if warnings is not None:
        warnings.extend(found)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

# Known keys per section. A dict value means "a section with these keys"; the
# string "*" means "any mapping" (checked elsewhere, e.g. systems: per driver).
CONFIG_SCHEMA: dict = {
    "collegenet": {k: None for k in (
        "instance", "base_url", "username", "password", "lookahead_days",
        "include_states", "state_param_style", "exclude_reservation_states",
        "default_pre_condition_minutes", "default_post_buffer_minutes",
        "merge_gap_minutes", "verify_tls")},
    "systems": "*",
    "default_system": None,
    "timezone": None,
    "retry": {"attempts": None, "backoff_seconds": None},
    "safety": {k: None for k in (
        "enabled", "min_events", "max_cleared_fraction", "state_file",
        "on_map_errors")},
    "alerts": {
        "enabled": None, "notify_on_success": None, "webhook_url": None,
        "webhook_format": None, "webhook_notify_on_success": None,
        "email": {k: None for k in (
            "enabled", "smtp_host", "smtp_port", "security", "use_tls",
            "username", "from_addr", "to_addrs", "notify_on_success",
            "report", "attach_csv", "subject_prefix")},
    },
    "monitoring": {"ping_url": None, "ping_fail_url": None},
    "schedule": {"enabled": None, "times": None, "run_on_start": None},
    "space_map_file": None,
    "extra_bookings_file": None,
    "low_temp_file": None,
    "log_file": None,
    "log_max_mb": None,
    "log_backups": None,
    "niagara": "*",                      # pre-1.0 block, migrated
}


def _unknown_keys(data: dict, schema: dict, where: str, prefix: str = "") -> list:
    out = []
    for key, val in data.items():
        if key not in schema:
            out.append(f"{where}: unknown key `{prefix}{key}` — ignored. Check "
                       "the spelling against config.example.yaml.")
            continue
        sub = schema[key]
        if isinstance(sub, dict) and isinstance(val, dict):
            out.extend(_unknown_keys(val, sub, where, f"{prefix}{key}."))
    return out


def _as_int(section: dict, key: str, where: str, errors: list,
            minimum: Optional[int] = None, maximum: Optional[int] = None) -> None:
    """Validate (and normalise in place) an integer setting."""
    value = section.get(key)
    if value is None:
        return
    if isinstance(value, bool):
        errors.append(f"{where}{key} must be a whole number, got {value!r}.")
        return
    try:
        number = int(value)
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{where}{key} must be a whole number, got {value!r}.")
        return
    if minimum is not None and number < minimum:
        errors.append(f"{where}{key} must be at least {minimum}, got {number}.")
        return
    if maximum is not None and number > maximum:
        errors.append(f"{where}{key} must be at most {maximum}, got {number}.")
        return
    section[key] = number


def _as_float(section: dict, key: str, where: str, errors: list,
              minimum: Optional[float] = None,
              maximum: Optional[float] = None) -> None:
    value = section.get(key)
    if value is None:
        return
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
    except (TypeError, ValueError):
        errors.append(f"{where}{key} must be a number, got {value!r}.")
        return
    if minimum is not None and number < minimum:
        errors.append(f"{where}{key} must be at least {minimum:g}, got {number:g}.")
        return
    if maximum is not None and number > maximum:
        errors.append(f"{where}{key} must be at most {maximum:g}, got {number:g}.")
        return
    section[key] = number


def _as_bool(section: dict, key: str, where: str, errors: list) -> None:
    value = section.get(key)
    if value is None or isinstance(value, bool):
        return
    errors.append(f"{where}{key} must be true or false, got {value!r}.")


def _as_verify(section: dict, key: str, where: str, errors: list,
               warnings: list) -> None:
    """TLS verification: true, false, or the path of a CA bundle. A quoted
    "true" / "false" is taken as meant, rather than as a file name, and a
    blank one as not set — verifying — where it used to turn checking off."""
    value = section.get(key)
    if isinstance(value, bool):
        return
    if value is None:
        section.pop(key, None)             # `verify_tls:` alone: not set
        return
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ("true", "yes", "1", "false", "no", "0"):
            section[key] = text.lower() in ("true", "yes", "1")
            return
        if not text:
            section.pop(key)
            warnings.append(f"{where}{key} is blank, so TLS certificates are verified "
                            "(the default). Set it to false to turn that off, or to "
                            "a CA bundle's path.")
            return
        section[key] = text
        return
    errors.append(f"{where}{key} must be true, false, or the path of a CA bundle, "
                  f"got {value!r}.")


def _as_choice(section: dict, key: str, where: str, errors: list,
               choices: tuple) -> None:
    value = section.get(key)
    if value in (None, ""):
        return
    norm = str(value).strip().lower()
    if norm not in choices:
        errors.append(f"{where}{key} must be one of {', '.join(choices)}, "
                      f"got {value!r}.")
        return
    section[key] = norm


def _int_list(section: dict, key: str, where: str, errors: list) -> None:
    value = section.get(key)
    if value is None:
        return
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        errors.append(f"{where}{key} must be a list of numbers, got {value!r}.")
        return
    try:
        section[key] = [int(v) for v in value]
    except (TypeError, ValueError):
        errors.append(f"{where}{key} must be a list of numbers, got {value!r}.")


def _as_str(section: dict, key: str, where: str, errors: list) -> None:
    """A text setting. Numbers are accepted and kept as text (YAML reads an
    unquoted `default_system: 5` as an int); anything else is an error."""
    value = section.get(key)
    if value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        section[key] = str(value)
        return
    errors.append(f"{where}{key} must be text, got {value!r}.")


def parse_hhmm(value) -> str:
    """'2:00', '02:00' or 120 -> '02:00'. Raises ValueError otherwise.

    The number is there because YAML 1.1 reads an unquoted `02:00` as the
    base-60 integer 120, so `times: [02:00]` arrives as minutes past midnight.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value < 24 * 60:
            return f"{value // 60:02d}:{value % 60:02d}"
        raise ValueError(f"{value!r} is not a 24-hour HH:MM time")
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", str(value or ""))
    if not m:
        raise ValueError(f"{value!r} is not a 24-hour HH:MM time")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _time_list(section: dict, key: str, where: str, errors: list) -> None:
    """A list of HH:MM times, normalised to sorted, distinct 'HH:MM' text."""
    value = section.get(key)
    if value is None:
        return
    if not isinstance(value, list):
        value = [value]
    try:
        section[key] = sorted({parse_hhmm(v) for v in value})
    except ValueError as exc:
        errors.append(f"{where}{key}: {exc}, e.g. \"02:00\".")


def check_timezone(value, where: str, errors: list) -> None:
    """IANA zone names only. A typo here used to surface as a
    ZoneInfoNotFoundError traceback from inside --validate itself."""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}timezone must be an IANA name such as "
                      f"America/New_York, got {value!r}.")
        return
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(
            f"{where}timezone {value!r} is not a known IANA timezone (e.g. "
            "America/New_York, America/Chicago). On Windows, make sure the "
            "`tzdata` package is installed for this Python.")


def validate_config(cfg: dict, warnings: Optional[list] = None) -> list:
    """
    Every problem with a merged config, as a list of messages (empty when
    usable). Normalises numeric strings in place, so `lookahead_days: "7"`
    is accepted and stored as 7.
    """
    errors: list = []
    warnings = warnings if warnings is not None else []

    for section in ("collegenet", "retry", "safety", "alerts", "monitoring",
                    "schedule"):
        if not isinstance(cfg.get(section), dict):
            errors.append(f"`{section}:` must be a mapping of settings, got "
                          f"{type(cfg.get(section)).__name__}.")
    if cfg.get("systems") is not None and not isinstance(cfg.get("systems"), dict):
        errors.append("`systems:` must be a mapping of system name -> settings.")
    if errors:
        return errors                      # the rest assumes the shapes

    check_timezone(cfg.get("timezone"), "", errors)
    for key in ("default_system", "space_map_file", "extra_bookings_file",
                "low_temp_file", "log_file"):
        _as_str(cfg, key, "", errors)

    cn = cfg["collegenet"]
    w = "collegenet."
    for key in ("instance", "base_url", "username", "password"):
        _as_str(cn, key, w, errors)
    _as_int(cn, "lookahead_days", w, errors, minimum=1, maximum=366)
    _as_int(cn, "default_pre_condition_minutes", w, errors, minimum=0, maximum=1440)
    _as_int(cn, "default_post_buffer_minutes", w, errors, minimum=0, maximum=1440)
    _as_int(cn, "merge_gap_minutes", w, errors, minimum=0, maximum=1440)
    _int_list(cn, "include_states", w, errors)
    _int_list(cn, "exclude_reservation_states", w, errors)
    _as_choice(cn, "state_param_style", w, errors, STATE_PARAM_STYLES)
    _as_verify(cn, "verify_tls", w, errors, warnings)

    retry = cfg["retry"]
    _as_int(retry, "attempts", "retry.", errors, minimum=0, maximum=20)
    _as_float(retry, "backoff_seconds", "retry.", errors, minimum=0, maximum=300)

    safety = cfg["safety"]
    _as_str(safety, "state_file", "safety.", errors)
    _as_bool(safety, "enabled", "safety.", errors)
    _as_int(safety, "min_events", "safety.", errors, minimum=0)
    _as_float(safety, "max_cleared_fraction", "safety.", errors, minimum=0, maximum=1)
    _as_choice(safety, "on_map_errors", "safety.", errors, MAP_ERROR_POLICIES)

    alerts = cfg["alerts"]
    _as_str(alerts, "webhook_url", "alerts.", errors)
    _as_bool(alerts, "enabled", "alerts.", errors)
    _as_bool(alerts, "notify_on_success", "alerts.", errors)
    _as_bool(alerts, "webhook_notify_on_success", "alerts.", errors)
    _as_choice(alerts, "webhook_format", "alerts.", errors, WEBHOOK_FORMATS)
    email = alerts.get("email")
    if email is not None and not isinstance(email, dict):
        errors.append("alerts.email must be a mapping of settings.")
    elif email:
        w = "alerts.email."
        for key in ("smtp_host", "username", "from_addr", "subject_prefix"):
            _as_str(email, key, w, errors)
        _as_bool(email, "enabled", w, errors)
        _as_int(email, "smtp_port", w, errors, minimum=1, maximum=65535)
        _as_choice(email, "security", w, errors, SMTP_SECURITY)
        _as_bool(email, "notify_on_success", w, errors)
        _as_bool(email, "attach_csv", w, errors)
        _as_choice(email, "report", w, errors, EMAIL_REPORT_LEVELS)
        to_addrs = email.get("to_addrs")
        if to_addrs is not None and not isinstance(to_addrs, (list, str)):
            errors.append("alerts.email.to_addrs must be a list of addresses.")

    for key in ("ping_url", "ping_fail_url"):
        _as_str(cfg["monitoring"], key, "monitoring.", errors)
    schedule = cfg["schedule"]
    _as_bool(schedule, "enabled", "schedule.", errors)
    _as_bool(schedule, "run_on_start", "schedule.", errors)
    _time_list(schedule, "times", "schedule.", errors)
    _as_int(cfg, "log_max_mb", "", errors, minimum=0)
    _as_int(cfg, "log_backups", "", errors, minimum=0, maximum=1000)

    systems = cfg.get("systems") or {}
    for name, sys_cfg in systems.items():
        where = f"systems.{name}."
        if not isinstance(sys_cfg, dict):
            errors.append(f"systems.{name} must be a mapping of settings, got "
                          f"{type(sys_cfg).__name__}.")
            continue
        driver = sys_cfg.get("driver")
        if not driver:
            errors.append(f"{where}driver is missing — set one of the drivers "
                          "listed by `--list-drivers`.")
            continue
        from .drivers import DriverError, load_driver_class
        try:
            cls = load_driver_class(driver)
        except DriverError as exc:
            errors.append(f"{where}driver: {exc}")
            continue
        if sys_cfg.get("timezone") not in (None, ""):
            check_timezone(sys_cfg.get("timezone"), where, errors)
        if "verify_tls" in cls.config_keys:
            _as_verify(sys_cfg, "verify_tls", where, errors, warnings)
        if getattr(cls, "deprecated", ""):
            warnings.append(f"{where[:-1]}: the {cls.name} driver is deprecated — "
                            f"{cls.deprecated}")
        known = set(cls.config_keys) | {"driver", "timezone", "password", "note"}
        for key in sys_cfg:
            if key not in known:
                warnings.append(
                    f"{where[:-1]}: unknown key `{key}` for the {cls.name} "
                    f"driver — ignored. Known: {', '.join(sorted(known))}.")
        errors.extend(f"{where}{msg}" for msg in cls.check_config(sys_cfg))

    default = (cfg.get("default_system") or "").strip()
    if default and systems and default not in systems:
        errors.append(f"default_system '{default}' is not defined under "
                      f"`systems:` (known: {', '.join(sorted(systems))}).")
    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Secrets
# ─────────────────────────────────────────────────────────────────────────────

def system_password_env(name: str) -> str:
    """
    Environment variable holding a BAS system's password.

        campus_bacnet  ->  BAS_SYS_CAMPUS_BACNET_PASSWORD

    Per-system rather than per-vendor, so a campus with two Niagara supervisors
    under different service accounts can keep them apart.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"BAS_SYS_{slug}_PASSWORD"


# Legacy single-BAS password variable, still honored so a pre-1.0 scheduled
# task keeps working after the upgrade. Keyed by driver name.
LEGACY_DRIVER_PASSWORD_ENV = {
    "niagara": "BAS_NIAGARA_PASSWORD",
}

# A webhook URL is a bearer credential: anyone holding it can post to the
# channel. Sites that treat it as a secret can keep it out of config.yaml.
WEBHOOK_URL_ENV = "BAS_ALERT_WEBHOOK_URL"


def _driver_name(driver) -> str:
    """The registered name for a driver setting, resolving aliases
    (`BACnet`, `n4`, `none`); the raw text if it isn't a known driver."""
    from .drivers import DriverError, load_driver_class
    try:
        return load_driver_class(str(driver or "")).name
    except DriverError:
        return str(driver or "")


def load_credentials(cfg: dict) -> None:
    """
    Apply passwords from the environment and warn about anything still unset.

        BAS_25LIVE_PASSWORD                 25Live service account
        BAS_SYS_<SYSTEM>_PASSWORD           that BAS system's account
        BAS_NIAGARA_PASSWORD                pre-1.0 fallback for niagara
        BAS_SMTP_PASSWORD                   alert email, read at send time
        BAS_ALERT_WEBHOOK_URL               overrides alerts.webhook_url

    A password written into config.yaml is honored but warned about — the file
    is gitignored, not encrypted, and tends to end up in a backup or a ticket.

    Any of these the environment lacks are filled in first from the passwords
    the web UI stores in state/secrets.json (bassync/secretstore.py); the
    environment always wins.
    """
    from . import secretstore
    secretstore.fill_environ(secretstore.store_file(cfg))
    cn_pw = os.environ.get("BAS_25LIVE_PASSWORD")
    if cn_pw:
        cfg["collegenet"]["password"] = cn_pw
    elif cfg["collegenet"].get("password") not in (None, "", PLACEHOLDER_PASSWORD):
        logging.warning("25Live password came from config.yaml — prefer the "
                        "BAS_25LIVE_PASSWORD environment variable.")
    if cfg["collegenet"].get("password") in (None, "", PLACEHOLDER_PASSWORD):
        logging.warning("25Live password is unset — set BAS_25LIVE_PASSWORD "
                        "before a live run.")

    webhook = os.environ.get(WEBHOOK_URL_ENV)
    if webhook:
        cfg["alerts"]["webhook_url"] = webhook

    for name, sys_cfg in (cfg.get("systems") or {}).items():
        if not isinstance(sys_cfg, dict):
            continue
        driver = _driver_name(sys_cfg.get("driver", ""))
        env_var = system_password_env(name)
        value = os.environ.get(env_var)
        if not value:
            legacy_var = LEGACY_DRIVER_PASSWORD_ENV.get(driver)
            if legacy_var:
                value = os.environ.get(legacy_var)
        if value:
            sys_cfg["password"] = value
            continue
        existing = sys_cfg.get("password")
        if existing in (None, "", PLACEHOLDER_PASSWORD):
            # BACnet/IP has no credential of its own, the preview driver
            # talks to nothing, and a rest system can be set to auth: none;
            # silence is correct there. Everything else logs in.
            auth_mode = ((sys_cfg.get("auth") or {}).get("mode")
                         if isinstance(sys_cfg.get("auth"), dict) else None)
            if driver not in ("bacnet", "preview") and auth_mode != "none":
                logging.warning(
                    "System '%s' (%s) has no password — set %s before a live run.",
                    name, driver or "?", env_var)
        else:
            logging.warning(
                "System '%s' password came from config.yaml — prefer %s.",
                name, env_var)
