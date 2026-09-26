"""Offline tests: config validation, unknown keys, and target canonicalisation."""

import os
import tempfile

import pytest

from bassync.config import ConfigError, load_config, load_credentials
from bassync.drivers.bacnet import BacnetScheduleWriter, foreign_exceptions
from bassync.model import Destination
from bassync.spacemap import load_space_map
from tests.helpers import base_config, with_yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(text: str, defaults: str = "") -> tuple:
    """load_config over YAML text; returns (cfg, warnings)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        dpath = None
        if defaults:
            dpath = os.path.join(d, "defaults.yaml")
            with open(dpath, "w", encoding="utf-8") as fh:
                fh.write(defaults)
        warnings: list = []
        return load_config(path, dpath, warnings), warnings


# ── config.yaml ──────────────────────────────────────────────────────────────

def test_shipped_examples_load_cleanly():
    """The example files are the documentation. They must load without a
    single error or warning, or every new site starts with noise."""
    warnings: list = []
    cfg = load_config(os.path.join(REPO, "config.example.yaml"),
                      os.path.join(REPO, "defaults.example.yaml"), warnings)
    assert warnings == [], warnings
    sm = load_space_map(os.path.join(REPO, "space_mapping.example.yaml"), cfg)
    assert sm.errors == [], sm.errors
    assert sm.warnings == [], sm.warnings
    assert len(sm) > 5


def test_misspelt_timezone_is_a_config_error_not_a_traceback():
    with pytest.raises(ConfigError, match="America/NewYork"):
        _load('timezone: "America/NewYork"\n')


def test_empty_section_keeps_the_defaults():
    """`alerts:` with nothing under it parses as None; that used to replace
    the whole section and crash the run on the first lookup."""
    cfg, _ = _load("alerts:\nsafety:\ncollegenet:\n")
    assert cfg["alerts"]["enabled"] is False
    assert cfg["safety"]["max_cleared_fraction"] == 0.34
    assert cfg["collegenet"]["lookahead_days"] == 7


def test_numeric_strings_are_normalised_and_ranges_enforced():
    cfg, _ = _load('collegenet:\n  lookahead_days: "14"\n')
    assert cfg["collegenet"]["lookahead_days"] == 14
    with pytest.raises(ConfigError, match="lookahead_days"):
        _load("collegenet:\n  lookahead_days: 0\n")
    with pytest.raises(ConfigError, match="max_cleared_fraction"):
        _load("safety:\n  max_cleared_fraction: 3\n")


def test_bad_smtp_port_fails_at_load():
    """It used to make send_alert — which must never raise — raise."""
    with pytest.raises(ConfigError, match="smtp_port"):
        _load("alerts:\n  email:\n    smtp_port: 587x\n")


def test_unknown_keys_are_warned_about():
    _cfg, warnings = _load("alerts:\n  notify_on_sucess: true\ntimezon: UTC\n")
    text = "\n".join(warnings)
    assert "alerts.notify_on_sucess" in text
    assert "timezon" in text


def test_systems_are_validated_per_driver():
    with pytest.raises(ConfigError, match="Mars/Base"):
        _load("systems:\n  a:\n    driver: preview\n    timezone: Mars/Base\n")
    with pytest.raises(ConfigError, match="Unknown BAS driver"):
        _load("systems:\n  a:\n    driver: webctrl\n")
    with pytest.raises(ConfigError, match="event_priority"):
        _load("systems:\n  a:\n    driver: bacnet\n    event_priority: 20\n")
    _cfg, warnings = _load("systems:\n  a:\n    driver: preview\n    csv_fil: x\n")
    assert any("csv_fil" in w for w in warnings), warnings


def test_default_system_must_exist():
    with pytest.raises(ConfigError, match="default_system"):
        _load("systems:\n  a:\n    driver: preview\ndefault_system: b\n")


def test_defaults_file_is_validated_too():
    with pytest.raises(ConfigError, match="default_pre_condition_minutes"):
        _load("", defaults="pre_condition_minutes: -30\n")
    _cfg, warnings = _load("", defaults="lookahead: 9\n")
    assert any("lookahead" in w for w in warnings), warnings


def test_webhook_url_can_come_from_the_environment(monkeypatch):
    cfg, _ = _load("")
    monkeypatch.setenv("BAS_ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    load_credentials(cfg)
    assert cfg["alerts"]["webhook_url"] == "https://hooks.example/abc"


# ── space_mapping.yaml ───────────────────────────────────────────────────────

def bacnet_config():
    cfg = base_config()
    cfg["systems"] = {"bac": {"driver": "bacnet"}}
    cfg["default_system"] = "bac"
    return cfg


def test_misspelt_target_is_warned_about():
    text = ('buildings:\n  - {id: b, target: "9:9"}\n'
            'spaces:\n  - {space_id: 1, building: b, tagret: "12001:5"}\n')
    sm = with_yaml(text, lambda p: load_space_map(p, bacnet_config()))
    assert any("`tagret`" in w and "Did you mean `target`" in w
               for w in sm.warnings), sm.warnings


def test_out_of_range_buffers_are_row_errors():
    text = ('buildings: []\nspaces:\n'
            '  - {space_id: 1, target: "1:1", pre_condition_minutes: -120}\n'
            '  - {space_id: 2, target: "1:2", post_buffer_minutes: 100000}\n'
            '  - {space_id: 3, target: "1:3"}\n')
    sm = with_yaml(text, lambda p: load_space_map(p, bacnet_config()))
    assert sorted(sm.spaces) == ["3"]
    assert len(sm.errors) == 2, sm.errors


def test_two_spellings_of_one_bacnet_schedule_are_one_destination():
    """`12001:5` and `12001:5@10.4.2.30` are the same object. Written
    separately, the second write would erase the first room's bookings."""
    text = ('buildings: []\nspaces:\n'
            '  - {space_id: 1, target: "12001:5"}\n'
            '  - {space_id: 2, target: "12001:5@10.4.2.30"}\n'
            '  - {space_id: 3, target: "12001:6"}\n')
    sm = with_yaml(text, lambda p: load_space_map(p, bacnet_config()))
    assert sm.errors == [], sm.errors
    assert sm.destinations() == {
        Destination("bac", "12001:5@10.4.2.30:47808"),
        Destination("bac", "12001:6@10.4.2.30:47808"),   # the pin is per device
    }
    assert any("target of 2 rooms" in w for w in sm.warnings), sm.warnings


