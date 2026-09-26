"""Offline tests: run orchestration, safety state, reports and notifications."""

import json
import logging
from pathlib import Path

import pytest
import yaml

import bassync.sync as sync_mod
from bassync import notify, safety
from bassync.config import load_config
from bassync.lock import RunLock
from bassync.model import Destination, RawEvent
from bassync.report import RunReport
from tests.helpers import TZ, dt

MAP = """
buildings:
  - id: b
    name: Main Hall
    target: "B/Occ"
spaces:
  - space_id: 1
    space_name: Main Hall 101
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    target: "B/Rm2"
"""

BROKEN_ROW = """
  - space_id: 3
    target: "B/Rm3"
    pre_condition_minutes: lots
"""


@pytest.fixture
def campus(tmp_path, monkeypatch):
    """A config on the preview driver, a room map, and a stubbed 25Live."""
    map_path = tmp_path / "map.yaml"
    map_path.write_text(MAP, encoding="utf-8")
    cfg = load_config("/nonexistent/config.yaml")
    cfg["collegenet"]["base_url"] = "http://stub"
    cfg["space_map_file"] = str(map_path)
    cfg["safety"]["state_file"] = str(tmp_path / "state" / "last_run.json")
    cfg["systems"] = {"sys": {"driver": "preview",
                              "csv_file": str(tmp_path / "out.csv")}}
    cfg["default_system"] = "sys"
    events = [RawEvent("E1", "Class", "1", dt(9, day=10), dt(10, day=10))]
    monkeypatch.setattr(sync_mod, "_fetch", lambda *a, **kw: list(events))
    return cfg, tmp_path, events


def _state(cfg) -> dict:
    return json.loads(Path(cfg["safety"]["state_file"]).read_text(encoding="utf-8"))


# ── room-map error policy ────────────────────────────────────────────────────

def test_bad_row_is_skipped_and_the_rest_syncs(campus):
    cfg, tmp, _ = campus
    Path(cfg["space_map_file"]).write_text(MAP + BROKEN_ROW, encoding="utf-8")
    report = RunReport("SYNC", "t", TZ)
    code = sync_mod.run_sync(cfg, report=report)
    assert code == sync_mod.EXIT_NO_MAP                 # still alerts
    assert "sys:B/Rm1" in _state(cfg)["windows"]        # but the campus synced
    assert "sys:B/Rm3" not in _state(cfg)["windows"]    # the bad row untouched
    assert len(report.map_errors) == 1
    assert "broken room-map row" in report.outcome


def test_abort_policy_writes_nothing(campus):
    cfg, tmp, _ = campus
    Path(cfg["space_map_file"]).write_text(MAP + BROKEN_ROW, encoding="utf-8")
    cfg["safety"]["on_map_errors"] = "abort"
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_NO_MAP
    assert not Path(cfg["safety"]["state_file"]).exists()


def test_unreadable_map_always_stops(campus):
    cfg, tmp, _ = campus
    Path(cfg["space_map_file"]).write_text("spaces: [unclosed\n", encoding="utf-8")
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_NO_MAP
    assert not Path(cfg["safety"]["state_file"]).exists()


# ── run lock ─────────────────────────────────────────────────────────────────

def test_second_run_exits_without_writing(campus):
    cfg, tmp, _ = campus
    holder = RunLock(Path(cfg["safety"]["state_file"]).with_name("run.lock"))
    assert holder.acquire()
    try:
        assert sync_mod.run_sync(cfg) == sync_mod.EXIT_LOCKED
        assert not Path(cfg["safety"]["state_file"]).exists()
    finally:
        holder.release()
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK      # free again


# ── dry run and safety ───────────────────────────────────────────────────────

def test_dry_run_reports_what_the_safety_check_would_do(campus, monkeypatch, caplog):
    cfg, tmp, _ = campus
    assert sync_mod.run_sync(cfg) == sync_mod.EXIT_OK      # establish a baseline
    monkeypatch.setattr(sync_mod, "_fetch", lambda *a, **kw: [])
    cfg["safety"]["min_events"] = 0
    with caplog.at_level(logging.INFO):
        assert sync_mod.run_sync(cfg, dry_run=True) == sync_mod.EXIT_OK
    assert "a live run would ABORT" in caplog.text


def test_missing_baseline_is_a_warning_not_silence(campus, caplog):
    cfg, tmp, _ = campus
    with caplog.at_level(logging.WARNING):
        sync_mod.run_sync(cfg)
    assert "No baseline for the mass-clear check" in caplog.text


