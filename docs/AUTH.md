# Authentication contract

This is the published contract between Sandbox Platform and **any** client that
calls it. It is written for someone integrating from outside this repository and
outside this organization: read it together with the
[HTTP and SDK contract](API.md) for the route table and
[`../contracts/control-plane-openapi.yaml`](../contracts/control-plane-openapi.yaml) for exact
schemas.

Sandbox Platform is a standalone product. It has no privileged peer, no partner
service whose word it takes for an identity, and no client that gets a shortcut
through the rules below. Everything here applies to the first integrator and the
hundredth equally.

Two consequences worth stating before the details, because they are what
integrators most often assume otherwise:

* **A tenant is decided by the credential, never by the request.** There is no
  header, body field, or query parameter that lets a client choose which tenant
  its call belongs to. If you hold a tenant key, you are that tenant, and
  sending `X-Sandbox-Tenant` anyway is a `403` - including when the value you
  sent is your own tenant.
* **A refused request is refused, not downgraded.** Nothing in this contract
  silently ignores something it will not honour. A capability you were not
  granted produces an error, not a quieter version of the operation.

## 1. Sign-in methods

Three, for three different kinds of caller. A deployment decides which exist;
`GET /v1/auth/methods` reports the answer without needing a credential:

```json
{"local_login": false, "oidc": true}
```

### OpenID Connect - for people, in a browser

The operator console signs users in through the deployment's **own** identity
provider. Control Plane acts as a relying party: OpenID Connect Authorization Code with
PKCE (`S256`), RS256 ID tokens, verified against the provider's JWKS.

Control Plane never issues an assertion, and never accepts one from another service. If
you are building a client that needs "one login for several products", get that
from the identity provider's own session, not from a token minted by one product
and honoured by another.

* `GET /v1/auth/oidc/login` starts the flow and redirects to the provider.
* `GET /v1/auth/oidc/callback` completes it and sets an `HttpOnly` session cookie
  plus a readable CSRF cookie. Mutating requests must echo the CSRF value in
  `X-Console-CSRF`.
* `POST /v1/auth/logout` clears both.

The session is a browser mechanism. **Do not build a machine client on it** -
there is no way to obtain the cookie without a browser flow, and none is coming.

Two properties an operator configures and an integrator should know about:

* Control Plane pins an **audience** (`SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE`). It has no default. A
  deployment that puts several products behind one identity provider must give
  each a different audience, otherwise an ID token minted for one is spendable at
  the others.
* Group and claim mapping decides whether a user becomes an administrator or one
  tenant's user. An identity that maps to neither is refused with `403`. Nothing
  is created by signing in: a claim naming a tenant that does not exist, or one
  that is suspended, is a `403`, not a new tenant.

### API keys - for machines, and for people without single sign-on

This is the route for any programmatic client. Send the key as a bearer token:

```
Authorization: Bearer sk_<random>_<scope>_<random>
```

