"""The object-store layer that replaced the MinIO Client subprocess.

Five behaviours were reimplemented rather than moved, and none of them had a
test: the size ceiling on a read, the version ordering, the delete-every-version
path and its fallback, and the HEAD-to-stat mapping. `mc` did all of these
itself, so the old code had nothing of its own to check.

``core.py`` reads its environment on import, so each case runs in a subprocess
under a minimal volume-role environment with a fake S3 client installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE = '''
import datetime
from control_plane import core


def when(day):
    return datetime.datetime(2026, 9, day, tzinfo=datetime.timezone.utc)


class Body:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def read(self, size=-1):
        if size is None or size < 0:
            out, self.data = self.data, b""
            return out
        out, self.data = self.data[:size], self.data[size:]
        return out


class Paginator:
    def __init__(self, pages, calls, name):
        self.pages, self.calls, self.name = pages, calls, name
    def paginate(self, **kwargs):
        self.calls.append((self.name, kwargs))
        return self.pages


class Fake:
    def __init__(self, body=b"", pages=None, version_pages=None):
        self.calls = []
        self.body = body
        self.pages = pages or [{}]
        self.version_pages = version_pages or [{}]
    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {"Body": Body(self.body)}
    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        return self.head
    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        return {}
    def delete_objects(self, **kwargs):
        self.calls.append(("delete_objects", kwargs))
        return {}
    def get_paginator(self, name):
        pages = self.pages if name == "list_objects_v2" else self.version_pages
        return Paginator(pages, self.calls, name)
'''


def run(case: str) -> dict:
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
        [sys.executable, "-c", FAKE + case],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class ObjectStoreLayerTests(unittest.TestCase):
    def test_a_read_at_the_ceiling_is_returned_and_one_byte_over_is_refused(self) -> None:
        out = run('''
import json
store = Fake(body=b"x" * 10)
core.object_store = lambda: store
at_limit = core.object_get("b", "k", 10)
try:
    store.body = b"x" * 11
    core.object_get("b", "k", 10)
    over = "accepted"
except ValueError:
    over = "refused"
print(json.dumps({"at_limit": len(at_limit), "over": over}))
''')
        self.assertEqual(out["at_limit"], 10)
        # Exactly at the limit and one byte past it must not collapse into the
        # same answer; the read asks for ceiling+1 precisely so they don't.
        self.assertEqual(out["over"], "refused")

    def test_versions_are_ordered_oldest_first_and_latest_comes_from_the_store(self) -> None:
        out = run('''
import json
pages = [{
    "Versions": [
        {"Key": "k", "VersionId": "v2", "IsLatest": True, "Size": 2, "LastModified": when(2), "ETag": "e2"},
        {"Key": "k", "VersionId": "v1", "IsLatest": False, "Size": 1, "LastModified": when(1), "ETag": "e1"},
        {"Key": "other", "VersionId": "vx", "IsLatest": True, "Size": 9, "LastModified": when(3)},
    ],
    "DeleteMarkers": [
        {"Key": "k", "VersionId": "d1", "IsLatest": False, "LastModified": when(3)},
    ],
}]
store = Fake(version_pages=pages)
core.object_store = lambda: store
rows = core.object_versions("b", "k")
print(json.dumps({
    "ids": [r["version_id"] for r in rows],
    "ordinals": [r["version_ordinal"] for r in rows],
    "latest": [r["version_id"] for r in rows if r["is_latest"]],
    "markers": [r["version_id"] for r in rows if r["delete_marker"]],
    "leaked_sort_key": any("_sort_key" in r for r in rows),
}))
''')
        # Newest first in the response, ordinals ascending with age, and the
        # row for a different key is not swept in by the prefix search.
        self.assertEqual(out["ids"], ["d1", "v2", "v1"])
        self.assertEqual(out["ordinals"], [3, 2, 1])
        self.assertEqual(out["latest"], ["v2"])
        self.assertEqual(out["markers"], ["d1"])
        self.assertFalse(out["leaked_sort_key"])

    def test_deleting_every_version_falls_back_to_a_plain_delete(self) -> None:
        out = run('''
import json
store = Fake(version_pages=[{
    "Versions": [{"Key": "k", "VersionId": "v1"}],
    "DeleteMarkers": [{"Key": "k", "VersionId": "d1"}],
}])
core.object_store = lambda: store
core.object_delete_versions("b", "k")
versioned = [c for c in store.calls if c[0] == "delete_objects"]

empty = Fake(version_pages=[{}])
core.object_store = lambda: empty
core.object_delete_versions("b", "k")
plain = [c for c in empty.calls if c[0] == "delete_object"]
print(json.dumps({
    "targets": versioned[0][1]["Delete"]["Objects"] if versioned else [],
    "fell_back": bool(plain),
}))
''')
        self.assertEqual(
            out["targets"],
            [{"Key": "k", "VersionId": "v1"}, {"Key": "k", "VersionId": "d1"}],
        )
        # A store without versioning reports no versions at all; without this
        # branch every purge on such a store silently deletes nothing.
        self.assertTrue(out["fell_back"])

    def test_stat_accepts_both_metadata_spellings(self) -> None:
        out = run('''
import json
results = {}
for label, metadata in (("lower", {"sha256": "abc"}), ("mixed", {"Sha256": "abc"})):
    store = Fake()
    store.head = {"ContentLength": 3, "ETag": "e", "Metadata": metadata,
                  "ContentType": "text/plain"}
    core.object_store = lambda store=store: store
    item = core.object_stat("b", "k")
    results[label] = item["metadata"]["X-Amz-Meta-Sha256"]
    results[label + "_type"] = item["metadata"]["Content-Type"]
    results[label + "_size"] = item["size"]
print(json.dumps(results))
''')
        # S3 lower-cases user metadata; a checkpoint written through mc carried
        # the header spelling. Both have to read back or old checkpoints fail
        # their digest check.
        self.assertEqual(out["lower"], "abc")
        self.assertEqual(out["mixed"], "abc")
        self.assertEqual(out["lower_type"], "text/plain")
        self.assertEqual(out["lower_size"], 3)

    def test_listing_flattens_every_page(self) -> None:
        out = run('''
import json
store = Fake(pages=[
    {"Contents": [
        {"Key": "a", "Size": 1, "LastModified": when(1)},
        {"Key": "b", "Size": 2, "LastModified": when(2)},
    ]},
    {"Contents": [{"Key": "c", "Size": 3, "LastModified": when(3)}]},
    {},
])
core.object_store = lambda: store
rows = core.object_list("bucket", "prefix/")
print(json.dumps({
    "keys": [r["key"] for r in rows],
    "bytes": [r["bytes"] for r in rows],
    "stamps": [r["last_modified"] for r in rows],
    "prefix": [c[1]["Prefix"] for c in store.calls if c[0] == "list_objects_v2"],
}))
''')
        # Several rows within one page and several pages: a reader that
        # kept only the first of either would still return a plausible list.
        self.assertEqual(out["keys"], ["a", "b", "c"])
        self.assertEqual(out["bytes"], [1, 2, 3])
        self.assertEqual(out["stamps"][0], "2026-09-01T00:00:00+00:00")
        self.assertEqual(out["prefix"], ["prefix/"])


if __name__ == "__main__":
    unittest.main()
