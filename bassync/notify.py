# 25Live -> BAS Schedule Sync — alerting and run reports
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Telling people what a run did.

Two channels, either or both:

  * **Email** (SMTP) — the full run report: the outcome, the safety verdict,
    each BAS system's status, every problem logged, and every schedule with
    the exact booking windows written to it, as text and HTML, with the
    same data attached as a CSV. Sent on failure, and on success too when
    `notify_on_success` is on — a morning email of "here is what the
    buildings will do this week".
  * **Webhook** — a short summary for Slack, Microsoft Teams (Workflows), or
    anything that accepts a JSON POST.

Plus a dead-man's switch: `monitoring.ping_url` is fetched after every
successful run, so an external monitor can alarm when the pings stop — the one
failure (the job not running at all) that no alert can report.

Nothing here raises. An alerting problem must not change the run's outcome —
a BAS write that succeeded should not be reported as a failure because the
mail relay was down. Failures are logged and returned, so `--test-alert` can
show them without a real outage to trigger them.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable, Optional

import requests

WEBHOOK_TIMEOUT = 15
SMTP_TIMEOUT = 20
PING_TIMEOUT = 10

# Lines of detail a webhook message carries; the email has everything.
WEBHOOK_MAX_PROBLEMS = 10

# Environment variable holding the SMTP password. Read at send time so a
# rotated secret takes effect on the next run without a config change.
SMTP_PASSWORD_ENV = "BAS_SMTP_PASSWORD"

# Ports that conventionally mean implicit TLS (SMTPS). Used only to make the
# default sensible; `security:` always wins when set explicitly.
IMPLICIT_TLS_PORTS = {465}


class AlertResult:
    """Outcome of one notification channel."""

    __slots__ = ("channel", "ok", "detail")

    def __init__(self, channel: str, ok: bool, detail: str = ""):
        self.channel = channel
        self.ok = ok
        self.detail = detail

    def __str__(self) -> str:
        return f"[{'OK  ' if self.ok else 'FAIL'}] {self.channel} — {self.detail}"


