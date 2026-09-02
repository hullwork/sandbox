"""OpenID Connect Authorization Code + PKCE relying party for the Console.

Control Plane is a relying party only. It never issues assertions for anybody else and
it never accepts one: browser identity comes from the deployment's own provider,
and the two services this project used to federate with are, from here, ordinary
API-key tenants with no standing to name a Control Plane administrator.

Pure standard library on purpose. The Control Plane image carries database drivers and
nothing else (control_plane/requirements.lock), and an RS256 signature check is one
modular exponentiation plus a fixed PKCS#1 v1.5 comparison.

🔴 SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE has no default and no fallback to the client id. Every
service in a deployment that shares one provider must pin a **different**
audience, otherwise an ID token minted for one of them is accepted by the
others and separating the trust boundaries achieved nothing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .store import MANAGEMENT_TENANT


STATE_COOKIE = "__Host-sandbox_console_oidc"
CALLBACK_PATH = "/v1/auth/oidc/callback"
FLOW_SECONDS = 10 * 60
CLOCK_SKEW_SECONDS = 60
MAX_HTTP_BODY = 1024 * 1024
# SHA-256 DigestInfo prefix from RFC 8017 A.2.4; the bytes an RS256 signature
# must decrypt to ahead of the digest itself.
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class OidcError(PermissionError):
    """The flow is invalid or the provider cannot be trusted."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"OIDC endpoint refused redirect ({code})")


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect()
)


@dataclass(frozen=True)
class Config:
    issuer: str
    client_id: str
    client_secret: str
    audience: str
    redirect_url: str
    scopes: tuple[str, ...]
    groups_claim: str
    admin_groups: frozenset[str]
    tenant_claim: str
    allow_insecure_http: bool


def _url(value: str, name: str, *, allow_http: bool, errors: list[str]) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        errors.append(f"{name} is not a valid URL")
        return ""
    if parsed.scheme != "https" and not (
        allow_http and parsed.scheme == "http" and loopback
    ):
        errors.append(f"{name} must use HTTPS")
        return ""
    return value.rstrip("/")


