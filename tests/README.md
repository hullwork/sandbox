# Test suite map

Run everything with:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The suite only needs the standard library plus PyYAML. Modules that read
their environment on import (`core.py`, `file_service.py`,
`runtime_server.py`) are loaded either from their file path with the required
variables set, from an `ast`-extracted subset, or in a subprocess; no test
needs a cluster, a database server, or an object store.

## Source module -> test file -> invariants

### Control plane (`control_plane/`, with new code under `control_plane/`)

| Source | Test file | What it pins |
| --- | --- | --- |
| `control_plane/runtime_driver.py`, `control_plane/drivers/gvisor.py` | `test_runtime_driver.py` | Provider-neutral RuntimeSpec/RuntimeInstance/RuntimeUsage/Capabilities contract, translated provider errors, current gVisor identity, provider-owned manifest/endpoint/workspace lookup, no advertised suspend/resume, and Service-before-Pod deletion order. |
| `control_plane/store.py` | `test_store_behavior.py` | Runtime status changes are compare-and-swap: `pending -> active` once, `active` or `pending` -> `released` or `failed`, terminal rows never move again and are tenant scoped. `admit_runtime` refuses the `max_runtimes + 1`-th live runtime and leaves the live count unchanged; released/failed rows free the slot; quota is per tenant; the `limit` argument is honoured; untenanted rows do not count. `release_stale_pending_runtimes` fails only old pending rows. Workspace admission is idempotent (`reused`), reports `at_capacity` instead of raising, never moves a workspace between tenants, and soft delete frees the slot. Re-registering a soft-deleted workspace id is a known open defect (`expectedFailure`). |
| `control_plane/store.py` | `test_store_keys.py` | API keys of similar tenant ids get distinct prefixes and authenticate to the right tenant. |
| `control_plane/store.py` (`ensure_schema`) | `test_store_migrations.py` | A database physically built with the pre-rework schema gains every missing column, rows written before it keep authenticating and get the column defaults, and re-running changes nothing. PostgreSQL and MySQL statements are captured from a recording connection: each backend is asked which columns exist with its own schema scope, `ADD COLUMN IF NOT EXISTS` (PostgreSQL-only) is never sent, and MySQL never receives a `DEFAULT` on a `TEXT` column. |
| `control_plane/tracing.py`, `control_plane/api.py` | `test_tracing.py` | Trace shape rules (version, segment count, hex case, length, all-zero trace/span) and the order of preference, plus the wiring against a running Control Plane with a **real** downstream server: an inbound trace id is adopted and logged, the same id goes out with a fresh span id, `X-Request-Id` alone derives deterministically, nothing at all still yields a usable id, and the response echoes exactly what was logged. Trace flags are inherited from the caller (including `00`, the decision a blanket `01` would silently overturn) and chosen only for traces this service starts. Every malformed header is asserted to be **served normally** with a fresh id - a trace header may not decide whether a request succeeds. The published clauses that only a document can carry - casing is not guaranteed but receivers must match case-insensitively, and the object-storage hop that `boto3` does not yet trace - are pinned too. |
| `docs/acting-subject-vectors.json` | `test_acting_subject_vectors.py` | The vendored cross-repository vector, checked from the receiving side only: the header name this Control Plane reads is the agreed one and is the *only* acting-subject header it reads, its identity class accepts exactly the agreed language (asserted as equality, not merely overlap) and is applied with `fullmatch`, and every expected value passes each identity class a pseudonym crosses here - acting-subject, capability-ticket subject, stored principal, Workspace derivation. The derivation is deliberately **not** reimplemented: this platform never derives, so a reproduction test would only check a copy of the formula written for the test. |
| `docs/AUTH.md` | `test_auth_contract_doc.py` | The published contract matches the running platform: subject shape, permission vocabulary, key prefix, documented routes, key lifetime bounds and the activity window are all pinned to the implementation, every error string quoted in the document is one the Control Plane emits, the document is reachable from the docs index, and it names no particular client. |
| `control_plane/reaper.py` | `test_reaper_behavior.py` | Runtimes with `expires-at <= now` are deleted, fresh ones survive, missing/garbled annotations never delete; busy runtimes are reprieved until `hard-expires-at`; TTL reaping keeps running when the store raises; active rows without a Pod release their Service and quota slot; Pods with a tenant label but no live row are orphans; reconciliation uses a fresh Pod snapshot and is skipped when that list fails; idle workspaces without a runtime are removed and forgotten. |
| `control_plane/core.py` (`_idle_runtime_victims`, `_admit_new_runtime`) | `test_reaper_behavior.py` | Idle victims are the runtimes whose last touch is older than the cutoff; admission uses the Runtime Driver, evicts idle runtimes before admitting, rejects with 429 (`transient`) when nothing is idle, and admits without touching provider resources when there is room. |
| `control_plane/core.py` (object owner, paths, tickets, object-store queue gate) | `test_object_owner_partition.py` | The layer under the route that `test_object_owner_derivation.py` covers. `<tenant>/<subject>` accepts the forms the auth layer produces and refuses traversal, empty segments, NUL and over-length ones; dot-only segments are refused by an explicit check as well as by the character class, proven by widening the pattern. Object paths cannot escape their prefix and must start with an allowed root. Upload and agent keys carry the owner partition, the owner is validated before the scope, and no owner string can smuggle a path out of its prefix; `object_key_owner` reads it back and returns None for legacy or foreign keys. A workspace token with no owner claim is refused rather than falling back to the body, and a body naming another owner is refused. Tickets carry the partitioned key, cap and honour the TTL, do not verify for the other operation, and are refused when tampered, expired, malformed, or **validly signed over an unpartitioned key**. The object-store queue gate raises instead of waiting, releases its slot, and is a RuntimeError subclass. |
| `control_plane/volume.py` | `test_volume_local_files.py` | The volume role answers workspace file requests when no Runtime exists, so it sanitises paths with nothing in front of it: `..`, absolute paths, the reserved `.sandbox` directory, NUL bytes, a workspace id that is itself a traversal, and a symlink that only escapes after resolution are all refused, while a link staying inside is not. The root is addressable only with `allow_root`. Listings hide `.sandbox`, use the agreed type vocabulary and report truncation; reads return whole small files, page with `next_offset` only when something was cut, error on an offset past the end, hard-cut and declare an overlong line, and name UTF-8 rather than failing vaguely. `LOCAL_MAX_*` are pinned to `workspace_contract`, so the two implementations cannot answer the same read differently. |
| `control_plane/manifests.py` | `test_manifests_security.py` | Runtime Pod has the configured `runtimeClassName`, `automountServiceAccountToken: false`, `readOnlyRootFilesystem` and `capabilities.drop: [ALL]` on every container, uid/gid/fsGroup 65532 with `runAsNonRoot`, `RuntimeDefault` seccomp, no host namespaces, only PVC + emptyDir volumes, `nodeSelector` and toleration derived from one setting, pinned `restartPolicy`, tenant/sandbox/workspace/template labels, TTL annotations, template-registry images, scoped tokens, HTTP probes, storage-mode mounts; the Service selector matches exactly its Pod. |
| `control_plane/api.py` (`/readyz`, `/livez`, `/healthz`) | `test_readiness.py` | Before shutdown `/readyz` and `/livez` are 200; once `_SHUTTING_DOWN` is set `/readyz` and `/healthz` are 503 (sticky) while `/livez` stays 200. |
| `control_plane/api.py`, `control_plane/core.py`, `contracts/control-plane-openapi.yaml` | `test_openapi_contract.py` | Every documented route is in `ROUTE_AUTH` with matching security; every protected route answers 401 without credentials; operation ids, parameters and refs are consistent. |
| `control_plane/api.py`, `control_plane/core.py`, `control_plane/kube.py`, `k8s/rbac.yaml` | `test_monitoring.py` | Quantity parsing, runtime/node monitoring views, tenant scope never lists nodes, monitoring RBAC is read-only, local metrics-server pinning. |
| `control_plane/oidc.py` | `test_oidc_rp.py` | A fixed RSA/JWKS/ID-token vector is accepted; a token minted for a neighbouring service's audience is refused, as are a wrong issuer, a replayed nonce, an expired token, a tampered signature, `alg: none`/`HS256`, a mismatched callback state and a state cookie signed with another key. `SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE` has no default and its absence refuses to start. |
| `control_plane/core.py`, `control_plane/api.py` | `test_login_paths.py` | Which credentials sign in, asked of the HTTP API rather than the Console: the static token works without a provider, is off by default once one is configured (and 401 there even though `SANDBOX_CONTROL_PLANE_TOKEN` is set in the environment), can be switched back on, and no credential is always 401. Disabling every method, or configuring a provider without an audience, refuses to start. |
| `control_plane/api.py`, `control_plane/store.py` | `test_acting_subject.py` | A key without `act_as_subjects` naming a subject is refused with 403 and creates nothing; an authorized key gets a distinct workspace per subject; malformed subjects and a subject combined with a body principal are refused; the break-glass token may not act for anyone; a tenant-bound credential naming any tenant - its own included - is refused with 403 and writes nothing, while the management plane may still act for a tenant; expired keys stop authenticating and unknown permissions are rejected at issuance. |
| `capability_ticket.py` | `test_capability_tickets.py` | Sandbox A's ticket is refused by sandbox B, an expired ticket is refused on replay, an epoch bump invalidates tickets in both directions, one kind's ticket does not open another's, forged and non-ASCII tickets are verdicts rather than exceptions, and the subject character set is asserted from a single shared definition. |
| `control_plane/store.py` | `test_capability_epochs.py` | Epochs live in the sandbox and workspace rows, start usable, are absent for unknown or released subjects, move independently, and bumping returns the new value. |
| `control_plane/core.py`, `control_plane/api.py` | `test_forwarded_headers.py` | This platform's cookies (including `__Host-`/`__Secure-` variants) and identity headers never cross into a sandbox, the caller's own cookies pass through byte for byte, only an allow list crosses, and a platform header cannot be put on that allow list. |
| `control_plane/*` composition | `test_standalone_contract.py` | Acyclic composition root, one Python authority for workspace limits, provider-neutral base manifests, documented shell defaults match the runtime. |

