# Software supply chain

CI validates source, package, console, manifests, and image builds, runs Trivy
dependency vulnerability checks, and performs a full-history secret scan.
The tag workflow first reruns full CI and verifies a public repository, annotated
version-matching tag, and a commit reachable from `main`. It then publishes four
multi-architecture GHCR images plus Python wheel and sdist artifacts. The wheel and
sdist also go to PyPI as `sandbox-platform`, through Trusted Publishing rather than a
stored API token, from a job that waits on the `pypi` environment approval; the bytes
uploaded are the ones this run scanned, attested, and signed. It generates a
digest-pinned deployment manifest, CycloneDX SBOMs, vulnerability reports, checksums,
GitHub/Sigstore provenance, SBOM attestations, and keyless Cosign signatures.

Base images are digest pinned. Every third-party GitHub Action is pinned to a full
commit SHA with the reviewed tag in a comment. Dependabot may propose updates, but
maintainers must resolve tags from official repositories and review security notes.
The local development overlay vendors the official Metrics Server v0.9.0 manifest
(upstream asset SHA-256
`1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b`) and pins its
multi-architecture image manifest to
`sha256:d9862115e7c7881280d3d75ca26bda8ffc0fc213315979575bf23ce9826205c0`.

`scripts/generate-license-inventory.py` inventories the resolved SDK environment,
the npm lockfile, and manifest/Dockerfile image references. After all four images are
built, `scripts/prepare_release_assets.py` merges every component reported by their
CycloneDX SBOMs into the final `licenses.json` and `licenses.md`; this includes Python,
OS, Go, and npm packages found inside images. UNKNOWN entries require legal review
even when the technical workflow succeeds.

Verify downloaded artifacts and attestations:

```bash
sha256sum -c SHA256SUMS
kubectl apply --dry-run=client -f sandbox-vX.Y.Z.yaml
cosign verify-blob --bundle sandbox_platform-*.whl.sigstore.json sandbox_platform-*.whl
gh attestation verify sandbox_platform-*.whl --repo "$GITHUB_REPOSITORY"
cosign verify ghcr.io/hullwork/sandbox-control-plane@sha256:<digest> \
  --certificate-identity-regexp '.*/.github/workflows/release.yml' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Never publish kubeconfigs, environment files, database dumps, workspaces, test
transcripts, prompts, or credentials. Before the first public push, scan the complete
Git history—not only the working tree—and rotate any credential that ever appeared.
