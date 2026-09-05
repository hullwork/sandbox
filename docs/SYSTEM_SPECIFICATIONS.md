# System specifications and limits

This page describes Sandbox Platform's checked-in defaults. They are not promises of
unlimited capacity and may be tightened by Kubernetes, the storage provider, or an
operator-managed template.

## Runtime defaults

| Item | Default | Authority |
| --- | --- | --- |
| Runtime image | `sandbox-runtime:0.5.0` | `SANDBOX_RUNTIME_IMAGE` |
| Isolation class | `gvisor` | `SANDBOX_RUNTIME_CLASS` |
| Runtime lifetime | 1,800 seconds idle; 43,200 seconds absolute | Control Plane environment |
| Scoped access token | 900 seconds | `ACCESS_TOKEN_TTL_SECONDS` |
| Concurrent runtimes | 4 by default; local profile scales at 4 per active Runtime worker | `SANDBOX_MAX_RUNTIMES` (`0` pauses admission) |
| Workspaces | 64 per Control Plane deployment | `SANDBOX_MAX_WORKSPACES` |
| Workspace request | 1 GiB | `SANDBOX_WORKSPACE_QUOTA` |
| Shell sessions | 16 per Runtime | `SANDBOX_MAX_SHELL_SESSIONS` |
| Shell-session idle limit | 1,800 seconds | Runtime environment |
| Shell-session wall limit | 3,600 seconds | Runtime environment |
| Inline request body | 6 MiB | Control Plane code limit |
| Inline object | 4 MiB | Control Plane code limit |
| Streamed object | 64 MiB | `MAX_STREAM_OBJECT_BYTES` |
| Checkpoint retention | 30 days | `CHECKPOINT_RETENTION_SECONDS` |

Runtime Pods are Linux containers. The project does not emulate a fixed CPU model,
kernel version, regional placement, or internet bandwidth. Operators own those
properties through their Kubernetes nodes and Runtime templates.

## Storage modes

- `shared` is the portable reference. One operator-provided PVC is mounted with a
  workspace-specific `subPath`; production uses RWX and the local profile uses RWO.
- `per-workspace` is an optional advanced topology. It requires a storage-specific
  volume service that can also enumerate and purge all workspace claims.

Requested PVC capacity is not proof of a hard quota. Confirm enforcement with the
chosen CSI implementation.

## Unsupported or intentionally absent

Sandbox Platform does not currently provide accounts, regions, snapshots,
dynamic per-request firewall rules, brokered third-party credentials,
public-domain routing, or billing. Checkpoints are workspace archives in an
S3-compatible object store; they are not VM snapshots and do not capture processes,
memory, or the writable container layer.
