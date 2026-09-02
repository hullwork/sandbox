"""Relying-party checks for the Console OpenID Connect login.

The RSA key, JWKS and ID token here are a **fixed vector**, not a key generated
at test time. The three services in this deployment family each implement their
own relying party (they may be operated separately and must not share code), so
the only thing keeping them from drifting apart is running the same inputs
against the same expected verdicts. Regenerating the vector defeats that.

🔴 The audience case is the one that matters most: with a shared identity
provider, an ID token minted for a neighbouring service must not be spendable
here. Control Plane therefore pins SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE and that setting has no
default - an unset audience refuses to start rather than falling back to the
client id, which is the value most likely to be identical across services.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control_plane import oidc  # noqa: E402


MODULUS = 18332870390079872060156787993612985210801828456599179204347265411510045268107598382678015833520106104006958759303814524369694430463614134171775477361124685236685792826359409861754277056477967284666103706171892251305023124813217200283882415062654902272036769070507122366934256792499964569687181461312999991698120007219136456602111720873965965010383479826486871864419453499652581490741406389092863304754816054062787554183139025024058624823128382744039860515179558975796093057802930970499298402742019276266896510408890706648321637319796820018753884856388432129236574755284425451614016872422158022675780350232716309000403
PRIVATE_EXPONENT = 7504400780546450520436183400287708534876186764807455033264020159521796609663587619392146890547994056062906184839761237854430207150882347031847974902221522664213646705719576550059996499597699442234730978309555723114574291916996167539187230860213971699222277550917719330114977905189382326182126387577152612681053449013223767917951334906165849816265577644330925226207078913143296277816142848502575637903874100986817139916186542913457385033608626325204826738587322534258562422754096370319143810931083134704685730058880385191979696043070570960724590345394180688466962924256662797782343202038119324044909765698642154741233
PUBLIC_EXPONENT = 65537
KEY_ID = "vector-key-1"
ISSUER = "https://issuer.invalid/realms/sandbox"
SECRET = b"s" * 32
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign(message: bytes) -> bytes:
    size = (MODULUS.bit_length() + 7) // 8
    digest = hashlib.sha256(message).digest()
    padding = size - 3 - len(SHA256_DIGEST_INFO) - len(digest)
    encoded = (
        b"\x00\x01" + b"\xff" * padding + b"\x00" + SHA256_DIGEST_INFO + digest
    )
    return pow(
        int.from_bytes(encoded, "big"), PRIVATE_EXPONENT, MODULUS
    ).to_bytes(size, "big")


def id_token(claims: dict, *, header: dict | None = None, valid: bool = True) -> str:
    head = b64(json.dumps(
        header or {"alg": "RS256", "kid": KEY_ID}, separators=(",", ":")
    ).encode())
    body = b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signature = sign(f"{head}.{body}".encode("ascii"))
    if not valid:
        signature = bytes([signature[0] ^ 0xFF]) + signature[1:]
    return f"{head}.{body}.{b64(signature)}"


JWKS = {
    "keys": [{
        "kty": "RSA",
        "kid": KEY_ID,
        "use": "sig",
        "alg": "RS256",
        "n": b64(MODULUS.to_bytes((MODULUS.bit_length() + 7) // 8, "big")),
        "e": b64(PUBLIC_EXPONENT.to_bytes(3, "big")),
    }]
}

DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    "code_challenge_methods_supported": ["S256"],
    "id_token_signing_alg_values_supported": ["RS256"],
}

ENVIRONMENT = {
    "SANDBOX_CONTROL_PLANE_OIDC_ISSUER": ISSUER,
    "SANDBOX_CONTROL_PLANE_OIDC_CLIENT_ID": "sandbox-console",
    "SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE": "sandbox-control-plane",
    "SANDBOX_CONTROL_PLANE_OIDC_REDIRECT_URL": "https://sandbox.invalid/v1/auth/oidc/callback",
    "SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS": "platform-operators",
    "SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM": "sandbox_tenant",
}


class _Transport:
    """Stands in for every HTTP call the relying party makes."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.token_requests = 0

    def __call__(self, request):
        url = request.full_url
        if url.endswith("/.well-known/openid-configuration"):
            return dict(DISCOVERY)
        if url == DISCOVERY["jwks_uri"]:
            return dict(JWKS)
        if url == DISCOVERY["token_endpoint"]:
            self.token_requests += 1
            return {"id_token": self.token, "token_type": "Bearer"}
        raise AssertionError(f"unexpected request: {url}")


class OidcConfigurationTests(unittest.TestCase):
    def test_absent_issuer_means_no_provider_and_no_complaint(self) -> None:
        self.assertEqual(oidc.load_config({}), (None, []))

    def test_audience_is_required_and_has_no_default(self) -> None:
        environment = {k: v for k, v in ENVIRONMENT.items()
                       if k != "SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE"}
        config, errors = oidc.load_config(environment)
        self.assertIsNone(config)
        self.assertTrue(
            any("SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE" in error for error in errors), errors
        )
        # It must not silently become the client id, the one value every
        # service in a deployment is most likely to have in common. Nothing
        # falls back, so no usable configuration comes out at all.
        self.assertTrue(
            any("no default" in error for error in errors), errors
        )

    def test_a_role_mapping_is_required(self) -> None:
        environment = {k: v for k, v in ENVIRONMENT.items()
                       if k not in {"SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS",
                                    "SANDBOX_CONTROL_PLANE_OIDC_TENANT_CLAIM"}}
        config, errors = oidc.load_config(environment)
        self.assertIsNone(config)
        self.assertTrue(errors)

    def test_plain_http_issuer_is_refused(self) -> None:
        config, errors = oidc.load_config(
            {**ENVIRONMENT, "SANDBOX_CONTROL_PLANE_OIDC_ISSUER": "http://issuer.invalid"}
        )
        self.assertIsNone(config)
        self.assertTrue(any("HTTPS" in error for error in errors), errors)


class OidcFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        config, errors = oidc.load_config(dict(ENVIRONMENT))
        self.assertEqual(errors, [])
        assert config is not None
        self.config = config
        self._request_json = oidc._request_json

    def tearDown(self) -> None:
        oidc._request_json = self._request_json

    def flow(self, claims_overrides: dict, *, valid_signature: bool = True,
             header: dict | None = None, audience: object = "sandbox-control-plane"):
        """Run begin() then complete() against the vector provider."""
        oidc._request_json = _Transport("")
        location, envelope = oidc.begin(self.config, SECRET)
        state = oidc._unseal(SECRET, envelope)
        self.assertIn("code_challenge_method=S256", location)
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": audience,
            "sub": "9f1c",
            "email": "operator@example.invalid",
            "email_verified": True,
            "nonce": state["nonce"],
            "iat": now,
            "exp": now + 300,
            "groups": ["platform-operators"],
            **claims_overrides,
        }
        transport = _Transport(
            id_token(claims, header=header, valid=valid_signature)
        )
        oidc._request_json = transport
        headers = {"Cookie": f"{oidc.STATE_COOKIE}={envelope}"}
        return oidc.complete(
            self.config,
            SECRET,
            {"code": ["authorization-code"], "state": [state["state"]]},
            headers,
            secure=True,
        )

    def test_a_valid_token_from_the_vector_provider_is_accepted(self) -> None:
        claims = self.flow({})
        self.assertEqual(claims["sub"], "9f1c")
        self.assertEqual(
            oidc.role_of(self.config, claims), ("admin", None)
        )

    def test_a_token_minted_for_a_neighbouring_service_is_refused(self) -> None:
        # Same provider, same signing key, same everything except the audience.
        with self.assertRaises(oidc.OidcError) as raised:
            self.flow({}, audience="sites-control_plane")
        self.assertIn("audience", str(raised.exception))

    def test_a_token_listing_several_audiences_still_needs_ours(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.flow({}, audience=["sites-control_plane", "agent-control_plane"])

    def test_wrong_issuer_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.flow({"iss": "https://evil.invalid"})

    def test_replayed_nonce_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.flow({"nonce": "a-nonce-from-another-flow"})

    def test_expired_token_is_refused(self) -> None:
        now = int(time.time())
        with self.assertRaises(oidc.OidcError):
            self.flow({"exp": now - 3600, "iat": now - 7200})

    def test_tampered_signature_is_refused(self) -> None:
        with self.assertRaises(oidc.OidcError):
            self.flow({}, valid_signature=False)

    def test_unsigned_and_symmetric_algorithms_are_refused(self) -> None:
        for algorithm in ("none", "HS256"):
            with self.subTest(alg=algorithm):
                with self.assertRaises(oidc.OidcError):
                    self.flow({}, header={"alg": algorithm, "kid": KEY_ID})

    def test_callback_state_must_match_the_cookie(self) -> None:
        oidc._request_json = _Transport("")
        _, envelope = oidc.begin(self.config, SECRET)
        with self.assertRaises(oidc.OidcError):
            oidc.complete(
                self.config,
                SECRET,
                {"code": ["c"], "state": ["not-the-state-we-issued"]},
                {"Cookie": f"{oidc.STATE_COOKIE}={envelope}"},
                secure=True,
            )

    def test_state_cookie_signed_with_another_key_is_refused(self) -> None:
        oidc._request_json = _Transport("")
        _, envelope = oidc.begin(self.config, SECRET)
        with self.assertRaises(oidc.OidcError):
            oidc.complete(
                self.config,
                b"x" * 32,
                {"code": ["c"], "state": ["whatever"]},
                {"Cookie": f"{oidc.STATE_COOKIE}={envelope}"},
                secure=True,
            )


class RoleMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        config, _ = oidc.load_config(dict(ENVIRONMENT))
        assert config is not None
        self.config = config

    def test_tenant_claim_maps_to_that_tenant(self) -> None:
        self.assertEqual(
            oidc.role_of(self.config, {"groups": [], "sandbox_tenant": "acme"}),
            ("tenant", "acme"),
        )

    def test_an_unmapped_identity_is_refused_rather_than_defaulted(self) -> None:
        with self.assertRaises(oidc.OidcError):
            oidc.role_of(self.config, {"groups": ["everyone"]})


class SignatureVectorTests(unittest.TestCase):
    def test_the_verifier_accepts_and_rejects_the_fixed_vector(self) -> None:
        message = b"the quick brown fox"
        signature = sign(message)
        self.assertTrue(oidc.rsa_pkcs1_v15_sha256_verify(
            MODULUS, PUBLIC_EXPONENT, signature, message
        ))
        self.assertFalse(oidc.rsa_pkcs1_v15_sha256_verify(
            MODULUS, PUBLIC_EXPONENT, signature, message + b"!"
        ))
        self.assertFalse(oidc.rsa_pkcs1_v15_sha256_verify(
            MODULUS, PUBLIC_EXPONENT, signature[:-1], message
        ))


if __name__ == "__main__":
    unittest.main()
