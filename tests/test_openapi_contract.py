from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}


def load_spec() -> dict:
    return yaml.safe_load((ROOT / "contracts/control-plane-openapi.yaml").read_text(encoding="utf-8"))


def route_auth() -> tuple[tuple[str, str, bool], ...]:
    tree = ast.parse((ROOT / "control_plane/core.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "ROUTE_AUTH":
                return ast.literal_eval(node.value)
    raise AssertionError("control_plane.ROUTE_AUTH not found")


def path_pattern(template: str) -> re.Pattern[str]:
    escaped = re.escape(template)
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped) + "$")


def resolve_ref(spec: dict, ref: str):
    if not ref.startswith("#/"):
        raise AssertionError(f"external OpenAPI ref is not pinned: {ref}")
    value = spec
    for part in ref[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


class OpenApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_spec()

    def operations(self):
        for path, item in self.spec["paths"].items():
            for method, operation in item.items():
                if method in HTTP_METHODS:
                    yield path, method, operation

    def test_route_auth_manifest_is_covered_with_matching_security(self) -> None:
        for method, sample, requires_credentials in route_auth():
            matches = [
                (template, item)
                for template, item in self.spec["paths"].items()
                if path_pattern(template).fullmatch(sample) and method.lower() in item
            ]
            self.assertEqual(len(matches), 1, f"{method} {sample}")
            operation = matches[0][1][method.lower()]
            if requires_credentials:
                self.assertTrue(operation.get("security"), f"{method} {sample}")
            else:
                self.assertEqual(operation.get("security"), [], f"{method} {sample}")

    def test_workspace_resolver_is_dispatched_by_post_handler(self) -> None:
        source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        handler = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ApiHandler"
        )
        methods = {
            node.name: ast.get_source_segment(source, node)
            for node in handler.body
            if isinstance(node, ast.FunctionDef) and node.name in {"do_GET", "do_POST"}
        }
        self.assertNotIn('/v1/workspaces/resolve', methods["do_GET"])
        self.assertIn('/v1/workspaces/resolve', methods["do_POST"])

    def test_every_protected_route_reaches_its_authentication_boundary(self) -> None:
        probe = textwrap.dedent(
            """
            import threading
            import urllib.error
            import urllib.request
            from http.server import ThreadingHTTPServer

            from control_plane import api
            from control_plane import core as control_plane
            server = ThreadingHTTPServer(("127.0.0.1", 0), api.ApiHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            failures = []
            try:
                for method, path, protected in control_plane.ROUTE_AUTH:
                    if not protected:
                        continue
                    body = b"{}" if method in {"POST", "PUT"} else None
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}{path}",
                        method=method,
                        data=body,
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        urllib.request.urlopen(request, timeout=2)
                        status = 200
                    except urllib.error.HTTPError as exc:
                        status = exc.code
                    if status != 401:
                        failures.append((method, path, status))
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
            if failures:
                raise SystemExit(repr(failures))
            """
        )
        environment = {
            **os.environ,
            "SANDBOX_CONTROL_PLANE_ROLE": "volume",
            "SANDBOX_CONTROL_PLANE_TOKEN": "test-token",
            "SIGNING_KEY": "0" * 32,
            "WORKSPACE_ID_KEY": "1" * 32,
            "VOLUME_AGENT_TOKEN": "test-volume-token",
            "OBJECT_STORE_ENDPOINT": "http://127.0.0.1:1",
            "OBJECT_STORE_ACCESS_KEY": "test-access",
            "OBJECT_STORE_SECRET_KEY": "test-secret",
            "PYTHONPATH": str(ROOT),
        }
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_documented_route_is_inside_route_auth(self) -> None:
        covered = set()
        for method, sample, _ in route_auth():
            for template, item in self.spec["paths"].items():
                if path_pattern(template).fullmatch(sample) and method.lower() in item:
                    covered.add((template, method.lower()))
        extras = {
            (path, method)
            for path, method, _ in self.operations()
            if (path, method) not in covered
        }
        self.assertEqual(extras, set())

    def test_operations_have_unique_ids_tags_security_and_success_responses(self) -> None:
        ids: list[str] = []
        for path, method, operation in self.operations():
            self.assertIn("operationId", operation, f"{method} {path}")
            self.assertTrue(operation.get("tags"), f"{method} {path}")
            self.assertIn("security", operation, f"{method} {path}")
            responses = operation.get("responses", {})
            self.assertTrue(
                any(str(code).startswith(("2", "3")) for code in responses),
                f"{method} {path}",
            )
            ids.append(operation["operationId"])
        self.assertEqual(len(ids), len(set(ids)), "operationId values must be unique")

    def test_every_path_variable_has_a_declared_parameter(self) -> None:
        for path, item in self.spec["paths"].items():
            expected = set(re.findall(r"\{([^}]+)\}", path))
            for method, operation in item.items():
                if method not in HTTP_METHODS:
                    continue
                declared = set()
                for parameter in item.get("parameters", []) + operation.get("parameters", []):
                    if "$ref" in parameter:
                        parameter = resolve_ref(self.spec, parameter["$ref"])
                    if parameter.get("in") == "path" and parameter.get("required") is True:
                        declared.add(parameter["name"])
                self.assertEqual(declared, expected, f"{method} {path}")

    def test_all_local_refs_resolve(self) -> None:
        pending = [self.spec]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if "$ref" in value:
                    resolve_ref(self.spec, value["$ref"])
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)


if __name__ == "__main__":
    unittest.main()
