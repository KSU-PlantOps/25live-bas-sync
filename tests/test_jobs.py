"""The service's job runner: one process at a time, output kept."""

import sys
import time

import pytest

from bassync import jobs as jobs_mod
from bassync.jobs import JobBusy, JobManager

SCRIPT = ("import sys, time, signal\n"
          "args = sys.argv[1:]\n"
          "print('args:', *args, flush=True)\n"
          "if '--sleep' in args:\n"
          "    try:\n"
          "        time.sleep(float(args[args.index('--sleep') + 1]))\n"
          "    except KeyboardInterrupt:\n"
          "        print('interrupted', flush=True); sys.exit(130)\n"
          "for i in range(int(args[args.index('--lines') + 1]) if '--lines' in args else 0):\n"
          "    print('line', i)\n"
          "sys.exit(3 if '--fail' in args else 0)\n")


def manager(tmp_path, **kw):
    return JobManager(tmp_path / "jobs", command=[sys.executable, "-c", SCRIPT], **kw)


def started(job, timeout=15):
    """Wait until the script is past start-up (its handler is in place)."""
    deadline = time.time() + timeout
    while not any(line.startswith("args:") for line in list(job.lines)):
        assert time.time() < deadline, "job never started"
        time.sleep(0.02)


def wait(job, timeout=15):
    deadline = time.time() + timeout
    while job.running and time.time() < deadline:
        time.sleep(0.05)
    assert not job.running, "job did not finish"


def test_a_job_runs_and_its_output_and_result_are_kept(tmp_path):
    jm = manager(tmp_path)
    job = jm.start("dry-run", ["--fail"], trigger="test")
    wait(job)
    assert job.exit_code == 3
    assert list(job.lines)[0] == "args: --dry-run --fail"
    assert not jm.busy
    # A fresh manager (a restarted service) still has it, from disk.
    again = manager(tmp_path).get(job.id)
    assert again.exit_code == 3 and again.lines[0] == "args: --dry-run --fail"
    assert jm.recent()[0]["id"] == job.id and jm.recent()[0]["running"] is False


def test_one_job_at_a_time(tmp_path):
    jm = manager(tmp_path)
    job = jm.start("sync", ["--sleep", "5"])
    started(job)
    try:
        with pytest.raises(JobBusy, match="Sync"):
            jm.start("validate")
    finally:
        jm.stop(job.id, "test")
        wait(job)
    assert job.stopped_by == "test" and job.exit_code == 130
    assert "interrupted" in list(job.lines)
    jm.start("validate")                     # free again


def test_unknown_kinds_are_refused(tmp_path):
    with pytest.raises(ValueError):
        manager(tmp_path).start("rm -rf")


def test_output_since_skips_lines_that_fell_out_of_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "MAX_LINES", 10)
    jm = manager(tmp_path)
    job = jm.start("validate", ["--lines", "25"])
    wait(job)
    assert job.dropped == 16                 # 26 lines printed, 10 kept
    lines, nxt = job.output_since(0)
    assert nxt == 26 and lines[-1] == "line 24" and len(lines) == 10
    assert job.output_since(24) == (["line 23", "line 24"], 26)
    # The file keeps everything.
    assert len((tmp_path / "jobs" / f"{job.id}.log").read_text().splitlines()) == 26


def test_shutdown_lets_a_running_job_finish(tmp_path):
    jm = manager(tmp_path)
    job = jm.start("sync", ["--sleep", "0.5"])
    jm.shutdown(grace=10)
    wait(job)
    assert job.exit_code == 0 and not job.stopped_by
    with pytest.raises(JobBusy, match="shutting down"):
        jm.start("validate")


def test_shutdown_interrupts_a_job_that_overruns(tmp_path):
    jm = manager(tmp_path)
    job = jm.start("sync", ["--sleep", "30"])
    started(job)
    jm.shutdown(grace=0.2)
    wait(job)
    assert job.stopped_by == "service shutdown" and job.exit_code == 130


def test_a_job_left_running_by_a_restart_is_marked(tmp_path):
    folder = tmp_path / "jobs"
    folder.mkdir()
    (folder / "20260101T020000Z-sync-abcdef.json").write_text(
        '{"id": "20260101T020000Z-sync-abcdef", "kind": "sync", "label": "Sync", '
        '"args": [], "trigger": "schedule", "started": "2026-01-01T02:00:00+00:00", '
        '"finished": null, "exit_code": null}')
    recent = manager(tmp_path).recent()
    assert recent[0]["running"] is False
    assert recent[0]["stopped_by"] == "service restarted while it ran"


def test_old_jobs_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, "KEEP_JOBS", 2)
    jm = manager(tmp_path)
    for _ in range(4):
        job = jm.start("validate")
        wait(job)
    assert len(list((tmp_path / "jobs").glob("*.json"))) == 2
    assert len(list((tmp_path / "jobs").glob("*.log"))) == 2


def test_a_job_is_only_shown_finished_once_it_is_recorded_and_tidied(tmp_path, monkeypatch):
    """Regression: a job used to show as finished before its record was
    written and old ones pruned, so the next job could start half-way
    through — and pruning, which sorted same-second ids by their random
    part, could take the new job's files."""
    monkeypatch.setattr(jobs_mod, "KEEP_JOBS", 1)
    real_prune = JobManager._prune

    def slow_prune(self, *args, **kwargs):
        time.sleep(0.3)
        return real_prune(self, *args, **kwargs)

    monkeypatch.setattr(JobManager, "_prune", slow_prune)
    jm = manager(tmp_path)
    ids = []
    for _ in range(3):
        job = jm.start("validate")
        ids.append(job.id)
        wait(job)
        records = sorted(p.stem for p in (tmp_path / "jobs").glob("*.json"))
        logs = sorted(p.stem for p in (tmp_path / "jobs").glob("*.log"))
        assert records == logs == [job.id], (records, logs)
    assert [j["id"] for j in jm.recent()][:1] == ids[-1:]


def test_get_refuses_ids_that_are_not_job_ids(tmp_path):
    jm = manager(tmp_path)
    assert jm.get("../../etc/passwd") is None and jm.get("") is None
