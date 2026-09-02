"""The shared acting-subject vector, checked from the receiving side.

`docs/acting-subject-vectors.json` is vendored: every product on this boundary
carries an identical copy so that none of them depends on another's checkout.

🔴 What this file deliberately does **not** do is re-implement the derivation
and check that it reproduces the expected values. This platform never derives a
pseudonym - it only receives and validates one. A reproduction test here would
be testing a copy of the formula written for the test, which is the same thing
as a stub agreeing with itself: it would stay green while this platform rejected
every pseudonym the real deriver sends.

The failure mode that is real here is the opposite one: **the upstream derives
correctly and this side refuses it.** That is what is asserted - the header name
this platform reads, the character class it enforces, and every expected value
in the vector going through the validation path unchanged.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import json
import pathlib
import re
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VECTORS = ROOT / "docs/acting-subject-vectors.json"

#: sha256 over the load-bearing fields only - header, pattern, minimum salt and
#: the vectors themselves - with prose excluded. The artifact carries the same
#: value in its own `_payload_sha256`, and both are checked against a digest
#: computed here, which makes three independent ways to notice drift:
#:
#:   * vectors edited, recorded digest left alone -> self-consistency fails;
#:   * recorded digest edited, vectors left alone -> self-consistency fails;
#:   * both edited together                       -> the constant below fails.
#:
#: 🔴 The anchor is the data, not the file's byte hash. Each service may write
#: the surrounding prose in its own words, so comparing whole files would report
#: a difference on every copy and get switched off. Comparing the payload
#: reports a difference only when the payload differs.
CANONICAL_DIGEST = (
    "1a5daa90834c2cc2e4d793dcfd946689f7434e157d9bae9bb5019f83caa2c24e"
)


#: The exact keys the artifact's own `_payload_canonicalization` section names.
PAYLOAD_KEYS = ("header", "pseudonym_pattern", "min_salt_bytes", "vectors")
VECTOR_KEYS = ("salt", "tenant_id", "subject_id", "expected")


def canonical_digest(document: dict, *, ensure_ascii: bool = True) -> str:
    """The payload digest, spelled exactly as the artifact specifies it.

    🔴 `ensure_ascii` and the UTF-8 encoding are passed explicitly rather than
    left to their defaults. Both defaults happen to be right today, so an
    explicit value looks like noise - which is the point: the day a vector
    carries a non-ASCII `subject_id`, an implementation that silently inherited
    a different default reports a fork while the data is identical. That false
    alarm is the exact failure this anchor exists to prevent, so the setting
    that governs it is not left implicit.

    ``ensure_ascii`` is a parameter only so a test can demonstrate that it is
    load-bearing. Nothing in production passes anything but the default.
    """
    load_bearing = {
        "header": document["header"],
        "pseudonym_pattern": document["pseudonym_pattern"],
        "min_salt_bytes": document["min_salt_bytes"],
        "vectors": [
            {key: vector[key] for key in VECTOR_KEYS}
            for vector in document["vectors"]
        ],
    }
    return hashlib.sha256(
        json.dumps(
            load_bearing,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=ensure_ascii,
        ).encode("utf-8")
    ).hexdigest()


def load_control_plane():
    """Import control_plane/core.py for its identity regex, then put the world back.

    Import-time configuration and a leaked Kubernetes client are how an
    unrelated contract test once started failing only in a full-suite run. The
    volume role constructs no Kubernetes client at all.
    """
    import os

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT))
    snapshot = dict(os.environ)
    os.environ.update({
        "SANDBOX_CONTROL_PLANE_ROLE": "volume",
        "SANDBOX_CONTROL_PLANE_TOKEN": "test-token",
        "SIGNING_KEY": "0" * 32,
        "WORKSPACE_ID_KEY": "1" * 32,
        "VOLUME_AGENT_URL": "http://127.0.0.1:1",
        "VOLUME_AGENT_TOKEN": "test-volume-token",
        "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
        "OBJECT_STORE_ACCESS_KEY": "test-access",
        "OBJECT_STORE_SECRET_KEY": "test-secret",
    })
    try:
        spec = importlib.util.spec_from_file_location(
            "control_plane._test_vectors_core", ROOT / "control_plane/core.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


control_plane = load_control_plane()
capability_ticket = load_module("sandbox_ticket_vectors", "capability_ticket.py")
store = load_module("sandbox_store_vectors", "control_plane/store.py")


class VectorFileTests(unittest.TestCase):
    """The file itself, before anything is concluded from it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(VECTORS.read_text(encoding="utf-8"))

    def test_the_file_carries_enough_vectors_to_be_worth_running(self) -> None:
        # 🔴 Without this, emptying the file turns every loop below into zero
        # iterations and the whole suite reports success.
        self.assertGreaterEqual(len(self.document["vectors"]), 5)

    def test_every_vector_is_complete(self) -> None:
        for index, vector in enumerate(self.document["vectors"]):
            with self.subTest(vector=index):
                for field in ("salt", "tenant_id", "subject_id", "expected"):
                    self.assertIn(field, vector)
                    self.assertTrue(vector[field], field)
                self.assertGreaterEqual(
                    len(vector["salt"].encode("utf-8")),
                    self.document["min_salt_bytes"],
                    "a vector salt is weaker than the minimum it documents",
                )

    def test_the_expected_values_are_distinct(self) -> None:
        expected = [vector["expected"] for vector in self.document["vectors"]]
        self.assertEqual(len(set(expected)), len(expected))

    def test_the_data_matches_the_recorded_cross_repository_digest(self) -> None:
        # Drift in the shared data - one party editing its copy - shows up here.
        self.assertEqual(canonical_digest(self.document), CANONICAL_DIGEST)

    def test_the_artifact_agrees_with_its_own_recorded_digest(self) -> None:
        """The copy must be internally consistent before it is trusted.

        Catches the half-edit in either direction: vectors changed without
        updating the recorded digest, or the digest updated without the
        vectors. Either one means the copy in hand is not the copy some other
        service believes it is sharing.
        """
        recorded = self.document.get("_payload_sha256")
        self.assertIsNotNone(
            recorded, "the artifact no longer records its payload digest"
        )
        self.assertEqual(canonical_digest(self.document), recorded)

    def test_the_declared_fields_are_present(self) -> None:
        for field in ("header", "pseudonym_pattern", "min_salt_bytes"):
            self.assertIn(field, self.document)
        self.assertEqual(self.document["min_salt_bytes"], 32)


