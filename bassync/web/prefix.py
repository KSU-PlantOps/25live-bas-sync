# 25Live -> BAS Schedule Sync — web UI under a path prefix
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Serving the web UI under a path, e.g. https://host/bas/, behind a reverse
proxy that shares the host with something else.

Flask builds every link and redirect from the request's SCRIPT_NAME (the
prefix) and PATH_INFO (the rest), so all that's needed is to split the path
the right way before Flask sees it. PathPrefix does that, for the two ways a
proxy can forward `/bas/login`:

- the full path, unchanged (IIS URL Rewrite + ARR, nginx `proxy_pass` without
  a URI, ...), with BAS_WEB_BASE_PATH=/bas: the prefix is moved from
  PATH_INFO to SCRIPT_NAME. `/bas` redirects to `/bas/`, and anything else
  outside the prefix is 404 — a link that escaped the prefix shows up at once,
  instead of half-working.
- stripped to `/login`, with `X-Forwarded-Prefix: /bas` and
  BAS_WEB_BEHIND_PROXY=1: ProxyFix (outside this) has already set
  SCRIPT_NAME from the header.

With both set, BAS_WEB_BASE_PATH wins: SCRIPT_NAME is always exactly it, and
a forwarded prefix is only used to accept a path the proxy already stripped.

Either way SCRIPT_NAME must be a plain path, since it ends up in every link
and redirect: a forwarded prefix like `//evil.example` is refused with 400.
"""

from ..service import SAFE_PATH


def _respond(start_response, status: str, body: str, headers=()):
    data = body.encode()
    start_response(status, [("Content-Type", "text/plain; charset=utf-8"),
                            ("Content-Length", str(len(data))),
                            ("Cache-Control", "no-store"), *headers])
    return [data]


class PathPrefix:
    """WSGI middleware: serve `app` under `prefix` ('' for the site root)."""

    def __init__(self, app, prefix: str = ""):
        self.app, self.prefix = app, prefix

    def __call__(self, environ, start_response):
        script = environ.get("SCRIPT_NAME", "").rstrip("/")
        path = environ.get("PATH_INFO", "") or "/"
        prefix = self.prefix
        if prefix:
            if path == prefix:
                query = environ.get("QUERY_STRING", "")
                where = prefix + "/" + ("?" + query if query else "")
                return _respond(start_response, "308 Permanent Redirect",
                                f"The web UI is at {prefix}/\n", [("Location", where)])
            if path.startswith(prefix + "/"):
                environ["PATH_INFO"] = path[len(prefix):]
            elif script != prefix:            # not stripped by a proxy that said so
                return _respond(start_response, "404 Not Found",
                                f"Not found. The web UI is at {prefix}/\n")
            script = prefix
        elif not SAFE_PATH.fullmatch(script):
            return _respond(start_response, "400 Bad Request",
                            "X-Forwarded-Prefix is not a plain path.\n")
        environ["SCRIPT_NAME"] = script
        return self.app(environ, start_response)
