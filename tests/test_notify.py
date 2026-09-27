"""Offline tests: alerting."""

import os


def test_send_alert_webhook_and_disabled():
    from bassync import notify
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured.update(url=url, json=json)
        return type("R", (), {"status_code": 200, "text": ""})()

    original = notify.requests.post
    notify.requests.post = fake_post
    try:
        results = notify.send_alert({"enabled": True, "webhook_url": "http://hook"},
                                    "Subject X", "Body Y")
        assert captured.get("url") == "http://hook"
        assert "Subject X" in captured["json"]["text"]
        assert len(results) == 1 and results[0].ok, [str(r) for r in results]
        captured.clear()
        assert notify.send_alert({"enabled": False, "webhook_url": "http://hook"},
                                 "s", "b") == []
        assert captured == {}, "disabled alerts must not post"
    finally:
        notify.requests.post = original


def test_smtp_security_resolution():
    """`security:` wins; the pre-1.0 `use_tls:` boolean still works; and with
    neither, port 465 means implicit TLS and everything else STARTTLS. The
    default must never be plaintext — that would put a relay password on the
    wire without anyone asking for it."""
    from bassync.notify import _resolve_security
    assert _resolve_security({"security": "ssl"}, 587) == "ssl"
    assert _resolve_security({"security": "SMTPS"}, 587) == "ssl"
    assert _resolve_security({"security": "none"}, 465) == "none"
    assert _resolve_security({"use_tls": False}, 587) == "none"
    assert _resolve_security({"use_tls": True}, 587) == "starttls"
    assert _resolve_security({}, 465) == "ssl"
    assert _resolve_security({}, 587) == "starttls"
    assert _resolve_security({}, 25) == "starttls"


def test_smtp_config_validation():
    """Missing settings are named before we open a socket, so the operator
    sees the real problem instead of a relay's opaque 5xx."""
    from bassync.notify import _validate_email_cfg
    good = {"smtp_host": "smtp.example.edu", "from_addr": "a@example.edu",
            "to_addrs": ["b@example.edu"]}
    assert _validate_email_cfg(good) is None
    assert "smtp_host" in _validate_email_cfg({**good, "smtp_host": ""})
    assert "from_addr" in _validate_email_cfg({**good, "from_addr": " "})
    assert "to_addrs" in _validate_email_cfg({**good, "to_addrs": []})
    assert "to_addrs" in _validate_email_cfg({**good, "to_addrs": ["", "  "]})
    # A username with no password reaches the relay as an unauthenticated send
    # and gets rejected confusingly. Catch it here instead.
    os.environ.pop("BAS_SMTP_PASSWORD", None)
    problem = _validate_email_cfg({**good, "username": "svc"})
    assert problem and "BAS_SMTP_PASSWORD" in problem, problem
    os.environ["BAS_SMTP_PASSWORD"] = "x"
    try:
        assert _validate_email_cfg({**good, "username": "svc"}) is None
    finally:
        os.environ.pop("BAS_SMTP_PASSWORD", None)


def test_smtp_message_headers():
    """Date and Message-ID are set explicitly — mail without them gets
    quarantined by some filters, and an alert nobody sees is no alert."""
    from bassync.notify import build_message
    msg = build_message({"from_addr": "alerts@example.edu",
                         "to_addrs": ["a@example.edu", " b@example.edu "]},
                        "Subject X", "Body Y")
    assert msg["Subject"] == "Subject X"
    assert msg["To"] == "a@example.edu, b@example.edu", msg["To"]
    assert msg["Date"] and msg["Message-ID"], dict(msg)
    assert "example.edu" in msg["Message-ID"]
    assert msg.get_content().strip() == "Body Y"
    # A single string recipient is accepted as well as a list.
    single = build_message({"from_addr": "a@x.edu", "to_addrs": "b@x.edu"}, "s", "b")
    assert single["To"] == "b@x.edu"


def test_smtp_reports_refused_recipients():
    """A relay that accepts the message but rejects a recipient is a failure,
    not a success — that person never hears about the outage."""
    from bassync import notify

    class StubSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def send_message(self, msg):
            return {"bad@example.edu": (550, b"No such user")}

    original = notify.smtplib.SMTP
    notify.smtplib.SMTP = StubSMTP
    try:
        result = notify._send_email(
            {"smtp_host": "smtp.example.edu", "from_addr": "a@example.edu",
             "to_addrs": ["good@example.edu", "bad@example.edu"]}, "s", "b")
    finally:
        notify.smtplib.SMTP = original
    assert not result.ok, result
    assert "bad@example.edu" in result.detail, result.detail


def test_smtp_connection_failure_is_reported_not_raised():
    """An unreachable relay must be reported, never raised — alerting failing
    must not turn a successful sync into a crash."""
    from bassync import notify
    result = notify._send_email(
        # TEST-NET-1 is reserved and unroutable, so this cannot connect.
        {"smtp_host": "192.0.2.1", "smtp_port": 2525, "security": "none",
         "from_addr": "a@example.edu", "to_addrs": ["b@example.edu"]},
        "s", "b")
    assert not result.ok
    assert "could not connect" in result.detail, result.detail


def test_send_alert_force_sends_while_disabled():
    """--test-alert must exercise the channels even before alerts are switched
    on — that is the point of testing them."""
    from bassync import notify
    posted = []
    original = notify.requests.post
    notify.requests.post = lambda url, **kw: (
        posted.append(url), type("R", (), {"status_code": 200, "text": ""})())[1]
    try:
        cfg = {"enabled": False, "webhook_url": "http://hook"}
        assert notify.send_alert(cfg, "s", "b") == []
        results = notify.send_alert(cfg, "s", "b", force=True)
    finally:
        notify.requests.post = original
    assert len(results) == 1 and results[0].ok, [str(r) for r in results]
    assert posted == ["http://hook"], posted


def test_send_alert_survives_a_dead_webhook():
    """An alerting failure must never change the run's outcome."""
    from bassync import notify

    def boom(*a, **kw):
        raise notify.requests.RequestException("no route to host")

    original = notify.requests.post
    notify.requests.post = boom
    try:
        notify.send_alert({"enabled": True, "webhook_url": "http://hook"}, "s", "b")
    finally:
        notify.requests.post = original