## Reverse experiments

A test that has never been seen to fail is a test nobody has evidence for. Every
guard in this suite that is described as catching something has been made to
catch it: the thing it guards is broken on purpose, the test is confirmed red,
the change is restored, and the test is confirmed green again. Both exit codes
get recorded, not the conclusion drawn from them.

`scripts/mutation-experiment.py` runs one. Its value is not in applying a patch;
it is in the five answers it refuses to give, each of which is
indistinguishable from a real result:

| Refusal | The wrong answer it prevents |
| --- | --- |
| The mutated form is empty or a no-op | It cannot be undone, so the "restored" run silently measures the mutated tree |
| The apply anchor matches zero or several places | The mutation lands somewhere other than intended, and the red is about something else |
| The baseline is already red | An inherited failure is reported as this change's, sending the reader to fix a problem that is not there |
| The restore did not reproduce the original bytes | Every later reading in the session runs on a damaged tree. Only this row's **removal** is guarded: the branch fires when a filesystem write does not round-trip, which cannot be provoked in-process without a seam existing purely to provoke it |
| A third party wrote the file mid-experiment | Restoring blind-writes over their work, silently, and the tree still looks fine |

Note: the last two are why the restore is verified by hashing the file rather than
by searching for the mutated text and putting the original back. A reverse
search can match the wrong occurrence, or none at all, and report success either
way; a hash comparison answers the question actually being asked, which is
whether the file came back.

