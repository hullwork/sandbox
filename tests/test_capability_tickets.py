"""Capability tickets: expiry, per-instance binding, and revocation by epoch.

The scheme these replace was a plain HMAC over ``kind:subject``. It never
expired, could not be revoked, and one leaked signing key forged every ticket
that ever had been or would be issued. Each of those three properties gets a
test here, plus the separator-injection case that a single shared character-set
assertion exists to prevent.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import capability_ticket  # noqa: E402


SIGNING_KEY = b"signing-key-for-tests"
NOW = 1_800_000_000


def key(kind: str, subject: str, epoch: int) -> str:
    return capability_ticket.instance_key(SIGNING_KEY, kind, subject, epoch)


class TicketVerificationTests(unittest.TestCase):
    def test_a_fresh_ticket_is_accepted_by_its_own_instance(self) -> None:
        instance = key("runtime", "sb-000000000001", 1)
        ticket = capability_ticket.issue(
            instance, "runtime", "sb-000000000001", 1, now=NOW
        )
        self.assertTrue(capability_ticket.verify(
            instance, ticket, "runtime", "sb-000000000001", 1, now=NOW
        ))

    def test_a_ticket_for_sandbox_a_is_refused_by_sandbox_b(self) -> None:
        """Acceptance criterion 6."""
        ticket = capability_ticket.issue(
            key("runtime", "sb-00000000000a", 1),
            "runtime", "sb-00000000000a", 1, now=NOW,
        )
        self.assertFalse(capability_ticket.verify(
            key("runtime", "sb-00000000000b", 1),
            ticket, "runtime", "sb-00000000000b", 1, now=NOW,
        ))
        # Not even when B is somehow holding A's key: the subject in the ticket
        # is checked as well, so one shared key cannot open two sandboxes.
        self.assertFalse(capability_ticket.verify(
            key("runtime", "sb-00000000000a", 1),
            ticket, "runtime", "sb-00000000000b", 1, now=NOW,
        ))

    def test_an_expired_ticket_replayed_later_is_refused(self) -> None:
        """Acceptance criterion 7."""
        instance = key("workspace", "ws-00000000000a", 1)
        ticket = capability_ticket.issue(
            instance, "workspace", "ws-00000000000a", 1, ttl=60, now=NOW
        )
        self.assertTrue(capability_ticket.verify(
            instance, ticket, "workspace", "ws-00000000000a", 1, now=NOW + 59
        ))
        self.assertFalse(capability_ticket.verify(
            instance, ticket, "workspace", "ws-00000000000a", 1, now=NOW + 3600
        ))

    def test_an_epoch_bump_invalidates_every_ticket_issued_before_it(self) -> None:
        """Acceptance criterion 8."""
        ticket = capability_ticket.issue(
            key("runtime", "sb-00000000000a", 4),
            "runtime", "sb-00000000000a", 4, now=NOW,
        )
        self.assertFalse(capability_ticket.verify(
            key("runtime", "sb-00000000000a", 5),
            ticket, "runtime", "sb-00000000000a", 5, now=NOW,
        ))
        # And a ticket minted under the new epoch is refused by the instance
        # still holding the old key, so a revocation cuts both directions.
        renewed = capability_ticket.issue(
            key("runtime", "sb-00000000000a", 5),
            "runtime", "sb-00000000000a", 5, now=NOW,
        )
        self.assertFalse(capability_ticket.verify(
            key("runtime", "sb-00000000000a", 4),
            renewed, "runtime", "sb-00000000000a", 4, now=NOW,
        ))

    def test_a_workspace_ticket_does_not_open_the_runtime_credential(self) -> None:
        subject = "sb-00000000000a"
        ticket = capability_ticket.issue(
            key("workspace", subject, 1), "workspace", subject, 1, now=NOW
        )
        self.assertFalse(capability_ticket.verify(
            key("runtime", subject, 1), ticket, "runtime", subject, 1, now=NOW
        ))

    def test_a_forged_signature_is_refused(self) -> None:
        instance = key("runtime", "sb-00000000000a", 1)
        ticket = capability_ticket.issue(
            instance, "runtime", "sb-00000000000a", 1, now=NOW
        )
        payload, _, signature = ticket.partition(".")
        for tampered in (
            f"{payload}.{signature[:-1]}",
            f"{payload}.",
            payload,
            "",
            f"{payload}.{'A' * len(signature)}",
        ):
            with self.subTest(ticket=tampered[:24]):
                self.assertFalse(capability_ticket.verify(
                    instance, tampered, "runtime", "sb-00000000000a", 1, now=NOW
                ))

    def test_a_non_ascii_ticket_is_a_verdict_not_an_exception(self) -> None:
        # http.client decodes headers as iso-8859-1 and compare_digest raises
        # TypeError on non-ASCII str, which would kill the handler thread
        # instead of answering 401.
        instance = key("runtime", "sb-00000000000a", 1)
        self.assertFalse(capability_ticket.verify(
            instance, "caf\u00e9.caf\u00e9", "runtime", "sb-00000000000a", 1
        ))


class SubjectAssertionTests(unittest.TestCase):
    """One character-set rule, imported by issuer and verifier alike."""

    def test_the_separator_cannot_appear_in_a_subject(self) -> None:
        # Without this, kind="workspace" subject="x:runtime:sb-1" and
        # kind="workspace:x" subject="runtime:sb-1" derive the same key, and one
        # ticket is valid under two kinds.
        for kind, subject in (
            ("workspace", "x:runtime:sb-00000000000a"),
            ("workspace:x", "runtime"),
            ("runtime", "sb-1 sb-2"),
            ("runtime", ""),
            ("RUNTIME", "sb-00000000000a"),
        ):
            with self.subTest(kind=kind, subject=subject):
                with self.assertRaises(capability_ticket.TicketError):
                    capability_ticket.instance_key(SIGNING_KEY, kind, subject, 1)
                with self.assertRaises(capability_ticket.TicketError):
                    capability_ticket.issue("k" * 64, kind, subject, 1)
                self.assertFalse(
                    capability_ticket.verify("k" * 64, "a.b", kind, subject, 1)
                )

    def test_issuer_and_verifier_share_one_assertion(self) -> None:
        source = (ROOT / "capability_ticket.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("SUBJECT_PATTERN = "), 1)
        for module in (
            ROOT / "control_plane/core.py",
            ROOT / "runtime/runtime_server.py",
            ROOT / "file-service/file_service.py",
        ):
            text = module.read_text(encoding="utf-8")
            self.assertIn("capability_ticket", text, str(module))
            self.assertNotIn("SUBJECT_PATTERN =", text, str(module))

    def test_an_epoch_below_one_is_refused(self) -> None:
        for epoch in (0, -1, True, "1", None):
            with self.subTest(epoch=epoch):
                with self.assertRaises(capability_ticket.TicketError):
                    capability_ticket.instance_key(
                        SIGNING_KEY, "runtime", "sb-00000000000a", epoch
                    )


class DerivationTests(unittest.TestCase):
    def test_the_instance_key_is_not_the_signing_key(self) -> None:
        instance = key("runtime", "sb-00000000000a", 1)
        self.assertNotIn(SIGNING_KEY.decode(), instance)
        self.assertEqual(len(instance), 64)

    def test_every_field_changes_the_derived_key(self) -> None:
        baseline = key("runtime", "sb-00000000000a", 1)
        self.assertNotEqual(baseline, key("workspace", "sb-00000000000a", 1))
        self.assertNotEqual(baseline, key("runtime", "sb-00000000000b", 1))
        self.assertNotEqual(baseline, key("runtime", "sb-00000000000a", 2))
        self.assertNotEqual(
            baseline,
            capability_ticket.instance_key(
                b"another-signing-key", "runtime", "sb-00000000000a", 1
            ),
        )


if __name__ == "__main__":
    unittest.main()
