"""Signed browser session for Sandbox Console.

Only the OIDC callback mints one. It records what the identity provider said -
which Control Plane role the login mapped to and which tenant, if any - and nothing the
browser can influence afterwards.

🔴 The signing secret is derived from SIGNING_KEY under a fixed label, never
SANDBOX_CONTROL_PLANE_TOKEN. SANDBOX_CONTROL_PLANE_TOKEN is a bearer credential that its holders must send to
Control Plane on every call (RFC 6750); a value handed out that way cannot also be the
key that authenticates sessions, and NIST SP 800-57 §5.2 states the rule
directly: one key, one purpose.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from typing import Any


COOKIE = "__Host-sandbox_console_session"
CSRF_COOKIE = "__Host-sandbox_console_csrf"
CSRF_HEADER = "X-Console-CSRF"
TTL_SECONDS = 8 * 60 * 60
UNSAFE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def derive_secret(signing_key: bytes) -> bytes:
    """A dedicated subkey, so the session key and the token-signing key differ."""
    return hmac.new(signing_key, b"sandbox-console-session-v1", hashlib.sha256).digest()


def cookie_name(base: str, *, secure: bool) -> str:
    # The __Host- prefix is only legal on a Secure cookie; on a loopback HTTP
    # development origin the browser drops it silently, which reads exactly like
    # a broken login. Drop the prefix with the guarantee it stands for.
    return base if secure else base.removeprefix("__Host-")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(secret: bytes, encoded: str) -> str:
    return _b64(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())


def issue(
    secret: bytes,
    *,
    kind: str,
    tenant_id: str | None,
    subject: str,
    email: str,
    now: int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return (session value, CSRF token, claims)."""
    if kind not in {"admin", "tenant"}:
        raise ValueError("session kind is invalid")
    current = int(time.time() if now is None else now)
    claims = {
        "v": 1,
        "aud": "sandbox-console",
        "kind": kind,
        "tenant": tenant_id,
        "sub": subject,
        "email": email,
        "csrf": secrets.token_urlsafe(24),
        "iat": current,
        "exp": current + TTL_SECONDS,
    }
    encoded = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return f"{encoded}.{_sign(secret, encoded)}", str(claims["csrf"]), claims


def read(
    secret: bytes,
    headers: Any,
    *,
    secure: bool,
    method: str,
    now: int | None = None,
) -> dict[str, Any] | None:
    """The claims of the current browser session, or None.

    Mutating methods additionally require the CSRF token from the readable
    companion cookie to be echoed in a header. SameSite=Lax alone leaves
    top-level POST navigations through.
    """
    cookies = SimpleCookie()
    try:
        cookies.load(str(headers.get("Cookie", "")))
    except CookieError:
        return None
    morsel = cookies.get(cookie_name(COOKIE, secure=secure))
    if morsel is None or len(morsel.value) > 4096 or not morsel.value.isascii():
        return None
    encoded, _, signature = morsel.value.partition(".")
    if not encoded or not signature:
        return None
    if not hmac.compare_digest(signature, _sign(secret, encoded)):
        return None
    try:
        claims = json.loads(_unb64(encoded))
    except (ValueError, UnicodeDecodeError):
        return None
    current = int(time.time() if now is None else now)
    try:
        valid = (
            isinstance(claims, dict)
            and claims.get("v") == 1
            and claims.get("aud") == "sandbox-console"
            and claims.get("kind") in {"admin", "tenant"}
            and isinstance(claims.get("sub"), str)
            and bool(claims["sub"])
            and int(claims.get("iat", 0)) <= current + 5
            and current <= int(claims.get("exp", 0)) <= current + TTL_SECONDS
        )
    except (TypeError, ValueError):
        return None
    if not valid:
        return None
    if method in UNSAFE_METHODS and not hmac.compare_digest(
        str(claims.get("csrf") or ""), str(headers.get(CSRF_HEADER, ""))
    ):
        return None
    return claims


def _cookie(name: str, value: str, *, secure: bool, http_only: bool, max_age: int) -> str:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "SameSite=Lax"]
    if http_only:
        parts.insert(3, "HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def set_cookies(value: str, csrf: str, *, secure: bool) -> list[str]:
    return [
        _cookie(
            cookie_name(COOKIE, secure=secure), value,
            secure=secure, http_only=True, max_age=TTL_SECONDS,
        ),
        _cookie(
            cookie_name(CSRF_COOKIE, secure=secure), csrf,
            secure=secure, http_only=False, max_age=TTL_SECONDS,
        ),
    ]


def clear_cookies(*, secure: bool) -> list[str]:
    return [
        _cookie(
            cookie_name(COOKIE, secure=secure), "",
            secure=secure, http_only=True, max_age=0,
        ),
        _cookie(
            cookie_name(CSRF_COOKIE, secure=secure), "",
            secure=secure, http_only=False, max_age=0,
        ),
    ]
