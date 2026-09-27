"""Shared builders for the offline test suite."""

import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from bassync.config import load_config
from bassync.model import Destination, RawEvent, SpaceConfig

TZ = ZoneInfo("America/New_York")


def dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


def dest(target: str, system: str = "sys") -> Destination:
    return Destination(system=system, target=target)


def space(space_id, kind, target, building=None, merge_gap=5,
          system="sys") -> SpaceConfig:
    return SpaceConfig(
        space_id=str(space_id), space_name=str(space_id), space_type=kind,
        destination=dest(target, system),
        building_destination=dest(building, system) if building else None,
        pre_condition_minutes=0, post_buffer_minutes=0,
        merge_gap_minutes=merge_gap,
    )


def event(space_id, start, end, eid="E1") -> RawEvent:
    return RawEvent(event_id=eid, title="t", space_id=str(space_id),
                    start=start, end=end)


def with_yaml(text: str, fn):
    """Run fn(path) against a temp YAML file, always cleaning up."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return fn(path)
    finally:
        os.unlink(path)


def base_config(**overrides) -> dict:
    cfg = load_config("/nonexistent/config.yaml")
    cfg["systems"] = {"sys": {"driver": "preview"}}
    cfg["default_system"] = "sys"
    cfg.update(overrides)
    return cfg
