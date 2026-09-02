"""Response bodies against the OpenAPI schemas, without a jsonschema dependency.

A deliberately small validator (type, required, properties,
additionalProperties, $ref, allOf/oneOf/anyOf, enum, const, items, pattern,
minimum) is enough for the closed schemas in ``contracts/control-plane-openapi.yaml``.
The samples come from the real view functions (``workspace_view`` and
``sandbox_view`` are executed from ``control_plane/core.py`` via ast, the same way
``test_monitoring.py`` does) and from the response dict literals in
``control_plane/api.py``; ``test_api_authorization.py`` additionally validates live
responses of a SQLite-backed Control Plane with the same validator.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest
from typing import Any

import yaml

from control_plane import RuntimeInstance


ROOT = pathlib.Path(__file__).resolve().parents[1]
TYPE_NAMES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def load_spec() -> dict:
    return yaml.safe_load((ROOT / "contracts/control-plane-openapi.yaml").read_text(encoding="utf-8"))


def resolve_ref(spec: dict, ref: str) -> Any:
    value: Any = spec
    for part in ref[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def validate(spec: dict, schema: dict, value: Any, where: str = "$") -> list[str]:
    """Return a list of violations; an empty list means the value conforms."""
    if "$ref" in schema:
        return validate(spec, resolve_ref(spec, schema["$ref"]), value, where)
    errors: list[str] = []
    for sub in schema.get("allOf", []):
        errors += validate(spec, sub, value, where)
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema and not any(
            not validate(spec, sub, value, where) for sub in schema[keyword]
        ):
            errors.append(f"{where}: matches none of {keyword}")
    types = schema.get("type")
    if types is not None:
        allowed = tuple(TYPE_NAMES[name] for name in ([types] if isinstance(types, str) else types))
        if isinstance(value, bool) and bool not in allowed or not isinstance(value, allowed):
            return errors + [f"{where}: expected {types}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} not in enum {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: {value!r} != const {schema['const']!r}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{where}: {value!r} does not match {schema['pattern']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: {value} < minimum {schema['minimum']}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += validate(spec, schema["items"], item, f"{where}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{where}: missing required {name!r}")
        for name, item in value.items():
            if name in properties:
                errors += validate(spec, properties[name], item, f"{where}.{name}")
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{where}: unexpected property {name!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors += validate(spec, schema["additionalProperties"], item, f"{where}.{name}")
    return errors


def load_view_functions() -> dict:
    """Execute workspace_view and sandbox_view from core.py without importing it."""
    source = (ROOT / "control_plane/core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"workspace_view", "sandbox_view"}
    ]
    if len(body) != 2:
        raise AssertionError("core.py must define workspace_view and sandbox_view")
    namespace = {
        "WORKSPACE_IDLE_TTL_SECONDS": 21600,
        "RuntimeInstance": RuntimeInstance,
    }
    module = ast.Module(body=body, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "core.py", "exec"), namespace)
    return namespace


def response_literal_keys(marker: str) -> list[set[str]]:
    """Key sets of every ``send_json`` dict literal in api.py carrying ``marker``.

    ``**name`` splats are resolved when ``name`` is a dict literal assigned in
    the same function; ``**view`` (a sandbox_view result) is reported as the
    sentinel key ``<view>`` so callers can substitute the real keys.
    """
    source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: list[set[str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        assigned = {
            target.id: node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
            for target in node.targets
            if isinstance(target, ast.Name)
        }

        def keys_of(literal: ast.Dict) -> set[str]:
            keys: set[str] = set()
            for key, value in zip(literal.keys, literal.values):
                if isinstance(key, ast.Constant):
                    keys.add(key.value)
                elif key is None and isinstance(value, ast.Name):
                    if value.id in assigned:
                        keys |= keys_of(assigned[value.id])
                    else:
                        keys.add(f"<{value.id}>")
            return keys

        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_json"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Dict)
            ):
                keys = keys_of(node.args[1])
                if marker in keys:
                    found.append(keys)
    return found


def nested_response_literals(marker: str) -> list[tuple[set[str], dict[str, Any]]]:
    """Every dict literal (at any depth) inside a ``send_json`` payload carrying ``marker``.

    Returns ``(keys, constants)`` per literal, where ``constants`` holds the
    entries whose value is a literal constant, so a test can check not only
    the key set but also the type the implementation actually sends
    (``"kubernetes": "ok"`` is a string, whatever the contract once said).
    """
    source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: list[tuple[set[str], dict[str, Any]]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_json"
            and len(node.args) == 2
        ):
            continue
        for literal in ast.walk(node.args[1]):
            if not isinstance(literal, ast.Dict):
                continue
            keys = {key.value for key in literal.keys if isinstance(key, ast.Constant)}
            if marker not in keys:
                continue
            constants = {
                key.value: value.value
                for key, value in zip(literal.keys, literal.values)
                if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
            }
            found.append((keys, constants))
    return found


def method_view_keys(method_name: str, variable: str = "view") -> tuple[set[str], set[str]]:
    """Keys of the dict literal assigned to ``variable`` in an api.py method, and
    the keys later added with ``variable["key"] = ...``.

    ``whoami_view`` builds its answer this way rather than as one literal, which
    is how ``grafana`` stayed out of ``Identity`` unnoticed.
    """
    source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]
    if len(functions) != 1:
        raise AssertionError(f"api.py must define {method_name} exactly once")
    base: set[str] = set()
    added: set[str] = set()
    for node in ast.walk(functions[0]):
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == variable and isinstance(node.value, ast.Dict):
                base |= {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == variable
                and isinstance(target.slice, ast.Constant)
            ):
                added.add(target.slice.value)
    return base, added


class ValidatorSelfCheckTests(unittest.TestCase):
    """The validator must have discriminating power before it is trusted."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_spec()

    def schema(self, name: str) -> dict:
        return {"$ref": f"#/components/schemas/{name}"}

    def test_rejects_missing_required_and_unknown_keys(self) -> None:
        good = {"id": "sb-0123456789ab", "access_token": "t", "access_token_expires_in": 900}
        self.assertEqual(validate(self.spec, self.schema("ScopedToken"), good), [])
        self.assertTrue(validate(self.spec, self.schema("ScopedToken"), {**good, "expires_at": "x"}))
        self.assertTrue(validate(self.spec, self.schema("ScopedToken"), {"id": "sb-0123456789ab", "access_token": "t"}))
        self.assertTrue(validate(self.spec, self.schema("ScopedToken"), {**good, "access_token_expires_in": "900"}))
        self.assertTrue(validate(self.spec, self.schema("ScopedToken"), {**good, "access_token_expires_in": True}))

    def test_rejects_wrong_const_enum_and_pattern(self) -> None:
        lease = {
            "workspace_id": "ws-0123456789ab", "status": "ready",
            "created": False,
            "file_url": "/v1/workspaces/ws-0123456789ab/files",
            "access_token": "t", "access_token_expires_in": 900, "owner": None,
        }
        self.assertEqual(validate(self.spec, self.schema("WorkspaceLease"), lease), [])
        self.assertTrue(validate(self.spec, self.schema("WorkspaceLease"), {**lease, "status": "pending"}))
        self.assertTrue(validate(self.spec, self.schema("WorkspaceLease"), {**lease, "workspace_id": "ws-xyz"}))
        self.assertTrue(validate(self.spec, self.schema("Error"), {"error": "x", "retry_after_seconds": -1}))
        self.assertTrue(validate(self.spec, self.schema("ObjectTicket"), {"operation": "copy"}))


class ViewFunctionSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_spec()
        cls.views = load_view_functions()

    def assert_conforms(self, schema_name: str, value: Any) -> None:
        errors = validate(self.spec, {"$ref": f"#/components/schemas/{schema_name}"}, value)
        self.assertEqual(errors, [], f"{schema_name}: {value!r}")

    def test_workspace_view_matches_workspace_schema(self) -> None:
        entry = {"id": "ws-0123456789ab", "created_at": "1700000000", "last_used_at": "1700000600"}
        self.assert_conforms("Workspace", self.views["workspace_view"](entry, False))
        self.assert_conforms("Workspace", self.views["workspace_view"](entry, True, tenant_id="acme"))
        self.assert_conforms("Workspace", self.views["workspace_view"]({"id": "ws-0123456789ab"}, False))
        bare = self.views["workspace_view"]({"id": "ws-0123456789ab"}, False)
        self.assertIsNone(bare["idle_expires_at"])

    def test_sandbox_view_matches_sandbox_schema(self) -> None:
        runtime = RuntimeInstance(
            runtime_id="sb-0123456789ab",
            workspace_id="ws-0123456789ab",
            provider_id="runtime-sb-0123456789ab",
            state="running",
            ready=True,
            isolation="gvisor",
            template_id="default",
            created_at="1700000000",
            expires_at=1700001800,
        )
        view = self.views["sandbox_view"](runtime)
        self.assertEqual(view["status"], "running")
        self.assert_conforms("Sandbox", view)
        pending = RuntimeInstance(
            runtime_id=runtime.runtime_id,
            workspace_id=runtime.workspace_id,
            provider_id=runtime.provider_id,
            state="pending",
            ready=False,
            isolation="cluster-default",
        )
        self.assert_conforms("Sandbox", self.views["sandbox_view"](pending))
        self.assert_conforms(
            "SandboxLease",
            {**view, "mcp_url": "/v1/sandboxes/sb-0123456789ab/mcp", "access_token": "t", "access_token_expires_in": 900},
        )


