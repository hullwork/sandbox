# Third-party notices

The repository source is released under the [MIT License](LICENSE). Container images
built from this repository include third-party components under their own licenses.

| Component | License | Distribution note |
| --- | --- | --- |
| MinIO Client (`mc`) | AGPL-3.0 | **Runtime dependency of the Control Plane image.** Every object-store operation (checkpoints, imports, exports, tickets, garbage collection) is executed by Control Plane through the `/usr/local/bin/mc` subprocess (`OBJECT_STORE_CLIENT`). See below for source and modification details. |
| Python packages and base images | Their respective licenses | See lockfiles, base-image metadata, and container build provenance for exact versions. |

## MinIO Client in the Control Plane image

`control_plane/Dockerfile` builds `mc` from source rather than using the upstream binary:

- Upstream source: `https://github.com/minio/mc`, commit
  `77f82e18b5401a65958f1619df6ebb994634bd88`, fetched as a tarball whose SHA-256 is
  `167415edd21bc29f5360943dac64272aa5cda0a39f3070b15cfeca671c43d975` and verified
  during the build.
- Modification: [`mc/dependency-overrides.patch`](mc/dependency-overrides.patch) is
  applied before compiling. It changes only `go.mod` and `go.sum` (Go toolchain
  version and dependency versions); no `mc` source file is altered. The rebuilt
  binary reports version `SANDBOX.PATCHED`. [`mc/README.md`](mc/README.md) explains
  the patch and how to rebuild.
- What ships in the image: `/usr/local/bin/mc`, the AGPL-3.0 text at
  `/usr/share/licenses/minio-mc/LICENSE`, a `SOURCE` file at
  `/usr/share/licenses/minio-mc/SOURCE` naming the upstream commit and the patch,
  and a copy of the patch itself in the same directory.

Because the binary is modified, anyone who distributes the Control Plane image (or a
derivative) conveys a modified AGPL-3.0 work and must offer the Corresponding Source
under AGPL-3.0 sections 6 and 13. This repository satisfies that by pointing to the
exact upstream commit plus the checked-in patch, which together reproduce the binary;
distributors must keep that information (or an equivalent offer) with the image.
The obligation belongs to whoever distributes the image, not to users who only run
it. Replacing `mc` with another client is acceptable when the documented
object-storage operations are preserved.

This notice explains combined distribution; it does not relicense third-party code.
The repository may use MinIO Client as an S3 protocol client, but no deployment
profile runs MinIO Server. The local managed object store is Ceph RGW.
Each tag release generates `licenses.json`, `licenses.md`, and per-image CycloneDX
SBOMs from the resolved graph. Review UNKNOWN entries before publishing; this table
records special obligations but does not replace those inventories.