Keys are the only credential this platform issues to callers, and the only one
suitable for a service. See [section 2](#2-api-keys).

### `SANDBOX_CONTROL_PLANE_TOKEN` - break-glass, for operators only

A static administrator credential that exists so a deployment is not locked out
when its identity provider is unreachable. It is administrator-equivalent,
belongs to no tenant, cannot be revoked without a restart, and cannot be
attributed to a person.

**Do not integrate against it.** A client that authenticates with this value is
running as the deployment's administrator, and:

* every use is written to the Control Plane log with its source address;
* every use increments `sandbox_credential_uses_total{kind="break-glass"}`;
* the console shows a banner for the whole session;
* it is **off by default in any deployment that has configured an identity
  provider**, and when it is off the credential is not loaded into the process at
  all - the API refuses it, not merely the login form.

`GET /v1/whoami` reports `"kind": "break-glass"` when a request arrived this way.
If you see that in your own client's logs, you are on the wrong credential.

Switching this token off affects **only** this token. API keys keep
authenticating: they are the supported way for a service to call this platform,
and an operator turning off the emergency door does not cut off integrators.

## 2. API keys

### Shape

```
sk_<random>_<scope>_<random>
```

`<scope>` is the tenant id, or `admin` for a management-plane key. It is there
for operator attribution when a key turns up in a log or a secret store; **do not
parse it to decide anything**. The authoritative answer to "what is this
credential" is `GET /v1/whoami`, and the leading random segment - not the scope -
is what makes the first 12 characters unique for lookup.

🔴 The key is not reliably splittable, so this is stronger than a style
preference. The random segments are base64url, and that alphabet contains `_`:
roughly one key in six has an underscore inside a random segment, and
`key.split("_")[2]` then returns the wrong field. Treat the whole string as one
opaque value.

The plaintext exists once, in the response that issues it. The platform stores
only its SHA-256 and compares in constant time. There is no endpoint that returns
a key again; a lost key is replaced, not recovered.

### Obtaining one

An administrator issues them:

| Route | Issues |
| --- | --- |
| `POST /v1/admin/tenants/{tenant_id}/keys` | A key bound to that tenant |
| `POST /v1/admin/keys` | A management-plane key, bound to no tenant |

```json
{"label": "billing-exporter", "permissions": ["act_as_subjects"], "expires_in_seconds": 7776000}
```

`label` is required. Both other fields are optional and default to the narrow
answer: no permissions, and no expiry.

The response is the only sighting of the plaintext:

```json
{
  "id": "0123456789abcdef",
  "tenant_id": "acme",
  "label": "billing-exporter",
  "api_key": "sk_...",
  "permissions": ["act_as_subjects"],
  "expires_at": 1830000000,
  "note": "api_key is shown once and cannot be retrieved later"
}
```

### Lifecycle

| Field | Meaning |
| --- | --- |
| `expires_at` | Unix seconds, or `null` for a key that never expires. After it passes, the key answers `401` exactly like an unknown key |
| `permissions` | Closed vocabulary. Currently the single value `act_as_subjects` (see [section 3](#3-acting-for-a-subject)). An unrecognized value is **rejected at issuance** with `400` rather than stored |
| `revoked_at` | Set by `DELETE /v1/admin/keys/{key_id}`; effective on the next request, with no cached grace window |

`expires_in_seconds` accepts 1 second to 1 year. Ask for a lifetime: an
unexpiring key is a credential nobody will ever notice leaking.

`GET /v1/admin/keys` and `GET /v1/admin/tenants/{tenant_id}/keys` list the
metadata - id, prefix, label, permissions, `expires_at`, `last_used_at`,
`revoked_at` - and never the key itself. `last_used_at` means "used recently":
it is written at most once every five minutes, so it is accurate to that window
and not to the exact request. Allow for it when you use it to confirm that
traffic has moved off an old key.

### Rotation

Issue the new key, deploy it, confirm traffic has moved by watching
`last_used_at` on the old key, then revoke the old one. There is no built-in
overlap window because none is needed: two keys are simply two valid
credentials, which is what makes rotation safe.

## 3. Acting for a subject

A client that serves many end users needs each user's work kept apart -
separate workspaces, separate quotas, separate blast radius. This is how that is
expressed, and it is deliberately unlike "pass us the user id".

```
X-Acting-Subject: 3f2a91c47b0e5d8a6142cf03e9b7d5a0
```

### The value

Exactly **32 lowercase hexadecimal characters**. Nothing else is accepted.

It is an **opaque pseudonym that is stable within your tenant** - not an email,
not a username, not a database id. Derive it yourself, keeping the real identity
on your side. A keyed hash over your own tenant identifier and your own user
identifier is the intended construction:

```
subject = HMAC-SHA256(salt, tenant_id + "\u0000" + subject_id).digest()[:16].hex()
```

Three details in that one line, each of which has a wrong version that still
produces a plausible-looking value:

* **`.digest()[:16]`, then `.hex()`** - the first 16 **bytes of the digest**,
  rendered as 32 hex characters. Truncating the hex string instead gives 16
  characters, which this platform rejects outright. That rejection is the good
  outcome: a 16-character pseudonym would let two subjects sharing a prefix
  share a workspace.
* **The separator is NUL** (`\u0000`), not `:` or `-`. Without it,
  `("tenant-al", "ice")` and `("tenant-a", "lice")` concatenate identically and
  derive the same pseudonym - two different users, one workspace.
* **The salt is at least 32 bytes** and never leaves your deployment. A shorter
  one implements the formula without its strength.

A fixed set of inputs and expected outputs is published alongside this document
as [`acting-subject-vectors.json`](acting-subject-vectors.json), including the
two vectors that differ only in where the separator falls. Run your
implementation against them before you send anything: all three of the mistakes
above are visible in that file's expected values and in nothing else.

Three reasons this is the shape, since the constraint looks arbitrary until they
are stated:

1. Real user identities do not cross an organizational boundary. If this platform
   is operated by someone other than you, your users' names are not their
   business, and are very likely somebody's compliance problem.
2. Collision stops being probabilistic. Two of your users cannot land on one
   workspace by accident, because the derivation is over a composite key rather
   than a random draw.
3. Lowercase hex is simultaneously valid under every identifier rule on both
   sides of the boundary, so no client ever has to negotiate a character class
   with the platform.

### The permission

Naming a subject requires the `act_as_subjects` permission **on the key you are
using**. This is a property of your own identity, in the same sense as Kubernetes
impersonation: "may this credential act for someone else" is a fact about the
credential, not a setting on the platform.

🔴 A key without that permission sending the header is refused with **`403`**. It
is not ignored, and the request is not carried out as the credential itself.
Ignoring it would file the work under the wrong owner and answer `200`, leaving
you and the platform disagreeing about who owns the data with nothing reporting
it.

### What it does and does not decide

* **Does:** which workspace namespace within your tenant the request belongs to.
  The same `session_id` under two subjects gives two workspaces.
* **Does:** which object-storage partition your objects live under. Every object
  key begins `users/<tenant>/<subject>/`, where the tenant comes from your
  credential and the subject from this header. That partition is **persisted** -
  it is where the bytes are for as long as they exist, not a property of one
  request - which is why it is derived rather than accepted: see
  [object ownership](#object-ownership).
* **Does not:** which tenant the request belongs to. That is your credential,
  always, and there is no header that changes it. `X-Sandbox-Tenant` on a
  tenant-bound credential is refused with `403` rather than ignored, **even
  when it names that credential's own tenant**.

  Both halves of that are deliberate. Ignoring it would answer `200` while
  filing the work under a different owner than the caller recorded, with
  nothing reporting the disagreement. Accepting the matching case would be
  worse than either: a client sending the header out of habit would be served
  correctly for as long as the two values agreed, concluding that the header is
  what decides, and would learn otherwise only on the request where they
  differ - by which point it believes it wrote somewhere it did not.

  (The management plane is a different identity, not an exception. An admin
  credential carries no tenant of its own, so naming one is the only way it can
  act for a tenant at all.)

`principal` in a request body and `X-Acting-Subject` are two ways of saying the
same thing, so sending both is a `400`. Prefer the header; the body form exists
for callers that do not separate end users at all.

### Object ownership

Object routes take an `owner`, spelled `<tenant>/<subject>`. **You do not send
it.** The platform builds it from your credential and your `X-Acting-Subject`,
and a tenant-bound credential that sends one anyway is refused with `403` -
including when it names the exact partition it would have been given, for the
same reason `X-Sandbox-Tenant` is refused in that case.

Two consequences to design your client around:

* an object call needs a subject. Without `X-Acting-Subject` there is no second
  segment to build a partition from, and the request is a `400` rather than a
  write filed under some default. If you have data that belongs to no end user,
  derive a pseudonym for it the same way you derive the others;
* an object partition is not a place you can point at. Objects you wrote under
  one subject are reachable only by requests acting for that subject, and two
  tenants that happen to derive the same pseudonym still get different
  partitions, because the first segment is not something either of them chose.

The management plane is again a different identity rather than an exception: it
has no tenant of its own, so naming an owner is the only way it can act for one,
and it names owners exactly as before.

Every impersonating call is audited on the platform side as
`auth key=<key id> acting_as=<subject> route=<method> <path> outcome=<allow|deny>`.

## 4. Sandbox capability tickets

You will not handle these, and that is the point of documenting them: they
explain why a sandbox never accepts a credential you hold.

Communication from the control plane into a running sandbox uses a ticket that
is signed with a key derived **per sandbox instance**, carries its own expiry,
and names the exact kind and subject it is for. The invariants a client can rely
on:

* **A ticket is valid at one instance only.** A ticket for one sandbox presented
  to another is refused, even if the two somehow shared a key: the subject inside
  the ticket is checked as well as the signature.
* **A ticket expires** - minutes, not hours - so a captured one has a bounded
  life.
* **A ticket is revocable.** Each sandbox and workspace row carries an epoch.
  Provisioning a new Runtime moves it; releasing a sandbox moves it. Once it
  moves, the control plane can no longer mint anything the previous instance
  accepts, and the previous credential opens nothing on the new one. Tickets
  already issued run out their own expiry - that window is the ticket lifetime
  and nothing longer.
* **Nothing this platform issues to a browser reaches a sandbox.** Console
  session and CSRF cookies (including their `__Host-` and `__Secure-` forms) and
  the platform's identity headers are stripped before any request is forwarded
  into a workload. Your own cookies pass through byte for byte, unmodified.

Why a sandbox does not simply ask the control plane whether your credential is
good: that would give untrusted workloads an authenticated path back into the
control plane and make its availability a precondition for a sandbox answering
at all. The full reasoning, including why the signing key cannot be the
verification key, is [ADR 0005](adr/0005-sandbox-capability-tickets.md).

The credential you *do* handle for one workspace or runtime is the scoped access
token from `POST /v1/sandboxes/{sandbox_id}/token`, which is bound to that one
subject and expires in `access_token_expires_in` seconds (900 by default). Object
tickets are single-use and object-bound. Treat every one of them as short lived
and re-request rather than cache.

## 5. Errors

Distinguish these three questions, because the right client behavior differs for
each: *is my credential wrong* (fix the credential), *is my credential
insufficient* (ask an administrator), *is the platform unable to answer right now*
(retry).

| Status | Body `error` | Means | Do |
| --- | --- | --- | --- |
| `401` | `unauthorized` | No credential, or one this platform does not recognize: unknown, revoked, **expired**, or a static token this deployment has switched off | Fix the credential. Do not retry the same value |
| `401` | `invalid or expired scoped access token` | A workspace/runtime scoped token that has expired or is for another subject | Request a fresh scoped token; check clock skew |
| `401` | `invalid, expired, or already used object ticket` | Object tickets are single use | Request a new ticket |
| `403` | `this credential may not act for a subject` | `X-Acting-Subject` without the `act_as_subjects` permission | Have the key reissued with the permission. Retrying without the header changes the meaning of your request |
| `403` | `admin key required` / `admin operations require an admin key without X-Sandbox-Tenant` | A management-plane route reached with a tenant identity | Use a management-plane key. A management key acting for a tenant is not an administrator for this purpose |
| `403` | `this credential is bound to a tenant; X-Sandbox-Tenant is not accepted` | A tenant credential tried to select a tenant, whatever value it named | Drop the header. Your tenant is already decided by the key you are using |
| `403` | `this credential is bound to a tenant; owner is derived from the credential and X-Acting-Subject and is not accepted in a request` | A tenant credential sent an object `owner`, whatever value it named | Drop the field. The partition is already decided by your key and `X-Acting-Subject` |
| `400` | `object storage requires X-Acting-Subject` | A tenant credential asked for an object operation without naming a subject | Send the header. There is no second owner segment without it |
| `403` | `tenant is suspended: <id>` | The tenant exists and is deactivated | An operator decision; retrying will not clear it |
| `403` | `sign-in was refused` | The OIDC flow failed, or the identity mapped to no role or to a tenant that does not exist | The Control Plane log says which check refused. The response deliberately does not |
| `400` | `X-Acting-Subject must be 32 lowercase hex characters` | Malformed pseudonym | Fix the derivation; do not strip the header |
| `404` | `workspace not found` / `sandbox not found` | The id does not exist **or belongs to another tenant** | These are one answer on purpose: distinguishing them would confirm that another tenant's id exists. Check the id against your own tenant |
| `404` | `unknown tenant: <id>` | Management-plane route naming a tenant that does not exist | |
| `409` | `tenants require SANDBOX_STORE_BACKEND to be configured` | A single-tenant deployment was asked something multi-tenant | Configuration, not credentials |
| `429` | quota text | A tenant or global admission limit | Back off; this is the platform working, not failing |
| `503` | `control plane store unavailable: ...` | The control-plane database is unreachable | **Retry with backoff.** Deliberately not `401`: your credential may be perfectly good, and rotating it is the wrong reaction |

Every response carries an `X-Request-Id` header, which is the id Control Plane logged
for that request. Capture it alongside the error and whoever operates the
platform can find the request without you having to reconstruct it from
timestamps. If your own client already propagates W3C `traceparent`, Control Plane
adopts it and the two sides share one trace; see the
[HTTP and SDK contract](API.md#request-tracing).

The `401`/`503` split is the one worth wiring into your client explicitly. A
platform that answered `401` during a database outage would send every integrator
off rotating credentials that were never wrong.

`GET /v1/whoami` is the supported way to check what a credential currently is:

```json
{"kind": "tenant", "tenant_id": "acme", "key_id": "0123456789abcdef",
 "acting_subject": null, "capabilities": ["sandboxes:write", "workspaces:read"]}
```

Decide what your client may do from `capabilities`, never from `kind`. They are
computed for the request that asked, and a capability is present only when the
matching route will actually accept the call.

The clearest case: a management-plane key acting for a tenant still reports
`"kind": "admin"`, because `kind` describes the credential rather than the scope
of this request. What narrows is `capabilities` - the management-plane entries
are absent, and management-plane routes answer `403`. A client branching on
`kind` would render administration it cannot perform.

## 6. Compatibility promise

Versioning, artifacts and support windows are in the
[release policy](RELEASE.md); tested combinations are in
[compatibility](COMPATIBILITY.md). Specific to this contract:

**Stable.** These change only with a major version, announced in the
[changelog](../CHANGELOG.md):

* the three sign-in methods and their meanings;
* `Authorization: Bearer` for API keys, and the `sk_` prefix;
* the `X-Acting-Subject` name, its 32-lowercase-hex shape, its
  tenant-scoped-pseudonym meaning, and `403`-not-ignored on an unauthorized key;
* the rule that a tenant comes from the credential and cannot be selected by a
  request, and that attempting to select one is refused rather than ignored;
* the `users/<tenant>/<subject>/` shape of an object partition, that both
  segments are derived rather than accepted from a tenant-bound credential, and
  that sending `owner` on one is refused rather than ignored;
* the status codes in [section 5](#5-errors) for the situations described there;
* `GET /v1/whoami` and `GET /v1/auth/methods` and their field names.

**May change in a minor version.** Do not build on:

* the exact wording of any `error` string - branch on status plus route, and log
  the text for humans;
* the `permissions` vocabulary, which will grow. Treat an unknown value in a
  listing as a value you do not understand, not as an error;
* the `<scope>` segment inside a key, and the length of either random segment;
* capability names in `capabilities`, which track routes as routes are added;
* everything in [section 4](#4-sandbox-capability-tickets). Ticket format, epoch
  handling and lifetimes are internal, and are documented so that integrators
  understand what a sandbox will refuse - not so that anything can be built on
  them.

**No compatibility is promised** for a deployment mixing component versions from
different releases; see [compatibility](COMPATIBILITY.md).

A change to anything in the stable list requires a changelog entry in the same
pull request that makes it, and the route/authentication manifest in
[`../contracts/control-plane-openapi.yaml`](../contracts/control-plane-openapi.yaml) is checked
against the implementation in CI, so the published contract cannot drift away
from the running one without a test going red.