class CanonicalizationRuleTests(unittest.TestCase):
    """This implementation against the rules the artifact writes down.

    The artifact carries a `_payload_canonicalization` section because the rule
    "anchor on the payload digest" is unusable without it - recomputing the
    digest by guesswork reports a fork that is not there. These tests check that
    the implementation here follows that section rather than a private guess
    that currently happens to agree.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(VECTORS.read_text(encoding="utf-8"))

    def rules(self) -> str:
        return " ".join(self.document.get("_payload_canonicalization", []))

    def test_the_artifact_states_how_to_canonicalize(self) -> None:
        # Without the rules, every party reimplements them by inference and the
        # anchor starts reporting forks that are not there.
        self.assertTrue(self.rules(), "the artifact no longer states the rules")

    def test_the_payload_keys_match_the_stated_ones(self) -> None:
        for key in PAYLOAD_KEYS:
            self.assertIn(key, self.rules())
        for key in VECTOR_KEYS:
            self.assertIn(key, self.rules())
        # `_why` and anything else must be excluded, or two copies that differ
        # only in commentary would digest differently.
        self.assertIn("_why", self.rules())
        digest_with_commentary = hashlib.sha256(
            json.dumps(self.document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertNotEqual(digest_with_commentary, self.document["_payload_sha256"])

    def test_dropping_a_vector_annotation_does_not_move_the_digest(self) -> None:
        stripped = json.loads(json.dumps(self.document))
        for vector in stripped["vectors"]:
            vector.pop("_why", None)
        self.assertEqual(
            canonical_digest(stripped), canonical_digest(self.document)
        )

    def test_ensure_ascii_is_load_bearing_and_pinned(self) -> None:
        """🔴 The setting that was invisible until the data made it visible.

        While every field was ASCII, both values of the flag produced the same
        digest and an implementation that inherited the wrong one looked
        correct. A vector carrying a non-ASCII subject id now makes the two
        disagree on the **real** payload, so this is covered by the shared data
        rather than by a synthetic case each party would have to write, get
        right, and maintain.

        The coverage is asserted, not assumed: remove that vector and the first
        assertion says the protection is gone, instead of the test quietly
        reverting to proving nothing.
        """
        self.assertIn("ensure_ascii", self.rules())
        self.assertTrue(
            [
                vector for vector in self.document["vectors"]
                if not vector["subject_id"].isascii()
            ],
            "no vector carries a non-ASCII subject id any more, so this flag is "
            "untested against the real payload again",
        )
        self.assertNotEqual(
            canonical_digest(self.document, ensure_ascii=True),
            canonical_digest(self.document, ensure_ascii=False),
            "the flag stopped mattering; the rule may no longer be needed",
        )
        # Checked on the call node itself, not by searching the file for a
        # string: a guard that looks for its own literal is defeated by any
        # edit that rewrites both at once, and reports success while the thing
        # it guards is gone. (Found exactly that way - a mutation that replaced
        # the encoding call everywhere also rewrote the assertion looking for
        # it, and the test stayed green.)
        dumps = [
            node for node in ast.walk(ast.parse(inspect.getsource(canonical_digest)))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dumps"
        ]
        self.assertEqual(len(dumps), 1)
        self.assertIn(
            "ensure_ascii",
            {keyword.arg for keyword in dumps[0].keywords},
            "the flag is inherited from a default again",
        )
        # The UTF-8 encoding is deliberately *not* asserted: str.encode()
        # defaults to UTF-8 by language definition, so unlike ensure_ascii there
        # is no value it could drift to. Writing it out is documentation, and a
        # test that cannot fail for a real reason is worse than no test.


class HeaderNameTests(unittest.TestCase):
    """The name this platform reads must be the name the sender writes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(VECTORS.read_text(encoding="utf-8"))
        cls.source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")

    def test_every_header_this_platform_reads_is_the_agreed_one(self) -> None:
        """🔴 A divergent header name fails in the worst possible way.

        The sender sets a header nobody reads, so the request arrives looking
        exactly like one from a caller that is not acting for anyone - the work
        is filed under the credential itself and answered `200`. Not generic
        "unhandled header" reasoning: it is this project's recurring shape,
        where not working and working are indistinguishable at the boundary.
        """
        tree = ast.parse(self.source)
        read = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and "acting" in node.args[0].value.lower()
        }
        self.assertEqual(read, {self.document["header"]})

    def test_the_contract_document_publishes_the_same_name(self) -> None:
        published = (ROOT / "docs/AUTH.md").read_text(encoding="utf-8")
        self.assertIn(self.document["header"], published)


