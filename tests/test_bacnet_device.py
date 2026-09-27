"""
BACnet driver against a simulated controller.

Each test stands up a real BACpypes3 device on loopback — a Device object, a
Schedule object whose Schedule_Default sets the value type, and an analog
value for the heartbeat — and drives it with the production driver over real
UDP. This is the round-trip the encoding tests cannot prove: that what the
sync puts on the wire is accepted, typed correctly, and read back.

Skipped when BACpypes3 isn't installed.
"""

import asyncio
import socket
import threading
from datetime import datetime

import pytest

pytest.importorskip("bacpypes3")

from bacpypes3.app import Application  # noqa: E402
from bacpypes3.basetypes import (  # noqa: E402
    BinaryPV,
    CalendarEntry,
    DailySchedule,
    DateRange,
    SpecialEvent,
    SpecialEventPeriod,
    TimeValue,
)
from bacpypes3.constructeddata import ArrayOf  # noqa: E402
from bacpypes3.local.analog import AnalogValueObject  # noqa: E402
from bacpypes3.local.device import DeviceObject  # noqa: E402
from bacpypes3.local.networkport import NetworkPortObject  # noqa: E402
from bacpypes3.local.schedule import ScheduleObject  # noqa: E402
from bacpypes3.primitivedata import (  # noqa: E402
    Boolean,
    Date,
    Enumerated,
    Real,
    Time,
    Unsigned,
)

from bassync.drivers.bacnet import BacnetScheduleWriter  # noqa: E402
from bassync.drivers.base import DriverError  # noqa: E402
from bassync.model import OccupancyWindow  # noqa: E402
from tests.helpers import TZ  # noqa: E402

DEVICE_ID = 12001


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SimController:
    """A BACnet device on 127.0.0.1 running on its own thread and loop."""

    def __init__(self, default, exceptions=None):
        self.port = _free_port()
        self.default = default
        self.exceptions = exceptions or []
        self.schedule = None
        self.heartbeat = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(10), "simulated controller did not start"
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._main())
        loop.close()

    async def _main(self):
        device = DeviceObject(objectIdentifier=("device", DEVICE_ID),
                              objectName="SimController", vendorIdentifier=999)
        # /32 so no broadcast listener is created — targets are pinned.
        port = NetworkPortObject(f"127.0.0.1/32:{self.port}",
                                 objectIdentifier=("network-port", 1),
                                 objectName="NP-1")
        self.schedule = ScheduleObject(
            objectIdentifier=("schedule", 5), objectName="Rm101 Booking",
            presentValue=self.default, scheduleDefault=self.default,
            weeklySchedule=ArrayOf(DailySchedule)(
                [DailySchedule(daySchedule=[]) for _ in range(7)]),
            exceptionSchedule=self.exceptions,
            listOfObjectPropertyReferences=[], priorityForWriting=16,
            effectivePeriod=DateRange(startDate=Date((100, 1, 1, 255)),
                                      endDate=Date((200, 12, 31, 255))))
        self.heartbeat = AnalogValueObject(
            objectIdentifier=("analog-value", 1), objectName="Sync Heartbeat",
            presentValue=Real(0.0))
        app = Application.from_object_list([device, port, self.schedule,
                                            self.heartbeat])
        for link_layer in app.link_layers.values():
            await link_layer.server._local_transport_ready.wait()
        self._ready.set()
        while not self._stop.is_set():
            await asyncio.sleep(0.02)
        app.close()
        await asyncio.sleep(0)


def _writer(**extra) -> BacnetScheduleWriter:
    cfg = {"local_address": f"127.0.0.1/32:{_free_port()}", "device_id": 599001,
           "operation_timeout": 5, **extra}
    return BacnetScheduleWriter("sim", cfg, TZ)


def _window(day=10, start=9, end=11):
    return OccupancyWindow(datetime(2026, 10, day, start, tzinfo=TZ),
                           datetime(2026, 10, day, end, tzinfo=TZ))


def _written_values(sim) -> list:
    return [tv.value.get_value() if hasattr(tv.value, "get_value") else tv.value
            for event in sim.schedule.exceptionSchedule
            for tv in event.listOfTimeValues]


def test_enumerated_schedule_gets_enumerated_values():
    """A binary schedule is usually ENUMERATED (active/inactive). Writing
    BOOLEAN into it is a datatype violation real controllers reject."""
    with SimController(BinaryPV("inactive")) as sim:
        w = _writer()
        try:
            w.write_schedule(f"{DEVICE_ID}:5@{sim.address}", [_window()])
        finally:
            w.close()
        values = _written_values(sim)
        assert values and all(isinstance(v, Enumerated) for v in values), values
        assert [int(v) for v in values] == [1, 0]


