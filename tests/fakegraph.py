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
    def __init__(self, account="act_111", video_processing=0, first_id=23850000000001):
        self.account = account if account.startswith("act_") else f"act_{account}"
        self.store = {}                    # id -> object dict (kind, name, status, fields...)
        self.images = {}                   # hash -> bytes length
        self.calls = []
        self.failures = []                 # [(method, edge, response, times)]
        self.video_processing = video_processing   # how many status reads a new video answers "processing"
        self._ids = itertools.count(first_id)   # ids are unique per Meta; tests that keep several graphs space them out
        self.edits = []                    # [{"id", "status" | "daily_budget"}]: every change to an existing object
        self.daily = {}                    # (ad id, "YYYY-MM-DD") -> {"spend": "12.34", "impressions": 1000, ...} (M2.5)
        self.headers = {}                  # extra response headers on every answer (usage headers)
        self.insight_page = 500            # rows per insights page (a test makes it small to see paging)

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
        edge = parts[-1] if parts else ("ids" if "ids" in call["params"] else "")
        for f in self.failures:
            if f[0] == method and (edge == f[1] or (len(parts) == 1 and f[1] == "object")) and f[3] > 0:
                f[3] -= 1
                if f[2] == "lost":
                    self._answer(method, parts, call)     # Meta made it...
                    import requests
                    raise requests.ReadTimeout("read timed out")   # ...and we never heard back
                if isinstance(f[2], adhttp.Response):
                    return f[2]
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
        return adhttp.Response(200, dict(self.headers), body)

    def _err(self, name):
        return self._fixture(name)

    def _new_id(self):
        return str(next(self._ids))

    def _answer(self, method, parts, call):
        d = call["data"]
        if not parts and method == "GET" and call["params"].get("ids"):
            return self._read_ids(call["params"])
        if len(parts) == 2 and parts[0] == self.account and parts[1] == "insights" and method == "GET":
            return self._insights(call["params"])
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
                if "daily_budget" in d:
                    if o["kind"] not in ("campaign", "adset") or not str(d["daily_budget"]).isdigit():
                        return self._err("meta/error_100_invalid.json")
                    o["daily_budget"] = int(d["daily_budget"])
                if "status" in d:
                    if d["status"] not in ("ACTIVE", "PAUSED", "ARCHIVED", "DELETED"):
                        return self._err("meta/error_100_invalid.json")
                    o["status"] = d["status"]
                    o["effective_status"] = self._effective(o)
                    for child in self.store.values():        # a parent's switch shows in its children's effective status
                        if child.get("parent") == oid or self.store.get(child.get("parent") or "", {}).get("parent") == oid:
                            child["effective_status"] = self._effective(child)
                self.edits.append({"id": oid, **{k: v for k, v in d.items() if k in ("status", "daily_budget")}})
                return self._ok(fixture("meta/manage_success.json"))
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
            if kind == "campaign" and str(d.get("daily_budget") or "").isdigit():
                self.store[oid]["daily_budget"] = int(d["daily_budget"])
            if kind == "ad":
                self.store[oid]["creative"] = cr
            self.store[oid]["effective_status"] = self._effective(self.store[oid])
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

    # ------------------------------------------------------------ manage (M2.4): reads and effective status
    def _effective(self, o):
        """Meta's effective_status: an object set ACTIVE under a paused parent shows the parent's pause."""
        if o["status"] != "ACTIVE":
            return o["status"]
        if o.get("review"):                      # set by a test: PENDING_REVIEW, DISAPPROVED, WITH_ISSUES...
            return o["review"]
        parent = self.store.get(o.get("parent") or "")
        if o["kind"] == "ad" and parent is not None:
            if parent["status"] != "ACTIVE":
                return "ADSET_PAUSED"
            camp = self.store.get(parent.get("parent") or "")
            if camp is not None and camp["status"] != "ACTIVE":
                return "CAMPAIGN_PAUSED"
        if o["kind"] == "adset" and parent is not None and parent["status"] != "ACTIVE":
            return "CAMPAIGN_PAUSED"
        return "ACTIVE"

    def _read_ids(self, params):
        """GET /?ids=a,b&fields=...: one answer per id that exists; a field the node's type doesn't have fails the
        whole read, as on Meta (an ad has no daily_budget)."""
        fields = [f.strip() for f in (params.get("fields") or "id").split(",")]
        out = {}
        for oid in params["ids"].split(","):
            o = self.store.get(oid)
            if o is None or o["status"] == "DELETED":
                continue
            if o["kind"] == "ad" and ("daily_budget" in fields or "lifetime_budget" in fields):
                return self._err("meta/error_100_nonexisting_field.json")
            r = {"id": oid}
            for f in fields:
                if f in ("name", "status", "effective_status"):
                    r[f] = o[f]
                elif f == "daily_budget" and o.get("daily_budget") is not None:
                    r[f] = str(o["daily_budget"])
            out[oid] = r
        return self._ok(out)

    # ------------------------------------------------------------ insights (M2.5)
    def _insights(self, params):
        """GET /act_<id>/insights?level=ad&time_increment=1: one row per ad per day with delivery (Meta leaves out days
        with nothing), for the campaigns in `filtering`, inside `time_range`, paged by `after` (an offset here)."""
        import json as _json
        if params.get("level") != "ad" or params.get("time_increment") != "1":
            return self._err("meta/error_100_invalid.json")
        tr = _json.loads(params.get("time_range") or "{}")
        camps = set()
        for f in _json.loads(params.get("filtering") or "[]"):
            if f.get("field") == "campaign.id" and f.get("operator") == "IN":
                camps |= {str(v) for v in f.get("value") or []}
        rows = []
        for (ad_id, day), m in sorted(self.daily.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            ad = self.store.get(ad_id)
            if ad is None or not (tr.get("since", "") <= day <= tr.get("until", "9999")):
                continue
            adset = self.store.get(ad.get("parent") or "", {})
            camp = adset.get("parent")
            if camps and camp not in camps:
                continue
            spend, imp = m.get("spend", "0"), int(m.get("impressions", 0))
            clicks, link = int(m.get("clicks", m.get("link_clicks", 0))), int(m.get("link_clicks", 0))
            actions = [{"action_type": "link_click", "value": str(link)}] if link else []
            values = []
            for t, k in (("offsite_conversion.fb_pixel_purchase", "purchases"), ("purchase", "purchases"),
                         ("omni_purchase", "purchases"), ("offsite_conversion.fb_pixel_lead", "leads"), ("lead", "leads")):
                if m.get(k):
                    actions.append({"action_type": t, "value": str(m[k])})
            if m.get("value"):
                values = [{"action_type": t, "value": m["value"]} for t in ("offsite_conversion.fb_pixel_purchase",
                                                                            "purchase", "omni_purchase")]
            row = {"ad_id": ad_id, "ad_name": ad["name"], "adset_id": ad.get("parent"), "campaign_id": camp,
                   "date_start": day, "date_stop": day, "account_currency": m.get("currency", "USD"), "spend": spend,
                   "impressions": str(imp), "reach": str(m.get("reach", imp)), "clicks": str(clicks),
                   "inline_link_clicks": str(link), "ctr": f"{clicks * 100 / imp:.6f}" if imp else "0"}
            if actions:
                row["actions"] = actions
            if values:
                row["action_values"] = values
            rows.append(row)
        start = int(params.get("after") or 0)
        page = rows[start:start + min(int(params.get("limit") or 500), self.insight_page)]
        body = {"data": page}
        nxt = start + len(page)
        if nxt < len(rows):
            body["paging"] = {"cursors": {"before": str(start), "after": str(nxt)}, "next": "https://graph.facebook.com/next"}
        else:
            body["paging"] = {"cursors": {"before": str(start), "after": str(nxt)}}
        return self._ok(body)
