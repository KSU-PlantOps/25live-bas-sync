# 25Live -> BAS Schedule Sync — Tridium Niagara driver
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
DEPRECATED — Tridium Niagara (N4) driver that writes BooleanSchedule
SpecialEvents through a REST service on the station.

Use the `bacnet` driver against the station's BACnet schedule export instead
(docs/bas-setup.md, "Tridium Niagara"). The station applies those writes to the
BooleanSchedule as native special events, visible and editable in Workbench,
using only what a stock Niagara 4 station with the BACnet driver provides.

This driver is kept so existing deployments keep running, and it logs a
deprecation warning. Stock Niagara 4 does not ship the REST API it calls: the
standard web API, oBIX, reads and writes point values and invokes point
actions, but offers no way we have found to create schedule special events.

Like every driver, it owns its targets: each run deletes the schedule's special
events and writes this run's bookings, so point it at dedicated booking
schedules, not ones operators put holidays on.

Target syntax
-------------
    "SocialSciences/Rm1021_Occ"

An ORD relative to `schedule_base_path` (default `slot:/Schedules`). An
absolute ORD starting with `slot:` or `station:` is used as-is, so a schedule
living outside the base path is still reachable.

Confirm the REST contract for YOUR station
------------------------------------------
Niagara's REST surface varies by version and by which web service the station
runs. Three knobs cover the differences and all three are settable from
config.yaml, so adapting is a YAML edit rather than a code change:

    rest_base            default "/rest/v1"
    special_event_type   default "baja:BooleanSchedule$SpecialEvent"
    ord_style            "slot" (default) or "station"

