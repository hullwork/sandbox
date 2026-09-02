# Using Sandbox Platform

Four surfaces reach the same Control Plane: the Python SDK, the `sandbox` CLI,
the stdio MCP bridge, and the operator console. This is the reference for all
four, plus the tasks that come up once something is running.

The README covers what this project is and how to get it running; start there
if you have not.

## Using it

### Python SDK

```bash
export SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080
export SANDBOX_TOKEN="$(make --no-print-directory dev-token)"
python3 - <<'PY'
from sandbox_platform.sandbox_client import Sandbox

sandbox = Sandbox.get_or_create("demo")
result = sandbox.run_command("printf", ["sandbox-ready\\n"])
print(result.stdout, end="")
sandbox.stop()  # Runtime stops; Workspace files remain.
PY
```

`Sandbox` is the high-level facade. The lower-level `SandboxManager` — exported as
`sandbox_client.MANAGER` — carries the object-store, checkpoint, and shell-session
operations used in [Common tasks](#common-tasks).

### CLI

```bash
sandbox create demo
sandbox exec demo -- python -c 'print("sandbox-ready")'
sandbox run --name demo --stop -- sh -lc 'printf "done\\n"'
sandbox list
```

`sandbox create` prints the name, Runtime id, and Workspace id separated by tabs
(`demo<TAB>sb-...<TAB>ws-...`). `sandbox list` prints one active Runtime per line as
`id`, `workspace_id`, `status`, `template`. Both accept `--json` for machine output.

`sandbox` is the user surface. `sandboxctl` is a separate operator surface and may
expose tenant-wide administrative data; keep the two credentials distinct.

### MCP bridge

`sandbox-mcp` is a stdio MCP server. It requires three environment variables and
refuses to start without them:

| Variable | Meaning |
| --- | --- |
| `SANDBOX_CONTROL_PLANE_URL` | Control Plane base URL, for example `http://127.0.0.1:18080` |
| `SANDBOX_TOKEN` | Control Plane or tenant bearer token |
| `SANDBOX_SESSION_ID` | Any stable string identifying this agent session; it selects the Workspace the session lands in |

Claude Code:

```bash
claude mcp add sandbox \
  -e SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080 \
  -e SANDBOX_TOKEN="$(make --no-print-directory dev-token)" \
  -e SANDBOX_SESSION_ID=my-project \
  -- sandbox-mcp
```

Any host that reads an `mcpServers` object:

```json
{
  "mcpServers": {
    "sandbox": {
      "command": "sandbox-mcp",
      "env": {
        "SANDBOX_CONTROL_PLANE_URL": "http://127.0.0.1:18080",
        "SANDBOX_TOKEN": "<paste the value of make dev-token>",
        "SANDBOX_SESSION_ID": "my-project"
      }
    }
  }
}
```

From a source checkout without installing the wheel, use
`python3 -m sandbox_platform.mcp` as the command with the same environment.

The bridge exposes exactly nine agent-scoped tools: `shell`, `shell_session`,
`sandbox_status`, `file_read`, `file_write`, `file_edit`, `file_glob`, `file_grep`,
and `workspace_checkpoint`. Tenant, key, template, audit, and arbitrary Runtime
administration are deliberately outside the agent surface.

### Operator Console

Keep this running and open <http://127.0.0.1:18081>:

```bash
make console-forward
```

The Console embeds no credential; a key you paste stays in tab-scoped
`sessionStorage`. For the local profile, paste the value of `make dev-token` — it is
cluster-administrator-equivalent and is intended only for this profile.

A deployment offers whichever of these sign-in methods it has configured:

- **Single sign-on.** Set `SANDBOX_CONTROL_PLANE_OIDC_*` and the Control Plane runs an
  OpenID Connect Authorization Code + PKCE flow against your provider. It is a relying
  party only: it issues assertions for nobody and accepts none.
  `SANDBOX_CONTROL_PLANE_OIDC_AUDIENCE` has no default — give this Control Plane an
  audience no other service shares, or an ID token minted for a neighbouring service
  is accepted here too.
- **An API key.** Keys issued through `/v1/admin/keys` are revocable, attributable,
  and can carry an expiry and a permission set. This is the route for machine callers
  and the everyday route for people without SSO.
- **`SANDBOX_CONTROL_PLANE_TOKEN`, the break-glass token.** The way in when the
  identity provider is unreachable. It is administrator-equivalent, cannot be revoked,
  and cannot be attributed to a person — so every use is written to the Control Plane
  log with its source address, counted under
  `sandbox_credential_uses_total{kind="break-glass"}`, and shown as a banner for the
  whole Console session. It is off by default whenever an OIDC provider is configured,
  and switching it off removes the credential from the process: the API refuses it, not
  just the login form. Configuring no provider *and* switching it off makes the
  Control Plane refuse to start.

That escape hatch is documented on purpose. One that exists only in the source is one
the operators do not know about and an attacker reading the code does.

Configuration details are in the [configuration reference](CONFIGURATION.md); the
[authentication contract](AUTH.md) is the published agreement for client authors.

The Console ships English and Simplified Chinese. It follows the browser language on
first use and stores an explicit choice in `localStorage` under
`sandbox-console-language`. Catalogs live in `console/src/i18n/locales`, typed against
the English source so a missing key fails `npm --prefix console run typecheck`; adding
a locale means adding its code to `LOCALES` in `console/src/i18n/index.tsx`, adding a
catalog, and adding the option to `LanguageSwitcher`.

---

## Common tasks

Every method named here exists in
[`sandbox_platform/sandbox_client.py`](../sandbox_platform/sandbox_client.py) and
[`sandbox_platform/sandbox_cli.py`](../sandbox_platform/sandbox_cli.py). Numeric limits
come from [System specifications](SYSTEM_SPECIFICATIONS.md).

**Upload a file.** Text up to 1 MB goes straight into the Workspace:

```python
sandbox.write_file("input/data.csv", csv_text)
sandbox.write_files([{"path": "a.txt", "content": "1"}, {"path": "b.txt", "content": "2"}])
```

Larger or binary inputs go through the object store: `MANAGER.put_agent_blob(...)`
uploads with a single-use ticket, then
`MANAGER.import_object_to_workspace(locator, "input/data.bin")` copies the object into
the Workspace.

When an agent host keeps its durable file library in a different S3 plane, the
recommended integration is federated transfer: the host's artifact service streams to
and from these same ticketed Control Plane endpoints. The host never receives the
Sandbox S3 credential and the Sandbox never receives the host's. Deliberately sharing
one physical bucket is also possible, but that is a decision the integrating platform
owns.

**Run a long task.** One `shell` call is capped at 30 seconds
(`MAX_EXEC_TIMEOUT_SECONDS` in `runtime/runtime_server.py`). For anything longer,
start a PTY session and poll it; a session may live up to one hour of wall time:

```python
MANAGER.shell_session("exec", "build", command="make -j4 test", async_mode=True)
result = MANAGER.shell_session("wait", "build", timeout_seconds=120)  # repeat until done
```

From the CLI, `sandbox exec demo --timeout 30 -- <command>` uses the same 30-second cap.

**Get artifacts back.** `sandbox.read_file("out/report.txt")` returns bounded text. For
whole files, `MANAGER.export_workspace_object("out/report.pdf", locator)` writes the
file to the object store and `MANAGER.open_object(locator)` downloads it. For a
directory of many small deliverables,
`MANAGER.export_workspace_collection("artifacts", locator)` creates one bounded
`tar.gz` with a `manifest.json` of member paths, sizes, and SHA-256 values. It exports
regular files only and omits internal state, legacy compaction data, and symlinks.

**Checkpoint and restore.** Checkpoints are explicit recovery points, not the normal
file-persistence path — the Workspace PVC already survives Runtime release.
`sandbox.checkpoint()` archives the Workspace *files* (not processes or memory) into
object storage; `MANAGER.list_workspace_checkpoints()` lists archives and
`MANAGER.restore_workspace(checkpoint_id)` replaces the Workspace with one. The MCP
tool `workspace_checkpoint` exposes the same archive operation to agents.

**Run several sandboxes in parallel.** Each name is an independent Workspace with its
own Runtime; `Sandbox.create("job-1")` and `Sandbox.create("job-2")` share no files.
The reference deployment admits 4 concurrent Runtimes (`SANDBOX_MAX_RUNTIMES`) and 64
Workspaces (`SANDBOX_MAX_WORKSPACES`); `sandbox list` shows what is running.

**Install packages.** The default Runtime image deliberately ships without `pip` or any
package manager ([`runtime/Dockerfile`](../runtime/Dockerfile) removes pip after the build
so its vendored dependencies and installation attack surface go with it). The image
carries Python 3.14 with the pptx/docx/xlsx/pdf libraries, Node.js 24 with npm, plus
bash, git, curl, jq, make, and unzip. To add packages, build your own image, register
it under `SANDBOX_TEMPLATES`, and pass `--template <id>` to `sandbox create` or
`template=` to `Sandbox.create`.

---

