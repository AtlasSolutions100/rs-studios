"""A fake TikTok API for Business v1.3 (tests and screenshots only). It keeps advertisers, identities, pixels, videos,
images, campaigns, ad groups and ads in memory the way business-api.tiktok.com/open_api/v1.3 does, answers in the
shapes of tests/fixtures/tiktok/*.json (HTTP 200 with {"code", "message", "request_id", "data"}; hand-written from
TikTok's v1.3 reference, to re-record with tools/tiktok_sandbox_launch.py), and can be told to fail.

    f = FakeTikTok()
    f.advertiser("7001", "Ember Shop", currency="USD", tz="America/Los_Angeles")
    f.identity("7001", "id-1", "Ember Shop"); f.pixel("7001", "7300000000000000001", "CEMBERPIXEL0001", "Ember pixel")
    f.code("auth-good", user="tt-user-1", advertisers=["7001"])
    adhttp.TRANSPORT = f
    f.fail("/adgroup/create/", "tiktok/error_budget.json")    # the next call whose path ends so answers that
    f.fail("/campaign/create/", "lost")                        # applied, then the answer is lost (a timeout)

What it checks, like TikTok: the app id and secret on the token exchange, Access-Token on every call, access to the
advertiser; a new campaign must be turned off (DISABLE); an ad group needs a daily budget of at least 20.00, a known
pixel_id (TikTok's numeric id, not the code) when it optimizes for an event, location ids and age bands; an ad needs a
custom identity of the advertiser, an uploaded video and cover, and ad text of 1 to 100 characters without emoji.
"""
import itertools
import json
import re
from urllib.parse import parse_qs, urlsplit

import adhttp

try:
    from adfake import fixture
except ImportError:            # imported from outside tests/
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from adfake import fixture

APP_ID, APP_SECRET = "7000000000000000001", "tiktok-fake-app-secret-2718"
PREFIX = "/open_api/v1.3"
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿]")


def terr(code, message):
    return {"code": code, "message": message, "request_id": f"fakeTT{abs(hash(message)) % 10 ** 8}", "data": {}}


class Refuse(Exception):
    def __init__(self, answer):
        super().__init__("refused")
        self.answer = answer