class PublishedFormulaTests(unittest.TestCase):
    """What the contract tells client authors must match the shared artifact.

    The formula is published in two places - the vector file and the document a
    third party actually reads - and the document is the one they will copy
    from. If the two drift, every implementation built from the document is
    wrong in a way that reproduces perfectly against itself.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(VECTORS.read_text(encoding="utf-8"))
        cls.published = (ROOT / "docs/AUTH.md").read_text(encoding="utf-8")

    def test_the_document_publishes_the_vector_formula_verbatim(self) -> None:
        self.assertIn(self.document["_formula"], self.published)

    def test_the_document_states_the_minimum_salt(self) -> None:
        self.assertIn(
            f"at least {self.document['min_salt_bytes']} bytes", self.published
        )

    def test_the_document_names_the_separator_and_the_truncation(self) -> None:
        # The two mistakes that produce a plausible-looking wrong value.
        self.assertIn("separator is NUL", self.published)
        self.assertIn("bytes of the digest", self.published)

    def test_the_document_points_at_the_vector_file(self) -> None:
        self.assertIn("acting-subject-vectors.json", self.published)
        self.assertTrue(VECTORS.exists())


class PatternRelationshipTests(unittest.TestCase):
    """How this platform's identity class relates to the agreed one.

    Stated explicitly rather than assumed: the two are **exactly equal** here,
    not merely overlapping. A superset would also be acceptable on the receiving
    side, so the relationship is asserted rather than left to be inferred from a
    passing test.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(VECTORS.read_text(encoding="utf-8"))
        cls.agreed = re.compile(cls.document["pseudonym_pattern"])

    def probes(self) -> list[str]:
        return [
            "0" * 32, "f" * 32, "0123456789abcdef" * 2,
            "0" * 31, "0" * 33, "", "A" * 32, "g" * 32,
            "0" * 16 + "-" + "0" * 15, "0" * 16 + ":" + "0" * 15,
            " " + "0" * 32, "0" * 32 + " ", "0" * 32 + "\n",
        ]

    def test_this_platform_accepts_exactly_the_agreed_language(self) -> None:
        for probe in self.probes():
            with self.subTest(probe=repr(probe)):
                self.assertEqual(
                    bool(control_plane.ACTING_SUBJECT_RE.fullmatch(probe)),
                    bool(self.agreed.fullmatch(probe)),
                )

    def test_the_agreed_pattern_is_anchored_at_both_ends(self) -> None:
        self.assertTrue(self.document["pseudonym_pattern"].startswith("^"))
        self.assertTrue(self.document["pseudonym_pattern"].endswith("$"))

    def test_this_platform_anchors_its_own_check(self) -> None:
        """The regex is unanchored, so how it is *called* carries the anchor.

        `re.match` would accept a 40-character value whose first 32 characters
        happen to be hex, and `re.search` would accept one anywhere inside a
        longer string. Only `fullmatch` rejects both.
        """
        source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
        uses = re.findall(r"ACTING_SUBJECT_RE\.(\w+)\(", source)
        self.assertEqual(uses, ["fullmatch"])
        self.assertFalse(control_plane.ACTING_SUBJECT_RE.fullmatch("0" * 32 + "zz"))


