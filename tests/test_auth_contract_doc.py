"""The published authentication contract must describe the running platform.

`docs/AUTH.md` is what a third-party client author reads and builds against, so
the load-bearing facts in it are pinned here rather than trusted to review. The
failure this prevents is quiet: an implementation detail changes, the document
keeps promising the old one, and nothing is wrong until somebody outside this
repository has already built on the promise.

Only the things a client can actually depend on are pinned - the identifier
shapes, the permission vocabulary, the error strings the document tells clients
to branch near, and the routes it names. Prose is left to review.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/AUTH.md"


def load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store = load("sandbox_store_for_auth_doc", "control_plane/store.py")


class AuthContractDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DOCUMENT.read_text(encoding="utf-8")
        cls.api = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
        cls.control_plane = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")

    def test_the_document_is_reachable_from_the_index(self) -> None:
        index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
        self.assertIn("(AUTH.md)", index)

    def test_the_acting_subject_shape_matches_the_implementation(self) -> None:
        # The document tells clients to derive exactly this. A tightened regex
        # in the Control Plane would start refusing values the contract promised.
        self.assertIn("32 lowercase hexadecimal characters", self.text)
        self.assertIn(
            'ACTING_SUBJECT_RE = re.compile(r"[0-9a-f]{32}")', self.control_plane
        )
        example = re.search(r"X-Acting-Subject: ([0-9a-f]+)", self.text)
        self.assertIsNotNone(example, "the document must show a usable example")
        self.assertRegex(example.group(1), r"^[0-9a-f]{32}$")

    def test_the_permission_vocabulary_matches_the_store(self) -> None:
        documented = set(re.findall(r"`(act_as_subjects)`", self.text))
        self.assertEqual(documented, set(store.KEY_PERMISSIONS))

    def test_the_key_prefix_matches_what_is_issued(self) -> None:
        """Every claim the document makes about the shape of a key.

        Deliberately not `issued.split("_")[2]`: that is the parse the document
        tells clients is unreliable, and an assertion doing it fails about one
        run in six. The claims below are the ones a client can actually hold
        the platform to, and each is checked the way the document states it.
        """
        self.assertIn("sk_<random>_<scope>_<random>", self.text)
        for tenant, scope in (("acme", "acme"), (None, "admin")):
            with self.subTest(scope=scope):
                issued = store.generate_key(tenant)
                self.assertTrue(issued.startswith("sk_"), issued[:8])
                self.assertIn(f"_{scope}_", issued)
                self.assertGreater(len(issued), 40)
                # "the leading random segment - not the scope - is what makes
                # the first 12 characters unique for lookup": the scope must
                # therefore be absent from the lookup prefix entirely.
                prefix = issued[:store.KEY_PREFIX_LENGTH]
                self.assertNotIn(scope, prefix, prefix)
        # ...and that prefix must actually vary for one tenant, which is the
        # whole reason the random material comes first.
        prefixes = {
            store.generate_key("acme")[:store.KEY_PREFIX_LENGTH]
            for _ in range(200)
        }
        self.assertEqual(len(prefixes), 200)

    def test_the_document_warns_that_a_key_is_not_splittable(self) -> None:
        """🔴 Not merely "do not parse it" - it *cannot* be parsed reliably.

        The random segments are base64url, whose alphabet includes `_`. About
        one key in six carries an underscore inside a random segment, so the
        obvious `key.split("_")[2]` reads the wrong field that often. A client
        author who is told only "do not parse" may still try; telling them it is
        ambiguous is what stops them. (This test exists because the guard it
        replaced did exactly that split, and failed on one run in six.)
        """
        self.assertIn("do not\nparse it to decide anything", self.text)
        self.assertIn("base64url", self.text)
        keys = [store.generate_key("acme") for _ in range(500)]
        self.assertTrue(
            any(key.count("_") > 3 for key in keys),
            "the random segments no longer contain the separator; if the key "
            "alphabet changed, revisit what the contract says about parsing",
        )

    def test_every_documented_error_string_is_one_the_control_plane_emits(self) -> None:
        # Clients are told to branch on status plus route and to log the text.
        # A text that no longer exists sends them reading a string that will
        # never appear.
        rows = [
            line for line in self.text.splitlines()
            if line.startswith("| `4") or line.startswith("| `5")
        ]
        self.assertGreaterEqual(len(rows), 10, "the error table lost rows")
        quoted = set()
        for row in rows:
            for value in re.findall(r"`([^`]+)`", row.split("|")[2]):
                quoted.add(value)
        # Adjacent string literals are joined first: a message the Control Plane
        # wraps across two source lines is one string at runtime, and looking
        # for it verbatim would report a drift that does not exist.
        source = re.sub(r'"\s*\n\s*"', "", self.api + self.control_plane)
        missing = [
            value for value in quoted
            if value.replace("<id>", "").strip() not in source
            and value.split(":")[0].strip() not in source
        ]
        self.assertEqual(missing, [], f"not emitted anywhere: {missing}")

    def test_the_documented_routes_exist_in_the_dispatcher(self) -> None:
        for route in (
            "/v1/auth/methods",
            "/v1/auth/oidc/login",
            "/v1/auth/oidc/callback",
            "/v1/auth/logout",
            "/v1/whoami",
            "/v1/admin/keys",
        ):
            with self.subTest(route=route):
                self.assertIn(route, self.text)
                self.assertIn(f'"{route}"', self.api)

    def test_the_documented_key_lifetime_bounds_match_the_handler(self) -> None:
        self.assertIn("1 second to 1 year", self.text)
        self.assertIn("not 0 < raw_expiry <= 365 * 24 * 3600", self.api)

    def test_the_documented_activity_window_matches_the_store(self) -> None:
        self.assertIn("at most once every five minutes", self.text)
        self.assertEqual(store.TOUCH_THROTTLE_SECONDS, 300)

    def test_the_document_names_no_particular_client(self) -> None:
        # A contract published by a product that may be operated separately must
        # read the same for every integrator. A named client in it is a design
        # leak, and the first thing the next reader assumes is privileged.
        #
        # Word boundaries, not substrings: "your own client" contains "our
        # client", and a guard that cannot tell those apart gets deleted the
        # first time it cries wolf.
        for marker in (
            r"\bagent host\b",
            r"\bthe agent\b",
            r"\bour own client\b",
            r"\bthe first client\b",
            r"\bpartner service\b(?! whose)",
        ):
            with self.subTest(marker=marker):
                self.assertIsNone(
                    re.search(marker, self.text, re.IGNORECASE),
                    f"names a particular client: {marker}",
                )


if __name__ == "__main__":
    unittest.main()
