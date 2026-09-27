# 25Live -> BAS Schedule Sync — BACnet/IP driver
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Vendor-neutral BACnet/IP driver — writes ASHRAE 135 Schedule objects.

This is the driver to reach for first on a mixed campus. Automated Logic
WebCTRL, Schneider EcoStruxure Building Operation, Tridium Niagara and any
other BTL-listed controller are all required to expose standard Schedule
objects (Object_Type 17), so one code path drives every one of them. No vendor
SDK, no per-version REST contract to chase.

Ownership — read this before pointing it at a schedule
------------------------------------------------------
The sync OWNS the `Exception_Schedule` of every target. Each run replaces the
whole array in one WriteProperty, which is what makes the write atomic and a
cancelled booking actually disappear — and it also means any exception an
operator entered by hand on that same object (a holiday, a shutdown) is erased
on the next run.

So give the sync its own schedule object per zone: a dedicated "booking"
schedule whose weekly schedule is empty or Unoccupied, combined with the
zone's normal occupancy schedule in the controller's logic (typically an OR).
Holidays and shutdowns stay on the normal schedule, where nothing touches
them. `--validate` reads each target's current exceptions and warns about any
the sync did not write, before a live run erases them.

What it writes
--------------
Only `Exception_Schedule` (BACnetARRAY of BACnetSpecialEvent). The weekly
schedule, Schedule_Default and Priority_For_Writing are left alone.

Encoding: ONE special event per calendar date, holding a list of time/value
pairs that alternate ON at each window start and OFF at each window end:

    2026-06-10   09:00 -> ON,  11:30 -> OFF,  13:00 -> ON,  17:00 -> OFF

That matters for real controllers. A naive "one special event per booking"
encoding blows past the Exception_Schedule array limits that field controllers
actually enforce (commonly 10-25 entries); grouping by date caps the array at
one entry per day of lookahead no matter how heavily booked the rooms are.

Value types
-----------
ASHRAE 135 requires every value in a schedule to have the same datatype as its
Schedule_Default, and controllers reject (or fault on) anything else. A
schedule driving a binary point is usually ENUMERATED (active/inactive), not
BOOLEAN, so the driver reads each target's Schedule_Default before writing and
encodes ON/OFF to match:

    boolean      true / false
    enumerated   1 (active) / 0 (inactive)
    real         1.0 / 0.0
    unsigned     multistate — set occupied_value / unoccupied_value, because
                 state numbers are site-specific (1 = Occupied, 2 = Unoccupied…)

`value_type` pins the type instead of reading it. `unoccupied_value: null`
writes NULL at each window end, which relinquishes to the weekly schedule
rather than forcing OFF — only on controllers that support it (135-2012+).

Target syntax (the `target:` in space_mapping.yaml)
--------------------------------------------------
    "12001:5"                   device instance 12001, Schedule instance 5
    "12001:5@10.4.2.30"         same, but skip Who-Is and talk to that address
    "12001:5@10.4.2.30:47808"   ...with an explicit UDP port
    "12001:5@2001:0x21"         a routed MS/TP device: network 2001, MAC 0x21

The address form is worth using on a big campus: it removes a broadcast
round-trip per schedule and works even where Who-Is does not cross a subnet.
A pin belongs to the DEVICE, so one pinned target pins every schedule on that
device, and two different pins for the same device are a mapping error. At
the first contact with a pinned address the driver reads that device's own
object back, so a stale IP that now belongs to another controller fails
instead of writing into it.

Priorities — two different things, often confused
-------------------------------------------------
`event_priority` (1-16, default 16) is the BACnetSpecialEvent's own priority:
which *exception* wins when two cover the same moment. Lower number wins.

It is NOT the priority array (1-16) that commandable outputs use, and not the
Schedule object's own `Priority_For_Writing` (which decides at what priority
the schedule commands its listed points). This driver never changes
`Priority_For_Writing` — that is the controls engineer's setting.

Heartbeat
---------
`heartbeat_object: "599100:analog-value,1"` (optionally with `@address`) is
written after every run so the BAS itself can alarm when the job stops:
an analog value gets the Unix time in hours, a characterstring-value an ISO
timestamp, a datetime-value the date and time. Alarm on "unchanged for more
than a day".

