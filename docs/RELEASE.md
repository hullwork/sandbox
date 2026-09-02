# Release and compatibility policy

Sandbox Platform uses Semantic Versioning after the first public release. During `0.x`,
minor releases may contain breaking API or deployment changes, which must be called
out in release notes and migration instructions.

Each release should publish:

- an annotated Git tag and GitHub release with changes, risks, migrations, and known
  limitations;
- a Python sdist and wheel containing the SDK, MCP bridge, and operator CLI, uploaded
  to PyPI as [`sandbox-platform`](https://pypi.org/project/sandbox-platform/);
- multi-architecture, digest-pinned container images for Runtime, file service,
  Control Plane, and console;
- a rendered Kubernetes manifest whose four component references use those exact
  GHCR digests and whose pull policies permit registry deployment;
- checksums, SBOMs, provenance attestations, vulnerability-scan results, and the
  source commit used to build every artifact;
- an API/deployment compatibility table and exact E2E evidence.

Checked-in local image tags may identify component build lines independently. A
published release uses one repository version for the Python artifacts, all four
multi-architecture images, and the digest-pinned deployment manifest. Mixed
component versions remain unsupported unless a future compatibility suite states
otherwise.

The repository currently has no stable-release compatibility promise. Until a matrix
is published, deploy Control Plane, Runtime, file service, console, manifests, SDK, and MCP
from the same release tag. `main` is development state, not a release channel.

Supported security fixes target the newest release and current `main`. See
`SECURITY.md` for private reporting.

## Before the repository goes public

Each of these is a GitHub setting or an external registration that no workflow
can create, and most of them fail silently when forgotten: the links keep
rendering, they just lead nowhere. Do them in this order, on the day the
repository is made public, before the first tag.

1. **Private vulnerability reporting.** Turn it on under **Settings -> Code
   security -> Private vulnerability reporting**. It only exists for public
   repositories and is off by default, and `SECURITY.md`, `SUPPORT.md` and both
   contact links in `.github/ISSUE_TEMPLATE/config.yml` send people to
   `security/advisories/new`. With blank issues disabled in that same file, a
   reporter who finds a `404` there has no channel at all. Confirm with
   `gh api repos/hullwork/sandbox/private-vulnerability-reporting`, which must
   answer `"enabled": true`.
2. **Old repository names.** Confirm that the repositories this code lived in
   before the rename are deleted, not merely redirecting. GitHub redirects an
   old name indefinitely, so a fork, CI cache or clone made under it keeps
   working and keeps pointing at whatever history it had. `gh repo view
   <old-owner>/<old-name>` must fail.
3. **CODEOWNERS.** `.github/CODEOWNERS` must name a team or an account that
   exists in the `hullwork` organisation with write access; an unresolvable
   owner is ignored without a warning, and review requests stop being
   assigned. Remove paths that no longer exist. The **Settings -> Code
   review** page reports CODEOWNERS syntax errors.
4. **Branch protection on `main`.** Require a pull request, require the CI
   check, dismiss stale approvals, and block force pushes and deletion. The
   release workflow refuses non-`main` commits; nothing else refuses a direct
   push to `main`.
5. **PyPI name.** `sandbox-platform` is unregistered on PyPI at the time of
   writing and registration is first come, first served. Create the pending
   publisher described in [One-time PyPI setup](#one-time-pypi-setup) before
   announcing anything, so the first tag claims the name rather than
   discovering someone else did.
6. **Secret and history audit.** Run the full-history secret scan one last
   time from a fresh clone and check `git log --all --format=%ae | sort -u`
   lists only the intended author addresses. Force-pushing a rewritten
   history after the repository is public is a breaking change for every fork.

## Maintainer procedure

1. Make the repository public, set `project.version` in `pyproject.toml`, update
   release notes and compatibility evidence, and pass the Lima/kubeadm gVisor E2E.
2. Merge the release commit to `main`, then create and push an annotated `vX.Y.Z`
   tag whose value exactly matches the Python project version. The release workflow
   reruns full CI and rejects private repositories, lightweight tags, non-`main`
   commits, and version mismatches.
3. The workflow creates a draft GitHub Release. Confirm anonymous pulls of the four
   GHCR digests plus the rendered manifest, Python artifacts, checksums, CycloneDX
   files, vulnerability reports, merged license inventory, Cosign signatures, and
   GitHub attestations before publishing it.
4. Approve the `pypi` environment when the `pypi-publish` job requests it. That
   approval is the last decision of the release and the only irreversible one: PyPI
   never lets a version number be reused, even after the file is deleted. Everything
   needed to judge it - the draft release, the promoted image digests, the scan
   reports - already exists when the request arrives.
5. Follow [Supply Chain](SUPPLY_CHAIN.md) to independently verify a downloaded Python
   artifact and every deployed image digest.

## One-time PyPI setup

Publishing uses PyPI Trusted Publishing, so the repository holds no PyPI credential
of any kind. Two things have to exist before the first tag, and neither can be
created from a workflow. Both are on top of making the repository public, which
step 1 above already requires and the release workflow already refuses to proceed
without.

First, a GitHub Environment named `pypi` in **Settings -> Environments**. Add the
maintainers as required reviewers, and restrict its deployment branches and tags to
the tag pattern `v*`. The environment is what makes the upload wait for a person,
and naming it in the publisher below is what stops a workflow run outside it from
minting a PyPI identity at all.

Second, a *pending publisher* on PyPI under **Your projects -> Publishing** (use
<https://pypi.org/manage/account/publishing/> while the project does not exist yet).
Fill it in with exactly these values:

| Field | Value |
| --- | --- |
| PyPI Project Name | `sandbox-platform` |
| Owner | `hullwork` |
| Repository name | `sandbox` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The repository name is the field that goes wrong quietly. PyPI matches it against the
`repository` claim in the OpenID Connect token GitHub mints, and that claim carries
the repository's name **at the moment the workflow runs** - not a name it used to
have. So `sandbox` here has to track the repository, and anyone renaming the
repository has to update this publisher in the same change. A mismatch fails the
token exchange with a message about an unmatched claim, which does not read as "the
repository was renamed".

Checking that from a clone is worth one warning, because the obvious command cannot
answer it: `git remote get-url origin` reports whatever string the clone was created
with, and GitHub keeps redirecting an old name indefinitely, so a stale remote fetches
and pushes perfectly while naming a repository that no longer exists under that name.
Ask GitHub instead:

```sh
gh api "repos/$OWNER/$NAME" --jq .full_name
```

which resolves redirects and answers with the current name.

Nothing needs to be registered on TestPyPI. `sandbox-platform` there belongs to an
unrelated project, so the name cannot be claimed for a rehearsal; the metadata
failures a rehearsal would find are checked instead by `twine check --strict` in the
build job, on every tag.

`scripts/check-wheel-surface.py` runs in CI, in the release build, and again on the
bytes about to be uploaded. It refuses any wheel whose top-level entries are not
exactly `sandbox_platform`, because those names are installed into a shared
`site-packages` and collide globally with every other distribution that claims one.
