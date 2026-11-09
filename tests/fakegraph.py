"""A fake Meta Graph / Marketing API for the launch saga (tests and screenshots only). It keeps ad images, videos,
creatives, campaigns, ad sets and ads in memory the way the Marketing API does, answers in the shapes of
tests/fixtures/meta/launch_*.json, and can be told to fail. No request leaves the machine.

    g = FakeGraph(account="act_111")
    adhttp.TRANSPORT = g                      # adhttp calls land here instead of graph.facebook.com
    g.fail("POST", "adsets", "meta/error_100_invalid.json")     # next POST to /act_.../adsets answers that error
    g.fail("POST", "campaigns", "lost")       # creates the campaign, then the answer is lost (a timeout)
    g.objects("campaign")                     # what exists (not deleted)

What it checks, like Meta: a parent must exist (an ad set's campaign, an ad's ad set and creative); a create
must say status=PAUSED (the saga never creates anything that runs); deleted objects aren't listed; the
`filtering` param on name (CONTAIN, EQUAL) works for campaigns, ad sets and ads.
"""
import itertools
import json
from urllib.parse import urlsplit

import adhttp

try:
    from adfake import fixture
except ImportError:            # imported from outside tests/
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from adfake import fixture

EDGE_KIND = {"adcreatives": "creative", "campaigns": "campaign", "adsets": "adset", "ads": "ad", "advideos": "video",
             "adimages": "image"}


