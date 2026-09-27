"""Run history: what the web UI's dashboard and history page read."""

import logging
from datetime import timedelta

from bassync import history
from bassync.model import OccupancyWindow
from bassync.report import RunReport

from .helpers import TZ, dt


def _report(code=0, minutes_later=0):
    report = RunReport("SYNC", "9.9", TZ)
    report.started = dt(2, day=10) + timedelta(minutes=minutes_later)
    report.event_count = 3
    report.add_schedule("campus", "12001:5", "Main Hall", [
        OccupancyWindow(dt(9, day=11), dt(10, day=11), ["E1"])], "written")
    report.finish(code, "" if code == 0 else "BAS unreachable")
    return report


def test_saved_runs_list_newest_first_and_load_in_full(tmp_path):
    first = history.save_report(_report(), tmp_path)
    second = history.save_report(_report(code=3, minutes_later=5), tmp_path)
    assert first and second and first != second
    runs = history.list_runs(tmp_path)
    assert [r["id"] for r in runs] == [second, first]
    assert runs[0]["ok"] is False and runs[0]["exit_code"] == 3
    assert runs[1]["counts"]["written"] == 1
    assert "text" not in runs[0]                       # summaries only
    full = history.load_run(tmp_path, first)
    assert "Main Hall" in full["text"] and "<table" in full["html"]
    assert "12001:5" in full["csv"]


def test_two_runs_in_the_same_second_both_kept(tmp_path):
    a = history.save_report(_report(), tmp_path)
    b = history.save_report(_report(), tmp_path)
    assert a != b and len(history.list_runs(tmp_path)) == 2


def test_oldest_runs_are_pruned(tmp_path):
    for i in range(5):
        history.save_report(_report(minutes_later=i), tmp_path, keep=3)
    assert len(history.list_runs(tmp_path)) == 3


def test_ids_cannot_reach_outside_the_folder(tmp_path):
    (tmp_path / "secret.json").write_text("{}")
    runs = tmp_path / "runs"
    for bad in ("../secret", "..", "", "20260101T000000Z-sync/../../x"):
        assert history.load_run(runs, bad) is None


def test_an_unwritable_history_costs_the_entry_not_the_run(tmp_path, caplog):
    blocker = tmp_path / "runs"
    blocker.write_text("a file where the folder should be")
    with caplog.at_level(logging.WARNING):
        assert history.save_report(_report(), blocker) is None
    assert "Could not save this run" in caplog.text


def test_runs_dir_follows_the_state_file(tmp_path):
    cfg = {"safety": {"state_file": str(tmp_path / "s" / "last_run.json")}}
    assert history.runs_dir(cfg) == tmp_path / "s" / "runs"
