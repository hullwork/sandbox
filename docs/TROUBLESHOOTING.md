# Troubleshooting

Each entry lists the symptom, the cause, how to confirm it, and the fix. Commands
assume the local profile (`make up-local`); replace the context and namespaces for
other clusters. The local kubeconfig is `.sandbox/kubeconfig` with context
`sandbox-local`; Make targets set `KUBECONFIG` for you, direct `kubectl` calls do not.

```bash
export KUBECONFIG="$PWD/.sandbox/kubeconfig"
```

## `context "sandbox-local" does not exist`

**Symptom.** `make dev-token`, `make control-plane-forward`, or a manual `kubectl --context sandbox-local` fails with this message.

**Cause.** `kubectl` is reading a kubeconfig other than `.sandbox/kubeconfig`: the
VM has not been created yet, `SANDBOX_STATE_DIR` pointed somewhere else at
`make up-local` time, or you ran `kubectl` directly without `KUBECONFIG`.

**Check.**

```bash
scripts/local-cluster.sh kubeconfig          # prints the expected path
ls -l .sandbox/kubeconfig
KUBECONFIG=.sandbox/kubeconfig kubectl config get-contexts
```

**Fix.** Run `make up-local` if the file is missing. For manual `kubectl`, export
`KUBECONFIG` as shown above, or pass `--kubeconfig .sandbox/kubeconfig`. If you
overrode `SANDBOX_STATE_DIR` or `SANDBOX_KUBE_CONTEXT`, use the same values again.

## Runtime Pod stays `Pending`

**Symptom.** `sandbox create` or `Sandbox.create()` blocks, then fails; the Pod in
`sandbox-workloads` stays `Pending` or shows `ContainerCreating` with a RuntimeClass error.

**Cause.** Either the `gvisor` RuntimeClass is missing, `runsc` is not installed on the
node that was selected, or no node carries the label Control Plane uses for placement
(`sandbox.hullwork.com/node-role=runtime` in the local profile, `SANDBOX_RUNTIME_NODE_SELECTOR`
elsewhere).

**Check.**

```bash
kubectl get runtimeclass gvisor
kubectl get nodes --show-labels | grep sandbox-node
kubectl -n sandbox-workloads get pods
kubectl -n sandbox-workloads describe pod <pod> | sed -n '/Events/,$p'
```

`describe` shows `FailedCreatePodSandBox ... runsc` when the handler is missing on the
node and `0/1 nodes are available: node(s) didn't match Pod's node affinity/selector`
when the label is missing.

**Fix.** In the local profile, rerun `make up-local`; it re-applies the label and the
gVisor installer (`scripts/install-gvisor-kubeadm.sh`) idempotently. On other
clusters, install gVisor on the Runtime nodes, apply `k8s/platform/runtime-class.yaml`,
and label the nodes to match `SANDBOX_RUNTIME_NODE_SELECTOR`. A RuntimeClass object
alone is not proof that `runsc` works; `make status-local` and the E2E suite verify it.

## `ErrImageNeverPull` or `ImagePullBackOff`

**Symptom.** Control Plane, console, volume agent, or Runtime Pods show
`ErrImageNeverPull`; Runtime or Metrics Server Pods show `ImagePullBackOff`.

**Cause.** Project images use fixed local tags with `imagePullPolicy: Never`
(`k8s/control-plane.yaml`, `k8s/console.yaml`, `k8s/volume-agent.yaml`); they must be
present in the VM's containerd `k8s.io` namespace. `make up-local` builds them with
`make images` and imports them with `docker save | ctr images import`. A failed
build, a changed image tag, or a fresh VM without a rebuild leaves the tag absent.
Runtime images use `IfNotPresent` and are pulled from the registry named in the
template, which must be reachable from the node.

**Check.**

```bash
docker image ls | grep -E 'sandbox-(runtime|file-service|control plane|console)'
limactl shell sandbox-local -- sudo ctr --namespace k8s.io images ls | grep sandbox-
kubectl -n sandbox-system describe pod <pod> | sed -n '/Events/,$p'
```

**Fix.** Rerun `make up-local`; it rebuilds, re-imports, and restarts the Deployments
so they pick up the new image under the same tag. For a custom Runtime template,
make sure the registry is allowed by `SANDBOX_IMAGE_REGISTRIES` and reachable from
the node (see the network section below).

## `SANDBOX_TOKEN is required; local tool fallback is disabled`

**Symptom.** `sandbox`, the SDK, or `sandbox-mcp` exits immediately with this message.

**Cause.** The Control Plane credential is not in the environment. This is intentional
fail-closed behavior: the SDK never runs commands on the host instead.

**Check.**

```bash
printenv SANDBOX_TOKEN SANDBOX_CONTROL_PLANE_URL
make --no-print-directory dev-token | wc -c      # non-zero when the Secret exists
```

**Fix.**

```bash
export SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080
export SANDBOX_TOKEN="$(make --no-print-directory dev-token)"
```

`dev-token` reads the `sandbox-api-credentials` Secret in `sandbox-system`; if that
fails, resolve the kubeconfig entry above first. In production use a tenant or admin
key issued through `sandboxctl admin-key-create` rather than the bootstrap token.

## `sandbox-mcp` starts but every tool fails, or the host reports the server exited

**Symptom.** The MCP host lists the server but each call returns an error, or the
server exits at startup with a message naming an environment variable.

