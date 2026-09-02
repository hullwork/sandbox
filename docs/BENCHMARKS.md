# Reproducible benchmarks

Benchmarks complement the functional and adversarial E2E suite; they never replace its
security assertions. A report is valid only when it contains raw samples, the evaluated
summary, and the environment captured by the same run.

## Local runner

Deploy the local profile and expose Control Plane, then run:

```bash
export SANDBOX_CONTROL_PLANE_URL=http://127.0.0.1:18080
export SANDBOX_TOKEN="$(make --no-print-directory dev-token)"
python3 bench/runner.py \
  --kube-context sandbox-local \
  --iterations 100 \
  --warmups 10 \
  --output-dir "bench-results/$(date -u +%Y%m%dT%H%M%SZ)" \
  --require-excellent
```

The output directory is new for every run and contains:

- `samples.jsonl`: one measured operation per line, including failures;
- `warmup-samples.jsonl`: excluded warmup observations, retained for audit;
- `summary.json`: sample counts, success rates, percentiles, and every threshold check;
- `environment.json`: source state, OS/architecture/Python, endpoint, Kubernetes version,
  RuntimeClass, and Pod image identities. It never contains the supplied token.

The smoke default is 20 recorded iterations. Formal publication uses at least 100
iterations and five independent runs on each architecture. Do not combine arm64 and
amd64 samples or different storage classes into one percentile distribution.

## Excellent profile

`bench/excellent-thresholds.json` is the machine-readable authority. Thresholds must be
committed before collecting a release candidate's measurements.

| Metric | Excellent threshold |
| --- | --- |
| Functional, isolation, tenant boundary, persistence, recovery | 100% correct; zero leaked resources |
| All recorded operations | success rate at least 99.9% |
| Runtime cold start | p50 <= 5s, p95 <= 15s, p99 <= 30s; success rate at least 99% |
| Warm shell execution | p95 <= 300ms, p99 <= 750ms |
| Non-provisioning Control Plane API | p95 <= 100ms, p99 <= 250ms |
| 1 KiB file read/write | p95 <= 500ms, p99 <= 1s, content digest always exact |
| 30-minute steady load | success rate at least 99.9%; 5xx at most 0.1% |
| 100 MiB object/PVC transfer | at least 60% of a direct-PVC baseline on the same node |
| 1,000-file collection export | one archive; manifest paths, sizes and SHA-256 values 100% exact |
| Control Plane restart | recovery within 30s; zero data loss |
| Runtime recreate | ready within 30s; Workspace hash unchanged |
| Quota and tenant isolation | 100% over-limit rejection; zero cross-tenant access |
| Idle footprint | Control Plane plus Console <= 500 MiB; each Runtime <= 150 MiB |

The checked-in runner currently automates the first latency path: health, Workspace
creation, Runtime cold start, warm shell execution, and 1 KiB file read/write with
per-iteration cleanup. The cluster E2E remains the authority for isolation, restart,
object storage, and adversarial behavior. Future benchmark scenarios must emit the same
JSONL schema and add their threshold before their first reported result.

## Comparative evaluation

Comparisons with hosted or third-party sandboxes use one adapter per provider and only
the common operation set. Every report records provider, region, paid tier, test time,
client version, concurrency, and network origin. Local measurements and hosted-provider
measurements remain separate tables; otherwise infrastructure differences look like
product differences.
