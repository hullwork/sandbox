#!/usr/bin/env python3
"""Pure Kubernetes manifest generation for sandbox runtime resources."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Callable
import time


@dataclass(frozen=True)
class ManifestSettings:
    workload_namespace: str
    workspace_pvc: str
    workspace_storage_mode: str
    runtime_class: str
    runtime_node_selector: dict[str, str]
    runtime_tolerations: tuple[dict[str, object], ...]
    runtime_ttl_seconds: int
    runtime_hard_ttl_seconds: int
    runtime_name: Callable[[str], str]
    template_image: Callable[[str, str | None], str]
    #: (kind, subject) -> the capability key that one sandbox instance verifies
    #: with, and the epoch it was derived under. Two callables rather than one
    #: pair-returning callable so the manifest layer stays free of any opinion
    #: about how either value is produced.
    capability_key: Callable[[str, str], str]
    capability_epoch: Callable[[str, str], int]


def _workspace_mount(
    settings: ManifestSettings, workspace_id: str
) -> dict[str, Any]:
    """Return the Runtime Pod mount description for /workspace.

    Production mounts the per-workspace PVC directly (volume name equals the
    workspace ID, with no subPath). The single-node RWO development overlay uses
    one shared PVC and the workspace ID as its subPath.
    """
    mount: dict[str, Any] = {"name": "workspaces", "mountPath": "/workspace"}
    if settings.workspace_storage_mode == "shared":
        mount["subPath"] = workspace_id
    return mount

__all__ = (
    "ManifestSettings",
    "runtime_pod_manifest",
    "runtime_service_manifest",
    "workload_health_probes",
)


def workload_health_probes(port: int, timeout: int = 3) -> dict[str, dict[str, Any]]:
    """Configuration items: Three health probes for sandbox workloads.

    AI-LOCK: Must be httpGet, **Do not change back to exec**. exec type probes must be inside the container every time
        Start a Python interpreter and import urllib; under gVisor + `cpu: 500m` limit,
        Once the PTY session is still running in the container, starting +import will exceed timeoutSeconds.
        The kubelet then kills the container and restarts it. Measured: no Runtime holding a PTY session
        survived more than one minute (SIGTERM at T+48s), while /healthz kept returning 200 during the same period.
        ——The service is healthy, and it is the probe itself that times out. Use the unmodified old image for comparison. At the same time point,
        The same exit code confirms that it is a problem with the probe form itself.

        httpGet is initiated directly by kubelet with no interpreter overhead in
        the container. Production CNIs must allow narrowly scoped node probe
        traffic; see the header of k8s/network-policy.yaml.

        Runtime MCP (8080) keeps 3s - its probe behaviour was measured and tuned, and the window under PTY load
        verified. The file tool has been merged into the same process, so there is no longer a second set of probes."""
    probe = {"httpGet": {"path": "/healthz", "port": port}}
    return {
        "startupProbe": {
            **probe,
            # Probe promptly so the control plane does not add up to five
            # seconds of avoidable latency after the Runtime is already
            # serving. Keep the original 60 second failure window below.
            "periodSeconds": 1,
            "timeoutSeconds": 3,
            "failureThreshold": 60,
        },
        "readinessProbe": {
            **probe,
            "periodSeconds": 1,
            "timeoutSeconds": 3,
            "failureThreshold": 30,
        },
        "livenessProbe": {
            **probe,
            "periodSeconds": 10,
            "timeoutSeconds": 3,
            "failureThreshold": 6,
        },
    }



def runtime_service_manifest(settings: ManifestSettings, sandbox_id: str) -> dict:
    name = settings.runtime_name(sandbox_id)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": settings.workload_namespace,
            "labels": {
                "app.kubernetes.io/name": "sandbox-runtime",
                "convee.io/sandbox-id": sandbox_id,
                "app.kubernetes.io/managed-by": "sandbox-control-plane",
            },
        },
        "spec": {
            "selector": {
                "app.kubernetes.io/name": "sandbox-runtime",
                "convee.io/sandbox-id": sandbox_id,
            },
            "ports": [
                {
                    "name": "mcp",
                    "port": 8080,
                    "targetPort": "mcp",
                },
            ],
        },
    }


def runtime_pod_manifest(
    settings: ManifestSettings,
    sandbox_id: str,
    workspace_id: str,
    template_id: str = "default",
    tenant_id: str | None = None,
) -> dict:
    """Core function: Generate Runtime Pod spec.

    Constraints: The image can only be obtained by looking up the template_image table, and the parameter is template_id instead of
         image - No layer in the call chain can bypass the registry and insert any image.
         The validity of template_id is verified by the caller at entry (TEMPLATE_ID + existence).
         tenant_id determines which subset of the templates in the store is visible to this creation; default None = only the
         built-in global templates, identical to the behaviour before the template store was introduced."""
    name = settings.runtime_name(sandbox_id)
    created_at = int(time.time())
    labels = {
        "app.kubernetes.io/name": "sandbox-runtime",
        "convee.io/sandbox-id": sandbox_id,
        "convee.io/workspace-id": workspace_id,
        # Complete label instead of annotation: sandbox_view should read it back to tell the caller what is actually used
        # Which template (aligned with create's method of returning actual specifications), the list can also be filtered by template.
        "convee.io/template": template_id,
        "app.kubernetes.io/managed-by": "sandbox-control-plane",
    }
    # Single tenant mode (tenant_id=None) does not even write the key, it is the same as the runtimeClassName
    # "Don't write if not configured" semantics. The purpose is to count the per-tenant Runtime quota - Runtime
    # Without falling into the database, counting by tenant can only rely on label selector.
    # Constraints: The value range is consistent with store.TENANT_ID (lowercase alphanumeric plus hyphen, ≤32), originally
    # Legal label value, no need to encode.
    if tenant_id:
        labels["convee.io/tenant"] = tenant_id
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": settings.workload_namespace,
            "labels": labels,
            "annotations": {
                "convee.io/created-at": str(created_at),
                # Absolute upper limit, nailed on creation. touch_runtime only pushes expires-at,
                # Never touch this one - it's the ceiling for how long an active review can last.
                "convee.io/hard-expires-at": str(
                    created_at + settings.runtime_hard_ttl_seconds
                ),
                "convee.io/expires-at": str(
                    created_at + settings.runtime_ttl_seconds
                ),
            },
        },
        "spec": {
            # When left empty, even the key will not appear. See the description at SANDBOX_RUNTIME_CLASS.
            **(
                {"runtimeClassName": settings.runtime_class}
                if settings.runtime_class
                else {}
            ),
            # The same set of "key is not written if not configured" semantics of SANDBOX_RUNTIME_NODE_SELECTOR.
            **({"nodeSelector": dict(settings.runtime_node_selector)}
               if settings.runtime_node_selector else {}),
            **(
                {"tolerations": [dict(item) for item in settings.runtime_tolerations]}
                if settings.runtime_tolerations else {}
            ),
            "restartPolicy": "Always",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "fsGroup": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "shell-mcp",
                    "image": settings.template_image(template_id, tenant_id),
                    "imagePullPolicy": "IfNotPresent",
                    "env": [
                        {"name": "SANDBOX_ID", "value": sandbox_id},
                        {"name": "WORKSPACE_ID", "value": workspace_id},
                        {
                            "name": "SANDBOX_CAPABILITY_KEY",
                            "value": settings.capability_key("runtime", sandbox_id),
                        },
                        {
                            "name": "SANDBOX_CAPABILITY_EPOCH",
                            "value": str(
                                settings.capability_epoch("runtime", sandbox_id)
                            ),
                        },
                        {
                            "name": "WORKSPACE_CAPABILITY_KEY",
                            "value": settings.capability_key(
                                "workspace", workspace_id
                            ),
                        },
                        {
                            "name": "WORKSPACE_CAPABILITY_EPOCH",
                            "value": str(
                                settings.capability_epoch("workspace", workspace_id)
                            ),
                        },
                    ],
                    "ports": [{"name": "mcp", "containerPort": 8080}],
                    "resources": {
                        "requests": {"cpu": "25m", "memory": "128Mi"},
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    **workload_health_probes(8080),
                    "volumeMounts": [
                        _workspace_mount(settings, workspace_id),
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                },
            ],
            "volumes": [
                {
                    "name": "workspaces",
                    # Production uses the per-workspace claim. The RWO
                    # development overlay selects one shared claim + subPath.
                    "persistentVolumeClaim": {
                        "claimName": (
                            settings.workspace_pvc
                            if settings.workspace_storage_mode == "shared"
                            else workspace_id
                        )
                    },
                },
                # Runtime MCP also spools checkpoint archives here.
                {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
            ],
        },
    }
