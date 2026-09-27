"""Offline tests: the Docker container's built-in daily scheduler."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from bassync import scheduler

NY = ZoneInfo("America/New_York")


def test_runs_tonight_or_tomorrow():
    assert scheduler.next_run("02:00", datetime(2026, 6, 1, 1, 0, tzinfo=NY)) == \
        datetime(2026, 6, 1, 2, 0, tzinfo=NY)
    assert scheduler.next_run("02:00", datetime(2026, 6, 1, 2, 0, tzinfo=NY)) == \
        datetime(2026, 6, 2, 2, 0, tzinfo=NY)


def test_spring_forward_gap_runs_when_the_clock_jumps():
    """02:00 does not exist on 2026-03-08 in New York. The old shell loop's
    `date -d "tomorrow 02:00"` failed there and the container crash-looped."""
    target = scheduler.next_run("02:00", datetime(2026, 3, 7, 2, 5, tzinfo=NY))
    assert target.astimezone(timezone.utc) == datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    assert (target.hour, target.minute) == (3, 0)          # 03:00 EDT


def test_fall_back_hour_runs_once():
    """01:30 happens twice on 2026-11-01; the sync must run once, at the first."""
    first = scheduler.next_run("01:30", datetime(2026, 10, 31, 12, 0, tzinfo=NY))
    assert first.astimezone(timezone.utc) == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    after = scheduler.next_run("01:30", first.astimezone(timezone.utc).astimezone(NY))
    assert after.date() == datetime(2026, 11, 2).date()
    # From inside the repeated hour (01:10 EST, fold=1), the next run is still
    # tomorrow, not the second 01:30 today.
    repeated = datetime(2026, 11, 1, 6, 10, tzinfo=timezone.utc).astimezone(NY)
    assert scheduler.next_run("01:30", repeated).date() == datetime(2026, 11, 2).date()


@pytest.mark.parametrize("bad", ["29:00", "2:60", "0200", "", "noon"])
def test_invalid_times_are_rejected(bad):
    with pytest.raises(ValueError):
        scheduler.parse_at(bad)
    assert scheduler.main([bad]) == 2


def test_on_start_and_sync_args_are_separated(monkeypatch):
    """--on-start is ours; everything after `--` is the sync's."""
    calls = []
    monkeypatch.setattr(scheduler, "_run_sync", lambda args: calls.append(args) or 0)

    class Stop(Exception):
        pass

    def stop(_seconds):
        raise Stop
    monkeypatch.setattr(scheduler.time, "sleep", stop)
    with pytest.raises(Stop):
        scheduler.main(["02:00", "--on-start", "--", "--dry-run", "--system", "x"])
    assert calls == [["--dry-run", "--system", "x"]]
