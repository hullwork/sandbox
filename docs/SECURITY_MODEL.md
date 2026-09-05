# Security model

`SECURITY.md` explains vulnerability reporting. This document defines what the
runtime boundary protects, what it assumes, and what it does not protect.

## Protected assets and trust boundaries

- Host and Kubernetes nodes are outside tenant trust. Agent code is untrusted.
- Control Plane and cluster administrators are trusted control-plane principals.
- Runtime tokens are short-lived and bound to one workspace/runtime boundary.
- Object tickets are short-lived, object-bound, and single use.
- Workspace data, control-plane state, signing keys, object credentials, and
  Kubernetes credentials are separate assets and must not share a trust domain.

Runtime Pods run non-root with a read-only root filesystem, no service-account token,
restricted Linux capabilities, resource limits, NetworkPolicy, and gVisor in the
production reference. Workspace paths and archive entries are validated against
traversal, links, devices, size, and entry-count limits.

## Runtime network egress

The checked-in policy (`k8s/network-policy.yaml`) applies to every Pod in
`sandbox-workloads`:

- Default deny for ingress and egress.
- Ingress to Runtime port 8080 only from Control Plane in `sandbox-system`.
- Egress to `kube-dns` in `kube-system` on UDP/TCP 53.
- Egress to the public internet on **TCP 80 and 443 only**, with `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, and `127.0.0.0/8` excluded.

So agent code can download from public HTTP(S) endpoints but cannot reach private
networks, the node, the cloud metadata endpoint, or other cluster services by IP.
The exclusion list covers the RFC 1918 ranges only. If your cluster's Pod or Service
CIDR lies outside them (for example `100.64.0.0/10`, which some managed platforms
use), add it to the `except` list before relying on the policy; this is an
inference from the checked-in CIDRs, not a tested configuration. The policy is not a
domain firewall: it cannot distinguish one public host from another, and outbound on
ports other than 80/443 is denied. Enforcement requires a CNI that implements
NetworkPolicy; `scripts/verify-network-policy.sh` checks it on a live cluster.

## Threats covered

The design aims to contain malicious shell commands, cross-workspace access,
credential replay outside scope, archive traversal, arbitrary image selection,
unbounded Runtime lifetime, and accidental host fallback when Sandbox is unavailable.

## Assumptions and exclusions

- Kubernetes control-plane, node, gVisor, storage, DNS/CNI, and object-store
  compromise are outside the application boundary.
- The checked-in NetworkPolicy is not a dynamic domain firewall. DNS/IP changes and
  CNI enforcement remain operator responsibilities.
- The operator console is not a secret store and must sit behind an authenticated
  administrative ingress.
- The `shared` storage mode weakens storage isolation and quota semantics.
- The local overlay deliberately uses SQLite and single-OSD Ceph. Its scalable,
  tainted Runtime worker pool validates placement and gVisor isolation, but
  the two nodes remain in one Kubernetes trust domain and do not prove production
  durability or high availability.
- Side-channel resistance, malicious kernel exploits beyond gVisor's guarantees,
  and public anonymous multi-tenancy are not claimed.

## Verification requirements

Before production use, verify the Runtime Pod actually reports the expected
`runtimeClassName`, gVisor is installed on every selected node, default-deny policies
are enforced by the CNI, service-account token mounting is disabled, credentials are
least privilege, and a real cross-owner access test fails. A RuntimeClass object by
itself is not proof that `runsc` is functional.