**Cause.** `sandbox-mcp` needs `SANDBOX_CONTROL_PLANE_URL`, `SANDBOX_TOKEN`, and
`SANDBOX_SESSION_ID`. Hosts do not inherit your shell's exports; the variables must
be set in the host's server configuration. `SANDBOX_SESSION_ID` is the identity that
selects the Workspace; without it the bridge cannot resolve one.

**Check.**

```bash
SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080 \
SANDBOX_TOKEN="$(make --no-print-directory dev-token)" \
SANDBOX_SESSION_ID=probe \
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | sandbox-mcp
```

A working bridge answers with a JSON-RPC result listing nine tools.

**Fix.** Put all three variables in the host configuration; see the MCP section of the
[README](../README.md) for the `claude mcp add` form and the `mcpServers` JSON form.
`sandbox-mcp --help` prints the variable list.

## Cannot reach gVisor, `registry.k8s.io`, or `pkgs.k8s.io` from the VM

**Symptom.** `make up-local` stalls or fails while the VM provisions: apt cannot
fetch from `pkgs.k8s.io`, `curl` to `storage.googleapis.com/gvisor` times out, the
Metrics Server image from `registry.k8s.io` does not pull, or the Cilium chart from
`quay.io` fails to download. This is common on networks where those hosts are
blocked or throttled, including much of mainland China.

**Cause.** The local profile downloads its pinned dependencies directly from upstream
hosts (`scripts/local-cluster.yaml`, `scripts/install-gvisor-kubeadm.sh`,
`scripts/install-cilium-kubeadm.sh`, `overlays/local-dev/metrics-server.yaml`).

**Check.**

```bash
limactl shell sandbox-local -- curl -sSI --max-time 10 https://pkgs.k8s.io/ | head -1
limactl shell sandbox-local -- curl -sSI --max-time 10 \
  https://storage.googleapis.com/gvisor/releases/release/20260803.0/x86_64/runsc | head -1
docker pull registry.k8s.io/metrics-server/metrics-server@sha256:d9862115e7c7881280d3d75ca26bda8ffc0fc213315979575bf23ce9826205c0
```

**Fix.** Provide an HTTP(S) proxy to Lima (`HTTPS_PROXY` in the host environment is
propagated to the guest by Lima), or pre-seed the artifacts:

- Images: `docker pull` them on the host through a mirror, retag to the exact
  reference in `scripts/local-cluster.sh`, and rerun `make up-local`; the script skips
  `docker pull` when the image already exists locally and imports it into the VM.
- gVisor: the installer honors `GVISOR_RELEASE`, `GVISOR_RUNSC_SHA512`, and
  `GVISOR_SHIM_SHA512`, so a mirrored release can be used only with its checksums.
- Kubernetes packages and the Cilium chart have no override variable today; use a
  proxy or edit the URLs in the scripts for your environment and keep the checksum
  checks in place.

The repository does not ship alternative mirror URLs; verify any mirror's checksums
against upstream before trusting it.

## `401` after the token worked earlier

**Symptom.** Requests that succeeded start failing with `401` and
`invalid or expired scoped access token`, or with `the static control plane token has been
retired; use an admin API key`.

**Cause.** Scoped Runtime tokens live for `ACCESS_TOKEN_TTL_SECONDS` (900 seconds by
default) and are refreshed by the SDK; a stale token indicates a client that cached
a lease past its lifetime or a clock skew between client and Control Plane. A plain
`unauthorized` for the static `SANDBOX_CONTROL_PLANE_TOKEN` means the break-glass path is off in
this deployment - either explicitly with `SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED=false`, or by
default because `SANDBOX_CONTROL_PLANE_OIDC_ISSUER` is configured. `GET /v1/auth/methods` answers
which methods are live. The fix is a different credential, not a retry.

**Cause.** An API key can also expire: keys issued with `expires_in_seconds` stop
authenticating at that moment and answer `401` exactly like an unknown key.
`GET /v1/admin/keys` shows `expires_at` for each one.

**Check.**

```bash
curl -sS -H "Authorization: Bearer $SANDBOX_TOKEN" "$SANDBOX_CONTROL_PLANE_URL/v1/whoami"
kubectl -n sandbox-system logs deploy/sandbox-control-plane | grep -i 'unauthorized\|expired' | tail
```

**Fix.** For scoped-token errors, let the SDK re-resolve the lease (a new
`Sandbox.get(name)` or `MANAGER.ensure_runtime()` call) and check clocks. For the
break-glass token, create an admin key with `sandboxctl admin-key-create` and
export it as `SANDBOX_TOKEN`.

## Control Plane `/readyz` returns `503`

**Symptom.** The SDK reports `Sandbox Control Plane unavailable`; `kubectl -n sandbox-system
get pods` shows the Control Plane running but not Ready.

**Cause.** Readiness means "this replica should receive traffic". It turns `503` when
the store (PostgreSQL or SQLite) is unreachable, during the shutdown drain, or when a
required dependency check fails (see `docs/adr/0002-readiness-and-dependency-health.md`).

**Check.**

```bash
kubectl -n sandbox-system logs deploy/sandbox-control-plane --tail=100
kubectl -n sandbox-system port-forward deploy/sandbox-control-plane 18080:8080 &
curl -sS http://127.0.0.1:18080/readyz
```

**Fix.** Restore the dependency the log names. Control Plane is a single replica with
`strategy: Recreate`, so during an upgrade a short `503` window is expected; retry
after the new Pod is Ready.