The tool's own refusals are exercised by `test_dev_scripts.py` with an injected
test runner. **A tool used to validate guards, which has not itself been
validated, makes every green it reports unfounded** - and a harness that fails
to restore produces readings that look exactly like real ones.

The rules above are worth copying into any project doing this. The file is not:
one shared copy would add the kind of cross-project dependency this repository
is built to avoid, and a development tool drifting between projects has no
user-visible consequence, unlike a shared contract.

### Data plane (`file-service/`, `runtime/`)

| Source | Test file | What it pins |
| --- | --- | --- |
| `file-service/file_service.py` (`restore_checkpoint`, `_safe_member_path`) | `test_checkpoint_restore.py` | Symlink, hardlink, char/block device, FIFO, absolute path, `..`, empty/dot names, `.sandbox` members and duplicate paths are rejected with the workspace byte-for-byte unchanged and nothing written outside it; entry count over `MAX_CHECKPOINT_ENTRIES` and expanded size over `MAX_CHECKPOINT_SOURCE_BYTES` are rejected (exactly at the limit is accepted); a valid archive replaces the tree, keeps `.sandbox`, leaves no `restore-*`/`old-*` directories; a failing swap restores the old tree. |
| `file-service/file_service.py` (paths, journal, logging, sockets, Dockerfile) | `test_file_service_hardening.py` | `.sandbox` cannot be reached through symlinks, crash self-healing of an interrupted restore, request details never reach stdout, socket timeouts, PID 1 handling, every imported module is copied into the image. |
| `runtime/runtime_server.py` (`execute_shell`, `execute_shell_stream`, `shell_process_spec`) | `test_runtime_exec.py` | A timed-out command kills its whole process group, including a background child that keeps the pipe open and a foreground child; closed pipes do not extend the timeout; stdout and stderr are capped at `MAX_OUTPUT_BYTES` with `output_truncated` (exactly the limit is not truncated); streaming still caps the capture; `pipefail` is on; argument validation. |
| `runtime/shell_sessions.py` (`ShellSession.expire`, `SessionManager.reap_idle`) | `test_runtime_exec.py` | `expire()` marks `wall_timed_out`, kills the shell and its foreground child; `reap_idle` expires a command past `max_wall_seconds` and frees the slot; `close()` is idempotent. |
| `runtime/shell_sessions.py` (`ShellSession`, `SessionManager`) | `test_shell_sessions.py` | Session continuity: a second exec keeps the cwd and exported variables of the first and never leaks the completion marker; `pipefail` is on in the interactive shell too. The action set: async exec plus `session_input`, input refused when no command runs, a wait deadline that does not kill the command, `session_kill` restarting the same id. Disconnects: the exec that owns a command takes it down (cancelled callback, and a chunk consumer that raises), a wait disconnect leaves it running. Bounds: exec and wait reject on their own separate limits, an oversized command is refused before a slot is spent, a 2 KiB command still runs because user code travels through a `.sandbox-command-*.sh` file that is removed afterwards - including when user code exits the shell and `PROMPT_COMMAND` never runs. Teardown: wall-clock expiry is reported to the next wait, `close()` reaps a process whose first wait timed out, and it releases the PTY fd only after the reader thread is gone. Eviction reaps outside the manager lock, so `activity_snapshot` answers while one is in flight, and a full manager raises `SessionCapacityError` without leaving an unaddressable session behind. |
| `runtime/runtime_server.py` (`McpHandler`, `tool_failure`, `REQUEST_SOCKET_TIMEOUT_SECONDS`) | `test_runtime_mcp_errors.py` | Spoken over real HTTP against a real handler. An exhausted session quota comes back as `SESSION_BUSY_CODE`, on the SSE path as a final error frame rather than a truncated stream; a PTY write failure keeps its byte count and becomes an internal error, not a connection reset; anything unforeseen still answers 500 with JSON-RPC. `file_write`/`file_read` and the shell share one workspace and a `..` path is a structured `-32602`, not a traceback. The socket timeout really lands on the connection, drops a silent client and a lying `Content-Length` without pinning the thread, and does not cut a long SSE stream short. |
| `file-service/gc_workspaces.py` | `test_workspace_gc.py` | `.sandbox/last_used_at` is tenant-writable, so it is capped where it is read: a value inside the 24h tolerance is trusted (clock skew), one beyond it falls back to the marker's mtime with a log line, and that fallback is capped too because `touch -d` moves it as well - end to end, a forged marker no longer escapes TTL collection. Unparseable values fall back to the directory mtime. Collection takes only well-named directories, never a symlink, and refuses a non-positive TTL; purge refuses a path outside the root and does not follow links out of the workspace. One undeletable directory does not cancel the rest of the sweep: the report says `partial`, names the failure and exits non-zero, while a dry run deletes nothing. |
| `file-service/file_service.py` (`handle_glob`) | `test_file_search.py` | A glob pattern never passes through `safe_path`, so `handle_glob` stops both escapes itself: a `..` or absolute pattern is refused, and a symlink whose target lies outside the workspace is dropped from the results while one pointing inside is not. Match semantics are pinned alongside so an escape cannot disappear for the wrong reason: bare patterns match at any depth newest-first, `*` does not cross directories and `**` does, a search root scopes the match, noise directories and the `artifacts/compressed` mirror are pruned by path while a user's own `compressed/` and dot-directories stay searchable. |
| `mcp.py` | `test_mcp_contract.py` | The checkpoint tool description matches the runtime requirement. |
| `sandbox_client.py` | `test_sandbox_client.py` | Object owner defaults and validation, storage key normalisation, fail-closed without a token, resolve/lookup never create runtimes, command quoting, named runtime reuse. |
| `sandbox_cli.py` | `test_sandbox_cli.py` | Exec forwards streams and exit code; `run` releases the runtime even when the command fails; missing command is a clean error. |

