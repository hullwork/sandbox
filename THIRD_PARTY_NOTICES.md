# Third-party notices

The repository source is released under the [MIT License](LICENSE). Container images
built from this repository include third-party components under their own licenses.

| Component | License | Distribution note |
| --- | --- | --- |
| `psycopg` | LGPL-3.0-only | Control Plane image, PostgreSQL backend (`control_plane/requirements.lock`). Used unmodified as a separately replaceable wheel, which is how the LGPL relinking condition is met; anyone distributing the image must carry this notice and the LGPL-3.0 text with it. |
| `psycopg-binary` | LGPL-3.0-only; bundles `libpq` (PostgreSQL License) and OpenSSL (Apache-2.0) | Same image. The binary wheel statically bundles libpq and OpenSSL, so their notices travel with the image as well. |
| `boto3`, `botocore`, `s3transfer` | Apache-2.0 | Control Plane image, every object-store operation. The `NOTICE` files ship inside the wheels. |
| `PyMySQL` | MIT | Control Plane image, MySQL backend. |
| Other Python packages and base images | Their respective permissive licenses | See lockfiles, base-image metadata, and container build provenance for exact versions. `tests/test_third_party_notices.py` fails when a locked package outside the permissive set is missing from this table. |

## MinIO Client (`mc`) — removed 2026-09-02

No image built from this repository contains MinIO Client. Every object-store
operation -- checkpoints, imports, exports, tickets, garbage collection -- now goes
through `boto3` (Apache-2.0) in process, so nothing in the Control Plane image is
under a strong-copyleft licence.

This entry stays as a record rather than being deleted. Control Plane images built
from earlier revisions do contain a modified `mc`, which is AGPL-3.0-or-later by
MinIO, Inc., and whoever distributes such an image conveys a modified AGPL-3.0 work
and owes the Corresponding Source under sections 6 and 13. The material that
satisfies that obligation is in this repository's history: upstream commit
`77f82e18b5401a65958f1619df6ebb994634bd88` of `https://github.com/minio/mc`, the
tarball SHA-256 `167415edd21bc29f5360943dac64272aa5cda0a39f3070b15cfeca671c43d975`,
and `mc/dependency-overrides.patch` as it stood before this change. The obligation
belongs to whoever distributes such an image, not to users who only run it.

The repository has never run MinIO Server; `mc` was only ever an S3 protocol client,
and the managed object store is Ceph RGW.

Each tag release generates `licenses.json`, `licenses.md`, and per-image CycloneDX
SBOMs from the resolved graph. Review UNKNOWN entries before publishing; this table
records special obligations but does not replace those inventories.
