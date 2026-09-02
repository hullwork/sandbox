#!/usr/bin/env bash
# Normalize the environment shared by every cluster-level E2E scenario.

resolve_e2e_environment() {
  SANDBOX_KUBE_CONTEXT="${SANDBOX_KUBE_CONTEXT:-sandbox-local}"
  SANDBOX_CONTROL_PLANE_URL="${SANDBOX_CONTROL_PLANE_URL:-http://127.0.0.1:18080}"
  export SANDBOX_KUBE_CONTEXT SANDBOX_CONTROL_PLANE_URL
}
