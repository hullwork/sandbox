# Sandbox benchmark report — 2026-09-01

## Result

The formal local reference benchmark passed the predeclared **Excellent** gate in
five independent runs. Each run used 10 warmups and 100 measured iterations: 500
successful samples per metric, zero failures, and no warm Runtime pool.

| Operation | p50 | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Health | 4.96 ms | 23.34 ms | 26.45 ms | 57.62 ms |
| Workspace create | 29.21 ms | 51.42 ms | 97.73 ms | 175.54 ms |
| gVisor Runtime cold start | 2.497 s | 2.789 s | 3.081 s | 3.311 s |
| Warm execution | 35.72 ms | 48.08 ms | 89.63 ms | 101.66 ms |
| Write 1 KiB file | 29.83 ms | 54.60 ms | 64.87 ms | 122.28 ms |
| Read 1 KiB file | 32.46 ms | 54.14 ms | 98.12 ms | 274.11 ms |

Every measured cold start created a new Kubernetes Pod under gVisor and included
scheduling, container startup and readiness. The runner deleted the Runtime after
each iteration and verified cleanup; it did not recycle a pre-warmed Sandbox.

## Reproducible evidence

- Source revision: `155561e5210d50051d2af1f59b1e666a032ed977`

The raw evidence for the numbers below was produced on the maintainer's machine
and is **not published with this repository**: the runs were named
`sandbox-benchmark-155561e-dedicated-vms-20260901`,
`sandbox-calibration-155561e-dedicated-vms-20260901` and
`sandbox-calibration-155561e-20260901`. Nothing here lets you inspect those
directories, so treat the numbers as the maintainer's measurement rather than as
evidence you have seen.

What you can do instead is produce your own, which is the only form of
reproduction that means anything across different hardware. Check out the source
revision above, bring up a cluster per `docs/DEPLOYMENT.md`, and run:

```bash
python3 bench/runner.py \
  --kube-context sandbox-local \
  --iterations 100 \
  --warmups 10 \
  --output-dir "bench-results/$(date -u +%Y%m%dT%H%M%SZ)" \
  --require-excellent
```

Each run writes a fresh directory holding the raw per-run JSON, logs, the
thresholds it was judged against, the source and deployment revisions, image
identities, the host and cluster profile, and `SHA256SUMS`. Compare that against
the tables below; the host profile is recorded precisely because these numbers
do not transfer between machines.
Checksums were verified after collection.

## Test profile

- Apple Silicon host: 10 CPU cores, 32 GiB memory.
- Lima kubeadm control plane: 4 vCPU / 8 GiB.
- Lima kubeadm Runtime worker: 6 vCPU / 8 GiB.
- Sandbox was the only active product cluster during the formal run.
- Local Ceph RGW object storage; serial request stream.

The earlier mixed-host calibration is intentionally retained. It measured cold
start p50 3.453 s / p95 5.248 s and warm execution p95 504.8 ms while competing
host workloads were active. Dedicated resources improved predictability; the
comparison is operational evidence, not a claim that code alone produced the
entire delta.

## Interpretation and limits

This result demonstrates an excellent **ready-node, serial, local reference**
profile. It is not a cloud multi-tenant capacity test and does not include a node
autoscaler's decision or VM bootstrap. A node-cold benchmark must separately time:

1. pending Pod to scale decision;
2. VM and kubelet registration;
3. CNI, CSI and gVisor readiness;
4. Sandbox Pod readiness and runtime health.

Those phases depend on the infra node-pool provider and cloud implementation,
and must never be reported as the 2.789 s ready-node p95 above. Production sizing
also needs concurrent tenant/load tests, remote registry pulls, network variance,
failure injection and storage saturation tests on the target infrastructure.
