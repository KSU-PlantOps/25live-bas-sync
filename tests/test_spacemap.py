"""Offline tests: room map loader."""



from bassync.spacemap import load_space_map
from tests.helpers import base_config, dest, with_yaml


def test_loader_assigns_all_rooms_to_building():
    """Each room's `building` id resolves to the building's destination, so all
    rooms auto-roll-up without repeating the address."""
    text = """
buildings:
  - id: bldg_a
    name: "Building A"
    target: "A/Building_Occ"
spaces:
  - space_id: 11
    building: bldg_a
    target: "A/Rm11_Occ"
  - space_id: 12
    building: bldg_a
    target: "A/Rm12_Occ"
  - space_id: 13
    target: "A/Rm13_Occ"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert sm.spaces["11"].building_destination == dest("A/Building_Occ")
    assert sm.spaces["12"].building_destination == dest("A/Building_Occ")
    assert sm.spaces["13"].building_destination is None


def test_loader_accepts_legacy_niagara_path():
    """A pre-1.0 map using `niagara_path:` still loads unchanged, so an
    existing campus upgrades without a mass edit."""
    text = """
buildings:
  - id: b
    niagara_path: "B/Building_Occ"
spaces:
  - space_id: 1
    building: b
    niagara_path: "B/Rm1"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].destination == dest("B/Rm1")
    assert sm.spaces["1"].building_destination == dest("B/Building_Occ")


def test_loader_reads_per_room_merge_gap():
    """merge_gap_minutes is picked up per room, else the global default."""
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Rm1"
    merge_gap_minutes: 25
  - space_id: 2
    target: "B/Rm2"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert sm.spaces["1"].merge_gap_minutes == 25
    assert sm.spaces["2"].merge_gap_minutes == cfg["collegenet"]["merge_gap_minutes"]


def test_loader_preserves_explicit_zero_buffers():
    """An explicit 0 must be honored, not replaced by the default. Regression
    for the `value or default` coalescing bug — a room that deliberately
    disables pre-conditioning would otherwise start 30 minutes early."""
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Rm1"
    pre_condition_minutes: 0
    post_buffer_minutes: 0
    merge_gap_minutes: 0
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    s = sm.spaces["1"]
    assert (s.pre_condition_minutes, s.post_buffer_minutes,
            s.merge_gap_minutes) == (0, 0, 0), s


def test_building_runup_overrides_global_but_not_room():
    """Run-up/run-down precedence: room > building > global."""
    text = """
buildings:
  - id: b
    target: "B/Bldg"
    pre_condition_minutes: 50
    post_buffer_minutes: 20
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    target: "B/Rm2"
    pre_condition_minutes: 5
  - space_id: 3
    target: "B/Rm3"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert sm.spaces["1"].pre_condition_minutes == 50
    assert sm.spaces["1"].post_buffer_minutes == 20
    assert sm.spaces["2"].pre_condition_minutes == 5
    assert sm.spaces["2"].post_buffer_minutes == 20
    assert sm.spaces["3"].pre_condition_minutes == \
        cfg["collegenet"]["default_pre_condition_minutes"]


def test_system_precedence_room_over_building_over_default():
    """`system` inherits with the same precedence as the minute settings, so a
    building can move to a new BAS in one edit."""
    text = """
buildings:
  - id: b
    system: niagara
    target: "B/Bldg"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    system: bacnet_campus
    target: "12001:5"
  - space_id: 3
    target: "B/Rm3"
"""
    cfg = base_config()
    cfg["systems"] = {"niagara": {"driver": "preview"},
                      "bacnet_campus": {"driver": "preview"},
                      "sys": {"driver": "preview"}}
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].destination.system == "niagara"
    assert sm.spaces["2"].destination.system == "bacnet_campus"
    assert sm.spaces["3"].destination.system == "sys"      # the default
    assert sm.spaces["1"].building_destination.system == "niagara"


def test_loader_rejects_unknown_system():
    """A typo'd system name is a hard error, not a silent write somewhere else."""
    text = """
buildings: []
spaces:
  - space_id: 1
    system: typo_system
    target: "B/Rm1"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("typo_system" in e for e in sm.errors), sm.errors


def test_loader_reports_missing_target_and_duplicate_space_id():
    """Structural mistakes come back as errors, all at once."""
    text = """
buildings: []
spaces:
  - space_id: 1
    space_name: "no target"
  - space_id: 2
    target: "B/Rm2"
  - space_id: 2
    target: "B/Rm2dup"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("no `target:`" in e for e in sm.errors), sm.errors
    assert any("mapped twice" in e for e in sm.errors), sm.errors


def test_loader_warns_on_shared_target():
    """Two rooms on one schedule is legal but worth flagging."""
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Shared"
  - space_id: 2
    target: "B/Shared"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert any("2 rooms" in w for w in sm.warnings), sm.warnings


def test_loader_survives_broken_yaml():
    """A malformed map reports an error instead of raising into the run."""
    sm = with_yaml("buildings: [\nspaces:", lambda p: load_space_map(p, base_config()))
    assert sm.errors and not sm.spaces


def test_destinations_include_rollups():
    """Every schedule the sync owns — rooms AND roll-ups — is enumerated.

    Regression: the old clear-loop only checked room paths, so a building whose
    rooms all lost their bookings kept conditioning on last week's schedule
    indefinitely."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert sm.destinations() == {dest("B/Rm1"), dest("B/Occ")}, sm.destinations()