class FakeGraph:
    def __init__(self, account="act_111", video_processing=0):
        self.account = account if account.startswith("act_") else f"act_{account}"
        self.store = {}                    # id -> object dict (kind, name, status, fields...)
        self.images = {}                   # hash -> bytes length
        self.calls = []
        self.failures = []                 # [(method, edge, response, times)]
        self.video_processing = video_processing   # how many status reads a new video answers "processing"
        self._ids = itertools.count(23850000000001)

    # ------------------------------------------------------------ test controls
    def fail(self, method, edge, response, times=1):
        """The next `times` calls to <method> .../<edge> answer `response`: a fixture name, (status, body), an
        exception, or "lost" (the object is made, then the answer is lost like a timeout)."""
        self.failures.append([method.upper(), edge, response, times])
        return self

    def objects(self, kind=None, include_deleted=False):
        return [o for o in self.store.values() if (kind is None or o["kind"] == kind)
                and (include_deleted or o["status"] != "DELETED")]

    def creates(self, kind=None):
        return [c for c in self.calls if c["method"] == "POST" and c.get("created")
                and (kind is None or c.get("created") == kind)]

    # ------------------------------------------------------------ transport
    def __call__(self, method, url, params=None, data=None, headers=None, timeout=None, files=None):
        path = urlsplit(url).path
        parts = [p for p in path.split("/") if p][1:]          # drop the version
        call = {"method": method, "path": path, "params": dict(params or {}), "data": dict(data or {}),
                "headers": dict(headers or {}), "files": sorted((files or {}).keys())}
        self.calls.append(call)
        edge = parts[-1] if parts else ""
        for f in self.failures:
            if f[0] == method and (edge == f[1] or (len(parts) == 1 and f[1] == "object")) and f[3] > 0:
                f[3] -= 1
                if f[2] == "lost":
                    self._answer(method, parts, call)     # Meta made it...
                    import requests
                    raise requests.ReadTimeout("read timed out")   # ...and we never heard back
                return self._fixture(f[2])
        return self._answer(method, parts, call)

    def _fixture(self, r):
        if isinstance(r, BaseException):
            raise r
        status, body = (r if isinstance(r, tuple) else (None, r))
        if isinstance(body, str):
            body = fixture(body)
        if status is None:
            status = 400 if "error" in body else 200
            code = (body.get("error") or {}).get("code")
            if code in (1, 2):
                status = 500
            if code in (190, 102):
                status = 401
        return adhttp.Response(status, {}, body)

    def _ok(self, body):
        return adhttp.Response(200, {}, body)

    def _err(self, name):
        return self._fixture(name)

    def _new_id(self):
        return str(next(self._ids))

    def _answer(self, method, parts, call):
        d = call["data"]
        if len(parts) == 2 and parts[0] == self.account:
            edge = parts[1]
            kind = EDGE_KIND.get(edge)
            if method == "POST":
                return self._create(edge, kind, d, call)
            if method == "GET":
                return self._list(kind, call["params"])
        if len(parts) == 2 and parts[0].startswith("act_"):
            return self._err("meta/error_200.json")        # another ad account: no permission
        if len(parts) == 1:
            oid = parts[0]
            o = self.store.get(oid)
            if o is None or o["status"] == "DELETED":
                return self._err("meta/error_100_no_object.json")
            if method == "DELETE":
                o["status"] = o["effective_status"] = "DELETED"
                return self._ok(fixture("meta/launch_delete.json"))
            if method == "POST":
                if "status" in d:
                    o["status"] = o["effective_status"] = d["status"]
                return self._ok({"success": True})
            if method == "GET":
                if o["kind"] == "video":
                    left = o.get("processing", 0)
                    if left > 0:
                        o["processing"] = left - 1
                        return self._ok(fixture("meta/launch_video_processing.json") | {"id": oid})
                    return self._ok(fixture("meta/launch_video_ready.json") | {"id": oid})
                return self._ok({"id": oid, "name": o["name"], "status": o["status"],
                                 "effective_status": o["effective_status"]})
        raise AssertionError(f"fake graph has no answer for {method} /{'/'.join(parts)}")

    def _create(self, edge, kind, d, call):
        if kind == "image":
            import hashlib
            raw = d.get("bytes") or ""
            h = hashlib.md5(raw.encode()).hexdigest()
            self.images[h] = len(raw)
            body = fixture("meta/launch_adimages.json")
            body["images"] = {"bytes": dict(body["images"]["bytes"], hash=h)}
            call["created"] = "image"
            return self._ok(body)
        if kind == "video":
            if "source" not in call["files"]:
                return self._err("meta/error_100_invalid.json")
            oid = self._new_id()
            self.store[oid] = {"id": oid, "kind": "video", "name": d.get("title") or d.get("name") or "", "status": "ACTIVE",
                               "effective_status": "ACTIVE", "processing": self.video_processing}
            call["created"] = "video"
            return self._ok(fixture("meta/launch_advideos.json") | {"id": oid})
        if kind in ("creative", "campaign", "adset", "ad"):
            if kind != "creative" and d.get("status") != "PAUSED":
                return self._err("meta/error_100_invalid.json")
            parent = None
            if kind == "creative":
                spec = json.loads(d.get("object_story_spec") or "{}")
                if not spec.get("page_id"):
                    return self._err("meta/error_100_invalid.json")
                media = spec.get("link_data") or spec.get("video_data") or {}
                if "link_data" in spec and media.get("image_hash") not in self.images:
                    return self._err("meta/error_100_invalid.json")
                if "video_data" in spec:
                    v = self.store.get(media.get("video_id"))
                    if not v or v["status"] == "DELETED" or v.get("processing"):
                        return self._err("meta/error_100_invalid.json")
            elif kind == "adset":
                parent = d.get("campaign_id")
                if self.store.get(parent, {}).get("kind") != "campaign" or self.store[parent]["status"] == "DELETED":
                    return self._err("meta/error_100_invalid.json")
            elif kind == "ad":
                parent = d.get("adset_id")
                cr = json.loads(d.get("creative") or "{}").get("creative_id")
                if self.store.get(parent, {}).get("kind") != "adset" or self.store.get(cr, {}).get("kind") != "creative" \
                        or self.store[cr]["status"] == "DELETED":
                    return self._err("meta/error_100_invalid.json")
            if not d.get("name"):
                return self._err("meta/error_100_invalid.json")
            oid = self._new_id()
            st = d.get("status") or "ACTIVE"
            self.store[oid] = {"id": oid, "kind": kind, "name": d["name"], "status": st, "effective_status": st,
                               "parent": parent, "data": d}
            call["created"] = kind
            return self._ok(fixture(f"meta/launch_{edge}.json") | {"id": oid})
        raise AssertionError(f"fake graph: unknown edge {edge}")

    def _list(self, kind, params):
        rows = [o for o in self.store.values() if o["kind"] == kind and o["status"] != "DELETED"]
        flt = json.loads(params.get("filtering") or "[]")
        for f in flt:
            if f.get("field") == "name":
                if f.get("operator") == "CONTAIN":
                    rows = [o for o in rows if f["value"] in o["name"]]
                elif f.get("operator") == "EQUAL":
                    rows = [o for o in rows if o["name"] == f["value"]]
        out = []
        for o in rows:
            r = {"id": o["id"]}
            if kind == "video":
                r["title"] = o["name"]
            else:
                r["name"] = o["name"]
                if kind in ("campaign", "adset", "ad"):
                    r["effective_status"] = o["effective_status"]
            out.append(r)
        return self._ok({"data": out, "paging": {"cursors": {"before": "MA", "after": "MA"}}})
