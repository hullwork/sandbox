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
    """Modelled on botocore's StreamingBody, including the trap in it.

    `StreamingBody.__enter__` returns its `_raw_stream`, not itself -- so
    `with response["Body"] as x` hands back urllib3's response object and reads
    on it raise urllib3 exceptions that are neither ClientError nor
    BotoCoreError nor OSError. The earlier fake returned `self` from
    `__enter__`, which made the `with` form look harmless and is why this suite
    could not see the bug. It now raises instead: nothing here may use `with`.
    """
    def __init__(self, data):
        self.data = data
        self.closed = False
    def __enter__(self):
        raise AssertionError(
            "do not use `with` on a body: StreamingBody.__enter__ returns the "
            "raw urllib3 stream and its exceptions escape every handler"
        )
    def close(self):
        self.closed = True
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
    def __init__(self, body=b"", pages=None, version_pages=None, declared=None, omit_length=False):
        self.calls = []
        self.body = body
        self.declared = declared
        # A response with no ContentLength at all (a chunked body), as opposed
        # to one that declares more than it sends.
        self.omit_length = omit_length
        self.pages = pages or [{}]
        self.version_pages = version_pages or [{}]
    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        # Real responses carry ContentLength, and object_get compares it
        # against what it actually read. `declared` lets a case send fewer
        # bytes than it promises.
        declared = self.declared if self.declared is not None else len(self.body)
        if self.omit_length:
            return {"Body": Body(self.body)}
        return {"Body": Body(self.body), "ContentLength": declared}
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


TRUNCATED_CASE = """
import json
store = Fake(body=b"x" * 400, declared=1000)
core.object_store = lambda: store
try:
    core.object_get("b", "k", 4096)
    verdict = "accepted"
except core.ObjectStoreUnavailable:
    verdict = "refused"
except Exception as error:
    verdict = type(error).__name__
print(json.dumps({"verdict": verdict}))
"""


CHECKSUM_CASE = """
import json, socket, threading
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0)); srv.listen(4)
seen = []
def serve():
    conn, _ = srv.accept()
    seen.append(conn.recv(65536).decode("latin1").split("\\r\\n\\r\\n")[0])
    conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 0\\r\\n\\r\\n"); conn.close()
threading.Thread(target=serve, daemon=True).start()
core.OBJECT_STORE_ENDPOINT = "http://127.0.0.1:%d" % srv.getsockname()[1]
core._OBJECT_STORE_CLIENT = None
try:
    core.object_put("bkt", "k", b"hello")
except Exception:
    pass
head = seen[0] if seen else ""
print(json.dumps({
    "method": head.split(" ")[0] if head else "",
    "checksum_headers": [
        line.split(":")[0] for line in head.splitlines()
        if line.lower().startswith("x-amz-checksum")
        or line.lower().startswith("x-amz-sdk-checksum")
    ],
}))
"""


CEILING_CASE = """
import json
core.MAX_LIST_ENTRIES = 10
page = {"Contents": [
    {"Key": "k%d" % i, "Size": 1, "LastModified": when(1)} for i in range(6)
]}
pages = [dict(page) for _ in range(20)]

class Counting(list):
    def __init__(self, items):
        super().__init__(items)
        self.taken = 0
    def __iter__(self):
        for item in super().__iter__():
            self.taken += 1
            yield item

counted = Counting(pages)
store = Fake(pages=counted)
core.object_store = lambda: store
try:
    core.object_list("b", "p/")
    verdict = "accepted"
except ValueError:
    verdict = "refused"
except Exception as error:
    verdict = type(error).__name__
print(json.dumps({
    "verdict": verdict,
    "pages_fetched": counted.taken,
    "pages_available": len(pages),
}))
"""


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

    def test_a_body_shorter_than_its_declared_length_is_an_outage(self) -> None:
        out = run(TRUNCATED_CASE)
        # urllib3 only raises IncompleteRead when a read returns nothing at
        # all, so a connection cut mid-object hands back the prefix and no
        # error. get_object() would then hash the prefix and return an answer
        # that is self-consistent and wrong.
        self.assertEqual(out["verdict"], "refused")

    def test_an_upload_sends_no_checksum_headers(self) -> None:
        out = run(CHECKSUM_CASE)
        # boto3 1.36 began adding and signing x-amz-checksum-crc32 on every
        # upload. Older Ceph RGW and MinIO answer 400 or 501 to it, which would
        # fail every write against a store this platform claims to support --
        # and a 400 classifies as "rejected", pointing the caller at the request
        # instead of at the configuration. Asserted on the wire rather than on
        # the Config object, because what matters is what the store receives.
        self.assertEqual([], out["checksum_headers"])
        self.assertIn("PUT", out["method"])

    def test_a_listing_past_the_ceiling_is_refused_between_pages(self) -> None:
        out = run(CEILING_CASE)
        # The mc path had a byte ceiling per invocation; dropping the subprocess
        # dropped it. Nothing else bounds a listing -- read_timeout is per
        # socket read, so a slow trickle resets it forever while holding the one
        # operation slot and growing the list in memory.
        self.assertEqual("refused", out["verdict"])
        # Refused while paginating, not after draining every page: fetching one
        # page past the limit is the difference between a bounded failure and
        # reading the whole bucket first and complaining afterwards.
        self.assertLess(out["pages_fetched"], out["pages_available"])

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
versioned = [c[1] for c in store.calls if c[0] == "delete_object"]
batched = [c for c in store.calls if c[0] == "delete_objects"]