Requirements
------------
`pip install bacpypes3` (or `pip install -r requirements-bacnet.txt`). The
import is lazy, so a site using only the HTTP drivers never needs it.
"""

import asyncio
import logging
import re
import socket
from datetime import date as _date
from datetime import datetime
from importlib import import_module
from typing import Any, Optional

from ..model import OccupancyWindow
from .base import DriverError, ScheduleWriter

# Default BACnet/IP UDP port (0xBAC0).
DEFAULT_BACNET_PORT = 47808

# BACnetSpecialEvent priority. 16 is the LOWEST exception priority.
DEFAULT_EVENT_PRIORITY = 16

# Seconds to wait for a Who-Is reply before giving up on a device.
WHO_IS_TIMEOUT = 5.0

# Seconds to allow for the BACnet stack to bind and come up. BACpypes3 retries
# a failed socket bind indefinitely, in the background, so without a ceiling a
# busy port makes every request time out one by one instead of failing once.
CONNECT_TIMEOUT = 10.0

# Ceiling on any single BACnet operation (resolve + write + verify). BACpypes3
# has its own APDU retries; this is the backstop that keeps a nightly job from
# hanging, since a hung job never alerts.
OPERATION_TIMEOUT = 60.0

# BACnet object instance numbers are 22-bit. 4194303 is reserved to mean
# "unconfigured" in a Device object, so a target naming it is a mistake — it is
# what an out-of-the-box controller reports before commissioning.
MAX_INSTANCE = 4194302
UNCONFIGURED_INSTANCE = 4194303

VALUE_TYPES = ("auto", "boolean", "enumerated", "unsigned", "integer", "real", "double")

# target: "<device>:<schedule>" with an optional "@address[:port]" suffix.
_TARGET_RE = re.compile(
    r"^\s*(?P<device>\d+)\s*:\s*(?P<schedule>\d+)\s*"
    r"(?:@\s*(?P<address>[^\s]+?)\s*)?$"
)

# heartbeat: "<device>:<object-type>,<instance>" with an optional "@address".
_OBJECT_RE = re.compile(
    r"^\s*(?P<device>\d+)\s*:\s*(?P<type>[a-z][a-z-]*)\s*,\s*(?P<instance>\d+)\s*"
    r"(?:@\s*(?P<address>[^\s]+?)\s*)?$"
)
HEARTBEAT_TYPES = ("analog-value", "characterstring-value", "datetime-value")


class BacnetTarget:
    """A parsed `device:schedule[@address]` target."""

    __slots__ = ("device_id", "schedule_instance", "address")

    def __init__(self, device_id: int, schedule_instance: int,
                 address: Optional[str] = None):
        self.device_id = device_id
        self.schedule_instance = schedule_instance
        self.address = address

    @property
    def key(self) -> str:
        """The schedule's identity, independent of how it is reached."""
        return f"{self.device_id}:{self.schedule_instance}"

    def __str__(self) -> str:
        return f"{self.key}@{self.address}" if self.address else self.key


_ROUTED_RE = re.compile(r"^(?P<net>\d+):(?P<mac>0[xX][0-9a-fA-F]+|\d+)$")


def _normalize_address(address: Optional[str]) -> Optional[str]:
    """
    One spelling per address, so two ways of writing the same pin agree.

    `10.4.2.30` -> `10.4.2.30:47808` (the default port). A routed MS/TP
    station, `network:mac`, gets its MAC in decimal: `2001:0x21` ->
    `2001:33`, which is also how BACpypes3 prints it.
    """
    if not address:
        return None
    routed = _ROUTED_RE.match(address)
    if routed:
        return f"{int(routed.group('net'))}:{int(routed.group('mac'), 0)}"
    if ":" not in address:
        return f"{address}:{DEFAULT_BACNET_PORT}"
    return address


def _check_instance(value: int, what: str, text: str) -> None:
    if value == UNCONFIGURED_INSTANCE:
        raise DriverError(
            f"BACnet target {text!r}: {what} instance "
            f"{UNCONFIGURED_INSTANCE} is the reserved 'unconfigured' value "
            "— it is what a controller reports before commissioning, not a "
            "real address.")
    if value > MAX_INSTANCE:
        raise DriverError(
            f"BACnet target {text!r}: {what} instance {value} is above "
            f"the maximum {MAX_INSTANCE} (instances are 22-bit).")


def parse_target(target: str) -> BacnetTarget:
    """Parse a target string, raising DriverError with a usable message."""
    m = _TARGET_RE.match(target or "")
    if not m:
        raise DriverError(
            f"Invalid BACnet target {target!r}. Expected "
            "'<device-instance>:<schedule-instance>', optionally "
            "'@<ip>[:<port>]' — e.g. '12001:5' or '12001:5@10.4.2.30'.")
    device_id = int(m.group("device"))
    schedule_instance = int(m.group("schedule"))
    _check_instance(device_id, "device", target)
    _check_instance(schedule_instance, "schedule", target)
    return BacnetTarget(device_id, schedule_instance,
                        _normalize_address(m.group("address")))


def parse_object(text: str) -> tuple:
    """`599100:analog-value,1[@addr]` -> (device, type, instance, address)."""
    m = _OBJECT_RE.match(text or "")
    if not m:
        raise DriverError(
            f"Invalid BACnet object {text!r}. Expected "
            "'<device-instance>:<object-type>,<instance>', e.g. "
            "'599100:analog-value,1', optionally '@<ip>'.")
    device_id, instance = int(m.group("device")), int(m.group("instance"))
    _check_instance(device_id, "device", text)
    _check_instance(instance, "object", text)
    return device_id, m.group("type"), instance, _normalize_address(m.group("address"))


