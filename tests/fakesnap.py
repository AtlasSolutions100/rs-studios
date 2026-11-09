"""A fake Snapchat Marketing API and Snap OAuth server (tests and screenshots only). It keeps organizations, ad
accounts, public profiles, pixels, media, campaigns, ad squads, creatives and ads in memory the way adsapi.snapchat.com/v1
does, answers in the shapes of tests/fixtures/snapchat/*.json (hand-written from developers.snap.com, to re-record with
tools/snapchat_sandbox_launch.py), and can be told to fail. No request leaves the machine.

    f = FakeSnap()
    f.org("org-1", "Ember Shop LLC", accounts=[("acc-1", "Ember Shop", "USD", "America/Los_Angeles")],
          profiles=[("prof-1", "Ember Shop")], pixels={"acc-1": [("px-1", "Ember pixel")]})
    f.code("good-code", user="snap-user-1", orgs=["org-1"])
    adhttp.TRANSPORT = f
    f.fail("/adsquads", "snapchat/error_adsquad_budget.json")   # the next call whose path ends so answers that
    f.fail("/campaigns", "lost")                                 # applied, then the answer is lost (a timeout)

What it checks, like Snap: the OAuth code exchange (client secret, redirect URI), refresh tokens; bearer tokens; access
to an ad account (its organization); new campaigns, ad squads and ads must be PAUSED; an ad squad needs
daily_budget_micro of at least 5,000,000 and a known pixel; a creative needs READY media, a headline of 34 characters
or fewer and a public profile of the same organization; an ad needs a creative of the same ad account.
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

API_HOST, AUTH_HOST = "adsapi.snapchat.com", "accounts.snapchat.com"
CLIENT_SECRET = "snap-fake-client-secret-31415"


def serr(http, code, display, debug=None):
    return (http, {"request_status": "ERROR", "request_id": f"fakeSnapReq{abs(hash(display)) % 10 ** 6}",
                   "debug_message": debug or display, "display_message": display, "error_code": code})


class Refuse(Exception):
    def __init__(self, answer):
        super().__init__("refused")
        self.answer = answer


class FakeSnap:
    def __init__(self):
        self.orgs, self.accounts, self.profiles, self.pixels = {}, {}, {}, {}
        self.grants, self.refresh, self.tokens, self.org_of = {}, {}, {}, {}
        self.store = {}               # id -> object (kind in "_kind")
        self.calls, self.failures = [], []
        self.processing = 0           # media answers PROCESSING this many times after upload
        self.stats = {}               # campaign id -> [(day, {stats})]
        self._ids = itertools.count(1)
        self._tok = itertools.count(1)

    # ------------------------------------------------------------ test controls
    def org(self, oid, name, accounts=(), profiles=(), pixels=None):
        self.orgs[oid] = {"id": oid, "name": name}
        for a in accounts:
            aid, aname, cur, tz = a[:4]
            status = a[4] if len(a) > 4 else "ACTIVE"
            self.accounts[aid] = {"id": aid, "name": aname, "currency": cur, "timezone": tz, "status": status,
                                  "organization_id": oid, "type": "PARTNER"}
        for pid, pname in profiles:
            self.profiles[pid] = {"id": pid, "display_name": pname, "organization_id": oid}
        for aid, pxs in (pixels or {}).items():
            self.pixels[aid] = [{"id": i, "name": n, "status": "ACTIVE"} for i, n in pxs]
        return self

    def code(self, code, user="snap-user-1", orgs=(), refresh=True, name="Ember Owner"):
        self.grants[code] = {"user": user, "orgs": list(orgs), "refresh": refresh, "name": name}
        return self

    def grant(self, refresh_token, user, orgs=()):
        """A refresh token as if a connect had already happened (tests that seed ad_connections directly)."""
        self.refresh[refresh_token] = user
        self.org_of[user] = list(orgs)
        return self

    def fail(self, suffix, response, times=1, method=None):
        self.failures.append([suffix, response, times, method])
        return self

    def objects(self, kind=None, include_deleted=False):
        return [o for o in self.store.values() if (kind is None or o["_kind"] == kind)
                and (include_deleted or o.get("status") != "DELETED")]

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
        q = dict(params or {})
        q.update({k: v[0] for k, v in parse_qs(u.query).items()})
        call = {"method": method, "host": u.netloc, "path": u.path, "params": q, "files": files,
                "data": body if isinstance(body, dict) else dict(body or {}), "headers": dict(headers or {}), "raw": data}
        self.calls.append(call)
        for f in self.failures:
            if f[2] > 0 and u.path.endswith(f[0]) and (f[3] is None or f[3] == method):
                f[2] -= 1
                if f[1] == "lost":
                    self._route(method, u, call)
                    import requests
                    raise requests.ReadTimeout("read timed out")
                return self._answer(f[1])
        return self._route(method, u, call)

    def _answer(self, r):
        if isinstance(r, BaseException):
            raise r
        status, b = (r if isinstance(r, tuple) else (None, r))
        if isinstance(b, str):
            b = fixture(b)
        if status is None:
            status = 400 if (b.get("request_status") == "ERROR" or b.get("error")) else 200
        return adhttp.Response(status, {}, b)

    def _route(self, method, u, call):
        try:
            if u.netloc == AUTH_HOST:
                return self._oauth(u.path, call["data"])
            assert u.netloc == API_HOST, u.netloc
            user = self._auth(call["headers"])
            return adhttp.Response(200, {}, self._api(method, u.path[len("/v1"):], call, user))
        except Refuse as e:
            return self._answer(e.answer)

    # ------------------------------------------------------------ OAuth
    def _oauth(self, path, data):
        assert path == "/login/oauth2/access_token", path
        if data.get("client_secret") != CLIENT_SECRET:
            return adhttp.Response(401, {}, {"error": "invalid_client", "error_description": "Invalid client"})
        if data.get("grant_type") == "authorization_code":
            gr = self.grants.pop(data.get("code"), None)
            assert data.get("redirect_uri", "").endswith("/connect/snapchat/callback"), data.get("redirect_uri")
            if gr is None:
                return adhttp.Response(400, {}, {"error": "invalid_grant", "error_description": "Invalid code"})
            self.org_of[gr["user"]] = gr["orgs"]
            self.org_of.setdefault("_names", {})[gr["user"]] = gr["name"]
            access = self._mint(gr["user"])
            out = {"access_token": access, "token_type": "Bearer", "expires_in": 1800, "scope": "snapchat-marketing-api"}
            if gr["refresh"]:
                rt = f"snap-refresh-{next(self._tok)}-secretvalue"
                self.refresh[rt] = gr["user"]
                out["refresh_token"] = rt
            return adhttp.Response(200, {}, out)
        if data.get("grant_type") == "refresh_token":
            user = self.refresh.get(data.get("refresh_token"))
            if user is None:
                return adhttp.Response(400, {}, {"error": "invalid_grant", "error_description": "Invalid refresh token"})
            return adhttp.Response(200, {}, {"access_token": self._mint(user), "token_type": "Bearer", "expires_in": 1800,
                                             "refresh_token": data["refresh_token"], "scope": "snapchat-marketing-api"})
        raise AssertionError(data.get("grant_type"))

    def _mint(self, user):
        t = f"snap-access-{next(self._tok)}-secretvalue"
        self.tokens[t] = user
        return t

    def _auth(self, headers):
        h = headers.get("Authorization") or ""
        user = self.tokens.get(h[7:]) if h.startswith("Bearer ") else None
        if user is None:
            raise Refuse(serr(401, "E1002", "Unauthorized", "Invalid access token"))
        return user

    def _account(self, user, aid):
        a = self.accounts.get(aid)
        if a is None or a["organization_id"] not in self.org_of.get(user, []):
            raise Refuse(serr(403, "E3003", "You don't have permission to access this ad account."))
        return a

    # ------------------------------------------------------------ the API
    def _wrap(self, plural, singular, objs):
        return {"request_status": "SUCCESS", "request_id": "fakeSnapOk",
                plural: [{"sub_request_status": "SUCCESS", singular: self._clean(o)} for o in objs]}

    @staticmethod
    def _clean(o):
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def _new(self, kind, obj, account):
        oid = f"{kind[:2]}-{next(self._ids):06d}-0000-4000-8000-0000000000aa"
        o = dict(obj, id=oid, _kind=kind, _account=account, created_at="2026-11-09T12:00:00.000Z",
                 updated_at="2026-11-09T12:00:00.000Z")
        self.store[oid] = o
        return o

    def _get(self, kind, oid, user):
        o = self.store.get(oid)
        if o is None or o["_kind"] != kind:
            raise Refuse(serr(404, "E2002", f"{kind} not found"))
        self._account(user, o["_account"])
        return o

    def _api(self, method, path, call, user):
        d = call["data"]
        if path == "/me" and method == "GET":
            return {"request_status": "SUCCESS", "me": {"id": user, "email": f"{user}@example.com",
                                                        "display_name": (self.org_of.get("_names") or {}).get(user, user),
                                                        "organization_id": (self.org_of.get(user) or [None])[0]}}
        if path == "/me/organizations" and method == "GET":
            out = []
            for oid in self.org_of.get(user, []):
                org = dict(self.orgs[oid])
                if call["params"].get("with_ad_accounts") == "true":
                    org["ad_accounts"] = [{k: v for k, v in a.items() if k != "organization_id"}
                                          for a in self.accounts.values() if a["organization_id"] == oid]
                out.append(org)
            return self._wrap("organizations", "organization", out)
        m = re.fullmatch(r"/organizations/([^/]+)/public_profiles", path)
        if m and method == "GET":
            if m.group(1) not in self.org_of.get(user, []):
                raise Refuse(serr(403, "E3003", "No access to this organization."))
            return self._wrap("public_profiles", "public_profile",
                              [p for p in self.profiles.values() if p["organization_id"] == m.group(1)])
        m = re.fullmatch(r"/adaccounts/([^/]+)/(pixels|media|campaigns|creatives)", path)
        if m:
            aid, what = m.groups()
            self._account(user, aid)
            if what == "pixels" and method == "GET":
                return self._wrap("pixels", "pixel", self.pixels.get(aid, []))
            kind = {"media": "media", "campaigns": "campaign", "creatives": "creative"}[what]
            if method == "GET":
                return self._wrap(what, kind, [o for o in self.store.values() if o["_kind"] == kind and o["_account"] == aid
                                               and o.get("status") != "DELETED"])
            if method == "POST":
                return self._create(kind, what, d[what], aid, user)
            if method == "PUT" and kind == "campaign":
                return self._put("campaign", "campaigns", d["campaigns"], user)
        m = re.fullmatch(r"/campaigns/([^/]+)/adsquads", path)
        if m:
            camp = self._get("campaign", m.group(1), user)
            if method == "GET":
                return self._wrap("adsquads", "adsquad", [o for o in self.objects("adsquad") if o["campaign_id"] == camp["id"]])
            if method == "POST":
                return self._create("adsquad", "adsquads", d["adsquads"], camp["_account"], user)
            if method == "PUT":
                return self._put("adsquad", "adsquads", d["adsquads"], user)
        m = re.fullmatch(r"/adsquads/([^/]+)/ads", path)
        if m:
            sq = self._get("adsquad", m.group(1), user)
            if method == "GET":
                return self._wrap("ads", "ad", [o for o in self.objects("ad") if o["ad_squad_id"] == sq["id"]])
            if method == "POST":
                return self._create("ad", "ads", d["ads"], sq["_account"], user)
        m = re.fullmatch(r"/media/([^/]+)/upload", path)
        if m and method == "POST":
            md = self._get("media", m.group(1), user)
            assert call["files"] and "file" in call["files"], "the upload sends the file as 'file'"
            md["media_status"] = "PROCESSING" if self.processing else "READY"
            md["_processing"] = self.processing
            return {"request_status": "SUCCESS", "result": {"id": md["id"], "media_status": md["media_status"]}}
        m = re.fullmatch(r"/campaigns/([^/]+)/stats", path)
        if m and method == "GET":
            camp = self._get("campaign", m.group(1), user)
            p = call["params"]
            assert p.get("granularity") == "DAY" and p.get("start_time") and p.get("end_time"), p
            ts = [{"start_time": f"{day}T00:00:00.000-08:00", "end_time": f"{day}T00:00:00.000-08:00", "stats": st}
                  for day, st in self.stats.get(camp["id"], [])]
            return {"request_status": "SUCCESS", "timeseries_stats": [{"sub_request_status": "SUCCESS", "timeseries_stat": {
                "id": camp["id"], "type": "CAMPAIGN", "granularity": "DAY", "start_time": p["start_time"],
                "end_time": p["end_time"], "timeseries": ts}}]}
        m = re.fullmatch(r"/(campaigns|adsquads|ads|creatives|media)/([^/]+)", path)
        if m:
            plural, oid = m.groups()
            kind = {"campaigns": "campaign", "adsquads": "adsquad", "ads": "ad", "creatives": "creative", "media": "media"}[plural]
            o = self._get(kind, oid, user)
            if method == "GET":
                if kind == "media" and o.get("_processing"):
                    o["_processing"] -= 1
                    if not o["_processing"]:
                        o["media_status"] = "READY"
                return self._wrap(plural, kind, [o])
            if method == "DELETE" and kind in ("ad", "adsquad", "campaign"):
                o["status"] = "DELETED"
                return {"request_status": "SUCCESS", "request_id": "fakeSnapOk"}
        raise AssertionError(f"no fake Snapchat answer for {method} {path}")

    def _create(self, kind, plural, objs, aid, user):
        assert isinstance(objs, list) and len(objs) == 1, "Campaignify sends one object per create"
        o = dict(objs[0])
        if kind in ("campaign", "adsquad", "ad") and o.get("status") != "PAUSED":
            raise AssertionError(f"a new {kind} must be PAUSED, got {o.get('status')}")
        if kind == "campaign":
            assert o.get("ad_account_id") == aid and o.get("start_time") and (o.get("objective_v2_properties") or {}).get(
                "objective_v2_type") in ("AWARENESS_AND_ENGAGEMENT", "SALES", "TRAFFIC", "APP_PROMOTION", "LEADS")
        if kind == "adsquad":
            if int(o.get("daily_budget_micro") or 0) < 5_000_000:
                raise Refuse(serr(400, "E1008", "The daily budget must be at least 5.00."))
            if o.get("pixel_id") and o["pixel_id"] not in {p["id"] for p in self.pixels.get(aid, [])}:
                raise Refuse(serr(400, "E1001", "Pixel not found for this ad account."))
            for k in ("type", "placement_v2", "billing_event", "optimization_goal", "bid_strategy", "delivery_constraint",
                      "targeting"):
                assert k in o, f"ad squad needs {k}"
        if kind == "creative":
            md = self.store.get(o.get("top_snap_media_id"))
            if md is None or md.get("media_status") != "READY":
                raise Refuse(serr(400, "E2101", "The media isn't ready."))
            if len(o.get("headline") or "") > 34 or not o.get("headline"):
                raise Refuse(serr(400, "E1001", "Headline must be 1 to 34 characters."))
            prof = self.profiles.get((o.get("profile_properties") or {}).get("profile_id"))
            if prof is None or prof["organization_id"] != self.accounts[aid]["organization_id"]:
                raise Refuse(serr(400, "E1001", "A public profile of this organization is required."))
            assert o.get("type") == "WEB_VIEW" and (o.get("web_view_properties") or {}).get("url", "").startswith("https://")
            o["review_status"] = "PENDING_REVIEW"
        if kind == "ad":
            cr = self.store.get(o.get("creative_id"))
            if cr is None or cr["_account"] != aid:
                raise Refuse(serr(400, "E2002", "Creative not found."))
            o["review_status"] = "PENDING"
        if kind == "media":
            o["media_status"] = "PENDING_UPLOAD"
        made = self._new(kind, o, aid)
        return self._wrap(plural, kind, [made])

    def _put(self, kind, plural, objs, user):
        out = []
        for o in objs:
            cur = self._get(kind, o["id"], user)
            for k in ("name", "status"):
                assert k in o, f"a {kind} PUT carries the whole object ({k} missing)"
            cur.update({k: v for k, v in o.items() if k != "id"})
            out.append(cur)
        return self._wrap(plural, kind, out)
