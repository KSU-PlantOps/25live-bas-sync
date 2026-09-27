"""Offline tests: editor round-trip."""



from bassync.spacemap import load_space_map
from tests.helpers import base_config, dest, with_yaml


def test_editor_roundtrip_feeds_loader():
    """A room added in the editor resolves to its building's schedule, end to
    end through the editor's dump and the sync's loader."""
    from bassync import editor
    buildings = [{"id": "bldg_a", "name": "Building A", "target": "A/Building_Occ"}]
    rooms = [{"space_id": 11, "space_name": "A 101", "building": "bldg_a",
              "target": "A/Rm101_Occ", "pre_condition_minutes": 30},
             {"space_id": 12, "target": "A/Rm102_Occ"}]

    def _check(path):
        b2, f2, r2 = editor.load_mapping(path)
        assert len(b2) == 1 and not f2 and len(r2) == 2, (b2, f2, r2)
        return load_space_map(path, base_config())

    sm = with_yaml(editor.dump_mapping(buildings, [], rooms), _check)
    assert not sm.errors, sm.errors
    assert sm.spaces["11"].building_destination == dest("A/Building_Occ")
    assert sm.spaces["12"].building_destination is None


def test_editor_keeps_system_through_a_roundtrip():
    from bassync import editor
    rooms = [{"space_id": 1, "system": "campus_bacnet", "target": "12001:5"}]
    _b, _f, r2 = with_yaml(editor.dump_mapping([], [], rooms),
                           editor.load_mapping)
    assert r2[0]["system"] == "campus_bacnet", r2


def test_editor_flags_unknown_building():
    from bassync import editor
    assert editor.unknown_building_refs(
        [{"id": "bldg_a", "target": "A/Occ"}],
        [{"space_id": 11, "building": "typo_bldg", "target": "A/Rm.Occ"}]) == ["11"]


def test_editor_reads_systems_from_config():
    """The System dropdowns are populated from config.yaml, and a pre-1.0
    config still offers its single Niagara station."""
    from bassync import editor
    raw = {"systems": {"campus_bacnet": {"driver": "bacnet"},
                       "supervisor": {"driver": "niagara"}}}
    assert editor.config_systems(raw) == {"campus_bacnet": "bacnet",
                                          "supervisor": "niagara"}
    # A pre-1.0 config has a bare `niagara:` block instead of `systems:`.
    assert editor.config_systems({"niagara": {"host": "n4"}}) == {"niagara": "niagara"}
    # A missing or broken config must not stop the editor from opening.
    assert editor.config_systems({}) == {}
    assert editor.read_config_raw("/nonexistent/config.yaml") == {}


def test_editor_defaults_roundtrip_and_fallback():
    from bassync import editor
    text = editor.dump_defaults({"pre_condition_minutes": 40,
                                 "post_buffer_minutes": 10,
                                 "merge_gap_minutes": 3, "lookahead_days": 21})
    d = with_yaml(text, editor.load_defaults)
    assert d == {"pre_condition_minutes": 40, "post_buffer_minutes": 10,
                 "merge_gap_minutes": 3, "lookahead_days": 21}, d
    d2 = editor.load_defaults("/nonexistent/defaults.yaml")
    assert d2["pre_condition_minutes"] == 30 and d2["lookahead_days"] == 7, d2


# ── save safety ──────────────────────────────────────────────────────────────

def test_unchanged_files_are_not_rewritten(tmp_path):
    """Rewriting drops comments and burns the single .bak; skip it when
    nothing changed."""
    from bassync import editor
    path = tmp_path / "config.yaml"
    assert editor.write_if_changed(path, "a: 1\n")
    assert not (tmp_path / "config.yaml.bak").exists()
    assert not editor.write_if_changed(path, "a: 1\n")
    assert editor.write_if_changed(path, "a: 2\n")
    assert (tmp_path / "config.yaml.bak").read_text() == "a: 1\n"
    assert path.read_text() == "a: 2\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_unreadable_config_is_reported_not_emptied(tmp_path):
    from bassync import editor
    path = tmp_path / "config.yaml"
    path.write_text("collegenet: [unclosed\n")
    data, error = editor.read_config_checked(path)
    assert data == {} and error and "config.yaml" in error
    assert editor.read_config_checked(tmp_path / "missing.yaml") == ({}, None)


def test_building_dependents_split_by_what_would_break():
    from bassync import editor
    floors = [{"building": "b", "level": 1}, {"building": "c", "level": 1}]
    rooms = [{"space_id": 1, "building": "b"},
             {"space_id": 2, "building": "b", "target": "1:2"},
             {"space_id": 3, "building": "c"}]
    its_floors, rollup_only, with_target = editor.building_dependents("b", floors, rooms)
    assert its_floors == [floors[0]]
    assert [r["space_id"] for r in rollup_only] == [1]
    assert [r["space_id"] for r in with_target] == [2]


def test_editing_keeps_fields_the_form_does_not_show():
    from bassync import editor
    original = {"id": "b", "target": "1:1", "merge_gap_minutes": 20, "note": "x"}
    out = editor.merge_form_result(original, {"id": "b", "target": "1:2"},
                                   ["id", "name", "target", "note"])
    # merge_gap_minutes isn't a form field: kept. note is, and was cleared.
    assert out == {"id": "b", "target": "1:2", "merge_gap_minutes": 20}


def test_target_syntax_is_checked_per_driver():
    from bassync import editor
    raw = {"systems": {"bac": {"driver": "bacnet"}, "n4": {"driver": "niagara"}}}
    assert "Invalid BACnet target" in editor.target_problem("garbage", "bac", raw)
    assert editor.target_problem("12001:5", "bac", raw) is None
    assert editor.target_problem("Bldg/Rm1", "n4", raw) is None
    assert editor.target_problem("x", "unknown", raw) is None


def test_save_validation_uses_the_sync_loaders():
    """Cross-row problems the forms can't see — a floor of a deleted
    building, an unknown system — are caught before the file is written."""
    from bassync import editor
    raw = {"systems": {"bac": {"driver": "bacnet"}, "p": {"driver": "preview"}}}
    defaults = editor.load_defaults("/nonexistent")
    config_error, errors, _warnings = editor.validate_before_save(
        [], [{"building": "gone", "level": 1, "target": "1:1"}],
        [{"space_id": 1, "target": "12001:5"}], raw, defaults)
    assert config_error is None
    text = " ".join(errors)
    assert "unknown building 'gone'" in text
    assert "no `system:` and no default" in text          # two systems, no default
    bad_error, _e, _w = editor.validate_before_save(
        [], [], [], {"timezone": "Mars/Base"}, defaults)
    assert bad_error and "Mars/Base" in bad_error
