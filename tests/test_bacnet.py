"""Offline tests: BACnet encoding."""

from datetime import datetime

import pytest

from bassync.model import OccupancyWindow
from tests.helpers import TZ, dt


def test_bacnet_target_parsing():
    from bassync.drivers.bacnet import DEFAULT_BACNET_PORT, parse_target
    t = parse_target("12001:5")
    assert (t.device_id, t.schedule_instance, t.address) == (12001, 5, None)
    t = parse_target("12001:5@10.4.2.30")
    assert t.address == f"10.4.2.30:{DEFAULT_BACNET_PORT}"
    assert parse_target("12001:5@10.4.2.30:47810").address == "10.4.2.30:47810"


def test_bacnet_routed_mac_spellings():
    """A one-byte MS/TP MAC is canonical in decimal; a longer one — a
    BACnet/SC VMAC, or an IP device behind a router — stays hex with every
    byte, leading zeros included. Decimal used to swallow a six-byte VMAC
    into a number BACpypes3 rejects."""
    from bassync.drivers.bacnet import parse_target
    assert parse_target("12001:5@2001:0x21").address == "2001:33"
    assert parse_target("12001:5@2001:33").address == "2001:33"
    assert parse_target("12001:5@2001:0x0A").address == "2001:10"
    assert (parse_target("12001:5@3001:0x0012AB34CD56").address
            == "3001:0x0012ab34cd56")


def test_bacnet_routed_mac_rejects_ambiguous():
    from bassync.drivers.bacnet import parse_target
    from bassync.drivers.base import DriverError
    # Half a byte, and a decimal too big for one byte, are both mistakes.
    for bad in ("12001:5@3001:0x012ab34cd56", "12001:5@3001:1250999896491"):
        with pytest.raises(DriverError, match="0x0012ab34cd56"):
            parse_target(bad)


def test_bacnet_routed_vmac_pin_applies_to_the_device():
    """Two spellings of one SC device's pin agree, so they are one pin."""
    from bassync.drivers.bacnet import BacnetScheduleWriter
    canonical, errors = BacnetScheduleWriter.normalize_targets(
        {}, ["12001:5@3001:0x0012AB34CD56", "12001:6@3001:0x0012ab34cd56", "12001:7"])
    assert errors == {}
    assert set(canonical.values()) == {f"12001:{n}@3001:0x0012ab34cd56" for n in (5, 6, 7)}


def test_bacnet_routed_addresses_are_ones_bacpypes3_accepts():
    """The canonical spelling must round-trip through BACpypes3's Address,
    which is what the driver hands it."""
    pytest.importorskip("bacpypes3")
    from bacpypes3.pdu import Address

    from bassync.drivers.bacnet import parse_target
    for text, mac in (("12001:5@2001:0x21", "21"),
                      ("12001:5@3001:0x0012AB34CD56", "0012ab34cd56")):
        address = Address(parse_target(text).address)
        assert address.addrNet == int(text.split("@")[1].split(":")[0])
        assert bytes(address.addrAddr).hex() == mac
        assert str(address) == parse_target(text).address


def test_bacnet_target_rejects_garbage():
    from bassync.drivers.bacnet import parse_target
    from bassync.drivers.base import DriverError
    for bad in ("", "slot:/Schedules/Rm1", "12001", "abc:5"):
        try:
            parse_target(bad)
        except DriverError:
            continue
        raise AssertionError(f"{bad!r} should not parse as a BACnet target")


def test_bacnet_groups_windows_by_date():
    """One special event per calendar date, with alternating ON/OFF times.

    This is what keeps the Exception_Schedule array inside the limits real
    controllers enforce — a per-booking encoding would need one array entry per
    class."""
    from bassync.drivers.bacnet import windows_to_daily
    windows = [OccupancyWindow(dt(9, day=10), dt(11, day=10)),
               OccupancyWindow(dt(13, day=10), dt(17, day=10)),
               OccupancyWindow(dt(8, day=11), dt(9, day=11))]
    by_date = windows_to_daily(windows, TZ)
    assert len(by_date) == 2, by_date
    day10 = by_date[dt(9, day=10).date()]
    assert [(d.strftime("%H:%M"), v) for d, v in day10] == [
        ("09:00", True), ("11:00", False), ("13:00", True), ("17:00", False)], day10


def test_bacnet_splits_windows_across_midnight():
    """A booking running past midnight becomes an entry on each date — BACnet
    exceptions are keyed by calendar date and a time cannot be 24:00."""
    from bassync.drivers.bacnet import windows_to_daily
    by_date = windows_to_daily(
        [OccupancyWindow(dt(22, day=10), dt(2, day=11))], TZ)
    assert len(by_date) == 2, by_date
    first = by_date[dt(0, day=10).date()]
    second = by_date[dt(0, day=11).date()]
    # Day one turns ON and does not turn OFF — midnight rollover re-evaluates
    # against day two's exception, so a trailing OFF would be both illegal and
    # redundant.
    assert [(d.strftime("%H:%M"), v) for d, v in first] == [("22:00", True)], first
    assert [(d.strftime("%H:%M"), v) for d, v in second] == [
        ("00:00", True), ("02:00", False)], second


