"""gVisor Runtime Driver backed by Kubernetes RuntimeClass."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import Protocol

from .. import manifests
from ..kube import KubeError

from control_plane.runtime_driver import (
    RuntimeDriverCapabilities,
    RuntimeDriverError,
    RuntimeDriverErrorCode,
    RuntimeInstance,
    RuntimeSpec,
    RuntimeUsage,
)


class KubernetesClient(Protocol):
    def list(
        self,
        namespace: str,
        resource: str,
        *,
        label_selector: str = "",
    ) -> list[dict]: ...

    def list_group(
        self,
        namespace: str,
        group: str,
        version: str,
        resource: str,
        *,
        label_selector: str = "",
    ) -> list[dict]: ...

    def get(self, namespace: str, resource: str, name: str) -> dict: ...

    def create_or_get(
        self,
        namespace: str,
        resource: str,
        name: str,
        manifest: dict,
    ) -> dict: ...

    def patch_annotations(
        self,
        namespace: str,
        resource: str,
        name: str,
        annotations: dict[str, str],
    ) -> dict: ...

    def delete(self, namespace: str, resource: str, name: str) -> None: ...


@dataclass(frozen=True)
class GVisorRuntimeDriver:
    """Encapsulate all provider-specific Kubernetes Runtime operations."""

    kube: KubernetesClient
    settings: manifests.ManifestSettings
    port: int = 8080

    @property
    def capabilities(self) -> RuntimeDriverCapabilities:
        return RuntimeDriverCapabilities(
            driver="gvisor",
            isolation=(
                "sandboxed-kernel"
                if self.settings.runtime_class
                else "cluster-default"
            ),
            isolation_profile=self.settings.runtime_class or "cluster-default",
        )

    def resource_name(self, runtime_id: str) -> str:
        return self.settings.runtime_name(runtime_id)

    def endpoint(self, runtime_id: str) -> str:
        name = self.resource_name(runtime_id)
        return (
            f"http://{name}.{self.settings.workload_namespace}"
            f".svc.cluster.local:{self.port}"
        )

    @staticmethod
    def _error(exc: KubeError) -> RuntimeDriverError:
        code = {
            HTTPStatus.NOT_FOUND: RuntimeDriverErrorCode.NOT_FOUND,
            HTTPStatus.FORBIDDEN: RuntimeDriverErrorCode.FORBIDDEN,
            HTTPStatus.CONFLICT: RuntimeDriverErrorCode.CONFLICT,
            HTTPStatus.TOO_MANY_REQUESTS: RuntimeDriverErrorCode.CAPACITY,
            HTTPStatus.SERVICE_UNAVAILABLE: RuntimeDriverErrorCode.UNAVAILABLE,
            HTTPStatus.BAD_GATEWAY: RuntimeDriverErrorCode.UNAVAILABLE,
            HTTPStatus.GATEWAY_TIMEOUT: RuntimeDriverErrorCode.UNAVAILABLE,
        }.get(exc.status, RuntimeDriverErrorCode.UNKNOWN)
        return RuntimeDriverError(code, str(exc), status=int(exc.status))

    @staticmethod
    def _int_annotation(annotations: dict, name: str) -> int | None:
        try:
            value = int(annotations.get(name, ""))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _cpu_millicores(value: object) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        units = {"n": Decimal("0.000001"), "u": Decimal("0.001"), "m": Decimal(1)}
        suffix = value[-1] if value[-1] in units else ""
        raw = value[:-1] if suffix else value
        try:
            amount = Decimal(raw) * (units[suffix] if suffix else Decimal(1000))
        except InvalidOperation:
            return None
        return max(0, int(amount))

    @staticmethod
    def _memory_bytes(value: object) -> int | None:
        if not isinstance(value, str) or not value:
            return None
        units = {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "Pi": 1024**5,
            "Ei": 1024**6,
            "k": 1000,
            "K": 1000,
            "M": 1000**2,
            "G": 1000**3,
            "T": 1000**4,
            "P": 1000**5,
            "E": 1000**6,
        }
        raw = value
        multiplier = 1
        for suffix, factor in units.items():
            if value.endswith(suffix):
                raw = value[: -len(suffix)]
                multiplier = factor
                break
        try:
            return max(0, int(Decimal(raw) * multiplier))
        except InvalidOperation:
            return None

    @classmethod
    def _resource_total(
        cls,
        pod: dict,
        category: str,
        resource: str,
    ) -> int:
        parser = cls._cpu_millicores if resource == "cpu" else cls._memory_bytes
        total = 0
        for container in pod.get("spec", {}).get("containers", []):
            value = container.get("resources", {}).get(category, {}).get(resource)
            parsed = parser(value)
            if parsed is not None:
                total += parsed
        return total

    @classmethod
    def _instance(cls, pod: dict) -> RuntimeInstance:
        metadata = pod.get("metadata") or {}
        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        status = pod.get("status") or {}
        spec = pod.get("spec") or {}
        ready = any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in status.get("conditions", [])
        )
        runtime_id = labels.get("convee.io/sandbox-id") or ""
        workspace_id = labels.get("convee.io/workspace-id") or ""
        return RuntimeInstance(
            runtime_id=runtime_id,
            workspace_id=workspace_id,
            provider_id=metadata.get("name") or runtime_id,
            state="running" if ready else str(status.get("phase") or "unknown").lower(),
            ready=ready,
            isolation=str(spec.get("runtimeClassName") or "cluster-default"),
            template_id=labels.get("convee.io/template", "default"),
            tenant_id=labels.get("convee.io/tenant"),
            created_at=annotations.get("convee.io/created-at"),
            expires_at=cls._int_annotation(annotations, "convee.io/expires-at"),
            hard_expires_at=cls._int_annotation(
                annotations, "convee.io/hard-expires-at"
            ),
            message=status.get("message"),
            node=spec.get("nodeName"),
            restarts=sum(
                int(item.get("restartCount", 0))
                for item in status.get("containerStatuses", [])
            ),
            cpu_request_millicores=cls._resource_total(pod, "requests", "cpu"),
            cpu_limit_millicores=cls._resource_total(pod, "limits", "cpu"),
            memory_request_bytes=cls._resource_total(pod, "requests", "memory"),
            memory_limit_bytes=cls._resource_total(pod, "limits", "memory"),
        )

    @classmethod
    def _usage(cls, metric: dict) -> RuntimeUsage:
        cpu = 0
        memory = 0
        cpu_seen = False
        memory_seen = False
        for container in metric.get("containers", []):
            usage = container.get("usage") or {}
            parsed_cpu = cls._cpu_millicores(usage.get("cpu"))
            parsed_memory = cls._memory_bytes(usage.get("memory"))
            if parsed_cpu is not None:
                cpu += parsed_cpu
                cpu_seen = True
            if parsed_memory is not None:
                memory += parsed_memory
                memory_seen = True
        return RuntimeUsage(
            provider_id=(metric.get("metadata") or {}).get("name") or "",
            cpu_usage_millicores=cpu if cpu_seen else None,
            memory_usage_bytes=memory if memory_seen else None,
        )

    def list_runtimes(self) -> list[RuntimeInstance]:
        try:
            pods = self.kube.list(
                self.settings.workload_namespace,
                "pods",
                label_selector="app.kubernetes.io/name=sandbox-runtime",
            )
        except KubeError as exc:
            raise self._error(exc) from exc
        return [self._instance(pod) for pod in pods]

    def list_for_workspace(self, workspace_id: str) -> list[RuntimeInstance]:
        try:
            pods = self.kube.list(
                self.settings.workload_namespace,
                "pods",
                label_selector=(
                    "app.kubernetes.io/name=sandbox-runtime,"
                    f"convee.io/workspace-id={workspace_id}"
                ),
            )
        except KubeError as exc:
            raise self._error(exc) from exc
        return [self._instance(pod) for pod in pods]

    def list_runtime_metrics(self) -> list[RuntimeUsage]:
        try:
            metrics = self.kube.list_group(
                self.settings.workload_namespace,
                "metrics.k8s.io",
                "v1beta1",
                "pods",
                label_selector="app.kubernetes.io/name=sandbox-runtime",
            )
        except KubeError as exc:
            raise self._error(exc) from exc
        return [self._usage(metric) for metric in metrics]

    def get_runtime(self, runtime_id: str) -> RuntimeInstance:
        try:
            pod = self.kube.get(
                self.settings.workload_namespace,
                "pods",
                self.resource_name(runtime_id),
            )
        except KubeError as exc:
            raise self._error(exc) from exc
        return self._instance(pod)

    def create_runtime(self, spec: RuntimeSpec) -> RuntimeInstance:
        name = self.resource_name(spec.runtime_id)
        try:
            pod = self.kube.create_or_get(
                self.settings.workload_namespace,
                "pods",
                name,
                manifests.runtime_pod_manifest(
                    self.settings,
                    spec.runtime_id,
                    spec.workspace_id,
                    spec.template_id,
                    spec.tenant_id,
                ),
            )
        except KubeError as exc:
            raise self._error(exc) from exc
        return self._instance(pod)

    def ensure_endpoint(self, runtime_id: str) -> None:
        name = self.resource_name(runtime_id)
        try:
            self.kube.create_or_get(
                self.settings.workload_namespace,
                "services",
                name,
                manifests.runtime_service_manifest(self.settings, runtime_id),
            )
        except KubeError as exc:
            raise self._error(exc) from exc

    def touch_runtime(self, runtime_id: str, expires_at: int) -> RuntimeInstance:
        try:
            pod = self.kube.patch_annotations(
                self.settings.workload_namespace,
                "pods",
                self.resource_name(runtime_id),
                {"convee.io/expires-at": str(expires_at)},
            )
        except KubeError as exc:
            raise self._error(exc) from exc
        return self._instance(pod)

    def delete_endpoint(self, runtime_id: str) -> None:
        try:
            self.kube.delete(
                self.settings.workload_namespace,
                "services",
                self.resource_name(runtime_id),
            )
        except KubeError as exc:
            raise self._error(exc) from exc

    def delete_runtime(self, runtime_id: str) -> None:
        # Endpoint first preserves the existing fail-conservative teardown
        # order: no new request should be routed while compute is disappearing.
        self.delete_endpoint(runtime_id)
        try:
            self.kube.delete(
                self.settings.workload_namespace,
                "pods",
                self.resource_name(runtime_id),
            )
        except KubeError as exc:
            raise self._error(exc) from exc
