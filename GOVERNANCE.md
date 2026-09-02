# Governance

Sandbox Platform is currently maintained under a maintainer-led model. Maintainers are
listed in [MAINTAINERS.md](MAINTAINERS.md) and are responsible for release integrity,
security response, compatibility decisions, and enforcement of the documented
runtime boundary.

## Decisions

- Small, reversible changes are decided through normal pull-request review.
- Public API, storage, identity, isolation, or compatibility changes must describe
  alternatives, migration, rollback, and security impact.
- Long-lived architectural constraints belong in `docs/adr/`; implementation
  history that is no longer needed to understand current code should not accumulate
  in inline comments.
- A maintainer with a conflict of interest should ask another qualified reviewer to
  decide. While the project has only one maintainer, the conflict and rationale must
  be recorded in the pull request.

## Releases

Only maintainers create releases. A release must satisfy [docs/RELEASE.md](docs/RELEASE.md)
and the evidence requirements in [docs/SUPPLY_CHAIN.md](docs/SUPPLY_CHAIN.md).

## Changes to governance

Governance changes use the same public pull-request process as code changes. The
project may adopt a multi-maintainer voting model when sustained contributor volume
makes it useful.