class FakeTikTok:
    def __init__(self):
        self.advertisers, self.identities, self.pixels = {}, {}, {}
        self.grants, self.tokens = {}, {}
        self.store = {}
        self.calls, self.failures = [], []
        self.processing = 0
        self.report = []           # [{"dimensions": {...}, "metrics": {...}}]
        self._ids = itertools.count(1)
        self._tok = itertools.count(1)

    # ------------------------------------------------------------ test controls
    def advertiser(self, aid, name, currency="USD", tz="America/Los_Angeles", status="STATUS_ENABLE"):
        self.advertisers[aid] = {"advertiser_id": aid, "name": name, "currency": currency, "timezone": tz,
                                 "display_timezone": tz, "status": status}
        return self

    def identity(self, aid, iid, name):
        self.identities[iid] = {"identity_id": iid, "identity_type": "CUSTOMIZED_USER", "display_name": name,
                                "_advertiser": aid}
        return self

    def pixel(self, aid, pid, code, name):
        self.pixels.setdefault(aid, []).append({"pixel_id": pid, "pixel_code": code, "pixel_name": name})
        return self

    def code(self, code, user="tt-user-1", advertisers=()):
        self.grants[code] = {"user": user, "advertisers": list(advertisers)}
        return self

    def grant(self, token, user, advertisers=()):
        self.tokens[token] = {"user": user, "advertisers": list(advertisers)}
        return self

    def fail(self, suffix, response, times=1):
        self.failures.append([suffix, response, times])
        return self

    def objects(self, kind=None, include_deleted=False):
        return [o for o in self.store.values() if (kind is None or o["_kind"] == kind)
                and (include_deleted or o.get("operation_status") != "DELETE")]

    def find(self, suffix="", method=None):
        return [c for c in self.calls if c["path"].endswith(suffix) and (method is None or c["method"] == method)]

    # ------------------------------------------------------------ transport
    def __call__(self, method, url, params=None, data=None, headers=None, timeout=None, files=None):
        u = urlsplit(url)
        body = data
        if isinstance(data, str):
            try:
                body = json.loads(data)
            except ValueError:
                body = {}
        q = {k: v for k, v in (params or {}).items()}
        q.update({k: v[0] for k, v in parse_qs(u.query).items()})
        for k, v in list(q.items()):
            if isinstance(v, str) and v[:1] in "[{":
                try:
                    q[k] = json.loads(v)
                except ValueError:
                    pass
        call = {"method": method, "path": u.path, "params": q, "files": files, "headers": dict(headers or {}),
                "data": body if isinstance(body, dict) else {}}
        self.calls.append(call)
        assert u.netloc == "business-api.tiktok.com" and u.path.startswith(PREFIX), url
        path = u.path[len(PREFIX):]
        for f in self.failures:
            if f[2] > 0 and path.endswith(f[0]):
                f[2] -= 1
                if f[1] == "lost":
                    self._route(method, path, call)
                    import requests
                    raise requests.ReadTimeout("read timed out")
                r = f[1]
                status, b = (r if isinstance(r, tuple) else (200, r))
                return adhttp.Response(status, {}, fixture(b) if isinstance(b, str) else b)
        return self._route(method, path, call)

    def _route(self, method, path, call):
        try:
            data = self._api(method, path, call)
            return adhttp.Response(200, {}, {"code": 0, "message": "OK", "request_id": "fakeTTok", "data": data})
        except Refuse as e:
            return adhttp.Response(200, {}, e.answer)

    def _who(self, call):
        t = self.tokens.get(call["headers"].get("Access-Token") or "")
        if t is None:
            raise Refuse(terr(40105, "The access token is invalid or has been revoked."))
        return t

    def _adv(self, who, aid):
        if str(aid) not in who["advertisers"] or str(aid) not in self.advertisers:
            raise Refuse(terr(40001, "No permission to operate advertiser."))
        return self.advertisers[str(aid)]

    def _new(self, kind, obj, aid, idf):
        oid = str(1700000000000000000 + next(self._ids))
        o = dict(obj, _kind=kind, _advertiser=str(aid))
        o[idf] = oid
        self.store[oid] = o
        return o

    @staticmethod
    def _clean(o):
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def _page(self, rows, key, q):
        return {key: [self._clean(r) for r in rows], "page_info": {"page": int(q.get("page") or 1), "total_page": 1,
                                                                    "total_number": len(rows)}}

    def _api(self, method, path, call):
        d, q = call["data"], call["params"]
        if path == "/oauth2/access_token/" and method == "POST":
            if d.get("app_id") != APP_ID or d.get("secret") != APP_SECRET:
                raise Refuse(terr(40001, "app_id or secret is wrong"))
            gr = self.grants.pop(d.get("auth_code"), None)
            if gr is None:
                raise Refuse(terr(40002, "auth_code is invalid or expired"))
            tok = f"tt-access-{next(self._tok)}-secretvalue"
            self.tokens[tok] = gr
            return {"access_token": tok, "advertiser_ids": gr["advertisers"],
                    "scope": [4, 5, 6, 7, 8]}
        if path == "/oauth2/revoke_token/" and method == "POST":
            assert d.get("app_id") == APP_ID and d.get("secret") == APP_SECRET
            self.tokens.pop(d.get("access_token"), None)
            return {}
        who = self._who(call)
        if path == "/user/info/":
            return {"core_user_id": who["user"], "display_name": "Ember Owner", "email": f"{who['user']}@example.com"}
        if path == "/oauth2/advertiser/get/":
            assert q.get("app_id") == APP_ID and q.get("secret") == APP_SECRET
            return {"list": [{"advertiser_id": a, "advertiser_name": self.advertisers[a]["name"]} for a in who["advertisers"]
                             if a in self.advertisers]}
        if path == "/advertiser/info/":
            return {"list": [self._adv(who, a) for a in q.get("advertiser_ids") or []]}
        aid = str(q.get("advertiser_id") or d.get("advertiser_id") or "")
        adv = self._adv(who, aid)
        if path == "/identity/get/":
            return {"identity_list": [self._clean(i) for i in self.identities.values() if i["_advertiser"] == aid
                                      and i["identity_type"] == q.get("identity_type")]}
        if path == "/identity/create/":
            if not d.get("display_name"):
                raise Refuse(terr(40002, "display_name is required"))
            iid = f"ident-{next(self._ids)}"
            self.identity(aid, iid, d["display_name"])
            return {"identity_id": iid}
        if path == "/pixel/list/":
            rows = [p for p in self.pixels.get(aid, []) if not q.get("code") or p["pixel_code"] == q["code"]]
            return self._page(rows, "pixels", q)
        if path == "/file/video/ad/upload/":
            assert call["files"] and "video_file" in call["files"] and d.get("video_signature") and d.get("file_name")
            v = self._new("video", {"file_name": d["file_name"], "displayable": not self.processing,
                                    "_processing": self.processing}, aid, "video_id")
            return [self._clean(v)]
        if path == "/file/video/ad/search/":
            return self._page([o for o in self.store.values() if o["_kind"] == "video" and o["_advertiser"] == aid],
                              "list", q)
        if path == "/file/video/ad/info/":
            out = []
            for vid in q.get("video_ids") or []:
                v = self.store.get(str(vid))
                if v and v.get("_processing"):
                    v["_processing"] -= 1
                    v["displayable"] = not v["_processing"]
                if v:
                    out.append(self._clean(v))
            return {"list": out}
        if path == "/file/image/ad/upload/":
            assert call["files"] and "image_file" in call["files"] and d.get("image_signature")
            return {"image_id": self._new("image", {}, aid, "image_id")["image_id"]}
        if path == "/campaign/create/":
            if d.get("operation_status") != "DISABLE":
                raise AssertionError("a new campaign must be turned off (DISABLE)")
            assert d.get("objective_type") in ("WEB_CONVERSIONS", "TRAFFIC", "REACH") and d.get("campaign_name")
            return {"campaign_id": self._new("campaign", d, aid, "campaign_id")["campaign_id"]}
        if path == "/adgroup/create/":
            camp = self.store.get(str(d.get("campaign_id")))
            if camp is None or camp["_kind"] != "campaign":
                raise Refuse(terr(40002, "campaign_id does not exist"))
            if float(d.get("budget") or 0) < 20:
                raise Refuse(terr(40002, "The daily budget must be at least 20.00."))
            if d.get("optimization_event"):
                if str(d.get("pixel_id")) not in {p["pixel_id"] for p in self.pixels.get(aid, [])}:
                    raise Refuse(terr(40002, "pixel_id is invalid"))
            assert "pixel_code" not in d, "TikTok takes pixel_id, not the pixel code"
            for k in ("location_ids", "age_groups", "placements", "budget_mode", "optimization_goal", "billing_event",
                      "schedule_type", "schedule_start_time"):
                assert d.get(k), f"ad group needs {k}"
            return {"adgroup_id": self._new("adgroup", d, aid, "adgroup_id")["adgroup_id"]}
        if path == "/ad/create/":
            ag = self.store.get(str(d.get("adgroup_id")))
            if ag is None or ag["_kind"] != "adgroup":
                raise Refuse(terr(40002, "adgroup_id does not exist"))
            ids = []
            for cr in d.get("creatives") or []:
                ident = self.identities.get(cr.get("identity_id"))
                if ident is None or ident["_advertiser"] != aid:
                    raise Refuse(terr(40002, "identity_id is invalid"))
                v = self.store.get(str(cr.get("video_id")))
                if v is None or not v.get("displayable"):
                    raise Refuse(terr(40002, "The video is still being processed."))
                if not all(str(i) in self.store for i in cr.get("image_ids") or []) or not cr.get("image_ids"):
                    raise Refuse(terr(40002, "image_ids is invalid"))
                t = cr.get("ad_text") or ""
                if not 1 <= len(t) <= 100 or EMOJI.search(t):
                    raise Refuse(terr(40002, "ad_text is invalid"))
                ids.append(self._new("ad", dict(cr, adgroup_id=ag["adgroup_id"], operation_status="ENABLE"), aid,
                                     "ad_id")["ad_id"])
            return {"ad_ids": ids}
        m = re.fullmatch(r"/(campaign|adgroup|ad)/get/", path)
        if m:
            kind = m.group(1)
            f = q.get("filtering") or {}
            rows = [o for o in self.store.values() if o["_kind"] == kind and o["_advertiser"] == aid]
            for fk, field in (("campaign_ids", "campaign_id"), ("adgroup_ids", "adgroup_id"), ("ad_ids", "ad_id")):
                if f.get(fk):
                    rows = [o for o in rows if str(o.get(field)) in {str(x) for x in f[fk]}]
            return self._page(rows, "list", q)
        m = re.fullmatch(r"/(campaign|adgroup|ad)/status/update/", path)
        if m:
            kind = m.group(1)
            for oid in d.get(f"{kind}_ids") or []:
                o = self.store.get(str(oid))
                if o is None or o["_kind"] != kind:
                    raise Refuse(terr(40002, f"{kind} not found"))
                o["operation_status"] = d["operation_status"]
            return {}
        if path == "/report/integrated/get/":
            assert q.get("data_level") == "AUCTION_CAMPAIGN" and q.get("start_date") and q.get("end_date")
            return self._page(self.report, "list", q)
        raise AssertionError(f"no fake TikTok answer for {method} {path} ({adv['name']})")
