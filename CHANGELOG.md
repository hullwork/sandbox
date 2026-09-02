# Changelog

All notable release changes will be recorded here. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and will use Semantic
Versioning after its first public release.

## Unreleased

### Added

- Standalone local gVisor environment, Python SDK, CLI, stdio MCP, and operator Console
  surfaces for the first public release candidate.
- `make destroy-local`, a KUBECONFIG-aware Makefile, and resource checks in `make doctor`.
- `overlays/external-deps` as an opt-in example for an external MySQL/S3 control plane.
- Behavior tests for the store state machine, admission quota, reaper, Runtime manifest
  security fields, checkpoint restore validation, exec timeouts, and readiness.
- Route completeness and tenant-ownership tests that run a SQLite-backed Control Plane.
- `docs/TROUBLESHOOTING.md`, a terminology table, and MCP host configuration examples.
- MySQL control-plane backend (`SANDBOX_STORE_BACKEND=mysql`) with `PyMySQL` pinned in
  the Control Plane image; `docs/CONFIGURATION.md` documents the `SANDBOX_DB_*` settings.
- The wheel and sdist are published to PyPI as `sandbox-platform` from the release tag,
  through Trusted Publishing (OpenID Connect) rather than a stored API token, from a job
  that waits on the `pypi` environment approval and uploads the artifact this run
  already scanned, attested, and signed. Consumers can pin a version and hash instead of
  a `git+https://` requirement, which no lock file under `pip --require-hashes` accepts
  and which PyPI refuses in uploaded metadata. `docs/RELEASE.md` lists the one-time
  registration maintainers must do by hand.
- `scripts/check-wheel-surface.py` refuses a wheel whose top-level entries are not
  exactly `sandbox_platform`. Those names install into a shared `site-packages` and are
  claimed globally, so a flat module added to the packaging configuration would collide
  with any unrelated distribution of the same name. It runs in CI, in the release build,
  and once more on the bytes about to reach PyPI.
- `twine check --strict` runs on the wheel and sdist before a tag is spent, because PyPI
  rejects an unrenderable long description only after the version number is consumed.

### Changed

- Control Plane speaks S3 through `boto3` (Apache-2.0) instead of running the MinIO
  Client as a subprocess. `mc` is AGPL-3.0 and was the only strong-copyleft component
  in any image built here; the notices entry and the README's "known limitations"
  section both said so, and both are now records rather than warnings.
  Failure classification improves as a side effect: it read `mc`'s stderr and matched
  substrings, which is why a Ceph RGW 503 -- a real outage, and the expensive one to
  misjudge -- landed in "the request was rejected". botocore reports the status code.
  `SANDBOX_MAX_CONCURRENT_MC` is now `SANDBOX_MAX_CONCURRENT_OBJECT_OPS`,
  `SANDBOX_MAX_MC_QUEUE` is `SANDBOX_MAX_OBJECT_QUEUE`, `OBJECT_STORE_CLIENT` and the
  two Go runtime knobs are gone, and the `sandbox_mc_queue_*` metrics are
  `sandbox_object_store_queue_*` (dashboard and alert rule updated with them).

- The standalone Rook v1.20 RGW bootstrap now installs the Ceph CSI operator,
  which supplies the `CephConnection` CRD required by current Rook cluster
  reconciliation even when Sandbox does not provision Ceph block volumes. Its
  deterministic local loop OSD is selected by exact `/dev/loop0` path because
  current Rook excludes loop devices from `deviceFilter` matches. RGW readiness
  now waits on `status.phase=Ready`, which Rook v1.20 actually publishes.
- The consumer surface (`sandbox_client`, `mcp`, `sandbox_cli`, `sandboxctl`,
  `control_plane_transport`, `safe_stdout`) moved into the `sandbox_platform` package.
  Import paths changed; console-script names did not.
- `sandbox-mcp` validates `SANDBOX_CONTROL_PLANE_URL`, `SANDBOX_TOKEN`, and `SANDBOX_SESSION_ID`
  at startup and supports `--help` / `--version`.
- The OpenAPI document now matches Control Plane responses (`access_token_expires_in`,
  `workspace_id`, `template`, files routes) and closes fixed-shape schemas.
- Control Plane uses `strategy: Recreate`; database statements, idle transactions, and the store
  lock are bounded; the HTTP handler applies a socket timeout.

### Removed

- `control_plane/control_sso.py` and `POST /v1/auth/control-sso`. Accepting a federated
  assertion made another service the identity provider for this one, and used the
  bearer token that service holds as the signing key for it - a bearer token must
  be sent to the other party (RFC 6750) while a signing key must not be shared,
  and one value cannot satisfy both. Browser identity now comes from an OIDC
  provider, and the other services are ordinary API-key tenants here.
  `CONTROL_SSO_ISSUER` and `SANDBOX_CONTROL_PLANE_LEGACY_TOKEN_ENABLED` are gone; the second is
  replaced by `SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED`.

### Fixed

- Findings of the 2026-09-02 pre-release review, in four groups. Control plane:
  request handling, admission and readiness defects found by reading the API
  and store paths. Lifecycle: Runtime, workspace and checkpoint state transitions
  that could strand a row or a Pod. Supply chain: image, lockfile and release
  workflow gaps. Documentation and compliance: the object-store hop is described
  as "not traced yet" rather than "cannot be traced" (`mc` is gone), the
  configuration reference lists every environment variable the code reads
  across all four roles, `THIRD_PARTY_NOTICES.md` names the LGPL database
  driver, `docs/RELEASE.md` carries the before-public checklist, and the
  comments guarding the `/healthz`-versus-readiness decision and the shutdown
  budget are readable English again. Each group is pinned by tests that fail on
  the state the review found.
