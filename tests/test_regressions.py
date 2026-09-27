"""Offline tests: regressions found in the 1.1 audit."""

from datetime import datetime
from zoneinfo import ZoneInfo

from bassync.model import OccupancyWindow
from bassync.spacemap import load_space_map
from tests.helpers import base_config, with_yaml


def test_bacnet_splits_at_the_devices_midnight_not_the_campus():
    """A building in another timezone must be split at ITS local midnight.

    Splitting in campus time first produced a window that turned ON at 23:30
    and never turned OFF — the controller would have run until the next
    exception, which is to say all night and most of the next day."""
    from bassync.drivers.bacnet import windows_to_daily
    campus, device = ZoneInfo("America/New_York"), ZoneInfo("America/Chicago")
    # 00:30-02:00 Eastern is 23:30-01:00 Central: two dates on the device.
    w = OccupancyWindow(datetime(2026, 6, 11, 0, 30, tzinfo=campus),
                        datetime(2026, 6, 11, 2, 0, tzinfo=campus))
    by_date = windows_to_daily([w], device)
    assert len(by_date) == 2, by_date
    first = by_date[datetime(2026, 6, 10).date()]
    second = by_date[datetime(2026, 6, 11).date()]
    assert [(t.strftime("%H:%M"), v) for t, v in first] == [("23:30", True)], first
    assert [(t.strftime("%H:%M"), v) for t, v in second] == [
        ("00:00", True), ("01:00", False)], second


def test_bacnet_rejects_out_of_range_instances():
    """Instances are 22-bit, and 4194303 is the reserved 'unconfigured' value a
    controller reports before commissioning — never a real address."""
    from bassync.drivers.bacnet import parse_target
    from bassync.drivers.base import DriverError
    for bad in ("4194303:5", "12001:4194303", "4194304:5", "12001:9999999"):
        try:
            parse_target(bad)
        except DriverError:
            continue
        raise AssertionError(f"{bad!r} should be rejected")
    assert parse_target("4194302:5").device_id == 4194302   # the real maximum


def test_malformed_rows_are_reported_not_raised():
    """One bad room must not take the other four hundred down with it."""
    cases = {
        "non-numeric minutes": ('spaces:\n  - space_id: 1\n    target: "B/R"\n'
                                '    pre_condition_minutes: "forty-five"\n'),
        "non-numeric gap": ('spaces:\n  - space_id: 2\n    target: "B/R"\n'
                            '    merge_gap_minutes: wide\n'),
        "room is not a mapping": 'spaces:\n  - "just a string"\n',
        "building is not a mapping": 'buildings:\n  - "oops"\nspaces: []\n',
    }
    for name, text in cases.items():
        sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
        assert sm.errors, f"{name}: expected a reported error"
        assert not sm.spaces, f"{name}: the bad row must not be registered"


def test_good_rows_survive_a_bad_neighbour():
    """The rest of the map still loads around a broken row."""
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Rm1"
  - space_id: 2
    target: "B/Rm2"
    pre_condition_minutes: "oops"
  - space_id: 3
    target: "B/Rm3"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert set(sm.spaces) == {"1", "3"}, sm.spaces
    assert any("Room 2" in e for e in sm.errors), sm.errors


def test_integral_float_space_id_normalises():
    """YAML reads `1234.0` as a float; str() gives '1234.0', which would never
    match the '1234' 25Live sends and the room would silently never sync."""
    text = 'buildings: []\nspaces:\n  - space_id: 1234.0\n    target: "B/R"\n'
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert list(sm.spaces) == ["1234"], sm.spaces
