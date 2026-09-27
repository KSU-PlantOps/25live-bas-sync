# 25Live -> BAS Schedule Sync — web UI
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The web UI served by the service (bassync/service.py): status, run history,
"Sync now" and the read-only tools, and the same room-map and settings
editing the desktop editor offers.

It edits the same files the sync reads, through bassync/mapedit.py — the
desktop editor's own helpers — so both editors accept and refuse the same
things. It never talks to 25Live or a BAS itself: every sync and tool is a
`bas-sync` process started through bassync/jobs.py.

Security, since this can start a sync that writes to building controllers:

- One password, BAS_WEB_PASSWORD, checked in constant time; five wrong
  attempts from an address lock it out for five minutes. Changing the
  password signs every session out.
- Signed, HttpOnly, SameSite session cookies (Secure over HTTPS), which
  expire after 12 hours.
- A CSRF token on every form and every state-changing request.
- A strict Content-Security-Policy: no inline script, nothing loaded from
  anywhere but this server — which also means it works on a controls network
  with no internet.
- Every change and every job is logged with the address that made it.
"""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

from .. import __version__

SESSION_HOURS = 12
LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
_OPEN_ENDPOINTS = {"login", "static", "healthz"}


class LoginThrottle:
    """Wrong passwords per client address; too many locks that address out."""

    def __init__(self, attempts: int = LOGIN_ATTEMPTS, lockout: int = LOCKOUT_SECONDS):
        self.attempts, self.lockout = attempts, lockout
        self._fails: dict = {}
        self._lock = threading.Lock()

    def locked_for(self, addr: str) -> int:
        with self._lock:
            count, since = self._fails.get(addr, (0, 0.0))
            if count < self.attempts:
                return 0
            left = int(since + self.lockout - time.monotonic())
            if left <= 0:
                self._fails.pop(addr, None)
                return 0
            return left

    def failed(self, addr: str) -> None:
        with self._lock:
            count, _since = self._fails.get(addr, (0, 0.0))
            self._fails[addr] = (count + 1, time.monotonic())
            if len(self._fails) > 10000:             # don't grow without bound
                self._fails.clear()

    def succeeded(self, addr: str) -> None:
        with self._lock:
            self._fails.pop(addr, None)


def _secret_key(state_dir: Path) -> bytes:
    """A random key kept in the state folder, so sessions survive a restart.
    If it can't be stored, a fresh one per start just signs everyone out."""
    path = state_dir / "web_secret"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    except OSError:
        logging.warning("[web] could not store the session key in %s; sessions "
                        "end when the service restarts", state_dir)
    return key


def create_app(service, settings: dict) -> Flask:
    """The Flask app for a running Service. `settings` is service.web_settings()."""
    app = Flask(__name__)
    secure = bool(settings.get("cert")) or bool(settings.get("behind_proxy"))
    app.config.update(
        SECRET_KEY=_secret_key(service.paths.state_dir),
        SESSION_COOKIE_NAME="bas_sync_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(settings.get("cert")),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        TEMPLATES_AUTO_RELOAD=False,
    )
    if settings.get("behind_proxy"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    auth_required = settings.get("auth", "password") != "none"
    password = settings.get("password") or ""
    fingerprint = hmac.new(app.config["SECRET_KEY"], password.encode(),
                           hashlib.sha256).hexdigest()[:32]
    throttle = LoginThrottle()
    app.extensions["bassync"] = {"service": service, "settings": settings,
                                 "write_lock": threading.Lock()}

    # ── request guards ───────────────────────────────────────────────────────

    @app.before_request
    def _guard():
        g.addr = request.remote_addr or "?"
        if request.endpoint in _OPEN_ENDPOINTS:
            return None
        if auth_required and not (session.get("auth")
                                  and hmac.compare_digest(str(session.get("pw", "")),
                                                          fingerprint)):
            session.clear()
            if request.method == "GET" and not request.path.startswith("/api/"):
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            abort(401)
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
            if not hmac.compare_digest(sent, session["csrf"]):
                abort(400, "The form had expired or came from another site. "
                           "Reload the page and try again.")
        return None

    @app.after_request
    def _headers(response):
        csp = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
               "script-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
               "base-uri 'none'; form-action 'self'; object-src 'none'")
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        if secure and request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    # ── login ────────────────────────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth_required:
            return redirect(url_for("dashboard"))
        target = _safe_next(request.values.get("next"))
        error = ""
        if request.method == "POST":
            wait = throttle.locked_for(g.addr)
            if wait:
                error = f"Too many wrong passwords. Try again in {wait // 60 + 1} minute(s)."
            elif hmac.compare_digest(request.form.get("password", "").encode(),
                                     password.encode()):
                throttle.succeeded(g.addr)
                session.clear()
                session.permanent = True
                session.update(auth=True, pw=fingerprint,
                               csrf=secrets.token_urlsafe(32))
                logging.info("[web] sign-in from %s", g.addr)
                return redirect(target)
            else:
                throttle.failed(g.addr)
                logging.warning("[web] wrong password from %s", g.addr)
                error = "That password isn't right."
        return render_template("login.html", error=error, next=target), (
            401 if error else 200)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login") if auth_required else url_for("dashboard"))

    @app.route("/healthz")
    def healthz():
        return {"ok": True}

    # ── template helpers ─────────────────────────────────────────────────────

    @app.context_processor
    def _globals():
        return {
            "app_version": __version__,
            "csrf_token": session.get("csrf", ""),
            "auth_required": auth_required,
            "current_job": service.jobs.current,
            "local_time": lambda value, fmt="%Y-%m-%d %H:%M": local_time(service, value, fmt),
            "relative": relative_time,
            "duration": duration_text,
        }

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def _error(exc):
        return render_template("error.html", error=exc), exc.code

    @app.errorhandler(401)
    def _unauthorised(exc):
        return {"error": "sign in again"}, 401

    from . import views
    views.register(app)
    return app


def ctx() -> dict:
    from flask import current_app
    return current_app.extensions["bassync"]


def audit(message: str, *args) -> None:
    """Log a change with the address that made it."""
    logging.info("[web] " + message + " (from %s)", *args, g.get("addr", "?"))


def notice(message: str, kind: str = "ok") -> None:
    flash(message, kind)


def _safe_next(target: Optional[str]) -> str:
    """Only ever redirect to a path on this server after signing in."""
    if not target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def campus_zone(service) -> ZoneInfo:
    try:
        return ZoneInfo(service.schedule.timezone or "UTC")
    except Exception:                                  # noqa: BLE001
        return ZoneInfo("UTC")


def parse_time(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local_time(service, value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    parsed = parse_time(value)
    return parsed.astimezone(campus_zone(service)).strftime(fmt) if parsed else ""


def relative_time(value) -> str:
    """'in 3 h 5 min' / '12 min ago' from an aware datetime or ISO text."""
    parsed = parse_time(value)
    if parsed is None:
        return ""
    seconds = int((parsed - datetime.now(timezone.utc)).total_seconds())
    future, seconds = seconds > 0, abs(seconds)
    if seconds < 60:
        text = "under a minute"
    elif seconds < 3600:
        text = f"{seconds // 60} min"
    elif seconds < 86400 * 2:
        text = f"{seconds // 3600} h {seconds % 3600 // 60} min"
    else:
        text = f"{seconds // 86400} days"
    return f"in {text}" if future else f"{text} ago"


def duration_text(started, finished) -> str:
    start, end = parse_time(started), parse_time(finished)
    if start is None or end is None:
        return ""
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"
