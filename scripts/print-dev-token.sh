#!/usr/bin/env bash
set -euo pipefail

SANDBOX_KUBE_CONTEXT="${SANDBOX_KUBE_CONTEXT:-sandbox-local}"

kubectl --context "$SANDBOX_KUBE_CONTEXT" \
  --namespace sandbox-system \
  get secret sandbox-api-credentials \
  --output=jsonpath='{.data.control-plane-token}' \
  | python3 -c \
    'import base64,sys; sys.stdout.write(base64.b64decode(sys.stdin.buffer.read()).decode() + "\n")'
