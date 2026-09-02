"""Startup contract for the relational store backends.

A database driver missing from the Control Plane image is a build defect, not an
outage. These tests pin the three places that keep it from shipping again:
the hash-locked requirements, the build-time import guard in the Dockerfile,
and the hard exit in the composition root.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_store_module():
    path = ROOT / "control_plane/store.py"
    spec = importlib.util.spec_from_file_location("sandbox_store_startup_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


store_module = load_store_module()


def load_open_store():
    """Extract ``_open_store`` from core.py without importing the module.

    core.py validates the whole deployment environment at import time; the
    function under test only needs the store symbols and ``CONTROL_PLANE_ROLE``.
    """
    tree = ast.parse((ROOT / "control_plane/core.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_open_store"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "os": os,
        "pathlib": pathlib,
        "CONTROL_PLANE_ROLE": "api",
        "Store": store_module.Store,
        "StoreError": store_module.StoreError,
        "require_driver": store_module.require_driver,
        "connection_hardening": store_module.connection_hardening,
    }
    exec(compile(ast.fix_missing_locations(module), "core.py", "exec"), namespace)
    return namespace["_open_store"]


class RequirementsLockTests(unittest.TestCase):
    def test_mysql_driver_is_hash_locked(self) -> None:
        lines = (ROOT / "control_plane/requirements.lock").read_text(encoding="utf-8").splitlines()
        index = next(
            (i for i, line in enumerate(lines) if line.startswith("PyMySQL==")),
            None,
        )
        self.assertIsNotNone(index, "PyMySQL is not pinned in control_plane/requirements.lock")
        self.assertTrue(lines[index].rstrip().endswith("\\"))
        self.assertIn("--hash=sha256:", lines[index + 1])

    def test_dockerfile_imports_both_drivers_during_build(self) -> None:
        dockerfile = (ROOT / "control_plane/Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r'python3 -c "import [^"]*\bpymysql\b')
        self.assertRegex(dockerfile, r'python3 -c "import [^"]*\bpsycopg\b')

    def test_external_deps_overlay_uses_the_mysql_backend(self) -> None:
        documents = yaml.safe_load_all(
            (ROOT / "overlays/external-deps/control-plane-external.yaml").read_text(encoding="utf-8")
        )
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        backend = next(item["value"] for item in env if item["name"] == "SANDBOX_STORE_BACKEND")
        self.assertEqual(backend, "mysql")


class MissingDriverTests(unittest.TestCase):
    def test_require_driver_names_the_missing_package(self) -> None:
        with mock.patch.dict(sys.modules, {"pymysql": None}):
            with self.assertRaises(store_module.StoreError) as raised:
                store_module.require_driver("mysql")
        self.assertIn("PyMySQL", str(raised.exception))
        self.assertIn("requirements.lock", str(raised.exception))

    def test_open_store_exits_when_the_driver_is_missing(self) -> None:
        open_store = load_open_store()
        with tempfile.TemporaryDirectory() as directory:
            password = pathlib.Path(directory) / "password"
            password.write_text("secret", encoding="utf-8")
            environment = {
                "SANDBOX_STORE_BACKEND": "mysql",
                "SANDBOX_DB_HOST": "127.0.0.1",
                "SANDBOX_DB_PASSWORD_FILE": str(password),
            }
            with mock.patch.dict(os.environ, environment):
                with mock.patch.dict(sys.modules, {"pymysql": None}):
                    with self.assertRaises(SystemExit) as raised:
                        open_store()
        message = str(raised.exception)
        self.assertIn("PyMySQL", message)
        self.assertNotIn("unavailable: database", message)

    def test_open_store_returns_a_mysql_store_when_the_driver_imports(self) -> None:
        open_store = load_open_store()
        with tempfile.TemporaryDirectory() as directory:
            password = pathlib.Path(directory) / "password"
            password.write_text("secret", encoding="utf-8")
            environment = {
                "SANDBOX_STORE_BACKEND": "mysql",
                "SANDBOX_DB_HOST": "127.0.0.1",
                "SANDBOX_DB_PASSWORD_FILE": str(password),
            }
            # The test environment installs only `.[test]`; stand in for the
            # driver so the contract is "the import succeeds", not "PyMySQL
            # happens to be installed here".
            driver = types.ModuleType("pymysql")
            with mock.patch.dict(os.environ, environment):
                with mock.patch.dict(sys.modules, {"pymysql": driver}):
                    store = open_store()
        self.assertEqual(store.backend, "mysql")


if __name__ == "__main__":
    unittest.main()
