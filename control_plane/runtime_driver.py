"""Runtime-provider contract owned by the Sandbox Control Plane.

The first implementation is gVisor on Kubernetes.  The contract deliberately
contains only capabilities the platform supports today: create, inspect,
touch, expose, and delete.  Suspend, resume, and snapshot operations are not
stubbed out because advertising an unimplemented lifecycle verb would let the
control plane accept requests it cannot complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True)
class RuntimeDriverCapabilities:
    """Stable facts used for admission and operator diagnostics."""

    driver: str
    isolation: str
    isolation_profile: str
    exec: bool = True
    streaming: bool = True
    pty: bool = True
    file_tools: bool = True


@dataclass(frozen=True)
class RuntimeSpec:
    """Provider-neutral request passed from orchestration to a driver."""

    runtime_id: str
    workspace_id: str
    template_id: str = "default"
    tenant_id: str | None = None


class RuntimeDriverErrorCode(StrEnum):
    """Provider-neutral failure categories understood by orchestration."""

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    CAPACITY = "capacity"
    UNKNOWN = "unknown"


class RuntimeDriverError(RuntimeError):
    def __init__(
        self,
        code: RuntimeDriverErrorCode,
        message: str,
        *,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class RuntimeInstance:
    """Provider-neutral Runtime state consumed by policy and API layers."""

    runtime_id: str
    workspace_id: str
    provider_id: str
    state: str
    ready: bool
    isolation: str
    template_id: str = "default"
    tenant_id: str | None = None
    created_at: str | None = None
    expires_at: int | None = None
    hard_expires_at: int | None = None
    message: str | None = None
    node: str | None = None
    restarts: int = 0
    cpu_request_millicores: int = 0
    cpu_limit_millicores: int = 0
    memory_request_bytes: int = 0
    memory_limit_bytes: int = 0


@dataclass(frozen=True)
class RuntimeUsage:
    """Provider-neutral, optional live resource usage for one Runtime."""

    provider_id: str
    cpu_usage_millicores: int | None = None
    memory_usage_bytes: int | None = None


class RuntimeDriver(Protocol):
    """Infrastructure operations required by the current control plane.

    Drivers own provider-specific resource names, manifests, endpoints, and
    Kubernetes calls.  Quota, ownership, tokens, and lifecycle state remain
    control-plane responsibilities and must not leak into a driver.
    """

    @property
    def capabilities(self) -> RuntimeDriverCapabilities: ...

    def resource_name(self, runtime_id: str) -> str: ...

    def endpoint(self, runtime_id: str) -> str: ...

    def list_runtimes(self) -> list[RuntimeInstance]: ...

    def list_for_workspace(self, workspace_id: str) -> list[RuntimeInstance]: ...

    def list_runtime_metrics(self) -> list[RuntimeUsage]: ...

    def get_runtime(self, runtime_id: str) -> RuntimeInstance: ...

    def create_runtime(self, spec: RuntimeSpec) -> RuntimeInstance: ...

    def ensure_endpoint(self, runtime_id: str) -> None: ...

    def touch_runtime(self, runtime_id: str, expires_at: int) -> RuntimeInstance: ...

    def delete_endpoint(self, runtime_id: str) -> None: ...

    def delete_runtime(self, runtime_id: str) -> None: ...