### Deployment, scripts and documentation

| Source | Test file | What it pins |
| --- | --- | --- |
| `k8s/`, `overlays/`, `scripts/local-cluster.sh` | `test_storage_modes.py` | Per-workspace vs shared storage mounts, RWO overlay uses `Recreate`, SQLite patch keeps `/tmp`, local object store credentials are separated. |
| `console/` | `test_console_runtime_config.py` | DNS resolver and runtime upstream generation, deployment entrypoint and writable `/tmp`, mobile layout, mock API key shape. |
| `scripts/e2e-env.sh` | `test_e2e_environment.py` | Default single-cluster target, canonical overrides, every shell scenario sources the shared contract. |
| `scripts/prepare_release_assets.py`, `.github/workflows/*` | `test_release_assets.py` | Release manifests use digests, SBOM components are in the license inventory, release is gated by full CI, Lima image URL is immutable. |
| `Makefile` (`make dev-token`) | `test_dev_scripts.py` | The dev token target prints only the token. |
| `README.md`, `docs/`, `console/src/auth.ts` | `test_documentation.py` | Relative links resolve, README lists the current MCP tool surface, MCP instructions use real entrypoints, credential wording matches browser storage. |
| whole repository | `test_source_language.py` | Sources are English outside localisation and unicode fixtures. |

## Mutation self-check

The behaviour tests were verified against the mutation table of the
2026-08-27 open-source readiness review: each of the thirteen single-line
mutations (CAS guard, admission counters, restore path checks, exec kill,
session expiry, reaper predicate, idle eviction, readiness flag, three
manifest security fields) turns at least one test in the corresponding new
file red while the rest of the suite stays green.