class ReceivedPseudonymTests(unittest.TestCase):
    """🔴 The load-bearing case: every expected value must be accepted here.

    The claim being made executable is that lowercase hex is a subset of every
    identity character class on this side of the boundary, so no party has to
    negotiate one. Until now that was a sentence in a comment. Tighten any of
    these classes - forbid a leading digit, require a prefix, shorten a bound -
    and this goes red immediately, instead of a production pseudonym that
    happens to start with a digit being refused later.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(VECTORS.read_text(encoding="utf-8"))
        cls.expected = [vector["expected"] for vector in cls.document["vectors"]]

    def test_the_acting_subject_check_accepts_every_vector(self) -> None:
        for value in self.expected:
            with self.subTest(pseudonym=value):
                self.assertIsNotNone(control_plane.ACTING_SUBJECT_RE.fullmatch(value))

    def test_the_capability_ticket_subject_class_accepts_every_vector(self) -> None:
        for value in self.expected:
            with self.subTest(pseudonym=value):
                self.assertIsNotNone(
                    capability_ticket.SUBJECT_PATTERN.fullmatch(value)
                )

    def test_the_stored_principal_class_accepts_every_vector(self) -> None:
        # A pseudonym becomes the principal of a workspace ownership row.
        for value in self.expected:
            with self.subTest(pseudonym=value):
                self.assertIsNotNone(store.FREEFORM.fullmatch(value))

    def test_a_pseudonym_survives_workspace_derivation(self) -> None:
        # It also goes through Workspace id derivation as the principal id;
        # a value that broke that would fail far from here.
        derived = {
            control_plane.workspace_id_for_session(
                "session-1", tenant_id="acme",
                principal_kind="subject", principal_id=value,
            )
            for value in self.expected
        }
        self.assertEqual(len(derived), len(self.expected))
        for workspace_id in derived:
            self.assertRegex(workspace_id, r"^ws-[a-f0-9]{12}$")

    def test_the_hex_truncation_mistake_is_refused(self) -> None:
        """The one way the formula is easy to get wrong, seen from this side.

        Truncating the hex string instead of the digest yields 16 characters,
        not 32. This platform must reject it rather than accept a half-length
        pseudonym, because two subjects sharing a 16-character prefix would then
        share a workspace.
        """
        for value in self.expected:
            with self.subTest(pseudonym=value):
                self.assertIsNone(control_plane.ACTING_SUBJECT_RE.fullmatch(value[:16]))


if __name__ == "__main__":
    unittest.main()
