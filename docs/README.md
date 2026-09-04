# Documentation index

Sandbox Platform provides an execution-as-a-service boundary for agents: a Control Plane, gVisor Runtime, workspace volume services, official Python SDK, and MCP bridge. Start with the [README](../README.md), then use the documents below.

The project homepage is the zero-build static site in [index.html](index.html).
GitHub Pages serves this directory from `main:/docs`; assets stay repository-local
so the public site has no runtime dependency on a separate project or frontend build.

| Document | Audience | Use it for |
| --- | --- | --- |
| [README](../README.md) | All users | Product scope, quick start, architecture summary, and current status |
| [Architecture](ARCHITECTURE.md) | Contributors and reviewers | Process boundaries, module map, invariants, and split debt |
| [Platform capability contract](PLATFORM_CONTRACT.md) | Platform engineers | CNI, database, object storage, CSI, and cloud adapter interfaces |
| [Deployment and validation](DEPLOYMENT.md) | Operators | Local integration, cloud deployment, console checks, and E2E |
| [Troubleshooting](TROUBLESHOOTING.md) | Operators and first-time users | Symptom, cause, check, and fix for common local-profile and credential failures |
| [Authentication contract](AUTH.md) | Integrators and client authors | Sign-in methods, API key lifecycle, acting for a subject, errors, and what is stable |
| [HTTP and SDK contract](API.md) | Integrators | Authentication groups, API areas, and SDK surface |
| [Usage](USAGE.md) | SDK, CLI, MCP, and console users | The four client surfaces and the tasks that come up once something is running |
| [System specifications](SYSTEM_SPECIFICATIONS.md) | Evaluators | Defaults, limits, storage modes, and unsupported capabilities |
| [Reproducible benchmarks](BENCHMARKS.md) | Evaluators | Raw sample format, environment evidence, and predeclared excellent thresholds |
| [Formal benchmark report (2026-09-01)](BENCHMARK_REPORT_2026-09-01.md) | Evaluators | Five-run measured results, evidence paths, test profile, and limitations |
| [Compatibility](COMPATIBILITY.md) | Operators and releasers | Automated build and deployment targets |
| [Configuration](CONFIGURATION.md) | Operators | Secrets, environment settings, and startup behavior |
| [Security model](SECURITY_MODEL.md) | Security reviewers | Assets, trust boundaries, threats, assumptions, and verification |
| [Lifecycle and data](LIFECYCLE_AND_DATA.md) | Operators | Runtime/workspace state, persistence, backup, deletion, and recovery |
| [Production guide](PRODUCTION.md) | Platform teams | Required infrastructure, release gates, monitoring, upgrade, and rollback |
| [Release policy](RELEASE.md) | Maintainers | Versioning, artifacts, compatibility, and support |
| [Supply chain](SUPPLY_CHAIN.md) | Maintainers | CI, SBOM, signing, provenance, scanning, and licenses |
| [Architecture decisions](adr/README.md) | Contributors and reviewers | Durable design constraints and their consequences |
| [Contributing](../CONTRIBUTING.md) | Contributors | Development setup, validation, and review expectations |
| [Governance](../GOVERNANCE.md) and [maintainers](../MAINTAINERS.md) | Contributors | Decision ownership, review, and maintainer changes |
| [Changelog](../CHANGELOG.md) | Users and releasers | User-visible changes and release history |
| [Security policy](../SECURITY.md) | All users | Vulnerability reporting, supported versions, and isolation boundaries |
| [Support](../SUPPORT.md) | All users | Bug, question, and security routing |
| [Code of conduct](../CODE_OF_CONDUCT.md) | Community | Behavior expectations and reporting path |
| [Third-party notices](../THIRD_PARTY_NOTICES.md) | Distributors | Source and container-image licensing boundaries |
| [Roadmap](../ROADMAP.md) | All users | Public-release blockers and later candidates |

## Documentation rules

- Keep execution and file-access examples inside Sandbox APIs; never document host fallback as acceptable agent behavior.
- State the required identity type, object ownership, quota, node label, storage class, and failure behavior for operational procedures.
- Token examples must use files or environment placeholders, never plausible live credentials.
- Changes to Control Plane REST, MCP tools, workspace lifecycle, storage ownership, or Kubernetes security require README and contract updates in the same pull request.