- `WORKSPACE_ID_KEY` is required and no longer falls back to `SIGNING_KEY`.
- `GET /v1/admin/audit` returned no response because of a keyword-only argument.
- Re-registering a soft-deleted Workspace no longer fails on the retained row.

- The Control Plane image was built without the MySQL driver, so a `mysql` deployment passed
  `/readyz` while every tenant request failed with `503`. The driver is now installed,
  imported during the image build, and a missing driver stops Control Plane at startup with
  the package name instead of being logged as a transient store outage.

### Security

- Control Plane signs browser identity in through the deployment's own OpenID Connect
  provider (`control_plane/oidc.py`, Authorization Code + PKCE, standard library only).
  `SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE` has no default: every service sharing one provider must
  pin a different audience, or a token minted for one is spendable at the others.
- The static `SANDBOX_CONTROL_PLANE_TOKEN` is a break-glass credential. It is off by default
  wherever a provider is configured, is not read at all when off (so the API
  refuses it rather than only the login form hiding it), signs nothing any more,
  logs every use with its source address, and is documented in the README.
  Disabling every sign-in method makes Control Plane refuse to start.
- Internal Control Plane-to-sandbox credentials became capability tickets
  (`capability_ticket.py`): a per-instance key derived from `SIGNING_KEY` and an
  epoch stored in the sandbox or workspace row, plus a short-lived signed ticket.
  They now expire, a new Runtime rotates them, deleting a sandbox revokes them,
  and a leaked signing key alone no longer forges credentials for every sandbox
  that ever existed. Issuer and verifier share one subject character-set
  assertion, so the `kind:subject` separator cannot be injected.
- API keys carry `permissions` and `expires_at`. Acting for another identity is
  a permission on the key (`act_as_subjects`) and the caller names a pseudonym
  in `X-Acting-Subject` (32 lowercase hex); a key without the permission that
  sends the header is **refused with 403**, never silently ignored. The tenant of
  a request always comes from the credential: a tenant-bound credential sending
  `X-Sandbox-Tenant` is refused with 403 too, including when the value names its
  own tenant - accepting the matching case teaches a caller that the header
  decides, and it finds out otherwise only on the request where the two differ.
- Object ownership is derived, not declared. Every object key is
  `users/<tenant>/<subject>/...`; the tenant segment now comes from the calling
  credential and the subject segment from `X-Acting-Subject`, and a tenant-bound
  credential that sends `owner` is **refused with 403** whatever value it named,
  its own partition included. This is what lets the object routes accept a
  tenant credential at all - they were reachable by the management plane only,
  precisely because an owner the caller spelled out was an owner any caller
  could spell out. The two halves ship together: opening the routes without
  deriving the owner would be a cross-tenant write for every tenant, and
  deriving it without opening them would leave the derivation unreachable.
  Deactivating a tenant now also stops the object tickets it already handed out.
  The bundled client stops composing an owner of its own: `object_owner()` is
  removed, and an upload names an owner only when the caller passes one, which
  only a management-plane credential has reason to do. The subject it acts for
  moved from an attribute on the client to `acting_subject_context(...)`, a
  context variable: the client is a process-wide singleton, so an identity held
  on it belonged to every request in flight in every thread at once, and a
  request sent under the wrong subject is indistinguishable from one sent under
  the right one. An object call with no bound subject and no explicit owner now
  raises in the client, naming the operation, rather than travelling to the
  platform and coming back as a `400` with the call site no longer on the stack.
- Nothing this platform issues crosses into a sandbox: cookies whose names carry
  a platform prefix (including `__Host-`/`__Secure-` variants) and this
  platform's identity headers are stripped before forwarding, while the caller's
  own cookies pass through byte for byte.

- Upgrading a control-plane database in place is supported: `ensure_schema` adds
  columns an earlier database lacks, only when they are absent, on all three
  backends. `ADD COLUMN IF NOT EXISTS` is deliberately not used - only PostgreSQL
  accepts it, and the one spelling that reads as portable would fail during an
  upgrade on the other two.

- The acting-subject derivation is published with a fixed vector
  (`docs/acting-subject-vectors.json`), vendored so the repository depends on no
  other checkout. It is checked from the receiving side only - header name,
  identity character class, and every expected value through the validation path
  - because this platform never derives a pseudonym, and a reproduction test
  here would only check a copy of the formula written for the test.

- Requests are traced with the W3C `traceparent` header. A well-formed inbound
  header is adopted, `X-Request-Id` is otherwise derived from deterministically
  so that services given only the older header still land on one trace id, and
  failing both a fresh id is generated. Onward calls carry the same trace id
  with a new span id per hop, the access log carries `trace_id`, and every
  response echoes `X-Request-Id`. Trace flags are inherited unchanged from an
  inbound header - they carry the caller's sampling decision, and a hop that
  overwrote them would reverse it invisibly; Control Plane picks flags only for a trace
  it starts itself. A malformed header is treated as absent and never fails the
  request: an observability aid must not decide availability. Object-storage
  traffic leaves through `boto3`, which does not yet propagate `traceparent`,
  so that hop is not traced yet and the README says so.

- Documented gVisor, credential, workspace, object-store, and fail-closed boundaries.

The repository has not published a stable release. Release notes for `v0.1.0` will
replace this summary with the reviewed artifact, migration, compatibility, and E2E
evidence when the first signed tag is created.