class ApiLiteralSchemaTests(unittest.TestCase):
    """Key sets of the api.py response literals equal the closed schemas."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = load_spec()
        cls.views = load_view_functions()

    def properties(self, schema_name: str) -> set[str]:
        return set(self.spec["components"]["schemas"][schema_name]["properties"])

    def required(self, schema_name: str) -> set[str]:
        return set(self.spec["components"]["schemas"][schema_name]["required"])

    def test_lease_and_token_literals_match_schemas(self) -> None:
        literals = response_literal_keys("access_token_expires_in")
        self.assertEqual(len(literals), 4, literals)
        workspace = [keys for keys in literals if "file_url" in keys]
        token = [keys for keys in literals if "mcp_url" not in keys and "file_url" not in keys]
        sandboxes = [keys for keys in literals if "mcp_url" in keys]
        self.assertEqual(workspace, [self.properties("WorkspaceLease")])
        self.assertEqual(token, [self.properties("ScopedToken")])
        self.assertEqual(len(sandboxes), 2)
        view_keys = set(self.views["sandbox_view"](RuntimeInstance(
            runtime_id="", workspace_id="", provider_id="", state="unknown",
            ready=False, isolation="cluster-default",
        )))
        for keys in sandboxes:
            keys = (keys - {"<view>"}) | (view_keys if "<view>" in keys else set())
            self.assertTrue(self.required("SandboxLease") <= keys, keys)
            self.assertTrue(keys <= self.properties("SandboxLease"), keys - self.properties("SandboxLease"))

    def assert_constants_conform(self, schema_name: str, constants: dict[str, Any]) -> None:
        properties = self.spec["components"]["schemas"][schema_name]["properties"]
        for key, value in constants.items():
            errors = validate(self.spec, properties[key], value, f"{schema_name}.{key}")
            self.assertEqual(errors, [], errors)

    def test_identity_view_matches_identity_schema(self) -> None:
        # The literal is the always-present part; the subscript assignments
        # (tenant block, grafana capabilities) are the optional part.
        base, added = method_view_keys("whoami_view")
        self.assertEqual(base, self.required("Identity"))
        self.assertEqual(base | added, self.properties("Identity"))

    def test_health_literal_matches_health_schema(self) -> None:
        literals = nested_response_literals("database")
        self.assertEqual(len(literals), 1, literals)
        keys, constants = literals[0]
        self.assertEqual(keys, self.properties("Health"))
        self.assertEqual(keys, self.required("Health"))
        self.assert_constants_conform("Health", constants)

    def test_tenant_literals_match_tenant_schema(self) -> None:
        literals = nested_response_literals("max_runtimes")
        self.assertEqual(len(literals), 2, literals)
        for keys, constants in literals:
            self.assertTrue(self.required("Tenant") <= keys, keys)
            self.assertTrue(keys <= self.properties("Tenant"), keys - self.properties("Tenant"))
            self.assert_constants_conform("Tenant", constants)
        self.assertIn(self.properties("Tenant"), [keys for keys, _ in literals])

    def test_issued_key_literals_match_issued_api_key_schema(self) -> None:
        literals = nested_response_literals("api_key")
        self.assertEqual(len(literals), 2, literals)
        for keys, constants in literals:
            self.assertEqual(keys, self.properties("IssuedApiKey"))
            self.assertEqual(keys, self.required("IssuedApiKey"))
            self.assert_constants_conform("IssuedApiKey", constants)

    def test_error_literals_only_use_declared_keys(self) -> None:
        declared = self.properties("Error")
        health = self.properties("HealthFailure")
        for keys in response_literal_keys("error"):
            if "jsonrpc" in keys:
                continue  # MCP JSON-RPC error envelopes are not Control Plane errors
            self.assertTrue(
                keys <= declared or keys <= health,
                f"undeclared error keys {keys - declared}",
            )


if __name__ == "__main__":
    unittest.main()