empty = Fake(version_pages=[{}])
core.object_store = lambda: empty
core.object_delete_versions("b", "k")
plain = [c[1] for c in empty.calls if c[0] == "delete_object"]
print(json.dumps({
    "targets": [{"Key": c["Key"], "VersionId": c["VersionId"]} for c in versioned],
    "batched": len(batched),
    "fell_back": bool(plain) and "VersionId" not in plain[0],
}))
''')
        # One delete_object per version, and never delete_objects: that verb is
        # checksum-required in botocore, so the store receives a signed CRC32
        # header regardless of request_checksum_calculation, and older RGW /
        # MinIO answer 400 or 501 to it (see object_delete_versions).
        self.assertEqual(
            out["targets"],
            [{"Key": "k", "VersionId": "v1"}, {"Key": "k", "VersionId": "d1"}],
        )
        self.assertEqual(out["batched"], 0)
        # A store without versioning reports no versions at all; without this
        # branch every purge on such a store silently deletes nothing.
        self.assertTrue(out["fell_back"])

    def test_checkpoint_delete_reports_whether_history_really_went(self) -> None:
        out = run('''
import json
from botocore.exceptions import ClientError

def rejected(**kwargs):
    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"},
                       "ResponseMetadata": {"HTTPStatusCode": 403}}, "ListObjectVersions")

def outage(**kwargs):
    raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "x"},
                       "ResponseMetadata": {"HTTPStatusCode": 503}}, "ListObjectVersions")

class NoVersioning(Fake):
    def get_paginator(self, name):
        if name == "list_object_versions":
            return type("P", (), {"paginate": staticmethod(rejected)})()
        return super().get_paginator(name)

class Down(Fake):
    def get_paginator(self, name):
        if name == "list_object_versions":
            return type("P", (), {"paginate": staticmethod(outage)})()
        return super().get_paginator(name)

results = {}
ws = "ws-0123456789ab"
versioned = Fake(version_pages=[{"Versions": [{"Key": "workspaces/%s/checkpoints/cp-1.tar.gz" % ws, "VersionId": "v1"}]}])
core.object_store = lambda: versioned
results["versioned"] = core.delete_workspace_checkpoint(ws, "cp-1")["history_retained"]

flat = NoVersioning()
core.object_store = lambda: flat
results["fallback"] = core.delete_workspace_checkpoint(ws, "cp-1")["history_retained"]
results["fallback_plain_delete"] = [c[1].get("VersionId", "none") for c in flat.calls if c[0] == "delete_object"]

down = Down()
core.object_store = lambda: down
try:
    core.delete_workspace_checkpoint(ws, "cp-1")
    results["outage"] = "swallowed"
except core.ObjectStoreBusy:
    results["outage"] = "raised"
