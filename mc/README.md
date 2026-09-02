# MinIO Client build inputs

Control Plane calls the MinIO Client (`mc`, AGPL-3.0) as a subprocess for every
object-store operation. The Control Plane image compiles it from a pinned upstream commit
instead of downloading a release binary so that the build is reproducible and its
dependency graph is under this repository's control.

## What is here

- `dependency-overrides.patch`: a unified diff against upstream `mc` at commit
  `77f82e18b5401a65958f1619df6ebb994634bd88`. It touches only `go.mod` and `go.sum`:
  it raises the Go language version (`go 1.25.0`, dropping the `toolchain` line so the
  image's Go 1.26 toolchain is used) and bumps a handful of dependency versions with
  their checksums. No Go source file of `mc` is modified.

## How the image uses it

`control_plane/Dockerfile` stage `minio-client-build`:

1. Downloads `https://github.com/minio/mc/archive/77f82e18b5401a65958f1619df6ebb994634bd88.tar.gz`
   and verifies its SHA-256
   (`167415edd21bc29f5360943dac64272aa5cda0a39f3070b15cfeca671c43d975`).
2. Applies `mc/dependency-overrides.patch` with `patch -p1`.
3. Runs `go mod download && go mod verify` and builds a static binary with
   `-ldflags` that stamp the version as `SANDBOX.PATCHED` and the commit id above.

The final image copies the binary to `/usr/local/bin/mc` and places the license
text, a `SOURCE` file naming the upstream commit and this patch, and a copy of the
patch under `/usr/share/licenses/minio-mc/`.

## Rebuilding or updating

To rebuild only the client stage:

```bash
docker build --target minio-client -t sandbox-minio-client -f control_plane/Dockerfile .
docker run --rm sandbox-minio-client --version
```

To move to a new upstream commit:

1. Change the tarball URL, its SHA-256, and the `CommitID`/`ShortCommitID` values
   in `control_plane/Dockerfile`.
2. Regenerate the patch: extract the new tarball, apply the dependency changes to
   `go.mod`, run `go mod tidy`, then `git diff -- go.mod go.sum > mc/dependency-overrides.patch`.
3. Update the commit and checksum in `THIRD_PARTY_NOTICES.md` and in the `SOURCE`
   step of `control_plane/Dockerfile`.
4. Rebuild the Control Plane image and run the object-store E2E (`scripts/test-object-store.sh`).

Keep the patch limited to dependency metadata. Changing `mc` behavior would widen the
AGPL-3.0 corresponding-source obligation to those changes as well.
