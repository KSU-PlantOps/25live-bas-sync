"""Offline tests: config."""



from bassync.config import load_config, migrate_legacy_systems, resolve_default_system
from tests.helpers import with_yaml


def test_load_config_merges_and_builds_base_url():
    """config.yaml deep-merges over the built-ins and base_url derives from the
    instance name."""
    text = """
collegenet:
  instance: demo-univ
  lookahead_days: 14
timezone: America/Chicago
systems:
  main:
    driver: preview
"""
    cfg = with_yaml(text, load_config)
    assert cfg["collegenet"]["lookahead_days"] == 14
    assert cfg["timezone"] == "America/Chicago"
    # A deep merge preserves untouched defaults rather than replacing the section.
    assert cfg["collegenet"]["merge_gap_minutes"] == 5
    assert cfg["collegenet"]["base_url"].endswith("/demo-univ/run")
    cfg2 = load_config("/nonexistent/path/config.yaml")
    assert cfg2["collegenet"]["lookahead_days"] == 7


def test_load_config_reads_defaults_file():
    """defaults.yaml overrides the built-in scheduling defaults."""
    def _run(cpath):
        return with_yaml(
            "pre_condition_minutes: 45\npost_buffer_minutes: 25\n"
            "merge_gap_minutes: 8\nlookahead_days: 14\n",
            lambda dpath: load_config(cpath, dpath))
    cn = with_yaml("collegenet:\n  instance: demo\n", _run)["collegenet"]
    assert (cn["default_pre_condition_minutes"], cn["default_post_buffer_minutes"],
            cn["merge_gap_minutes"], cn["lookahead_days"]) == (45, 25, 8, 14), cn


def test_legacy_niagara_block_becomes_a_system():
    """A pre-1.0 config with a bare `niagara:` block keeps working: it is
    promoted to a system and becomes the default."""
    cfg = {"niagara": {"host": "n4.example.edu", "port": 8443,
                       "schedule_base_path": "slot:/Schedules"}}
    migrate_legacy_systems(cfg)
    assert "niagara" not in cfg, "the legacy block should be consumed"
    assert cfg["systems"]["niagara"]["driver"] == "niagara"
    assert cfg["systems"]["niagara"]["host"] == "n4.example.edu"
    assert cfg["default_system"] == "niagara"


def test_explicit_systems_beat_the_legacy_block():
    """A site mid-migration keeps both; the explicit entry wins."""
    cfg = {"niagara": {"host": "old.example.edu", "port": 8443},
           "systems": {"niagara": {"driver": "niagara", "host": "new.example.edu"}}}
    migrate_legacy_systems(cfg)
    assert cfg["systems"]["niagara"]["host"] == "new.example.edu"
    assert cfg["systems"]["niagara"]["port"] == 8443   # gap filled by the legacy block


def test_single_system_is_the_implicit_default():
    """One system needs no `default_system`; several with none is ambiguous and
    must not be guessed at."""
    assert resolve_default_system({"systems": {"only": {}}}) == "only"
    assert resolve_default_system({"systems": {"a": {}, "b": {}}}) == ""
    assert resolve_default_system({"systems": {"a": {}, "b": {}},
                                   "default_system": "b"}) == "b"