def load_config(environ: dict[str, str]) -> tuple[Config | None, list[str]]:
    """Read the RP configuration. Returns (config, configuration errors).

    ``None`` with no errors means this deployment simply has no provider
    configured. Every other gap is reported so the caller can refuse to start
    with the whole list at once, rather than one item per restart.
    """
    issuer_raw = environ.get("SANDBOX_CONTROL_PLANE_OIDC_ISSUER", "").strip()
    if not issuer_raw:
        return None, []
    errors: list[str] = []
    allow_http = environ.get(
        "SANDBOX_CONTROL_PLANE_OIDC_ALLOW_INSECURE_HTTP", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    issuer = _url(
        issuer_raw, "SANDBOX_CONTROL_PLANE_OIDC_ISSUER", allow_http=allow_http, errors=errors
    )
    redirect_url = _url(
        environ.get("SANDBOX_CONTROL_PLANE_OIDC_REDIRECT_URL", "").strip(),
        "SANDBOX_CONTROL_PLANE_OIDC_REDIRECT_URL",
        allow_http=allow_http,
        errors=errors,
    )
    client_id = environ.get("SANDBOX_CONTROL_PLANE_OIDC_CLIENT_ID", "").strip()
    if not client_id or len(client_id) > 512:
        errors.append(
            "SANDBOX_CONTROL_PLANE_OIDC_CLIENT_ID is required; the client registered for this "
            "Control Plane at the identity provider"
        )
    audience = environ.get("SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE", "").strip()
    if not audience:
        errors.append(
            "SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE is required and has no default; give this "
            "Control Plane an audience no other service shares, or an ID token issued "
            "for a neighbouring service is accepted here"
        )
    scopes = tuple(dict.fromkeys(
        environ.get("SANDBOX_CONTROL_PLANE_OIDC_SCOPES", "openid email profile").split()
    ))
    if "openid" not in scopes:
        errors.append("SANDBOX_CONTROL_PLANE_OIDC_SCOPES must include openid")
    admin_groups = frozenset(environ.get("SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS", "").split())
    tenant_claim = environ.get("SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM", "").strip()
    if not admin_groups and not tenant_claim:
        errors.append(
            "SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS or SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM is required; "
            "without one of them no provider identity maps to any Control Plane role "
            "and every login ends in 403"
        )
    if errors:
        return None, errors
    return Config(
        issuer=issuer,
        client_id=client_id,
        client_secret=environ.get("SANDBOX_CONTROL_PLANE_OIDC_CLIENT_SECRET", "").strip(),
        audience=audience,
        redirect_url=redirect_url,
        scopes=scopes,
        groups_claim=(
            environ.get("SANDBOX_CONTROL_PLANE_OIDC_GROUPS_CLAIM", "groups").strip() or "groups"
        ),
        admin_groups=admin_groups,
        tenant_claim=tenant_claim,
        allow_insecure_http=allow_http,
    ), []


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _read_json(response) -> dict[str, Any]:
    raw = response.read(MAX_HTTP_BODY + 1)
    if len(raw) > MAX_HTTP_BODY:
        raise OidcError("OIDC response is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OidcError("OIDC endpoint returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise OidcError("OIDC endpoint returned invalid JSON")
    return value


def _request_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with _OPENER.open(request, timeout=10) as response:
            return _read_json(response)
    except OidcError:
        raise
    except urllib.error.HTTPError as exc:
        raise OidcError(f"OIDC endpoint rejected the request ({exc.code})") from None
    except Exception as exc:
        raise OidcError("OIDC endpoint request failed") from exc


def discover(config: Config) -> dict[str, str]:
    document = _request_json(urllib.request.Request(
        f"{config.issuer}/.well-known/openid-configuration",
        headers={"Accept": "application/json"},
    ))
    if document.get("issuer") != config.issuer:
        raise OidcError("OIDC discovery issuer mismatch")
    metadata: dict[str, str] = {}
    errors: list[str] = []
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = document.get(key)
        if not isinstance(value, str):
            raise OidcError(f"OIDC discovery is missing {key}")
        metadata[key] = _url(
            value, key, allow_http=config.allow_insecure_http, errors=errors
        )
    if errors:
        raise OidcError(f"OIDC discovery is invalid: {errors[0]}")
    methods = document.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise OidcError("OIDC provider does not support PKCE S256")
    algorithms = document.get("id_token_signing_alg_values_supported")
    if isinstance(algorithms, list) and "RS256" not in algorithms:
        raise OidcError("OIDC provider does not support RS256 ID tokens")
    return metadata


def _seal(secret: bytes, payload: dict[str, Any]) -> str:
    """Integrity-protect the in-flight state for the round trip to the provider.

    Signed, not encrypted: the standard library ships no AEAD, and the value the
    cookie protects is the PKCE verifier, whose whole point is to be secret from
    *another* application that intercepted the authorization code. The browser
    holding the flow is the legitimate client; the cookie is HttpOnly, __Host-
    prefixed and scoped to the callback path, so no other origin can read it.
    """
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _unseal(secret: bytes, value: str, *, now: int | None = None) -> dict[str, Any]:
    encoded, _, signature = value.partition(".")
    if not encoded or not signature or not value.isascii():
        raise OidcError("OIDC state is invalid")
    expected = _b64(hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise OidcError("OIDC state is invalid")
    try:
        payload = json.loads(_unb64(encoded))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("OIDC state is invalid") from exc
    current = int(time.time() if now is None else now)
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < current:
        raise OidcError("OIDC state is invalid or expired")
    return payload


def begin(config: Config, secret: bytes, *, now: int | None = None) -> tuple[str, str]:
    """Return (authorization URL, sealed state for the flow cookie)."""
    metadata = discover(config)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    issued_at = int(time.time() if now is None else now)
    envelope = _seal(secret, {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "exp": issued_at + FLOW_SECONDS,
    })
    query = urlencode({
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_url,
        "scope": " ".join(config.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode("ascii")).digest()),
        "code_challenge_method": "S256",
    })
    separator = "&" if "?" in metadata["authorization_endpoint"] else "?"
    return f"{metadata['authorization_endpoint']}{separator}{query}", envelope


def state_cookie(value: str, *, secure: bool) -> str:
    # __Host- requires Path=/ and Secure, so the plain name is used whenever the
    # deployment allows loopback HTTP; the browser silently drops a __Host-
    # cookie that does not meet the rule and the flow would fail with no state.
    name = STATE_COOKIE if secure else STATE_COOKIE.removeprefix("__Host-")
    parts = [f"{name}={value}", "Path=/", f"Max-Age={FLOW_SECONDS if value else 0}",
             "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _state_from_cookie(
    secret: bytes, headers, *, secure: bool, now: int | None = None
) -> dict[str, Any]:
    cookies = SimpleCookie()
    try:
        cookies.load(str(headers.get("Cookie", "")))
    except CookieError as exc:
        raise OidcError("OIDC state cookie is invalid") from exc
    name = STATE_COOKIE if secure else STATE_COOKIE.removeprefix("__Host-")
    morsel = cookies.get(name)
    if morsel is None or len(morsel.value) > 4096:
        raise OidcError("OIDC state cookie is missing")
    return _unseal(secret, morsel.value, now=now)


def _exchange_code(
    config: Config, metadata: dict[str, str], *, code: str, verifier: str
) -> dict[str, Any]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_url,
        "client_id": config.client_id,
        "code_verifier": verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if config.client_secret:
        credentials = base64.b64encode(
            f"{quote(config.client_id, safe='')}:"
            f"{quote(config.client_secret, safe='')}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {credentials}"
    document = _request_json(urllib.request.Request(
        metadata["token_endpoint"],
        data=urlencode(form).encode("utf-8"),
        headers=headers,
        method="POST",
    ))
    if not isinstance(document.get("id_token"), str):
        raise OidcError("OIDC token response has no ID token")
    return document


def rsa_pkcs1_v15_sha256_verify(
    modulus: int, exponent: int, signature: bytes, message: bytes
) -> bool:
    """RFC 8017 RSASSA-PKCS1-v1_5 verification for SHA-256, standard library only."""
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size or modulus < 2 or exponent < 3:
        return False
    encoded = pow(int.from_bytes(signature, "big"), exponent, modulus)
    padding_length = size - 3 - len(_SHA256_DIGEST_INFO) - hashlib.sha256().digest_size
    if padding_length < 8:
        return False
    expected = (
        b"\x00\x01"
        + b"\xff" * padding_length
        + b"\x00"
        + _SHA256_DIGEST_INFO
        + hashlib.sha256(message).digest()
    )
    return hmac.compare_digest(encoded.to_bytes(size, "big"), expected)


def _signing_key(jwks: dict[str, Any], kid: str) -> tuple[int, int]:
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise OidcError("OIDC JWKS is invalid")
    key = next((
        item for item in keys
        if isinstance(item, dict)
        and item.get("kid") == kid
        and item.get("kty") == "RSA"
        and item.get("use", "sig") == "sig"
        and item.get("alg", "RS256") == "RS256"
        and (not isinstance(item.get("key_ops"), list) or "verify" in item["key_ops"])
    ), None)
    if key is None:
        raise OidcError("OIDC signing key is unavailable")
    try:
        if not isinstance(key.get("n"), str) or len(key["n"]) > 1024:
            raise ValueError("invalid RSA modulus")
        modulus = int.from_bytes(_unb64(str(key["n"])), "big")
        exponent = int.from_bytes(_unb64(str(key["e"])), "big")
    except (KeyError, ValueError) as exc:
        raise OidcError("OIDC signing key is unusable") from exc
    if modulus.bit_length() < 2048:
        raise OidcError("OIDC signing key is too small")
    return modulus, exponent


def verify_id_token(
    config: Config,
    metadata: dict[str, str],
    token: str,
    *,
    nonce: str,
    now: int | None = None,
) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise OidcError("OIDC ID token is malformed")
    try:
        header = json.loads(_unb64(parts[0]))
        claims = json.loads(_unb64(parts[1]))
        signature = _unb64(parts[2])
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("OIDC ID token is malformed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("OIDC ID token is malformed")
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise OidcError("OIDC ID token algorithm is not allowed")
    modulus, exponent = _signing_key(
        _request_json(urllib.request.Request(
            metadata["jwks_uri"], headers={"Accept": "application/json"}
        )),
        header["kid"],
    )
    if not rsa_pkcs1_v15_sha256_verify(
        modulus, exponent, signature, f"{parts[0]}.{parts[1]}".encode("ascii")
    ):
        raise OidcError("OIDC ID token signature is invalid")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if not audiences or not all(isinstance(value, str) for value in audiences):
        raise OidcError("OIDC ID token audience is invalid")
    if claims.get("iss") != config.issuer:
        raise OidcError("OIDC ID token issuer is invalid")
    # 🔴 The audience this Control Plane was given, not the client id: sharing one
    # provider between services is normal, and only a distinct audience keeps a
    # token minted for the service next door from being spent here.
    if config.audience not in audiences:
        raise OidcError("OIDC ID token audience is invalid")
    if len(audiences) > 1 and claims.get("azp") != config.client_id:
        raise OidcError("OIDC ID token authorized party is invalid")
    current = int(time.time() if now is None else now)
    try:
        expires_at = int(claims["exp"])
        issued_at = int(claims.get("iat", current))
        not_before = int(claims.get("nbf", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise OidcError("OIDC ID token time claims are invalid") from exc
    if expires_at < current - CLOCK_SKEW_SECONDS:
        raise OidcError("OIDC ID token is expired")
    if issued_at > current + CLOCK_SKEW_SECONDS:
        raise OidcError("OIDC ID token was issued in the future")
    if not_before > current + CLOCK_SKEW_SECONDS:
        raise OidcError("OIDC ID token is not active")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(
        str(claims["nonce"]), nonce
    ):
        raise OidcError("OIDC ID token nonce mismatch")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise OidcError("OIDC ID token subject is missing")
    return claims


def complete(
    config: Config,
    secret: bytes,
    query: dict[str, list[str]],
    headers,
    *,
    secure: bool,
    now: int | None = None,
) -> dict[str, Any]:
    if query.get("error"):
        raise OidcError("OIDC provider rejected authorization")
    code = query.get("code", [""])[0]
    supplied_state = query.get("state", [""])[0]
    if not code or len(code) > 4096 or not supplied_state:
        raise OidcError("OIDC callback is incomplete")
    flow = _state_from_cookie(secret, headers, secure=secure, now=now)
    expected_state = str(flow.get("state", ""))
    if not expected_state or not hmac.compare_digest(supplied_state, expected_state):
        raise OidcError("OIDC callback state mismatch")
    metadata = discover(config)
    tokens = _exchange_code(
        config, metadata, code=code, verifier=str(flow.get("verifier", ""))
    )
    return verify_id_token(
        config,
        metadata,
        str(tokens["id_token"]),
        nonce=str(flow.get("nonce", "")),
        now=now,
    )


def role_of(config: Config, claims: dict[str, Any]) -> tuple[str, str | None]:
    """Map provider claims onto (kind, tenant id).

    ``("admin", None)`` is the management plane; ``("tenant", "<id>")`` is one
    tenant. No default: an identity that matches neither rule is refused, so
    adding a user at the provider does not silently grant Control Plane access.
    """
    raw_groups = claims.get(config.groups_claim, [])
    groups = {str(value) for value in raw_groups} if isinstance(raw_groups, list) else set()
    if config.admin_groups & groups:
        return "admin", None
    if config.tenant_claim:
        tenant = claims.get(config.tenant_claim)
        if isinstance(tenant, str) and tenant.strip():
            if tenant.strip() == MANAGEMENT_TENANT:
                # The reserved management row is not a tenant a provider can
                # map a person onto; a claim naming it is a misconfiguration
                # (or a hostile IdP), not a sign-in.
                raise OidcError("the tenant claim names the reserved management tenant")
            return "tenant", tenant.strip()
    raise OidcError("no Control Plane role is mapped to this identity")