def windows_to_daily(windows: list, tz) -> "dict[_date, list[tuple[datetime, bool]]]":
    """
    Turn occupancy windows into { calendar date: [(local time, value), ...] }.

    This is the whole encoding decision, kept pure so it can be tested without
    a BACnet stack. Windows are split at local midnight, sorted, and collapsed
    into one alternating ON/OFF list per date.

    A window that ends exactly at the next midnight contributes no OFF entry:
    a BACnet time cannot be 24:00, and at midnight the controller re-evaluates
    against the next day's exception (or falls back to the weekly schedule)
    anyway, so the trailing OFF would be both illegal and redundant.

    Windows are converted to the device's zone BEFORE being split, because the
    midnight they must be split at is the controller's, not the campus's. Doing
    it the other way round on a building in another timezone produced a window
    that turned ON and never turned OFF.
    """
    local = [OccupancyWindow(w.start.astimezone(tz), w.end.astimezone(tz),
                             list(w.source_event_ids)) for w in windows]
    by_date: dict = {}
    for w in ScheduleWriter.split_at_midnight(local):
        start, end = w.start, w.end
        if end <= start:
            continue
        day = start.date()
        entries = by_date.setdefault(day, [])
        entries.append((start, True))
        # end.date() != day means the piece runs to exactly midnight.
        if end.date() == day:
            entries.append((end, False))
    for day in by_date:
        by_date[day].sort(key=lambda tv: (tv[0], not tv[1]))
    return by_date


# ─────────────────────────────────────────────────────────────────────────────
# Value types (pure helpers — BACpypes3 is imported only when called)
# ─────────────────────────────────────────────────────────────────────────────

def value_kind(value: Any) -> str:
    """
    The schedule datatype of a value read from a device: "boolean",
    "enumerated", "unsigned", "integer", "real", "double" or "null".

    Accepts the AnyAtomic wrapper BACpypes3 returns for Schedule_Default as
    well as a bare primitive.
    """
    from bacpypes3.primitivedata import Boolean, Double, Enumerated, Integer, Null, Real, Unsigned
    if hasattr(value, "get_value"):
        value = value.get_value()
    # Most specific first: in BACpypes3, Double is a subclass of Real.
    for cls, kind in ((Null, "null"), (Boolean, "boolean"),
                      (Enumerated, "enumerated"), (Unsigned, "unsigned"),
                      (Integer, "integer"), (Double, "double"), (Real, "real")):
        if isinstance(value, cls):
            return kind
    return type(value).__name__.lower()


def _is_null_setting(value) -> bool:
    return isinstance(value, str) and value.strip().lower() in ("null", "relinquish")


def encode_value(kind: str, occupied: bool, occupied_value=None,
                 unoccupied_value=None):
    """
    The BACpypes3 primitive to write for ON (`occupied`) or OFF.

    Raises DriverError for a multistate (unsigned) schedule with no values
    configured: which state number means "occupied" is site-specific, and a
    guess would command the wrong mode.
    """
    from bacpypes3.primitivedata import Boolean, Double, Enumerated, Integer, Null, Real, Unsigned
    configured = occupied_value if occupied else unoccupied_value
    if not occupied and _is_null_setting(unoccupied_value):
        return Null(())
    if kind == "boolean":
        return Boolean(bool(configured) if configured is not None else occupied)
    if kind == "enumerated":
        # BACnetBinaryPV: active = 1, inactive = 0.
        return Enumerated(int(configured) if configured is not None else int(occupied))
    if kind in ("real", "double"):
        number = float(configured) if configured is not None else float(occupied)
        return Real(number) if kind == "real" else Double(number)
    if kind in ("unsigned", "integer"):
        if configured is None:
            raise DriverError(
                f"this schedule's values are {kind.upper()} (multistate), so "
                "which number means Occupied is site-specific. Set "
                "occupied_value and unoccupied_value on this system (e.g. 1 "
                "and 2), or point the target at a binary schedule.")
        return Unsigned(int(configured)) if kind == "unsigned" else Integer(int(configured))
    raise DriverError(f"unsupported schedule datatype {kind!r}")


def foreign_exceptions(special_events: list, event_priority: int) -> int:
    """
    How many entries in an Exception_Schedule this sync did not write.

    The sync writes one single-date calendar entry per day at its configured
    priority. Anything else — a calendar reference (holidays), a date range, a
    week-and-day pattern, a different priority — came from somewhere else and
    will be erased by the next live run.
    """
    count = 0
    for event in special_events or []:
        period = getattr(event, "period", None)
        entry = getattr(period, "calendarEntry", None) if period is not None else None
        ours = (getattr(event, "eventPriority", None) == event_priority
                and entry is not None
                and getattr(entry, "date", None) is not None)
        if not ours:
            count += 1
    return count


def _bacnet_error_class():
    """BACpypes3's Error/Reject/Abort base class, or None if not installed.

    It derives from BaseException, NOT Exception — so an offline controller
    or a write-access-denied reply sails straight past every `except
    Exception` in the program. Every BACnet call in this driver goes through
    _run(), which converts it into a DriverError."""
    try:
        from bacpypes3.apdu import ErrorRejectAbortNack
    except ImportError:                                   # pragma: no cover
        return None
    return ErrorRejectAbortNack


def _describe_bacnet_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    if "no-response" in text:
        return (f"{text} — the device did not answer (offline, wrong address, "
                "or a BBMD/foreign-device registration problem)")
    return text


