# Compatibility and validation matrix

This matrix states evidence, not a blanket compatibility claim. Components from one
Sandbox Platform tag are tested and supported together; mixed-version deployments are
not supported until a versioned compatibility suite proves otherwise.

## Automated release targets

| Surface | Matrix | Evidence |
| --- | --- | --- |
| Python SDK, MCP, CLI | CPython 3.11 and 3.14 | CI unit, package build, clean wheel install, and entrypoint smoke tests |
| Container images | `linux/amd64`, `linux/arm64` | Buildx matrix, Trivy gate, CycloneDX SBOM, Cosign and GitHub attestations |
| Console | Node.js 22 build environment | Locked npm install, i18n, ESLint, TypeScript, and production build |
| Kubernetes manifests | Provider-neutral base, local integration, EKS | Kustomize render and contract tests |

The currently validated local integration stack is kubeadm/Kubernetes 1.36,
Cilium 1.19.6, Rook 1.20.6, Ceph 20.2.4, and Ceph-CSI 3.17.1 on Ubuntu Noble.
Rook and Ceph-CSI are separate checksum-pinned Helm charts; the Ceph and Ceph-CSI
images are digest-pinned. Noble's 6.8 kernel uses the CephFS FUSE mounter in this
profile. Treat any change to that tuple as a compatibility change that requires
the full local E2E suite.

## Compatibility rules

- Kubernetes must support RuntimeClass, NetworkPolicy, batch/v1 Jobs/CronJobs,
  leases, and the storage access mode selected by the overlay.
- The `gvisor` RuntimeClass must resolve to a working `runsc` handler on every node
  eligible for Runtime Pods. An object alone is not proof of isolation.
- Production deployments must replace development SQLite and the single-OSD Ceph
  profile with the dependencies and backup policies in
  [Production](PRODUCTION.md).
- The local profile provides Metrics Server only through its overlay. Production must
  provide its own Metrics API and trusted kubelet serving certificate path.
- A new Kubernetes, Cilium, gVisor, Python, Node, architecture, or storage-provider
  combination is unverified until its render/build checks and full E2E are recorded.
- The client-visible authentication surface has its own stability split, listed in
  the [authentication contract](AUTH.md). Items marked stable there change only
  with a major version; the error strings, permission vocabulary, and everything
  about internal sandbox capability tickets may change in a minor version.
- **The Control Plane, Runtime and File Service images of one tag are a single unit.**
  The credential Control Plane presents to a sandbox is derived per instance and
  verified inside it, so the two sides must agree on the scheme and on the
  environment variables carrying it. A mixed-version rollout is unsupported and
  fails **closed**: every call from Control Plane into a mismatched sandbox answers
  `401 unauthorized`, and the sandbox logs an authentication failure rather than
  a version complaint. Roll the three images together; if `401`s appear from
  internal calls immediately after a partial rollout, check image tags before
  investigating credentials.
- Upgrading a control-plane database in place is supported. `ensure_schema` adds
  columns that a database created by an earlier tag does not have, and does so
  only when they are absent, so starting a current deployment against a current
  database changes nothing. Downgrading is not supported: an older Control Plane does
  not know about the newer columns and no statement removes them.
