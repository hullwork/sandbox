# Contributing to Sandbox Platform

Thanks for improving Sandbox Platform. Changes must preserve workspace ownership, gVisor isolation, scoped credentials, object-storage boundaries, and fail-closed behavior when Control Plane is unavailable.

## Development setup

Requirements:

- Python 3.11+
- Docker, Lima, kubectl, and Helm for local cluster checks
- Node.js and npm for console changes
- A Kubernetes cluster with gVisor for full E2E

```bash
# Fork the repository on GitHub first, then clone your fork.
git clone git@github.com:<your-github-user>/sandbox.git
cd sandbox
make doctor
bash -n scripts/*.sh
make -n up-local
```

Run the standalone tests and package smoke check before opening a pull request:

```bash
python3 -m pip install -e '.[test]'
make test
python3 -m pip wheel --no-deps . --wheel-dir /tmp/sandbox-wheel
```

Console checks:

```bash
npm --prefix console ci --ignore-scripts
npm --prefix console run test:i18n
npm --prefix console run lint
npm --prefix console run typecheck
npm --prefix console run build
```

## Language conventions

- Write code comments, API documentation, commit messages, and pull-request descriptions in English. Quoted user-visible text may retain another language when reproducing an exact input or error.
- Console user-visible strings must live in the typed locale catalogs rather than directly in components. Add the English source entry first, keep every other locale complete, and use `t()` in components.
- Format dates, times, and relative times with the active locale through `Intl` or the helpers in `console/src/format.ts`.

Local single-node gVisor integration cluster:

```bash
make up-local
```

In another terminal:

```bash
export SANDBOX_TOKEN="$(make --no-print-directory dev-token)"
make console-forward
```

Full E2E requires an explicit kubeconfig, documented namespaces, a reachable Control Plane,
and a functional gVisor RuntimeClass. See `docs/DEPLOYMENT.md`.

```bash
bash scripts/run-all-e2e.sh
```

Cleanup:

```bash
make down-local     # stop the VM, keep its disk
make destroy-local  # delete the VM and local state
```

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) when a step fails.

## Change expectations

- Treat [`contracts/control-plane-openapi.yaml`](contracts/control-plane-openapi.yaml) as the Control Plane REST contract; update it and the SDK together.
- Runtime must not receive Kubernetes credentials, a writable root filesystem, unrestricted egress, or host file fallback.
- Preserve workspace owner checks, checkpoint validation, ticket single-use semantics, and object-store least privilege.
- Keep the SDK and MCP wheel surface separate from deployment-only modules.
- Do not commit tokens, kubeconfigs, `.env` files, workspace contents, transcripts, private prompts, or generated Python bytecode.
- User-visible API, tool, deployment, or security changes require README, integration, and contract updates in the same pull request.
- Architectural constraints and tradeoffs belong in `docs/adr/`; inline comments should explain the current contract rather than preserve a chronological incident log.

## Pull-request checklist

- [ ] Focused summary and risk statement
- [ ] Relevant unit checks pass
- [ ] Console checks pass when UI files changed
- [ ] Contract, SDK, and MCP behavior stay synchronized
- [ ] Kubernetes and storage security boundaries are tested or explicitly unchanged
- [ ] E2E or manual verification evidence is included for cluster-facing changes