Run `--validate` first: it resolves every ORD without writing. `--dry-run`
never contacts the station at all.
"""

import logging
from datetime import datetime
from urllib.parse import quote

import requests

from ..httputil import mount_retries, tls_verify
from .base import DriverError, ScheduleWriter

# Value pushed for an occupied window (True = Occupied on a BooleanSchedule).
OCCUPIED_VALUE = True

DEFAULT_REST_BASE = "/rest/v1"
DEFAULT_SPECIAL_EVENT_TYPE = "baja:BooleanSchedule$SpecialEvent"

HTTP_TIMEOUT_WRITE = 20
HTTP_TIMEOUT_HEALTH = 15


class NiagaraScheduleWriter(ScheduleWriter):
    """Writes SpecialEvents to Niagara N4 BooleanSchedules via REST."""

    name = "niagara"
    description = ("DEPRECATED — Niagara special events via a station REST "
                   "service. Use bacnet against the station's schedule export.")
    deprecated = ("it needs a REST service stock Niagara 4 doesn't ship. Use the "
                  "bacnet driver against the station's BACnet schedule export: "
                  "the bookings still appear as native special events in "
                  "Workbench. See docs/bas-setup.md, 'Tridium Niagara'.")
    config_keys = ("host", "port", "https", "username", "verify_tls",
                   "schedule_base_path", "heartbeat_path", "rest_base",
                   "special_event_type", "event_priority", "ord_style")

    @classmethod
    def check_config(cls, sys_cfg: dict) -> list:
        problems = []
        port = sys_cfg.get("port", 443)
        try:
            if isinstance(port, bool) or not 1 <= int(port) <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            problems.append(f"port must be 1-65535, got {port!r}.")
        try:
            if not 1 <= int(sys_cfg.get("event_priority", 16)) <= 16:
                raise ValueError
        except (TypeError, ValueError):
            problems.append("event_priority must be 1-16.")
        return problems

    def __init__(self, system_name: str, cfg: dict, tz, retry=None):
        super().__init__(system_name, cfg, tz, retry)
        proto = "https" if cfg.get("https", True) else "http"
        host = cfg.get("host", "localhost")
        port = cfg.get("port", 443)
        self.rest_base = cfg.get("rest_base", DEFAULT_REST_BASE)
        self.base = f"{proto}://{host}:{port}{self.rest_base}"
        self.schedule_base = cfg.get("schedule_base_path", "slot:/Schedules")
        self.heartbeat_path = cfg.get("heartbeat_path") or ""
        self.special_event_type = cfg.get("special_event_type",
                                          DEFAULT_SPECIAL_EVENT_TYPE)
        # BACnetSpecialEvent-style precedence among overlapping special events.
        # 16 is the lowest, so a hand-entered Niagara special event wins over
        # the booking overlay. (Not the BACnet priority array, and not the
        # schedule's own priority for writing to its linked points.)
        self.event_priority = int(cfg.get("event_priority", 16))

        self.session = requests.Session()
        self.session.auth = (cfg.get("username", ""), cfg.get("password", ""))
        # Retry only safe/idempotent verbs. POST writes are excluded so a retry
        # can never create duplicate special events.
        mount_retries(self.session, retry, allowed_methods=["GET", "DELETE"])
        # Verify by default. A station with a self-signed certificate should
        # point verify_tls at its CA bundle; `false` still works, loudly.
        self.session.verify = tls_verify(cfg)
        if not self.session.verify:
            logging.warning(
                "System '%s': Niagara TLS verification is DISABLED "
                "(verify_tls=false). Acceptable for a self-signed station cert "
                "on a trusted network; in production set verify_tls to a "
                "CA-bundle path.", system_name)
            # One intentional notice above beats a per-request warning flooding
            # a nightly log with hundreds of writes.
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def close(self) -> None:
        self.session.close()

    # ── ORDs ─────────────────────────────────────────────────────────────────

    def _full_ord(self, target: str) -> str:
        """Absolute ORD for a target, honoring an already-absolute one."""
        if target.startswith(("slot:", "station:", "/")):
            return target
        return f"{self.schedule_base.rstrip('/')}/{target.lstrip('/')}"

    @staticmethod
    def _encode_ord(path: str) -> str:
        """Percent-encode an ORD for a REST URL path, preserving the ORD
        structure characters ('/', ':', '$') while encoding spaces and other
        unsafe characters — schedule names very often contain spaces."""
        return quote(path, safe="/:$")

    def _endpoint(self, target: str, suffix: str = "") -> str:
        return f"{self.base}/{self._encode_ord(self._full_ord(target))}{suffix}"

    # ── pre-flight ───────────────────────────────────────────────────────────

    def health_check(self) -> tuple[bool, str]:
        url = f"{self.base}/about"
        try:
            r = self.session.get(url, timeout=HTTP_TIMEOUT_HEALTH)
        except requests.exceptions.SSLError as exc:
            # TLS is verified by default since 1.2, and stations often use a
            # self-signed certificate.
            return False, (f"{url}: TLS certificate not trusted ({exc}). Set "
                           "verify_tls to the station's CA bundle path, or to "
                           "false for a self-signed certificate on a trusted "
                           "network.")
        except requests.RequestException as exc:
            return False, f"{url}: {exc}"
        if r.status_code in (401, 403):
            return False, (f"{url}: auth failed (HTTP {r.status_code}) — check "
                           "the station username and its password env var")
        if r.status_code != 200:
            return False, f"{url}: HTTP {r.status_code}"
        return True, f"{url}: HTTP 200"

    def target_exists(self, target: str) -> tuple[bool, str]:
        try:
            r = self.session.get(self._endpoint(target), timeout=HTTP_TIMEOUT_HEALTH)
        except requests.RequestException as exc:
            return False, str(exc)
        return (r.status_code == 200), f"HTTP {r.status_code}"

    # ── writing ──────────────────────────────────────────────────────────────

    def write_schedule(self, target: str, windows: list) -> None:
        """Clear-then-write, so each run leaves exactly this run's bookings."""
        self._clear_special_events(target)
        for win in windows:
            self._write_special_event(target, win)
        logging.info("Wrote %d window(s) to %s", len(windows), target)

    def _clear_special_events(self, target: str) -> None:
        endpoint = self._endpoint(target, "/specialEvents")
        try:
            r = self.session.delete(endpoint, timeout=HTTP_TIMEOUT_WRITE)
        except requests.RequestException as exc:
            raise DriverError(f"Failed clearing {target}: {exc}") from exc
        if r.status_code not in (200, 204, 404):
            raise DriverError(
                f"Failed clearing {target}: HTTP {r.status_code} {r.text[:200]}")

    def _write_special_event(self, target: str, win) -> None:
        payload = {
            "type": self.special_event_type,
            "start": win.start.isoformat(),
            "end": win.end.isoformat(),
            "value": {"value": OCCUPIED_VALUE},
            "priority": self.event_priority,
        }
        endpoint = self._endpoint(target, "/specialEvents")
        try:
            r = self.session.post(endpoint, json=payload, timeout=HTTP_TIMEOUT_WRITE)
        except requests.RequestException as exc:
            raise DriverError(f"Write failed {target}: {exc}") from exc
        if r.status_code not in (200, 201):
            raise DriverError(
                f"Write failed {target}: HTTP {r.status_code} {r.text[:200]}")

    @property
    def has_heartbeat(self) -> bool:
        return bool(self.heartbeat_path)

    def write_heartbeat(self, stamp: datetime) -> None:
        """Stamp a point so the station itself can alarm if the job stops."""
        if not self.heartbeat_path:
            return
        endpoint = (f"{self.base}/{self._encode_ord(self.heartbeat_path)}"
                    "/out")
        try:
            r = self.session.post(endpoint, json={"value": stamp.isoformat()},
                                  timeout=HTTP_TIMEOUT_HEALTH)
        except requests.RequestException as exc:
            raise DriverError(f"heartbeat {self.heartbeat_path}: {exc}") from exc
        if r.status_code >= 400:
            raise DriverError(f"heartbeat {self.heartbeat_path}: HTTP "
                              f"{r.status_code} {r.text[:200]}")