def test_state_is_written_atomically_with_a_backup(tmp_path):
    path = str(tmp_path / "last_run.json")
    first = {Destination("s", "a"): [1, 2]}
    assert safety.save_state(path, first, 2)
    assert safety.save_state(path, {Destination("s", "a"): [1]}, 1)
    assert json.loads(Path(path + ".prev").read_text())["windows"] == {"s:a": 2}
    Path(path).write_text("{ truncated", encoding="utf-8")
    data, status = safety.load_state_with_status(path)
    assert status == "backup" and data["windows"] == {"s:a": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_upgrade_reads_the_old_state_location(tmp_path, monkeypatch):
    monkeypatch.setenv("BAS_HOME", str(tmp_path))
    legacy = tmp_path / "logs" / "last_run.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"windows": {"s:a": 3}}), encoding="utf-8")
    from bassync import paths
    data, status = safety.load_state_with_status(str(paths.state_file()))
    assert status == "legacy" and data["windows"] == {"s:a": 3}


# ── per-system isolation ─────────────────────────────────────────────────────

def test_any_exception_from_one_system_spares_the_others(campus, monkeypatch):
    """Driver construction used to be guarded against DriverError only; a
    bad per-system value raising anything else took down every system."""
    cfg, tmp, _ = campus
    cfg["systems"]["other"] = {"driver": "preview"}
    Path(cfg["space_map_file"]).write_text(
        MAP + '  - {space_id: 9, system: other, target: "O/1"}\n', encoding="utf-8")
    real = sync_mod.build_driver

    def flaky(name, *a, **kw):
        if name == "other":
            raise ValueError("boom")
        return real(name, *a, **kw)
    monkeypatch.setattr(sync_mod, "build_driver", flaky)
    report = RunReport("SYNC", "t", TZ)
    assert sync_mod.run_sync(cfg, report=report) == sync_mod.EXIT_WRITE_FAILURES
    statuses = {s.target: s.status for s in report.schedules}
    assert statuses["B/Rm1"] == "preview" and statuses["O/1"] == "failed"


# ── report rendering ─────────────────────────────────────────────────────────

def _report() -> RunReport:
    from bassync.model import OccupancyWindow
    r = RunReport("SYNC", "1.2.0", TZ)
    r.event_count, r.rooms = 3, 2
    r.add_schedule("sys", "B/Rm1", "Main Hall 101",
                   [OccupancyWindow(dt(9, day=10), dt(10, day=10), ["E1"]),
                    OccupancyWindow(dt(13, day=10), dt(14, day=10), ["E2"])],
                   "written")
    r.add_schedule("sys", "B/Rm2", "Main Hall 102", [], "written")
    r.add_schedule("sys", "B/Rm3", "Main Hall 103", [], "failed", "HTTP 500")
    r.problems.append("ERROR: Error writing sys:B/Rm3 — HTTP 500")
    r.finish(5, "1 write failure(s), 2 schedule(s) written")
    return r


def test_report_text_lists_what_was_scheduled():
    text = _report().to_text()
    assert "Result: FAILED (exit 5)" in text
    assert "Main Hall 101" in text and "09:00–10:00, 13:00–14:00" in text
    assert "no bookings — cleared" in text
    assert "error: HTTP 500" in text
    assert text.index("[FAILED]") < text.index("[WRITTEN]")    # failures first


def test_report_csv_and_html():
    r = _report()
    rows = list(__import__("csv").reader(r.to_csv().splitlines()))
    assert rows[0][:4] == ["status", "system", "target", "label"]
    assert len(rows) == 1 + 2 + 1 + 1           # header, 2 windows, 2 empty
    html = r.to_html()
    assert "Main Hall 101" in html and "Sync FAILED" in html
    assert "<script" not in _report().to_html()


# ── notification policy ──────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_send_email", lambda *a, **kw: (
        calls.append(("email", a, kw)) or notify.AlertResult("email", True)))
    monkeypatch.setattr(notify, "_send_webhook", lambda *a, **kw: (
        calls.append(("webhook", a, kw)) or notify.AlertResult("webhook", True)))
    return calls


def _alert_cfg(**alerts):
    base = {"enabled": True, "notify_on_success": False, "webhook_url": "http://hook",
            "email": {"enabled": True, "report": "full", "attach_csv": True}}
    base.update(alerts)
    return {"alerts": base}


def test_success_is_quiet_unless_asked(sent):
    r = _report()
    r.finish(0, "ok")
    assert notify.send_run_report(_alert_cfg(), r) == []
    cfg = _alert_cfg()
    cfg["alerts"]["email"]["notify_on_success"] = True     # daily email only
    notify.send_run_report(cfg, r)
    assert [c[0] for c in sent] == ["email"]


def test_failure_goes_to_every_channel_with_the_report(sent):
    notify.send_run_report(_alert_cfg(), _report())
    assert sorted(c[0] for c in sent) == ["email", "webhook"]
    email = next(c for c in sent if c[0] == "email")
    subject, body = email[1][1], email[1][2]
    assert "FAILED (exit 5)" in subject and "Main Hall 101" in body
    assert email[2]["attachments"][0][0].endswith(".csv")
    assert "<table" in email[2]["html_body"]