def test_bacnet_encodes_a_real_exception_schedule():
    """The array actually encodes and decodes as BACnet. Skipped when
    BACpypes3 isn't installed, since it is an optional dependency."""
    pytest.importorskip("bacpypes3")
    from bacpypes3.basetypes import CalendarEntry, SpecialEvent, SpecialEventPeriod, TimeValue
    from bacpypes3.constructeddata import ArrayOf
    from bacpypes3.primitivedata import Boolean, Time

    from bassync.drivers.bacnet import _bacnet_date, windows_to_daily

    by_date = windows_to_daily(
        [OccupancyWindow(dt(9, day=10), dt(17, day=10))], TZ)
    day = sorted(by_date)[0]
    events = [SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry(date=_bacnet_date(day))),
        listOfTimeValues=[TimeValue(time=Time((d.hour, d.minute, d.second, 0)),
                                    value=Boolean(v)) for d, v in by_date[day]],
        eventPriority=16)]
    array_type = ArrayOf(SpecialEvent)
    decoded = array_type.decode(array_type(events).encode())
    assert len(decoded) == 1
    assert decoded[0].eventPriority == 16
    assert len(decoded[0].listOfTimeValues) == 2
    assert str(decoded[0].period.calendarEntry.date).startswith("2026-6-10")


def test_bacnet_describe_is_offline():
    """--dry-run must never touch the network, including for BACnet."""
    from bassync.drivers.bacnet import BacnetScheduleWriter
    writer = BacnetScheduleWriter("campus", {"local_address": "10.0.0.1/24"}, TZ)
    text = writer.describe("12001:5", [OccupancyWindow(dt(9, day=10), dt(17, day=10))])
    assert "12001:5" in text and "09:00->ON" in text and "17:00->OFF" in text, text
    assert "CLEAR" in writer.describe("12001:5", [])


def test_bacnet_fails_fast_on_a_wrong_local_address():
    """A local_address that isn't this host's must fail immediately with a
    usable message. BACpypes3 retries a failed bind forever, so without this
    check a nightly run hangs instead of alerting — strictly worse, because a
    hung job never tells anyone."""
    import time

    from bassync.drivers.bacnet import BacnetScheduleWriter
    from bassync.drivers.base import DriverError
    # TEST-NET-1: reserved by RFC 5737, so it is never a real host address.
    writer = BacnetScheduleWriter("campus", {"local_address": "192.0.2.77/24"}, TZ)
    started = time.monotonic()
    try:
        ok, detail = writer.health_check()
    finally:
        writer.close()
    elapsed = time.monotonic() - started
    assert not ok, "a bogus local_address must not report healthy"
    assert elapsed < 5, f"took {elapsed:.1f}s — it should fail fast, not retry"
    # Without BACpypes3 the missing library is reported first: it is the harder
    # blocker, and naming the address instead would send the operator to fix
    # the wrong thing.
    import importlib.util
    expected = ("not an address on this host"
                if importlib.util.find_spec("bacpypes3")
                else "needs BACpypes3")
    assert expected in detail, detail
    try:
        writer.connect()
    except DriverError:
        return
    raise AssertionError("connect() should raise DriverError")


def test_bacnet_rejects_bad_event_priority():
    """eventPriority is 1-16; anything else is a config error caught at startup
    rather than a rejected write at 2 AM."""
    from bassync.drivers.bacnet import BacnetScheduleWriter
    from bassync.drivers.base import DriverError
    try:
        BacnetScheduleWriter("x", {"local_address": "10.0.0.1/24",
                                   "event_priority": 0}, TZ)
    except DriverError:
        return
    raise AssertionError("event_priority 0 should be rejected")


def test_split_at_midnight_handles_dst_forward():
    """The spring-forward day is 23 hours long. Splitting must still land on
    real local midnight, not 24 hours after the start."""
    from bassync.drivers.base import ScheduleWriter
    # 2026-03-08 is the US DST spring-forward date.
    start = datetime(2026, 3, 7, 22, 0, tzinfo=TZ)
    end = datetime(2026, 3, 8, 4, 0, tzinfo=TZ)
    pieces = ScheduleWriter.split_at_midnight([OccupancyWindow(start, end)])
    assert len(pieces) == 2, pieces
    boundary = pieces[0].end
    assert (boundary.hour, boundary.minute) == (0, 0), boundary
    assert boundary.date() == end.date(), boundary
    assert pieces[1].start == boundary and pieces[1].end == end
