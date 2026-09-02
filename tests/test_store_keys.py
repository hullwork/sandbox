from __future__ import annotations

import tempfile
import unittest
import importlib.util
import sys
from pathlib import Path


def load_store_module():
    path = Path(__file__).resolve().parents[1] / "control_plane/store.py"
    spec = importlib.util.spec_from_file_location("sandbox_store_for_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


store_module = load_store_module()
Store = store_module.Store
KEY_PREFIX_LENGTH = store_module.KEY_PREFIX_LENGTH


class ApiKeyGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.store = Store.sqlite(Path(self._temporary_directory.name) / "store.sqlite3")
        self.store.ensure_schema()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_similar_tenant_ids_get_unique_key_prefixes(self) -> None:
        first_tenant = "audit-0826-a"
        second_tenant = "audit-0826-b"
        self.store.create_tenant(
            first_tenant,
            "First audit tenant",
            max_workspaces=2,
            max_runtimes=2,
        )
        self.store.create_tenant(
            second_tenant,
            "Second audit tenant",
            max_workspaces=2,
            max_runtimes=2,
        )

        first_key, _ = self.store.issue_api_key(first_tenant, "audit")
        second_key, _ = self.store.issue_api_key(second_tenant, "audit")

        self.assertNotEqual(
            first_key[:KEY_PREFIX_LENGTH], second_key[:KEY_PREFIX_LENGTH]
        )
        self.assertIn(first_tenant, first_key)
        self.assertIn(second_tenant, second_key)
        self.assertEqual(self.store.authenticate(first_key).tenant_id, first_tenant)
        self.assertEqual(
            self.store.authenticate(second_key).tenant_id, second_tenant
        )


if __name__ == "__main__":
    unittest.main()
