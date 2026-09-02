"""Capability tickets for the internal Control Plane -> sandbox call path.

Control Plane is the only issuer; Runtime and File Service are the only verifiers.
The module is copied into all three images (see the Dockerfile COPY lines next
to workspace_contract.py) so that the issuing side and the verifying side run
**the same** character-set assertion. Two hand-written copies of that rule is
how a subject containing the ``:`` separator gets accepted on one side and
reinterpreted on the other, making one ticket valid across two kinds.

Shape of the credential:

    instance_key(K, kind, subject, epoch) = HMAC-SHA256(K, "kind:subject:epoch")

``instance_key`` is what a sandbox holds. It is derived from the Control Plane signing
key but never equals it, so a key read out of one Pod cannot mint a ticket for
any other sandbox. ``epoch`` lives in the control-plane row for that sandbox or
workspace: a new instance means a new epoch, and revoking means epoch + 1 -
after which Control Plane derives a different instance key and every ticket already
issued stops matching a freshly provisioned sandbox.

Tickets themselves are short lived and carry their own expiry, so the previous
property of the scheme they replace - a purely deterministic derivation that
never expired and could not be revoked - is gone on both axes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time


#: Default ticket lifetime. Every internal call is a single request/response
#: exchange, so this only has to cover one hop plus clock skew.
TICKET_TTL_SECONDS = 300
CLOCK_SKEW_SECONDS = 5

#: 🔴 The one authority for what may appear in a ticket subject. ``:`` is the
#: field separator of the derivation string, so it must never be a legal
#: subject character - otherwise ``kind=workspace, subject="x:runtime:sb-1"``
#: derives the same key as ``kind=workspace:x, subject="runtime:sb-1"``.
#: Both the issuer and the verifier import this; do not restate it anywhere.
SUBJECT_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")
KIND_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")


class TicketError(ValueError):
    """The kind, subject, epoch or ticket is not usable."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def validated(kind: str, subject: str, epoch: int) -> tuple[str, str, int]:
    """Assert the three fields of a derivation string, or raise.

    Called by derivation, issuance and verification alike. A caller that skips
    it does not get a weaker check, it gets no check.
    """
    if not isinstance(kind, str) or not KIND_PATTERN.fullmatch(kind):
        raise TicketError("capability kind is invalid")
    if not isinstance(subject, str) or not SUBJECT_PATTERN.fullmatch(subject):
        raise TicketError("capability subject is invalid")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise TicketError("capability epoch is invalid")
    return kind, subject, epoch


def instance_key(signing_key: bytes, kind: str, subject: str, epoch: int) -> str:
    """The verification key handed to one sandbox instance."""
    kind, subject, epoch = validated(kind, subject, epoch)
    return hmac.new(
        signing_key,
        f"{kind}:{subject}:{epoch}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def issue(
    key: str,
    kind: str,
    subject: str,
    epoch: int,
    *,
    ttl: int = TICKET_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint a ticket the holder of ``key`` will accept until it expires."""
    kind, subject, epoch = validated(kind, subject, epoch)
    if ttl < 1:
        raise TicketError("capability ticket ttl is invalid")
    issued_at = int(time.time() if now is None else now)
    payload = _b64(json.dumps(
        {"k": kind, "s": subject, "e": epoch, "exp": issued_at + ttl},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8"))
    signature = _b64(hmac.new(
        key.encode("ascii"), payload.encode("ascii"), hashlib.sha256
    ).digest())
    return f"{payload}.{signature}"


def verify(
    key: str,
    ticket: str,
    kind: str,
    subject: str,
    epoch: int,
    *,
    now: int | None = None,
) -> bool:
    """Whether ``ticket`` authorizes this exact (kind, subject, epoch) now.

    Returns a verdict rather than raising: a verifier answers 401 for every
    reason a ticket does not hold, and telling the caller which of the reasons
    applied is free help for whoever is probing.
    """
    try:
        kind, subject, epoch = validated(kind, subject, epoch)
    except TicketError:
        return False
    if not isinstance(ticket, str) or not ticket.isascii():
        # http.client decodes headers as iso-8859-1, and hmac.compare_digest
        # raises TypeError on non-ASCII str input rather than returning False.
        return False
    payload, _, signature = ticket.partition(".")
    if not payload or not signature:
        return False
    expected = _b64(hmac.new(
        key.encode("ascii"), payload.encode("ascii"), hashlib.sha256
    ).digest())
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(claims, dict):
        return False
    current = int(time.time() if now is None else now)
    try:
        expires_at = int(claims.get("exp"))
    except (TypeError, ValueError):
        return False
    return (
        claims.get("k") == kind
        and claims.get("s") == subject
        and claims.get("e") == epoch
        and expires_at >= current - CLOCK_SKEW_SECONDS
    )