results["outage_deletes"] = [c for c in down.calls if c[0] == "delete_object"]
print(json.dumps(results))
''')
        self.assertFalse(out["versioned"])
        # The fallback ran a plain delete: the versions are still there and the
        # response must say so instead of reporting a purge that did not happen.
        self.assertTrue(out["fallback"])
        self.assertEqual(out["fallback_plain_delete"], ["none"])
        # An outage on the listing is not "this store has no versioning".
        self.assertEqual(out["outage"], "raised")
        self.assertEqual(out["outage_deletes"], [])

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

    def test_a_paged_walk_never_trips_the_listing_ceiling(self) -> None:
        out = run('''
import json
core.MAX_LIST_ENTRIES = 10

class Paged(Fake):
    def __init__(self, pages):
        super().__init__()
        self.paged = pages
    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        index = int(kwargs.get("ContinuationToken") or 0)
        page = dict(self.paged[index])
        if index + 1 < len(self.paged):
            page["IsTruncated"] = True
            page["NextContinuationToken"] = str(index + 1)
        return page

pages = [
    {"Contents": [{"Key": "p/k%d-%d" % (n, i), "Size": 1, "LastModified": when(1)} for i in range(6)]}
    for n in range(5)
]
store = Paged(pages)
core.object_store = lambda: store
seen = []
token = None
rounds = 0
try:
    while True:
        items, token = core.object_list_page("b", "p/", continuation_token=token, page_size=6)
        seen.extend(item["key"] for item in items)
        rounds += 1
        if not token:
            break
    verdict = "walked"
except ValueError:
    verdict = "refused"
print(json.dumps({
    "verdict": verdict,
    "rounds": rounds,
    "keys": len(seen),
    "distinct": len(set(seen)),
    "tokens": [c[1].get("ContinuationToken") for c in store.calls if c[0] == "list_objects_v2"],
    "max_keys": sorted({c[1]["MaxKeys"] for c in store.calls if c[0] == "list_objects_v2"}),
}))
''')
        # 30 objects against a ceiling of 10: object_list would refuse between
        # pages, and the checkpoint GC that used it stopped for good once the
        # bucket outgrew the ceiling. The paged walk sees every key exactly once.
        self.assertEqual(out["verdict"], "walked")
        self.assertEqual((out["rounds"], out["keys"], out["distinct"]), (5, 30, 30))
        self.assertEqual(out["tokens"], [None, "1", "2", "3", "4"])
        self.assertEqual(out["max_keys"], [6])

    def test_a_truncated_body_is_refused_whatever_the_declared_length_says(self) -> None:
        out = run('''
import json, hashlib
LIMIT = 4096
body = b"x" * 2000
def attempt(declared, expected_sha256=None):
    store = Fake(body=body, declared=declared, omit_length=declared is None)
    core.object_store = lambda: store
    try:
        data = core.object_get("b", "k", LIMIT, expected_sha256=expected_sha256)
        return "accepted:%d" % len(data)
    except core.ObjectStoreUnavailable:
        return "refused_outage"
    except ValueError:
        return "refused_too_large"
    except Exception as error:
        return type(error).__name__
print(json.dumps({
    # Declared above the ceiling, cut before it: the old guard skipped this case.
    "declared_above_limit_cut_early": attempt(100000),
    # Declared under the ceiling, cut before it: the case the old guard did catch.
    "declared_under_limit_cut_early": attempt(4000),
    # No ContentLength and nothing to vouch for the body.
    "undeclared_without_digest": attempt(None),
    # No ContentLength, but the caller's digest matches: the only way through.
    "undeclared_with_matching_digest": attempt(None, hashlib.sha256(body).hexdigest()),
    "undeclared_with_wrong_digest": attempt(None, "0" * 64),
    # Complete bodies still pass, on both sides of the ceiling comparison.
    "complete": attempt(2000),
}))
''')
        # The Fake's ``declared`` sends fewer bytes than it promises (see
        # ``Fake.get_object``); ``None`` sends no ContentLength at all.
        self.assertEqual(out["declared_above_limit_cut_early"], "refused_outage")
        self.assertEqual(out["declared_under_limit_cut_early"], "refused_outage")
        self.assertEqual(out["undeclared_without_digest"], "refused_outage")
        self.assertEqual(out["undeclared_with_matching_digest"], "accepted:2000")
        self.assertEqual(out["undeclared_with_wrong_digest"], "refused_outage")
        self.assertEqual(out["complete"], "accepted:2000")


if __name__ == "__main__":
    unittest.main()