def test_conflicting_pins_for_one_device_are_errors():
    text = ('buildings: []\nspaces:\n'
            '  - {space_id: 1, target: "12001:5@10.4.2.30"}\n'
            '  - {space_id: 2, target: "12001:6@10.4.2.31"}\n'
            '  - {space_id: 3, target: "12002:1"}\n')
    sm = with_yaml(text, lambda p: load_space_map(p, bacnet_config()))
    assert sorted(sm.spaces) == ["3"]
    assert any("more than one address" in e for e in sm.errors), sm.errors


def test_malformed_targets_are_caught_at_load():
    text = ('buildings:\n  - {id: b, target: "not-a-target"}\n'
            'spaces:\n'
            '  - {space_id: 1, building: b}\n'              # rolls up only into b
            '  - {space_id: 2, building: b, target: "12001:5"}\n'
            '  - {space_id: 3, target: "12001:4194303"}\n')  # reserved instance
    sm = with_yaml(text, lambda p: load_space_map(p, bacnet_config()))
    assert sorted(sm.spaces) == ["2"], sm.spaces
    assert sm.spaces["2"].building_destination is None
    text_errors = "\n".join(sm.errors)
    assert "Building b" in text_errors and "4194303" in text_errors


# ── BACnet driver config ─────────────────────────────────────────────────────

def test_bacnet_check_config():
    assert BacnetScheduleWriter.check_config({}) == []
    problems = BacnetScheduleWriter.check_config(
        {"event_priority": 0, "value_type": "string",
         "heartbeat_object": "599100:binary-output,1",
         "unoccupied_value": "off"})
    text = " ".join(problems)
    assert "event_priority" in text and "value_type" in text
    assert "heartbeat_object" in text and "unoccupied_value" in text
    assert BacnetScheduleWriter.check_config({"unoccupied_value": "null"}) == []


def test_foreign_exceptions_are_counted():
    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    ours = Obj(eventPriority=16, period=Obj(calendarEntry=Obj(date=(126, 6, 10, 3))))
    holiday_ref = Obj(eventPriority=16, period=Obj(calendarEntry=None))
    other_priority = Obj(eventPriority=5, period=Obj(calendarEntry=Obj(date=(126, 6, 10, 3))))
    assert foreign_exceptions([ours], 16) == 0
    assert foreign_exceptions([ours, holiday_ref, other_priority], 16) == 2


def test_value_encoding_matches_the_schedule_type():
    pytest.importorskip("bacpypes3")
    from bacpypes3.primitivedata import Boolean, Enumerated, Null, Real, Unsigned

    from bassync.drivers.bacnet import DriverError, encode_value, value_kind
    assert isinstance(encode_value("boolean", True), Boolean)
    on, off = encode_value("enumerated", True), encode_value("enumerated", False)
    assert isinstance(on, Enumerated) and (int(on), int(off)) == (1, 0)
    assert isinstance(encode_value("real", True), Real)
    assert isinstance(encode_value("enumerated", False, unoccupied_value="null"), Null)
    with pytest.raises(DriverError, match="multistate"):
        encode_value("unsigned", True)
    assert int(encode_value("unsigned", True, occupied_value=1)) == 1
    assert isinstance(encode_value("unsigned", False, 1, 2), Unsigned)
    assert value_kind(Enumerated(0)) == "enumerated"
    assert value_kind(Boolean(False)) == "boolean"


def test_niagara_driver_is_deprecated_but_still_loads():
    """Niagara stations are driven through their BACnet schedule export; the
    REST driver keeps working for existing sites, with a warning saying so."""
    cfg, warnings = _load("systems:\n  station:\n    driver: niagara\n"
                          "    host: n4.example.edu\n")
    assert cfg["systems"]["station"]["driver"] == "niagara"
    assert any("niagara driver is deprecated" in w and "schedule export" in w
               for w in warnings), warnings
    # A pre-1.0 top-level `niagara:` block is promoted to the same driver, so
    # those sites see the warning too.
    _cfg, legacy = _load("niagara:\n  host: n4.example.edu\n")
    assert any("deprecated" in w for w in legacy), legacy


def test_bacnet_has_no_deprecation_warning():
    _cfg, warnings = _load("systems:\n  station:\n    driver: bacnet\n")
    assert warnings == [], warnings
