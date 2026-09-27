"""Offline tests: drivers: registry + generic REST."""

import os
import tempfile

from bassync.model import OccupancyWindow
from tests.helpers import TZ, dt


def test_driver_registry_resolves_and_rejects():
    from bassync.drivers import DriverError, driver_names, load_driver_class
    assert {"bacnet", "niagara", "rest", "preview"} <= set(driver_names())
    assert load_driver_class("n4").name == "niagara"      # alias
    try:
        load_driver_class("honeywell_webs")
    except DriverError as exc:
        assert "Unknown BAS driver" in str(exc)
        return
    raise AssertionError("an unknown driver should raise")


def test_rest_driver_renders_templates_without_calling_out():
    """Placeholders resolve into the path and payload — checked without a
    server, since the whole point of the driver is a site-supplied contract."""
    from bassync.drivers.rest import RestScheduleWriter, _fill, _window_context
    writer = RestScheduleWriter("ebo", {
        "base_url": "https://ebo.example.edu",
        "write": {"method": "POST", "path": "/api/sched/{target}/exceptions",
                  "payload": {"start": "{start_local}", "value": "{value}",
                              "n": "{index}", "note": "window {index} of {count}"}},
    }, TZ)
    ctx = writer._context("/Server 1/Bldg A/Occ",
                          _window_context(OccupancyWindow(dt(9, day=10),
                                                          dt(17, day=10)), 0, 1))
    path = writer.write_cfg["path"].replace("{target}", ctx["target"])
    assert "%2FServer%201" in path, path        # target is URL-encoded in paths
    body = _fill(writer.write_cfg["payload"], ctx)
    # A placeholder that is the whole value keeps its JSON type; inside a
    # longer string it renders as text.
    assert body == {"start": "2026-06-10T09:00:00", "value": True, "n": 0,
                    "note": "window 0 of 1"}, body


def test_rest_driver_sends_the_raw_target_in_bodies():
    """In a JSON body the target is data; percent-encoding it there sent
    '%2FServer%201%2F...' to APIs that expected the path itself."""
    from bassync.drivers.rest import RestScheduleWriter
    writer = RestScheduleWriter("ebo", {
        "base_url": "https://ebo.example.edu", "auth": {"mode": "none"},
        "write": {"method": "POST", "path": "/api/sched/{target}",
                  "payload": {"object": "{target}"}},
    }, TZ)
    sent = []

    class Resp:
        status_code = 200
        text = ""
    writer.session.request = lambda method, url, json=None, timeout=None: (
        sent.append((url, json)) or Resp())
    writer.write_schedule("/Server 1/Occ", [OccupancyWindow(dt(9, day=10), dt(10, day=10))])
    url, body = sent[0]
    assert url.endswith("/api/sched/%2FServer%201%2FOcc"), url
    assert body == {"object": "/Server 1/Occ"}, body


def test_preview_driver_writes_csv():
    from bassync.drivers.preview import PreviewScheduleWriter
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "preview.csv")
        writer = PreviewScheduleWriter("p", {"csv_file": csv_path}, TZ)
        writer.write_schedule("A/Rm1", [OccupancyWindow(dt(9), dt(10))])
        writer.write_schedule("A/Rm2", [])
        writer.close()
        rows = open(csv_path, encoding="utf-8").read().splitlines()
    assert len(rows) == 3, rows            # header + two targets
    assert "A/Rm1" in rows[1] and "A/Rm2" in rows[2], rows


def test_niagara_retry_adapter_excludes_post():
    """The session retries GET/DELETE but never POST, so a retry can't create
    duplicate special events."""
    from bassync.drivers.niagara import NiagaraScheduleWriter
    writer = NiagaraScheduleWriter("n", {"host": "h", "port": 443},
                                   TZ, {"attempts": 2, "backoff_seconds": 0})
    methods = set(writer.session.get_adapter("https://x").max_retries.allowed_methods)
    assert "GET" in methods and "DELETE" in methods, methods
    assert "POST" not in methods, methods
    writer.close()


def test_niagara_ord_encoding_and_absolute_targets():
    """Spaces in schedule names are encoded; an absolute ORD bypasses the base
    path so a schedule outside it is still reachable."""
    from bassync.drivers.niagara import NiagaraScheduleWriter
    writer = NiagaraScheduleWriter(
        "n", {"host": "h", "port": 443, "schedule_base_path": "slot:/Schedules"}, TZ)
    assert writer._full_ord("Bldg A/Rm 101") == "slot:/Schedules/Bldg A/Rm 101"
    assert writer._full_ord("slot:/Other/Sched") == "slot:/Other/Sched"
    assert "%20" in writer._endpoint("Bldg A/Rm 101")
    assert "slot:/Schedules" in writer._endpoint("Bldg A/Rm 101")
    writer.close()


def test_niagara_tls_failure_says_how_to_fix_it():
    """TLS is verified by default since 1.2; a self-signed station must get
    a message that names the setting, not a bare SSL traceback."""
    import requests

    from bassync.drivers.niagara import NiagaraScheduleWriter
    writer = NiagaraScheduleWriter("n4", {"host": "n4.example.edu"}, TZ)

    def refuse(*a, **kw):
        raise requests.exceptions.SSLError("certificate verify failed")
    writer.session.get = refuse
    ok, detail = writer.health_check()
    assert not ok and "verify_tls" in detail, detail
