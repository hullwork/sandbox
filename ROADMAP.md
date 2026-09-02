# Roadmap

This is directional, not a delivery commitment.

## Before the first public release

- Publish the first signed tag and attach the reviewed compatibility evidence through
  the configured release workflow.
- Run and publish repeatable gVisor E2E and cross-tenant isolation evidence.
- Review generated third-party license evidence and complete the one-time
  full-history secret audit before making the repository public.

## Later candidates

- Operator/Helm lifecycle, migrations, backup/restore automation, and SLO dashboards.
- Stronger egress controls, credential brokering, policy-driven Runtime templates,
  and additional storage providers.
- Snapshot-like developer ergonomics while preserving honest checkpoint semantics.

New API surface must be defined by this repository's own versioned contracts and
executable acceptance tests.
