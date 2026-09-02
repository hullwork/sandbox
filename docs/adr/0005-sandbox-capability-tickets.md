# ADR 0005: Sandboxes verify tickets with a per-instance key, not by asking the control plane

- Status: accepted

## Context

Control Plane must call into a running sandbox: the file API and the MCP endpoint both
live inside the Runtime Pod. Something has to authenticate those calls.

The credential this replaces was a deterministic derivation, `HMAC(SIGNING_KEY,
"kind:subject")`, written into the Pod's environment and compared for equality on
arrival. It had three defects: it never expired, it could not be revoked, and one
leaked signing key forged the credential for every sandbox that ever had been or
ever would be provisioned.

The obvious repair is to make the credential a signed token carrying an expiry
and have the sandbox verify it. That requires a verification key. The obvious
verification key is `SIGNING_KEY`, and it is exactly the one that must not be
used: Runtime Pods run in `sandbox-workloads`, the namespace of untrusted tenant
code, and `SIGNING_KEY` signs scoped tokens for **any** workspace. The volume
role is excluded from holding it for the same reason (see ADR 0004 and the
constraint comment above `SANDBOX_CONTROL_PLANE_ROLE` handling in `control_plane/core.py`).

The second obvious repair is to have the sandbox ask the control plane whether a
presented credential is currently valid. That is worse. It gives untrusted
workloads an authenticated path back into the control plane, turns every
internal call into two, and makes the control plane's availability a
precondition for a sandbox answering at all.

## Decision

Control Plane derives a **per-instance verification key** and writes only that into the
Pod:

```
instance_key = HMAC-SHA256(SIGNING_KEY, "<kind>:<subject>:<epoch>")
```

`epoch` is stored in that sandbox's or workspace's control-plane row. Control Plane then
mints a short-lived ticket for each call, signed with the instance key and
carrying its kind, subject, epoch and expiry. The sandbox verifies with the key
it already holds and checks those fields.

Issuing and verifying import the same module (`capability_ticket.py`, copied into
all three images beside `workspace_contract.py`) so that the character-set
assertion on a subject has exactly one definition. Two hand-written copies of
that rule is how a subject containing the `:` separator gets accepted on one side
and reinterpreted on the other, making one ticket valid under two kinds.

## Consequences

`SIGNING_KEY` never enters `sandbox-workloads`. A key read out of one Pod opens
that one sandbox instance and nothing else, and stops working when its epoch
moves.

Tickets expire, so a captured one has a bounded life. Provisioning a new Runtime
moves the epoch, which is what makes "restart rotates the credential" true.

Revocation is `epoch + 1`. It is **not instantaneous for tickets already
issued**: after the bump Control Plane can mint nothing the previous instance accepts,
and the previous key opens nothing on a new instance, but a ticket already handed
out remains valid at the still-running Pod until its own expiry. That window is
the ticket lifetime (300 seconds by default) and nothing longer. This is the
price of not putting a control-plane round trip in the verification path, and it
is a deliberate trade, not an oversight.

Control Plane must read the epoch row before it can talk to a sandbox, so a sandbox with
no live row cannot be reached at all. This is fail-closed and intentional: a
released sandbox's row stops answering, so no ticket can be minted for it.

Without a control-plane store there are no rows to rotate and the epoch is fixed;
tickets still expire, but rotation and revocation need the store.

## Verification

`tests/test_capability_tickets.py` covers cross-instance, cross-kind, expiry,
epoch-bump (in both directions), forged and non-ASCII tickets, and asserts that
the subject character set has a single definition that the Control Plane, Runtime and
File Service modules all import rather than restate.
`tests/test_capability_epochs.py` covers where epochs live and what moves them.
Manifest review must confirm that no `SIGNING_KEY` reference reaches a workload
Pod specification.
