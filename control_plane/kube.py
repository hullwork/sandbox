"""Small Kubernetes API client using the in-cluster service account."""

from __future__ import annotations

import json
import os
import ssl
from http import HTTPStatus
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import tracing


class KubeError(RuntimeError):
    def __init__(self, status: int, message: str, payload: object | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class KubeClient:
    def __init__(self) -> None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.base_url = f"https://{host}:{port}"
        token_path = os.getenv(
            "KUBERNETES_TOKEN_FILE",
            "/var/run/secrets/kubernetes.io/serviceaccount/token",
        )
        ca_path = os.getenv(
            "KUBERNETES_CA_FILE",
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        )
        with open(token_path, encoding="utf-8") as handle:
            self.token = handle.read().strip()
        self.ssl_context = ssl.create_default_context(cafile=ca_path)

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, str] | None = None,
        *,
        ignore_not_found: bool = False,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        if query:
            path = f"{path}?{urlencode(query)}"
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            **tracing.outbound_headers(),
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=30,
                context=self.ssl_context,
            ) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read()
            try:
                result = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                result = {"message": raw.decode("utf-8", errors="replace")}
            if ignore_not_found and exc.code == HTTPStatus.NOT_FOUND:
                return exc.code, result
            message = (
                result.get("message", exc.reason)
                if isinstance(result, dict)
                else str(result)
            )
            raise KubeError(exc.code, str(message), result) from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise KubeError(
                HTTPStatus.BAD_GATEWAY,
                f"Kubernetes API unavailable: {exc}",
            ) from exc

    @staticmethod
    def namespaced_path(
        namespace: str,
        plural: str,
        name: str | None = None,
    ) -> str:
        path = (
            f"/api/v1/namespaces/{quote(namespace, safe='')}/"
            f"{quote(plural, safe='')}"
        )
        if name:
            path += f"/{quote(name, safe='')}"
        return path

    @staticmethod
    def group_namespaced_path(
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str | None = None,
    ) -> str:
        path = (
            f"/apis/{quote(group, safe='')}/{quote(version, safe='')}/"
            f"namespaces/{quote(namespace, safe='')}/{quote(plural, safe='')}"
        )
        if name:
            path += f"/{quote(name, safe='')}"
        return path

    @staticmethod
    def cluster_path(plural: str, name: str | None = None) -> str:
        path = f"/api/v1/{quote(plural, safe='')}"
        if name:
            path += f"/{quote(name, safe='')}"
        return path

    @staticmethod
    def group_cluster_path(
        group: str,
        version: str,
        plural: str,
        name: str | None = None,
    ) -> str:
        path = (
            f"/apis/{quote(group, safe='')}/{quote(version, safe='')}/"
            f"{quote(plural, safe='')}"
        )
        if name:
            path += f"/{quote(name, safe='')}"
        return path

    def create_or_get(
        self,
        namespace: str,
        plural: str,
        name: str,
        manifest: dict,
    ) -> dict:
        path = self.namespaced_path(namespace, plural)
        try:
            _, result = self.request("POST", path, manifest)
            return result
        except KubeError as exc:
            if exc.status != HTTPStatus.CONFLICT:
                raise
            _, result = self.request(
                "GET",
                self.namespaced_path(namespace, plural, name),
            )
            return result

    def create_group(
        self,
        namespace: str,
        group: str,
        version: str,
        plural: str,
        manifest: dict,
    ) -> dict:
        _, result = self.request(
            "POST",
            self.group_namespaced_path(group, version, namespace, plural),
            manifest,
        )
        return result

    def list_group(
        self,
        namespace: str,
        group: str,
        version: str,
        plural: str,
        *,
        label_selector: str | None = None,
    ) -> list[dict]:
        query = {"labelSelector": label_selector} if label_selector else None
        _, result = self.request(
            "GET",
            self.group_namespaced_path(group, version, namespace, plural),
            query=query,
        )
        return result.get("items", [])

    def list_cluster(self, plural: str) -> list[dict]:
        _, result = self.request("GET", self.cluster_path(plural))
        return result.get("items", [])

    def list_cluster_group(
        self,
        group: str,
        version: str,
        plural: str,
    ) -> list[dict]:
        _, result = self.request(
            "GET", self.group_cluster_path(group, version, plural)
        )
        return result.get("items", [])

    def delete_group(
        self,
        namespace: str,
        group: str,
        version: str,
        plural: str,
        name: str,
    ) -> bool:
        status, _ = self.request(
            "DELETE",
            self.group_namespaced_path(
                group, version, namespace, plural, name
            ),
            {"propagationPolicy": "Background", "gracePeriodSeconds": 0},
            ignore_not_found=True,
        )
        return status != HTTPStatus.NOT_FOUND

    def get(self, namespace: str, plural: str, name: str) -> dict:
        _, result = self.request(
            "GET", self.namespaced_path(namespace, plural, name)
        )
        return result

    def list(
        self,
        namespace: str,
        plural: str,
        *,
        label_selector: str | None = None,
    ) -> list[dict]:
        query = {"labelSelector": label_selector} if label_selector else None
        _, result = self.request(
            "GET",
            self.namespaced_path(namespace, plural),
            query=query,
        )
        return result.get("items", [])

    def patch_annotations(
        self,
        namespace: str,
        plural: str,
        name: str,
        annotations: dict[str, str],
    ) -> dict:
        _, result = self.request(
            "PATCH",
            self.namespaced_path(namespace, plural, name),
            {"metadata": {"annotations": annotations}},
            content_type="application/merge-patch+json",
        )
        return result

    def delete(
        self,
        namespace: str,
        plural: str,
        name: str,
    ) -> bool:
        status, _ = self.request(
            "DELETE",
            self.namespaced_path(namespace, plural, name),
            {"propagationPolicy": "Background", "gracePeriodSeconds": 0},
            ignore_not_found=True,
        )
        return status != HTTPStatus.NOT_FOUND