class BacnetScheduleWriter(ScheduleWriter):
    """Writes BACnet Schedule Exception_Schedule over BACnet/IP."""

    name = "bacnet"
    description = ("Standard BACnet/IP Schedule objects — WebCTRL, "
                   "EcoStruxure, Niagara, any BTL-listed controller.")
    config_keys = (
        "local_address", "device_id", "device_name", "vendor_identifier",
        "bbmd_address", "bbmd_ttl_seconds", "event_priority", "verify_writes",
        "verify_device", "max_special_events", "who_is_timeout",
        "connect_timeout", "operation_timeout", "value_type", "occupied_value",
        "unoccupied_value", "heartbeat_object", "heartbeat_priority",
    )

    # ── configuration ────────────────────────────────────────────────────────

    @classmethod
    def check_config(cls, sys_cfg: dict) -> list:
        problems = []

        def whole(key, lo, hi, default):
            value = sys_cfg.get(key, default)
            try:
                if isinstance(value, bool) or not lo <= int(value) <= hi:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f"{key} must be a whole number {lo}-{hi}, got {value!r}.")

        def positive(key, default):
            value = sys_cfg.get(key, default)
            try:
                if isinstance(value, bool) or float(value) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f"{key} must be a positive number of seconds, got {value!r}.")

        whole("device_id", 0, MAX_INSTANCE, 599001)
        whole("event_priority", 1, 16, DEFAULT_EVENT_PRIORITY)
        whole("heartbeat_priority", 1, 16, 16)
        whole("max_special_events", 0, 10000, 0)
        whole("bbmd_ttl_seconds", 1, 65535, 900)
        whole("vendor_identifier", 0, 65535, 999)
        for key in ("who_is_timeout", "connect_timeout", "operation_timeout"):
            positive(key, 1)
        value_type = str(sys_cfg.get("value_type") or "auto").strip().lower()
        if value_type not in VALUE_TYPES:
            problems.append(f"value_type must be one of {', '.join(VALUE_TYPES)}, "
                            f"got {sys_cfg.get('value_type')!r}.")
        for key in ("occupied_value", "unoccupied_value"):
            value = sys_cfg.get(key)
            if value is None or (key == "unoccupied_value" and _is_null_setting(value)):
                continue
            try:
                if isinstance(value, bool):
                    continue
                float(value)
            except (TypeError, ValueError):
                problems.append(f"{key} must be a number"
                                + (" or null" if key == "unoccupied_value" else "")
                                + f", got {value!r}.")
        heartbeat = sys_cfg.get("heartbeat_object")
        if heartbeat:
            try:
                _dev, obj_type, _inst, _addr = parse_object(str(heartbeat))
                if obj_type not in HEARTBEAT_TYPES:
                    problems.append(f"heartbeat_object type must be one of "
                                    f"{', '.join(HEARTBEAT_TYPES)}, got {obj_type!r}.")
            except DriverError as exc:
                problems.append(str(exc))
        return problems

    @classmethod
    def normalize_targets(cls, sys_cfg: dict, targets: list) -> tuple:
        """
        One canonical string per schedule object.

        A pinned address belongs to the device, so it is applied to every
        schedule on that device: `12001:5` and `12001:6@10.4.2.30` become
        `12001:5@10.4.2.30:47808` and `12001:6@10.4.2.30:47808`. Two targets
        naming the same object therefore key the same destination and are
        written once, with both rooms' bookings unioned.
        """
        parsed: dict = {}
        errors: dict = {}
        for text in targets:
            try:
                parsed[text] = parse_target(text)
            except DriverError as exc:
                errors[text] = str(exc)
        pins: dict = {}
        for tgt in parsed.values():
            if tgt.address:
                pins.setdefault(tgt.device_id, set()).add(tgt.address)
        for text, tgt in parsed.items():
            if len(pins.get(tgt.device_id, ())) > 1:
                errors[text] = (
                    f"BACnet device {tgt.device_id} is pinned to more than one "
                    f"address ({', '.join(sorted(pins[tgt.device_id]))}). A "
                    "device has one address — make every target on it agree.")
        canonical = {}
        for text, tgt in parsed.items():
            if text in errors:
                continue
            addresses = pins.get(tgt.device_id)
            address = next(iter(addresses)) if addresses else None
            canonical[text] = f"{tgt.key}@{address}" if address else tgt.key
        return canonical, errors

    # ── lifecycle ────────────────────────────────────────────────────────────

    def __init__(self, system_name: str, cfg: dict, tz, retry=None):
        super().__init__(system_name, cfg, tz, retry)
        problems = self.check_config(cfg)
        if problems:
            raise DriverError(f"System '{system_name}': " + " ".join(problems))
        # This machine's BACnet identity. `local_address` must be a real NIC
        # address on this host with its prefix length, e.g. "10.4.1.55/24".
        self.local_address = cfg.get("local_address") or ""
        self.device_id = int(cfg.get("device_id", 599001))
        self.device_name = cfg.get("device_name") or "25Live-BAS-Sync"
        self.vendor_id = int(cfg.get("vendor_identifier", 999))
        # Foreign-device registration: needed whenever this host is not on the
        # same subnet as the controllers (the usual case for a server in a
        # data centre writing to field panels).
        self.bbmd_address = cfg.get("bbmd_address") or ""
        self.bbmd_ttl = int(cfg.get("bbmd_ttl_seconds", 900))
        self.event_priority = int(cfg.get("event_priority", DEFAULT_EVENT_PRIORITY))
        # 0 = no cap. Set it to your controllers' documented Exception_Schedule
        # limit and the driver truncates to the nearest days instead of letting
        # the controller reject the whole write.
        self.max_special_events = int(cfg.get("max_special_events", 0))
        self.verify_writes = bool(cfg.get("verify_writes", True))
        self.verify_device = bool(cfg.get("verify_device", True))
        self.who_is_timeout = float(cfg.get("who_is_timeout", WHO_IS_TIMEOUT))
        self.connect_timeout = float(cfg.get("connect_timeout", CONNECT_TIMEOUT))
        self.operation_timeout = float(cfg.get("operation_timeout", OPERATION_TIMEOUT))
        self.value_type = str(cfg.get("value_type") or "auto").strip().lower()
        self.occupied_value = cfg.get("occupied_value")
        self.unoccupied_value = cfg.get("unoccupied_value")
        self.heartbeat_object = cfg.get("heartbeat_object") or ""
        self.heartbeat_priority = int(cfg.get("heartbeat_priority", 16))

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._app: Any = None
        self._address_cache: dict = {}
        self._kind_cache: dict = {}
        self._verified_devices: set = set()
        self.registration_note = ""

    def connect(self) -> None:
        """
        Stand up the BACnet stack on its own event loop.

        bacpypes3 is asyncio-native while the rest of the sync is plain
        synchronous code. Rather than colour the whole program async for one
        driver, the loop lives here and each public method drives it to
        completion — the stack stays up for the whole run, so we register with
        the BBMD once rather than per schedule.
        """
        if self._app is not None:
            return
        if not self.local_address:
            raise DriverError(
                f"System '{self.system_name}': `local_address` is required for "
                "the bacnet driver — set it to this host's NIC address and "
                "prefix, e.g. \"10.4.1.55/24\".")
        # Fail here, with a fixable message, rather than deep inside the
        # first write at 2 AM.
        try:
            import_module("bacpypes3")
        except ImportError as exc:
            raise DriverError(
                "The bacnet driver needs BACpypes3. Install it with "
                "`pip install bacpypes3` (or "
                "`pip install -r requirements-bacnet.txt`).") from exc

        # Check the address ourselves first. BACpypes3 would retry the bind in
        # a loop and never surface the reason; this names it immediately, and
        # lists what the host actually has so the fix is obvious.
        _check_local_address(self.local_address, self.system_name)

        self._loop = asyncio.new_event_loop()
        try:
            self._app = self._loop.run_until_complete(
                asyncio.wait_for(self._start(), timeout=self.connect_timeout))
        except asyncio.TimeoutError as exc:
            self.close()                 # the half-started stack, then the loop
            raise DriverError(
                f"System '{self.system_name}': the BACnet stack did not bind "
                f"within {self.connect_timeout:g}s on {self.local_address}. "
                "Check that nothing else on this host is bound to that UDP "
                "port (a BAS server on the same machine usually is — give "
                "local_address its own port, e.g. \"10.4.1.55/24:47809\") and "
                "that the interface is up.") from exc
        except Exception as exc:
            self.close()
            raise DriverError(f"Could not start the BACnet stack: {exc}") from exc
        logging.info("BACnet stack up on %s as device %d (%s)",
                     self.local_address, self.device_id,
                     f"foreign device via BBMD {self.bbmd_address}"
                     if self.bbmd_address else "same-subnet broadcast")

    async def _start(self):
        app = await self._build_app()
        self._app = app
        # Application.from_object_list() returns before the UDP sockets exist
        # — BACpypes3 binds them in background tasks and retries forever on
        # failure. Wait for the bind here, inside the connect timeout, so a
        # busy port fails the connect instead of every write that follows.
        for link_layer in getattr(app, "link_layers", {}).values():
            server = getattr(link_layer, "server", None)
            ready = getattr(server, "_local_transport_ready", None)
            if ready is not None:
                await ready.wait()
        if self.bbmd_address:
            await self._await_registration(app)
        return app

    async def _await_registration(self, app) -> None:
        """Give the BBMD a moment to acknowledge foreign-device registration,
        so health_check can say whether it did. Not fatal: some BBMDs are slow
        and a pinned address still works without broadcast."""
        deadline = asyncio.get_running_loop().time() + min(self.connect_timeout / 2, 5)
        while asyncio.get_running_loop().time() < deadline:
            statuses = [getattr(ll, "bbmdRegistrationStatus", None)
                        for ll in getattr(app, "link_layers", {}).values()]
            if 0 in statuses:
                self.registration_note = "registered"
                return
            await asyncio.sleep(0.1)
        self.registration_note = "BBMD has not acknowledged registration yet"

    async def _build_app(self):
        from bacpypes3.app import Application
        from bacpypes3.basetypes import HostNPort
        from bacpypes3.local.device import DeviceObject
        from bacpypes3.local.networkport import NetworkPortObject

        address = self.local_address
        if ":" not in address.rsplit("/", 1)[-1]:
            address = f"{address}:{DEFAULT_BACNET_PORT}"

        device = DeviceObject(
            objectIdentifier=("device", self.device_id),
            objectName=self.device_name,
            vendorIdentifier=self.vendor_id,
        )
        port = NetworkPortObject(
            address,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )
        if self.bbmd_address:
            host, _, port_txt = self.bbmd_address.partition(":")
            port.bacnetIPMode = "foreign"
            port.fdBBMDAddress = HostNPort(
                host=dict(ipAddress=_ip_bytes(host)),
                port=int(port_txt or DEFAULT_BACNET_PORT),
            )
            port.fdSubscriptionLifetime = self.bbmd_ttl
        return Application.from_object_list([device, port])

    def close(self) -> None:
        if self._app is not None:
            try:
                self._app.close()
            except BaseException as exc:                  # noqa: BLE001
                logging.debug("BACnet app close: %s", exc)
            self._app = None
        self._close_loop()

    def _close_loop(self) -> None:
        if self._loop is None:
            return
        try:
            # A timed-out bind leaves BACpypes3's retry task pending; cancel it
            # so closing the loop doesn't warn about a task that never finished.
            for task in asyncio.all_tasks(self._loop):
                task.cancel()
            self._loop.run_until_complete(asyncio.sleep(0))
        except BaseException as exc:                      # noqa: BLE001
            logging.debug("BACnet loop drain: %s", exc)
        try:
            self._loop.close()
        except BaseException as exc:                      # noqa: BLE001
            logging.debug("BACnet loop close: %s", exc)
        self._loop = None

    def _run(self, coro):
        """
        Drive one coroutine to completion on the driver's loop.

        Every BACnet exchange goes through here, which is what guarantees two
        things the rest of the program relies on: nothing hangs past
        operation_timeout, and a BACnet Error/Reject/Abort (which BACpypes3
        raises as a BaseException) comes out as a DriverError.
        """
        if self._loop is None:
            self.connect()
        assert self._loop is not None
        bacnet_error = _bacnet_error_class()
        try:
            return self._loop.run_until_complete(
                asyncio.wait_for(coro, timeout=self.operation_timeout))
        except asyncio.TimeoutError:
            raise DriverError(f"no answer within {self.operation_timeout:g}s "
                              "(operation_timeout)") from None
        except BaseException as exc:
            if bacnet_error is not None and isinstance(exc, bacnet_error):
                raise DriverError(_describe_bacnet_error(exc)) from None
            raise

    # ── addressing ───────────────────────────────────────────────────────────

    async def _resolve(self, tgt: BacnetTarget):
        """Address for a device: the pinned one, else a cached/fresh Who-Is."""
        from bacpypes3.pdu import Address
        if tgt.address:
            address = Address(tgt.address)
            if self.verify_device and tgt.device_id not in self._verified_devices:
                await self._check_device(tgt.device_id, address)
            return address
        cached = self._address_cache.get(tgt.device_id)
        if cached is not None:
            return cached
        found = await self._app.who_is(tgt.device_id, tgt.device_id,
                                       timeout=self.who_is_timeout)
        if not found:
            raise DriverError(
                f"BACnet device {tgt.device_id} did not answer Who-Is. Pin its "
                f"address in the target (e.g. '{tgt.device_id}:"
                f"{tgt.schedule_instance}@10.4.2.30') or check BBMD/foreign-"
                "device registration.")
        address = found[0].pduSource
        self._address_cache[tgt.device_id] = address
        return address

    async def _check_device(self, device_id: int, address) -> None:
        """
        Confirm the device at a pinned address really is `device_id`.

        A pinned IP outlives the controller it was written for: re-addressed
        panels and replaced hardware are routine. Reading the Device object by
        its expected instance fails with unknown-object on any other device,
        so a stale pin stops here instead of writing someone else's schedule.
        """
        bacnet_error = _bacnet_error_class()
        try:
            await self._app.read_property(address, f"device,{device_id}", "object-name")
        except BaseException as exc:
            if bacnet_error is not None and isinstance(exc, bacnet_error):
                text = str(exc)
                if "unknown-object" in text:
                    raise DriverError(
                        f"the device at {address} is not BACnet device "
                        f"{device_id} — the pinned address is stale or wrong. "
                        "Fix the @address in space_mapping.yaml.") from None
                raise DriverError(
                    f"could not confirm device {device_id} at {address}: "
                    f"{_describe_bacnet_error(exc)}") from None
            raise
        self._verified_devices.add(device_id)

    async def _schedule_kind(self, tgt: BacnetTarget, address) -> str:
        """The datatype this schedule's values must be written in."""
        if self.value_type != "auto":
            return self.value_type
        cached = self._kind_cache.get(tgt.key)
        if cached:
            return cached
        default = await self._app.read_property(
            address, f"schedule,{tgt.schedule_instance}", "schedule-default")
        kind = value_kind(default)
        if kind == "null":
            # A NULL default says nothing about the type; the weekly schedule
            # usually does.
            weekly = await self._app.read_property(
                address, f"schedule,{tgt.schedule_instance}", "weekly-schedule")
            for daily in weekly or []:
                for tv in getattr(daily, "daySchedule", None) or []:
                    candidate = value_kind(tv.value)
                    if candidate != "null":
                        kind = candidate
                        break
                if kind != "null":
                    break
        if kind == "null":
            raise DriverError(
                f"{tgt}: Schedule_Default is NULL and the weekly schedule has no "
                "values, so the value type cannot be read. Set value_type on "
                "this system (boolean, enumerated, real, unsigned).")
        self._kind_cache[tgt.key] = kind
        return kind

    # ── pre-flight ───────────────────────────────────────────────────────────

    def health_check(self) -> tuple[bool, str]:
        try:
            self.connect()
        except DriverError as exc:
            return False, str(exc)
        detail = f"BACnet device {self.device_id} bound to {self.local_address}"
        if self.bbmd_address:
            detail += (f", foreign device on BBMD {self.bbmd_address}"
                       f" ({self.registration_note or 'registration pending'})")
        return True, detail

    def target_exists(self, target: str) -> tuple[bool, str]:
        exists, detail, _notes = self.inspect_target(target)
        return exists, detail

    def inspect_target(self, target: str) -> tuple:
        """
        Read the target back without writing: its name (proves it resolves
        and is a Schedule), its value type (proves the write will encode),
        and its current exceptions (warns about entries a live run will
        replace).
        """
        try:
            tgt = parse_target(target)
            name, kind, count, foreign = self._run(self._inspect(tgt))
        except DriverError as exc:
            return False, str(exc), []
        notes = []
        if foreign:
            notes.append(
                f"{tgt}: {foreign} of its {count} exception(s) were not written "
                "by this sync (holidays? calendar references?). The next live "
                "run REPLACES the whole Exception_Schedule — move them to the "
                "zone's normal schedule, or point this target at a dedicated "
                "booking schedule.")
        return True, f"schedule '{name}' ({kind} values)", notes

    async def _inspect(self, tgt: BacnetTarget) -> tuple:
        address = await self._resolve(tgt)
        name = await self._app.read_property(
            address, f"schedule,{tgt.schedule_instance}", "object-name")
        kind = await self._schedule_kind(tgt, address)
        # Prove the encoding works for this type before a live run finds out.
        encode_value(kind, True, self.occupied_value, self.unoccupied_value)
        encode_value(kind, False, self.occupied_value, self.unoccupied_value)
        current = await self._app.read_property(
            address, f"schedule,{tgt.schedule_instance}", "exception-schedule")
        current = list(current or [])
        return (str(name), kind, len(current),
                foreign_exceptions(current, self.event_priority))

    # ── writing ──────────────────────────────────────────────────────────────

    def write_schedule(self, target: str, windows: list) -> None:
        tgt = parse_target(target)
        try:
            self._run(self._write(tgt, windows))
        except DriverError as exc:
            raise DriverError(f"BACnet write to {tgt} failed: {exc}") from None
        except Exception as exc:                          # noqa: BLE001
            raise DriverError(
                f"BACnet write to {tgt} failed: {type(exc).__name__}: {exc}") from exc

    def build_special_events(self, windows: list, kind: str) -> list:
        """The SpecialEvent list for these windows, in the schedule's type."""
        from bacpypes3.basetypes import CalendarEntry, SpecialEvent, SpecialEventPeriod, TimeValue
        from bacpypes3.primitivedata import Time

        by_date = windows_to_daily(windows, self.tz)
        special_events = []
        for day in sorted(by_date):
            time_values = [
                TimeValue(time=Time((dt.hour, dt.minute, dt.second,
                                     dt.microsecond // 10000)),
                          value=encode_value(kind, value, self.occupied_value,
                                             self.unoccupied_value))
                for dt, value in by_date[day]
            ]
            special_events.append(SpecialEvent(
                period=SpecialEventPeriod(
                    calendarEntry=CalendarEntry(date=_bacnet_date(day))),
                listOfTimeValues=time_values,
                eventPriority=self.event_priority,
            ))
        return special_events

    async def _write(self, tgt: BacnetTarget, windows: list) -> None:
        from bacpypes3.basetypes import SpecialEvent
        from bacpypes3.constructeddata import ArrayOf

        address = await self._resolve(tgt)
        kind = await self._schedule_kind(tgt, address)
        special_events = self.build_special_events(windows, kind)
        days = len(special_events)

        if self.max_special_events and len(special_events) > self.max_special_events:
            dropped = len(special_events) - self.max_special_events
            logging.warning(
                "%s: %d special events exceeds max_special_events=%d — keeping "
                "the %d nearest days and dropping the %d furthest out. Shorten "
                "lookahead_days or raise the cap once you have confirmed the "
                "controller's real limit.",
                tgt, len(special_events), self.max_special_events,
                self.max_special_events, dropped)
            special_events = special_events[:self.max_special_events]

        array_type = ArrayOf(SpecialEvent)
        # Writing the whole array in one WriteProperty replaces the previous
        # run's overlay atomically. An empty array is the clear — which is why
        # "no bookings" writes [] rather than skipping the target.
        await self._app.write_property(
            address, f"schedule,{tgt.schedule_instance}",
            "exception-schedule", array_type(special_events))

        if self.verify_writes:
            await self._verify(tgt, address, len(special_events))

        logging.info("Wrote %d special event(s) covering %d day(s) to %s (%s)",
                     len(special_events), days, tgt, kind)

    async def _verify(self, tgt: BacnetTarget, address, expected: int) -> None:
        """Read Exception_Schedule back and confirm the array took.

        Some controllers accept a WriteProperty and quietly ignore it (schedule
        locked by the vendor tool, object out of service, insufficient
        privilege). Without a read-back the sync would report success while the
        building never changes.
        """
        bacnet_error = _bacnet_error_class()
        try:
            readback = await self._app.read_property(
                address, f"schedule,{tgt.schedule_instance}", "exception-schedule")
        except BaseException as exc:
            if bacnet_error is None or not isinstance(exc, (bacnet_error, Exception)):
                raise
            logging.warning("%s: wrote but could not read back to verify (%s). "
                            "Set verify_writes: false to silence this.", tgt, exc)
            return
        actual = len(readback) if readback is not None else 0
        if actual != expected:
            raise DriverError(
                f"{tgt}: wrote {expected} special event(s) but read back "
                f"{actual}. The controller may be rejecting the write — check "
                "that the schedule is not locked by the vendor tool and that "
                "this device has write privilege.")

    def describe(self, target: str, windows: list) -> str:
        """Preview the actual per-date encoding, not just the windows."""
        try:
            tgt = parse_target(target)
        except DriverError as exc:
            return f"{target}: INVALID — {exc}"
        by_date = windows_to_daily(windows, self.tz)
        if not by_date:
            return f"{tgt}: CLEAR Exception_Schedule (no bookings)"
        off = "NULL" if _is_null_setting(self.unoccupied_value) else "OFF"
        lines = [f"{tgt}: {len(by_date)} special event(s), "
                 f"eventPriority={self.event_priority}"]
        for day in sorted(by_date):
            pairs = ", ".join(
                f"{dt.strftime('%H:%M')}->{'ON' if val else off}"
                for dt, val in by_date[day])
            lines.append(f"      {day.isoformat()}  {pairs}")
        return "\n".join(lines)

    # ── heartbeat ────────────────────────────────────────────────────────────

    @property
    def has_heartbeat(self) -> bool:
        return bool(self.heartbeat_object)

    def write_heartbeat(self, stamp: datetime) -> None:
        """Write the run time to `heartbeat_object`, if configured."""
        if not self.heartbeat_object:
            return
        device_id, obj_type, instance, address = parse_object(self.heartbeat_object)
        tgt = BacnetTarget(device_id, instance, address)
        try:
            self._run(self._write_heartbeat(tgt, obj_type, stamp))
        except DriverError as exc:
            raise DriverError(f"heartbeat {self.heartbeat_object}: {exc}") from None
        logging.info("Heartbeat written to %s", self.heartbeat_object)

    async def _write_heartbeat(self, tgt: BacnetTarget, obj_type: str,
                               stamp: datetime) -> None:
        from bacpypes3.basetypes import DateTime
        from bacpypes3.primitivedata import CharacterString, Real, Time

        address = await self._resolve(tgt)
        local = stamp.astimezone(self.tz)
        value: Any
        if obj_type == "analog-value":
            # Hours since the Unix epoch fit a 32-bit REAL exactly, and change
            # every run — which is all a "value unchanged for a day" alarm
            # needs.
            value = Real(round(local.timestamp() / 3600.0, 2))
        elif obj_type == "characterstring-value":
            value = CharacterString(local.isoformat(timespec="seconds"))
        else:
            value = DateTime(date=_bacnet_date(local.date()),
                             time=Time((local.hour, local.minute, local.second, 0)))
        await self._app.write_property(
            address, f"{obj_type},{tgt.schedule_instance}", "present-value",
            value, priority=self.heartbeat_priority)


def _bacnet_date(day: _date):
    """A python date as a BACnet Date (year offset from 1900, 1-based weekday)."""
    from bacpypes3.primitivedata import Date
    return Date((day.year - 1900, day.month, day.day, day.isoweekday()))


def _check_local_address(local_address: str, system_name: str) -> None:
    """
    Fail fast, and usefully, when `local_address` is not this host's.

    Binding a throwaway UDP socket is the portable way to ask "is this address
    mine?" — it does not depend on enumerating interfaces, which differs across
    platforms. The port is left at 0 so this never collides with a BACnet stack
    already running here.
    """
    ip = local_address.split("/", 1)[0].split(":", 1)[0].strip()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((ip, 0))
    except OSError as exc:
        raise DriverError(
            f"System '{system_name}': local_address {local_address!r} is not an "
            f"address on this host ({exc.strerror or exc}). It must be this "
            "machine's own NIC address with its prefix length, e.g. "
            f"\"10.4.1.55/24\". Addresses available here: "
            f"{', '.join(_host_addresses()) or 'none found'}.") from exc
    finally:
        probe.close()


def _host_addresses() -> list:
    """
    This host's IPv4 addresses, for the error message above.

    Two sources, because neither alone is reliable: resolving the hostname
    misses addresses on a box with no DNS entry for itself, and the
    connect-to-a-remote trick only ever reveals the primary outbound
    interface — which is usually, but not always, the one you want here.
    No packets are sent by the UDP connect.
    """
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))     # TEST-NET-1: reserved, unroutable
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(a for a in found if not a.startswith("127."))


def _ip_bytes(host: str) -> bytes:
    """Dotted-quad (or resolvable hostname) to the 4 bytes a HostNPort wants."""
    return socket.inet_aton(socket.gethostbyname(host))
