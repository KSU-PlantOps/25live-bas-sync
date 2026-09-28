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


def test_campus_is_a_label_the_sync_accepts_quietly():
    from bassync.config import load_config
    from bassync.spacemap import load_space_map
    from tests.helpers import with_yaml
    cfg = load_config("/nonexistent/config.yaml")
    cfg["systems"] = {"sys": {"driver": "preview"}}
    sm = with_yaml("buildings:\n  - {id: A, campus: Marietta, target: 'A/Occ'}\n"
                   "spaces:\n  - {space_id: 1, building: A}\n",
                   lambda path: load_space_map(path, cfg))
    assert sm.errors == [] and sm.warnings == []


# ── equipment: one schedule for many rooms, many schedules for one room ──────

EQUIPMENT_MAP = """
buildings:
  - id: sci
    name: Science
    target: "B/Occ"
    equipment:
      - id: ahu_3
        name: AHU-3
        target: "B/AHU3"
      - id: vav_1b
        target: "B/VAV1B"
spaces:
  - space_id: 1
    building: sci
    target: "B/VAV1A"
    equipment: [ahu_3, vav_1b]
  - space_id: 2
    building: sci
    equipment: ahu_3
  - space_id: 3
    building: sci
"""


def test_rooms_share_equipment_and_a_room_drives_several():
    sm = with_yaml(EQUIPMENT_MAP, lambda p: load_space_map(p, base_config()))
    assert not sm.errors and not sm.warnings, (sm.errors, sm.warnings)
    one, two, three = sm.spaces["1"], sm.spaces["2"], sm.spaces["3"]
    assert one.all_destinations() == [dest("B/VAV1A"), dest("B/AHU3"), dest("B/VAV1B"),
                                      dest("B/Occ")]
    assert two.destination is None                      # roll-up only, through the AHU
    assert two.rollup_destinations() == [dest("B/AHU3"), dest("B/Occ")]
    assert three.equipment_destinations == ()
    assert sm.equipment == {("sci", "ahu_3"): dest("B/AHU3"),
                            ("sci", "vav_1b"): dest("B/VAV1B")}
    assert {dest("B/AHU3"), dest("B/VAV1B")} <= sm.destinations()
    assert sm.labels[dest("B/AHU3")] == "AHU-3 (Science)"


def test_equipment_no_room_lists_yet_is_still_managed():
    text = EQUIPMENT_MAP.replace("    equipment: [ahu_3, vav_1b]\n", "").replace(
        "    equipment: ahu_3\n", "")
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert dest("B/AHU3") in sm.destinations()          # cleared when nothing books it


def test_equipment_problems_are_reported_and_its_rooms_still_sync():
    text = """
buildings:
  - id: sci
    target: "B/Occ"
    equipment:
      - id: ahu_3
        target: "B/AHU3"
      - id: ahu_3
        target: "B/AHU3-again"
      - id: no_target
      - id: odd
        target: "B/X"
        system: nowhere
        colour: blue
  - id: art
    target: "A/Occ"
    equipment: "not a list"
spaces:
  - space_id: 1
    building: sci
    equipment: [ahu_3, no_target, missing]
  - space_id: 2
    equipment: [ahu_3]
    target: "B/Rm2"
  - space_id: 3
    building: art
    equipment: [ahu_3]
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    joined = "\n".join(sm.errors)
    assert "Equipment ahu_3 of 'sci' is defined more than once" in joined
    assert "Equipment no_target of 'sci': no `target:`" in joined
    assert "system 'nowhere'" in joined and "must be a list" in joined
    warned = "\n".join(sm.warnings)
    assert "unknown key `colour`" in warned
    assert "lists equipment 'no_target', which is broken" in warned
    assert "lists equipment 'missing', which isn't defined under building 'sci'" in warned
    assert "Room 2 lists equipment but no building" in warned
    assert "lists equipment 'ahu_3', which isn't defined under building 'art'" in warned
    # Each room keeps what it can still drive.
    assert sm.spaces["1"].equipment_destinations == (dest("B/AHU3"),)
    assert sm.spaces["2"].destination == dest("B/Rm2")
    assert sm.spaces["3"].equipment_destinations == ()


def test_a_room_with_only_broken_equipment_drives_nothing_and_holds_nothing_wrongly():
    text = """
buildings: []
spaces:
  - space_id: 1
    equipment: [ahu]
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("drive nothing" in e for e in sm.errors), sm.errors
    assert "1" not in sm.spaces


def test_equipment_targets_are_canonicalised_with_the_rest():
    """Two spellings of one BACnet schedule — on a room and on equipment — are
    one schedule, written once."""
    text = """
buildings:
  - id: sci
    target: "12001:1"
    equipment:
      - id: ahu
        target: "12001:5@10.0.0.9"
spaces:
  - space_id: 1
    building: sci
    target: "12001:5"
    equipment: [ahu]
"""
    cfg = base_config()
    cfg["systems"] = {"sys": {"driver": "bacnet", "local_address": "10.0.0.5/24"}}
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    room = sm.spaces["1"]
    assert room.destination == room.equipment_destinations[0]


def test_shared_room_targets_point_at_equipment():
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/AHU"
  - space_id: 2
    target: "B/AHU"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("`equipment:`" in w for w in sm.warnings), sm.warnings
