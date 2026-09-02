"""Three-way route completeness: api.py dispatch == OpenAPI paths == ROUTE_AUTH.

Control Plane routing is an if/elif chain inside four ``do_*`` methods. Nothing in
that structure notices a branch that was added without registering it in the
contract or in the authentication manifest. This test extracts every literal
the dispatcher compares against (``path == "..."``, ``path in (...)`` and
``match_path(r"...")``) straight from the source, normalizes the regular
expressions into OpenAPI path templates, and requires the three sets to be
identical. Any difference is printed as an explicit set diff.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}
DISPATCHERS = {"do_GET": "GET", "do_POST": "POST", "do_PUT": "PUT", "do_DELETE": "DELETE"}

# A capture group that is not a plain alternation of literal words is an
# identifier; its placeholder name is decided by the literal segment before it.
PLACEHOLDERS = {
    "workspaces": "workspace_id",
    "sandboxes": "sandbox_id",
    "checkpoints": "checkpoint_id",
    "tenants": "tenant_id",
    "owner-tenants": "owner_tenant_id",
    "keys": "key_id",
    "templates": "template_id",
}
LITERAL_ALTERNATION = re.compile(r"[a-z-]+(\|[a-z-]+)*")
EQUALITY_LITERAL = re.compile(r'path == "([^"]+)"')
MEMBERSHIP_TUPLE = re.compile(r"path in \(((?:\s*\"[^\"]+\",?)+)\s*\)")
MATCH_PATH_CALL = re.compile(r'match_path\(\s*((?:r"[^"]*"\s*)+),')


def dispatcher_sources() -> dict[str, str]:
    source = (ROOT / "control_plane/api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ApiHandler"
    )
    found = {
        node.name: ast.get_source_segment(source, node)
        for node in handler.body
        if isinstance(node, ast.FunctionDef) and node.name in DISPATCHERS
    }
    missing = set(DISPATCHERS) - set(found)
    if missing:
        raise AssertionError(f"ApiHandler lacks dispatchers: {sorted(missing)}")
    return found


def regex_to_templates(pattern: str) -> list[str]:
    """Expand one ``match_path`` regex into the OpenAPI templates it serves."""
    variants: list[list[str]] = [[]]
    for segment in pattern.split("/"):
        group = re.fullmatch(r"\((.*)\)", segment)
        if group is None:
            options = [segment]
        elif LITERAL_ALTERNATION.fullmatch(group.group(1)):
            options = group.group(1).split("|")
        else:
            previous = variants[0][-1] if variants[0] else ""
            if previous not in PLACEHOLDERS:
                raise AssertionError(
                    f"no placeholder rule for identifier after {previous!r} in {pattern!r}"
                )
            options = ["{" + PLACEHOLDERS[previous] + "}"]
        variants = [prefix + [option] for prefix in variants for option in options]
    return ["/".join(parts) for parts in variants]


def dispatched_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for name, body in dispatcher_sources().items():
        method = DISPATCHERS[name]
        for literal in EQUALITY_LITERAL.findall(body):
            routes.add((method, literal))
        for group in MEMBERSHIP_TUPLE.findall(body):
            for literal in re.findall(r'"([^"]+)"', group):
                routes.add((method, literal))
        for call in MATCH_PATH_CALL.findall(body):
            pattern = "".join(re.findall(r'r"([^"]*)"', call))
            for template in regex_to_templates(pattern):
                routes.add((method, template))
    return routes


def documented_routes(spec: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method in HTTP_METHODS
    }


def route_auth_samples() -> tuple[tuple[str, str, bool], ...]:
    tree = ast.parse((ROOT / "control_plane/core.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "ROUTE_AUTH":
                return ast.literal_eval(node.value)
    raise AssertionError("control_plane.ROUTE_AUTH not found")


def template_pattern(template: str) -> re.Pattern[str]:
    escaped = re.escape(template)
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", r"[^/]+", escaped) + "$")


def manifest_routes(spec: dict) -> set[tuple[str, str]]:
    """ROUTE_AUTH samples mapped onto templates; unmatched samples stay raw."""
    routes: set[tuple[str, str]] = set()
    for method, sample, _ in route_auth_samples():
        matches = [
            template for template in spec["paths"]
            if template_pattern(template).fullmatch(sample)
        ]
        if len(matches) == 1:
            routes.add((method, matches[0]))
        else:
            routes.add((method, sample))
    return routes


class RouteCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = yaml.safe_load(
            (ROOT / "contracts/control-plane-openapi.yaml").read_text(encoding="utf-8")
        )
        cls.api = dispatched_routes()
        cls.openapi = documented_routes(cls.spec)
        cls.manifest = manifest_routes(cls.spec)

    def assert_same_routes(self, left_name: str, left: set, right_name: str, right: set) -> None:
        only_left = sorted(left - right)
        only_right = sorted(right - left)
        self.assertEqual(
            (only_left, only_right),
            ([], []),
            f"\nonly in {left_name}: {only_left}\nonly in {right_name}: {only_right}",
        )

    def test_extractor_sees_every_dispatcher_style(self) -> None:
        # Guards the extractor itself: each syntactic form the dispatcher uses
        # must contribute at least one route, so a refactor that switches
        # forms cannot silently empty one side of the comparison.
        self.assertIn(("GET", "/v1/whoami"), self.api)
        self.assertIn(("GET", "/v1/storage/objects/stat"), self.api)
        self.assertIn(("GET", "/v1/workspaces/{workspace_id}/files/grep"), self.api)
        self.assertIn(
            ("DELETE", "/v1/admin/tenants/{tenant_id}/owner-tenants/{owner_tenant_id}"),
            self.api,
        )
        self.assertGreaterEqual(len(self.api), 40)

    def test_regex_expansion_is_exact(self) -> None:
        self.assertEqual(
            regex_to_templates(r"/v1/workspaces/(ws-[a-f0-9]{12})/files/(read|list)"),
            [
                "/v1/workspaces/{workspace_id}/files/read",
                "/v1/workspaces/{workspace_id}/files/list",
            ],
        )
        with self.assertRaises(AssertionError):
            regex_to_templates(r"/v1/debug/([a-z]+)")

    def test_dispatched_routes_equal_openapi_paths(self) -> None:
        self.assert_same_routes("api.py", self.api, "openapi", self.openapi)

    def test_openapi_paths_equal_route_auth_manifest(self) -> None:
        self.assert_same_routes("openapi", self.openapi, "ROUTE_AUTH", self.manifest)

    def test_dispatched_routes_equal_route_auth_manifest(self) -> None:
        self.assert_same_routes("api.py", self.api, "ROUTE_AUTH", self.manifest)


if __name__ == "__main__":
    unittest.main()
