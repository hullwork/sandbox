## Summary

<!-- What changed, and why is this the smallest useful change? -->

## Risk and boundaries

<!-- Cover workspace ownership, credentials, gVisor, storage, networking, and fail-closed behavior when relevant. -->

## Validation

- [ ] `make test`
- [ ] Console checks when `console/` changed
- [ ] Shell and Kustomize checks when deployment files changed
- [ ] Live gVisor E2E evidence for cluster-facing behavior, or an explanation of why it is unchanged
- [ ] API/OpenAPI, SDK, MCP, and documentation remain synchronized

## Release and migration impact

<!-- State compatibility, configuration, migration, rollback, and documentation impact. Use "none" when not applicable. -->
