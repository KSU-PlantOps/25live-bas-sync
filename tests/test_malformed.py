"""Offline tests: malformed input handling."""



from bassync.config import load_config
from bassync.spacemap import load_space_map
from tests.helpers import base_config, dest, with_yaml


def test_load_config_rejects_malformed_yaml():
    """A stray tab in config.yaml fails with one clear, file-named line rather
    than a stack trace — and never by silently falling back to defaults, which
    would write the wrong schedules to the wrong station."""
    from bassync.config import ConfigError
    try:
        with_yaml("collegenet:\n\tinstance: oops\n", load_config)
    except ConfigError as exc:
        assert ".yaml" in str(exc), exc
    else:
        raise AssertionError("malformed YAML should raise ConfigError")
    # A top level that isn't a mapping is equally unusable.
    try:
        with_yaml("- just\n- a list\n", load_config)
    except ConfigError as exc:
        assert "mapping" in str(exc), exc
    else:
        raise AssertionError("a non-mapping config should raise ConfigError")


def test_read_yaml_tolerates_missing_and_empty():
    """Missing and empty are normal, not errors — a site that hasn't written
    defaults.yaml yet must still run."""
    from bassync.config import read_yaml
    assert read_yaml("/nonexistent/nothing.yaml") == {}
    assert with_yaml("", read_yaml) == {}
    assert with_yaml("# just a comment\n", read_yaml) == {}


def test_editor_config_form_roundtrip_preserves_other_sections():
    """The Connection tab must not eat config it doesn't display — retry,
    alerts and safety all have to survive a save."""
    from bassync import editor
    raw = {"systems": {"campus": {"driver": "bacnet",
                                  "local_address": "10.1.1.5/24"},
                       "sup": {"driver": "niagara", "host": "n4", "port": 8443}},
           "default_system": "campus",
           "retry": {"attempts": 5},
           "alerts": {"enabled": True, "webhook_url": "http://hook"},
           "safety": {"max_cleared_fraction": 0.5}}
    assert editor.config_systems(raw) == {"campus": "bacnet", "sup": "niagara"}

    form = editor.form_from_raw(raw, "campus", "bacnet")
    assert form["systems.campus.local_address"] == "10.1.1.5/24"
    form["systems.campus.local_address"] = "10.9.9.9/24"
    out = editor.apply_config_form(raw, form, "campus", "bacnet")

    assert out["systems"]["campus"]["local_address"] == "10.9.9.9/24"
    # The system that wasn't on screen is untouched...
    assert out["systems"]["sup"] == {"driver": "niagara", "host": "n4",
                                     "port": 8443}, out["systems"]["sup"]
    # ...and so is everything the form never shows.
    assert out["retry"] == {"attempts": 5}
    assert out["alerts"]["webhook_url"] == "http://hook"
    assert out["safety"] == {"max_cleared_fraction": 0.5}


def test_editor_config_form_shows_driver_specific_fields():
    """A BACnet system must not be offered Niagara's ORD boxes."""
    from bassync import editor
    bacnet = {k for k, *_ in editor.system_config_fields("s", "bacnet")}
    niagara = {k for k, *_ in editor.system_config_fields("s", "niagara")}
    assert ("systems", "s", "local_address") in bacnet
    assert ("systems", "s", "schedule_base_path") not in bacnet
    assert ("systems", "s", "schedule_base_path") in niagara
    assert ("systems", "s", "local_address") not in niagara
    # An unrecognised driver contributes no fields rather than exploding.
    assert editor.system_config_fields("s", "nonesuch") == []


def test_editor_config_form_invalid_int_raises():
    """A non-numeric port is reported against its label, not swallowed."""
    from bassync import editor
    form = editor.form_from_raw({}, "sup", "niagara")
    form["systems.sup.port"] = "eight-thousand"
    try:
        editor.apply_config_form({}, form, "sup", "niagara")
    except ValueError as exc:
        assert "Port" in str(exc), exc
        return
    raise AssertionError("a non-numeric int should raise ValueError")


def test_editor_floors_roundtrip():
    """Floors survive the editor's dump -> load -> sync-loader path."""
    from bassync import editor
    buildings = [{"id": "b", "target": "B/Occ"}]
    floors = [{"building": "b", "level": 3, "target": "B/F3_Corridor"}]
    rooms = [{"space_id": 1, "building": "b", "floor": 3, "target": "B/Rm301"}]

    def _check(path):
        b2, f2, r2 = editor.load_mapping(path)
        assert len(b2) == 1 and len(f2) == 1 and len(r2) == 1, (b2, f2, r2)
        return load_space_map(path, base_config())

    sm = with_yaml(editor.dump_mapping(buildings, floors, rooms), _check)
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].floor_destination == dest("B/F3_Corridor")


def test_editor_migrates_legacy_key_in_all_three_sections():
    """A pre-1.0 map — buildings, floors and rooms — opens with every
    niagara_path renamed to target."""
    from bassync import editor
    text = ("buildings:\n  - id: b\n    niagara_path: 'B/Occ'\n"
            "floors:\n  - building: b\n    level: 1\n"
            "    niagara_path: 'B/F1'\n"
            "spaces:\n  - space_id: 1\n    niagara_path: 'B/Rm1'\n")
    buildings, floors, rooms = with_yaml(text, editor.load_mapping)
    for row in (buildings[0], floors[0], rooms[0]):
        assert "niagara_path" not in row, row
        assert row["target"].startswith("B/"), row