def test_boolean_schedule_gets_boolean_values():
    with SimController(Boolean(False)) as sim:
        w = _writer()
        try:
            w.write_schedule(f"{DEVICE_ID}:5@{sim.address}", [_window()])
        finally:
            w.close()
        values = _written_values(sim)
        assert values and all(isinstance(v, Boolean) for v in values), values


def test_multistate_schedule_needs_configured_values():
    with SimController(Unsigned(2)) as sim:
        target = f"{DEVICE_ID}:5@{sim.address}"
        w = _writer()
        try:
            with pytest.raises(DriverError, match="multistate"):
                w.write_schedule(target, [_window()])
        finally:
            w.close()
        w = _writer(occupied_value=1, unoccupied_value=2)
        try:
            w.write_schedule(target, [_window()])
        finally:
            w.close()
        assert [int(v) for v in _written_values(sim)] == [1, 2]


def test_empty_windows_clear_the_exception_schedule():
    with SimController(BinaryPV("inactive")) as sim:
        target = f"{DEVICE_ID}:5@{sim.address}"
        w = _writer()
        try:
            w.write_schedule(target, [_window(), _window(day=11)])
            assert len(sim.schedule.exceptionSchedule) == 2
            w.write_schedule(target, [])
        finally:
            w.close()
        assert list(sim.schedule.exceptionSchedule or []) == []


def test_stale_pinned_address_is_refused():
    """The controller at the pinned IP is device 12001; a target claiming
    device 55555 there must fail instead of writing 12001's schedule 5."""
    with SimController(BinaryPV("inactive")) as sim:
        w = _writer()
        try:
            with pytest.raises(DriverError, match="is not BACnet device 55555"):
                w.write_schedule(f"55555:5@{sim.address}", [_window()])
        finally:
            w.close()
        assert list(sim.schedule.exceptionSchedule or []) == []


def test_bacnet_errors_become_driver_errors():
    """BACpypes3 raises Error/Reject/Abort as BaseException, which used to
    escape every `except Exception` and crash the whole nightly run."""
    with SimController(BinaryPV("inactive")) as sim:
        w = _writer()
        try:
            with pytest.raises(DriverError, match="unknown-object"):
                w.write_schedule(f"{DEVICE_ID}:77@{sim.address}", [_window()])
            ok, detail = w.target_exists(f"{DEVICE_ID}:77@{sim.address}")
            assert not ok and "unknown-object" in detail
        finally:
            w.close()


def test_offline_device_fails_that_target_quickly():
    w = _writer(operation_timeout=2)
    try:
        with pytest.raises(DriverError):
            w.write_schedule(f"{DEVICE_ID}:5@127.0.0.1:{_free_port()}", [_window()])
    finally:
        w.close()


def test_validate_warns_about_exceptions_the_sync_did_not_write():
    """Under the ownership model the sync replaces the whole array; a
    hand-entered holiday on the target must be flagged before that happens."""
    holiday = SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry(
            date=Date((126, 12, 25, 255)))),
        listOfTimeValues=[TimeValue(time=Time((0, 0, 0, 0)),
                                    value=BinaryPV("inactive"))],
        eventPriority=3)
    with SimController(BinaryPV("inactive"), exceptions=[holiday]) as sim:
        w = _writer()
        try:
            exists, detail, notes = w.inspect_target(f"{DEVICE_ID}:5@{sim.address}")
        finally:
            w.close()
        assert exists, detail
        assert "Rm101 Booking" in detail and "enumerated" in detail
        assert notes and "1 of its 1 exception(s)" in notes[0], notes


def test_heartbeat_is_written():
    with SimController(BinaryPV("inactive")) as sim:
        w = _writer(heartbeat_object=f"{DEVICE_ID}:analog-value,1@{sim.address}")
        try:
            w.write_heartbeat(datetime(2026, 10, 1, 2, 0, tzinfo=TZ))
        finally:
            w.close()
        expected = datetime(2026, 10, 1, 2, 0, tzinfo=TZ).timestamp() / 3600
        assert abs(float(sim.heartbeat.presentValue) - expected) < 1


def test_a_busy_port_fails_the_connect_and_cleans_up():
    """BACpypes3 binds in the background and retries forever. The connect
    timeout must cover the bind, and a failed connect must leave nothing
    half-started behind — a retry reports the same clean error."""
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", port))
    try:
        w = BacnetScheduleWriter("sim", {"local_address": f"127.0.0.1/32:{port}",
                                         "connect_timeout": 1}, TZ)
        for _attempt in range(2):
            ok, detail = w.health_check()
            assert not ok and "did not bind" in detail, detail
            assert w._app is None and w._loop is None
        w.close()
    finally:
        blocker.close()
