# 25Live -> BAS Schedule Sync — Microsoft Entra ID sign-in (OpenID Connect)
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Signing in with Microsoft Entra ID: the OpenID Connect authorization-code
flow, with PKCE, against one tenant.

    1. /auth/login sends the browser to Entra with a fresh `state`, `nonce`
       and PKCE challenge, kept in the (signed) session.
    2. Entra sends it back to /auth/callback with a one-time code.
    3. This server trades the code — with the client secret and the PKCE
       verifier — for an ID token, straight from Entra's token endpoint.
    4. The token's claims are checked (audience, issuer, tenant, expiry,
       nonce) and its `groups` claim decides the role (see access.py).

The ID token arrives directly from the token endpoint over verified TLS, so
its signature isn't checked separately — OpenID Connect Core 3.1.3.7 allows
exactly that for this flow, and it keeps a cryptography library out of the
image. Everything else about the token is checked.

Entra has to be told to put group IDs in the token: in the app registration,
Token configuration → Add groups claim → Security groups (or, for large
directories, "Groups assigned to the application"), emitted as Group ID.
A user in more groups than a token can hold gets an "overage" marker instead
of the list; that sign-in is refused with that explanation.
"""

import base64
import hashlib
import json
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

import requests

CLOCK_SKEW = 300
SCOPES = "openid profile email"


class SsoError(Exception):
    """Sign-in failed; the message is safe to show the user."""


def authority(settings: dict, override: str = "") -> str:
    """https://login.microsoftonline.com/<tenant> — or $BAS_WEB_SSO_AUTHORITY,
    which exists for testing (http is accepted only on localhost)."""
    base = (override or f"https://{settings['sso']['authority_host']}").rstrip("/")
    parts = urlparse(base)
    if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "localhost"):
        raise SsoError("The sign-in authority must be an https URL.")
    return f"{base}/{settings['sso']['tenant_id']}"


def new_request() -> dict:
    """The per-sign-in secrets kept in the session until the callback."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
            "verifier": verifier, "challenge": challenge, "started": time.time()}


def authorize_url(settings: dict, redirect_uri: str, pending: dict,
                  override: str = "") -> str:
    query = {
        "client_id": settings["sso"]["client_id"],
        "response_type": "code",
        "response_mode": "query",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": pending["state"],
        "nonce": pending["nonce"],
        "code_challenge": pending["challenge"],
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{authority(settings, override)}/oauth2/v2.0/authorize?{urlencode(query)}"


def redeem(settings: dict, client_secret: str, code: str, redirect_uri: str,
           pending: dict, override: str = "", post=None) -> dict:
    """Trade the code for an ID token and return its checked claims."""
    post = post or requests.post
    try:
        response = post(
            f"{authority(settings, override)}/oauth2/v2.0/token",
            data={"client_id": settings["sso"]["client_id"],
                  "client_secret": client_secret,
                  "grant_type": "authorization_code",
                  "code": code,
                  "redirect_uri": redirect_uri,
                  "code_verifier": pending["verifier"],
                  "scope": SCOPES},
            timeout=20)
    except requests.RequestException as exc:
        raise SsoError(f"Couldn't reach Microsoft to finish signing in: {exc}")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "id_token" not in body:
        detail = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
        raise SsoError(f"Microsoft refused the sign-in: {str(detail).splitlines()[0]}")
    return check_claims(decode(body["id_token"]), settings, pending, override)


def decode(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise SsoError("Microsoft returned an ID token that couldn't be read.")
    if not isinstance(data, dict):
        raise SsoError("Microsoft returned an ID token that couldn't be read.")
    return data


def check_claims(claims: dict, settings: dict, pending: dict, override: str = "",
                 now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    sso = settings["sso"]
    tenant = sso["tenant_id"].lower()
    expected_issuer = f"{authority(settings, override)}/v2.0".lower()
    problems = []
    if str(claims.get("aud", "")).lower() != sso["client_id"].lower():
        problems.append("it was issued for a different application")
    if str(claims.get("tid", "")).lower() != tenant:
        problems.append("it is from a different tenant")
    if str(claims.get("iss", "")).lower() != expected_issuer:
        problems.append("its issuer is not your tenant")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - CLOCK_SKEW:
        problems.append("it has expired")
    if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now + CLOCK_SKEW:
        problems.append("it isn't valid yet (check the server's clock)")
    if not secrets.compare_digest(str(claims.get("nonce", "")), pending["nonce"]):
        problems.append("it doesn't belong to this sign-in")
    if problems:
        raise SsoError("The sign-in token was refused: " + "; ".join(problems) + ".")
    names = claims.get("_claim_names")
    if (isinstance(names, dict) and "groups" in names) or claims.get("hasgroups"):
        raise SsoError(
            "Your account is in more groups than Microsoft puts in a sign-in "
            "token. An administrator can fix this in the Entra app registration: "
            "Token configuration → groups claim → \"Groups assigned to the "
            "application\", and assign the permitted groups to the app.")
    if not isinstance(claims.get("groups"), list):
        raise SsoError(
            "Microsoft didn't say which groups you are in. An administrator "
            "needs to add a groups claim in the Entra app registration "
            "(Token configuration → Add groups claim → Group ID).")
    return claims


def identity(claims: dict) -> dict:
    return {"name": str(claims.get("name") or claims.get("preferred_username") or "?"),
            "username": str(claims.get("preferred_username") or claims.get("upn")
                            or claims.get("email") or ""),
            "oid": str(claims.get("oid") or "")}