def _safely(channel: str, fn: Callable, *args, **kwargs) -> AlertResult:
    """Run one channel; anything it raises becomes a failed AlertResult."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:                              # noqa: BLE001
        return AlertResult(channel, False, f"{type(exc).__name__}: {exc}")


def _log_failures(results: list) -> list:
    for result in results:
        if not result.ok:
            logging.warning("Alert channel %s failed: %s",
                            result.channel, result.detail)
    return results


def send_alert(alerts_cfg: dict, subject: str, body: str,
               force: bool = False) -> list:
    """
    A plain message through whichever channels are configured. Returns a list
    of AlertResult — empty when alerting is switched off.

    `force` sends even when `alerts.enabled` is false, which is what
    `--test-alert` uses to prove the plumbing before turning it on.
    """
    results: list = []
    if not alerts_cfg:
        return results
    if not (alerts_cfg.get("enabled") or force):
        return results

    url = alerts_cfg.get("webhook_url")
    if url:
        results.append(_safely("webhook", _send_webhook, url, subject, body,
                               alerts_cfg.get("webhook_format") or "slack"))

    email = alerts_cfg.get("email") or {}
    if email.get("enabled") or (force and email.get("smtp_host")):
        results.append(_safely("email", _send_email, email, subject, body))
    return _log_failures(results)


def _wants(channel_setting, global_setting) -> bool:
    return bool(global_setting if channel_setting is None else channel_setting)


def send_run_report(cfg: dict, report) -> list:
    """
    Send a finished live run's report through each channel that wants it:
    every channel on failure, and on success only the channels whose
    `notify_on_success` (or the global one) is on.
    """
    alerts = cfg.get("alerts") or {}
    if not alerts.get("enabled"):
        return []
    results: list = []
    notify_success = alerts.get("notify_on_success")

    url = alerts.get("webhook_url")
    if url and (not report.ok or _wants(alerts.get("webhook_notify_on_success"),
                                        notify_success)):
        subject = report.subject()
        lines = report.summary_lines()
        if report.problems:
            lines.append("Problems:")
            lines.extend(f"• {p}" for p in report.problems[:WEBHOOK_MAX_PROBLEMS])
            if len(report.problems) > WEBHOOK_MAX_PROBLEMS:
                lines.append(f"… and {len(report.problems) - WEBHOOK_MAX_PROBLEMS} "
                             "more (see the email report or the log)")
        results.append(_safely("webhook", _send_webhook, url, subject,
                               "\n".join(lines),
                               alerts.get("webhook_format") or "slack",
                               ok=report.ok, exit_code=report.exit_code))

    email = alerts.get("email") or {}
    if email.get("enabled") and (not report.ok or _wants(
            email.get("notify_on_success"), notify_success)):
        full = (email.get("report") or "full") == "full"
        attachments = []
        if email.get("attach_csv", True) and report.schedules:
            name = f"25live-sync-{report.started.strftime('%Y-%m-%d')}.csv"
            attachments.append((name, report.to_csv()))
        results.append(_safely(
            "email", _send_email, email,
            report.subject(email.get("subject_prefix") or ""),
            report.to_text(full), html_body=report.to_html(full),
            attachments=attachments))
    return _log_failures(results)


def ping_monitor(monitoring: dict, ok: bool) -> Optional[AlertResult]:
    """
    Tell an external monitor the job ran: `ping_url` after a successful live
    run, `ping_fail_url` after a failed one. The monitor alarms when the
    success pings stop arriving — the case where nothing runs at all, so
    nothing else can report it.
    """
    url = (monitoring or {}).get("ping_url" if ok else "ping_fail_url")
    if not url:
        return None
    try:
        r = requests.get(url, timeout=PING_TIMEOUT)
    except requests.RequestException as exc:
        result = AlertResult("monitoring ping", False, str(exc))
    else:
        result = AlertResult("monitoring ping", r.status_code < 400,
                             f"HTTP {r.status_code}")
    if not result.ok:
        logging.warning("Monitoring ping to %s failed: %s", url, result.detail)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────────────────────────────────────────

def webhook_payload(fmt: str, subject: str, body: str, ok: Optional[bool] = None,
                    exit_code: Optional[int] = None) -> dict:
    """
    The JSON body for each webhook flavour.

      slack    {"text": ...} — Slack incoming webhooks, and most chat tools
      teams    an Adaptive Card message, which is what Microsoft Teams
               Workflows ("Post to a channel when a webhook request is
               received") expects; the retired Office 365 connectors took
               plain {"text": ...}
      generic  {"text", "subject", "ok", "exit_code"} for your own tooling
    """
    fmt = (fmt or "slack").lower()
    if fmt == "teams":
        colour = "Default" if ok is None else ("Good" if ok else "Attention")
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": subject, "weight": "Bolder",
                         "size": "Medium", "wrap": True, "color": colour},
                        {"type": "TextBlock", "text": body.replace("\n", "\n\n"),
                         "wrap": True},
                    ],
                },
            }],
        }
    if fmt == "generic":
        return {"text": f"{subject}\n\n{body}", "subject": subject, "ok": ok,
                "exit_code": exit_code}
    return {"text": f"{subject}\n\n{body}"}


def _send_webhook(url: str, subject: str, body: str, fmt: str = "slack",
                  ok: Optional[bool] = None,
                  exit_code: Optional[int] = None) -> AlertResult:
    try:
        r = requests.post(url, json=webhook_payload(fmt, subject, body, ok, exit_code),
                          timeout=WEBHOOK_TIMEOUT)
    except requests.RequestException as exc:
        return AlertResult("webhook", False, str(exc))
    if r.status_code >= 400:
        return AlertResult("webhook", False,
                           f"HTTP {r.status_code} {r.text[:150]}")
    return AlertResult("webhook", True, f"HTTP {r.status_code}")


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_security(email_cfg: dict, port: int) -> str:
    """
    Which transport security to use: "starttls", "ssl", or "none".

    `security:` is the current key. `use_tls:` is the pre-1.0 boolean and is
    still honored. With neither, port 465 implies implicit TLS and everything
    else implies STARTTLS — the safe default, since a plaintext fallback would
    silently put a relay password on the wire.
    """
    explicit = (email_cfg.get("security") or "").strip().lower()
    if explicit in ("starttls", "ssl", "tls", "smtps", "none", "plain"):
        return {"tls": "ssl", "smtps": "ssl", "plain": "none"}.get(explicit, explicit)
    use_tls = email_cfg.get("use_tls")
    if use_tls is False:
        return "none"
    return "ssl" if port in IMPLICIT_TLS_PORTS else "starttls"


def _recipients(email_cfg: dict) -> list:
    recipients = email_cfg.get("to_addrs") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    return [str(r).strip() for r in recipients if str(r).strip()]


def _validate_email_cfg(email_cfg: dict) -> Optional[str]:
    """First missing required setting, as a message. None when it's usable."""
    if not (email_cfg.get("smtp_host") or "").strip():
        return "alerts.email.smtp_host is not set"
    if not (email_cfg.get("from_addr") or "").strip():
        return "alerts.email.from_addr is not set"
    if not _recipients(email_cfg):
        return "alerts.email.to_addrs is empty"
    try:
        port = int(email_cfg.get("smtp_port", 587))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return f"alerts.email.smtp_port {email_cfg.get('smtp_port')!r} is not a port number"
    if (email_cfg.get("username") or "").strip() \
            and not os.environ.get(SMTP_PASSWORD_ENV):
        # Sending unauthenticated when a username was configured gets rejected
        # by most relays with an opaque 5xx. Say what is actually wrong.
        return (f"alerts.email.username is set but ${SMTP_PASSWORD_ENV} is not — "
                "set it, or clear the username for an open relay")
    return None


def build_message(email_cfg: dict, subject: str, body: str,
                  html_body: Optional[str] = None,
                  attachments: Optional[list] = None) -> EmailMessage:
    """
    The message itself, separated out so tests can inspect it offline.

    Plain text always; an HTML alternative when given (mail clients show that
    one, text-only clients and ticket systems the other); then attachments as
    (filename, text) pairs, each sent as a CSV-typed text part.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = ", ".join(_recipients(email_cfg))
    # Explicit Date and Message-ID: some relays and spam filters treat mail
    # without them as suspect, and an alert that lands in quarantine is an
    # alert nobody sees.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=email_cfg["from_addr"].split("@")[-1]
                                   or None)
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for filename, content in attachments or []:
        msg.add_attachment(content, subtype="csv", filename=filename)
    return msg


def _send_email(email_cfg: dict, subject: str, body: str,
                html_body: Optional[str] = None,
                attachments: Optional[list] = None) -> AlertResult:
    problem = _validate_email_cfg(email_cfg)
    if problem:
        return AlertResult("email", False, problem)

    host = email_cfg["smtp_host"].strip()
    port = int(email_cfg.get("smtp_port", 587))
    security = _resolve_security(email_cfg, port)
    username = (email_cfg.get("username") or "").strip()
    password = os.environ.get(SMTP_PASSWORD_ENV)
    msg = build_message(email_cfg, subject, body, html_body, attachments)

    # Each stage is reported separately: "connection refused" and "auth
    # rejected" and "relay denied this recipient" need three different fixes,
    # and a single "email failed" tells the operator none of them.
    smtp: smtplib.SMTP
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT,
                                    context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    except (OSError, smtplib.SMTPException) as exc:
        return AlertResult("email", False,
                           f"could not connect to {host}:{port} ({security}): {exc}")

    try:
        with smtp:
            smtp.ehlo()
            if security == "starttls":
                try:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                except smtplib.SMTPException as exc:
                    return AlertResult(
                        "email", False,
                        f"STARTTLS failed on {host}:{port} ({exc}). If this "
                        "relay uses implicit TLS, set alerts.email.security: "
                        "ssl (usually port 465); if it is plaintext-only on a "
                        "trusted network, set security: none.")
            if username and password:
                try:
                    smtp.login(username, password)
                except smtplib.SMTPAuthenticationError as exc:
                    return AlertResult(
                        "email", False,
                        f"authentication rejected for {username}: {exc}")
            refused = smtp.send_message(msg)
    except smtplib.SMTPException as exc:
        return AlertResult("email", False, f"send failed: {exc}")
    except OSError as exc:
        return AlertResult("email", False, f"connection lost: {exc}")

    if refused:
        return AlertResult("email", False,
                           f"relay refused {len(refused)} recipient(s): "
                           f"{', '.join(sorted(refused))}")
    return AlertResult("email", True,
                       f"sent via {host}:{port} ({security}) to {msg['To']}")