def test_a_channel_that_raises_is_reported_not_raised(monkeypatch):
    def explode(*a, **kw):
        raise RuntimeError("relay exploded")
    monkeypatch.setattr(notify, "_send_email", explode)
    results = notify.send_run_report(_alert_cfg(webhook_url=""), _report())
    assert len(results) == 1 and not results[0].ok
    assert "relay exploded" in results[0].detail


def test_email_carries_html_and_the_csv():
    msg = notify.build_message(
        {"from_addr": "a@example.edu", "to_addrs": ["b@example.edu"]},
        "s", "plain", html_body="<b>rich</b>", attachments=[("x.csv", "a,b\n")])
    types = [part.get_content_type() for part in msg.walk()]
    assert "text/plain" in types and "text/html" in types and "text/csv" in types


def test_teams_webhook_is_an_adaptive_card():
    payload = notify.webhook_payload("teams", "Subject", "Body", ok=False)
    card = payload["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["body"][0]["color"] == "Attention"
    assert notify.webhook_payload("slack", "S", "B") == {"text": "S\n\nB"}


def test_monitoring_ping_picks_the_right_url(monkeypatch):
    hit = []

    class Resp:
        status_code = 200
    monkeypatch.setattr(notify.requests, "get",
                        lambda url, timeout: hit.append(url) or Resp())
    mon = {"ping_url": "https://hc/ok", "ping_fail_url": "https://hc/fail"}
    assert notify.ping_monitor(mon, True).ok
    notify.ping_monitor(mon, False)
    assert notify.ping_monitor({"ping_url": "https://hc/ok"}, False) is None
    assert hit == ["https://hc/ok", "https://hc/fail"]


# ── through the CLI ──────────────────────────────────────────────────────────

def test_cli_live_run_emails_what_it_scheduled(tmp_path, monkeypatch):
    from bassync import cli
    (tmp_path / "map.yaml").write_text(MAP, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "collegenet": {"base_url": "http://stub"},
        "systems": {"sys": {"driver": "preview"}},
        "safety": {"state_file": str(tmp_path / "state" / "last_run.json")},
        "log_file": str(tmp_path / "sync.log"),
        "alerts": {"enabled": True, "email": {
            "enabled": True, "notify_on_success": True, "smtp_host": "smtp.x",
            "from_addr": "a@x.edu", "to_addrs": ["b@x.edu"]}},
    }), encoding="utf-8")
    monkeypatch.setattr(sync_mod, "_fetch", lambda *a, **kw: [
        RawEvent("E1", "Class", "1", dt(9, day=10), dt(10, day=10))])
    captured = []
    monkeypatch.setattr(notify, "_send_email", lambda cfg, subject, body, **kw: (
        captured.append((subject, body, kw)) or notify.AlertResult("email", True)))
    try:
        code = cli.main(["--config", str(tmp_path / "config.yaml"),
                         "--defaults", str(tmp_path / "none.yaml"),
                         "--space-map", str(tmp_path / "map.yaml")])
    finally:
        logging.getLogger().handlers.clear()
    assert code == 0
    subject, body, kw = captured[0]
    assert subject.startswith("[25Live sync] OK — 3 schedule(s) written")
    assert "Main Hall 101" in body and "09:00–10:00" in body
    assert "Building Main Hall" in body
    assert "B/Rm1" in kw["attachments"][0][1]


def test_discover_prints_loadable_yaml(monkeypatch, capsys):
    from bassync.collegenet import CollegeNetClient
    monkeypatch.setattr(CollegeNetClient, "discover_spaces", lambda self, days: [
        {"space_id": "12", "space_name": 'Hall "A" \\ Annex: East'}])
    cfg = load_config("/nonexistent/config.yaml")
    cfg["collegenet"]["base_url"] = "http://stub"
    assert sync_mod.run_discover(cfg, 30) == 0
    data = yaml.safe_load(capsys.readouterr().out)
    assert data["spaces"][0]["space_name"] == 'Hall "A" \\ Annex: East'


def test_validate_names_a_state_style_that_works(campus, monkeypatch):
    cfg, tmp, _ = campus
    from bassync.collegenet import CollegeNetClient
    monkeypatch.setattr(CollegeNetClient, "fetch_events", lambda self, sm, now=None: (
        [object()] * 5 if self.state_param_style == "comma" else []))
    name, ok, detail = sync_mod._bookings_check(
        cfg, CollegeNetClient(cfg["collegenet"], TZ), {"1": None})
    assert not ok and "'comma' returns 5" in detail
