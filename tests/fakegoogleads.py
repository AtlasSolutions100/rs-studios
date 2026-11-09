"""A fake Google Ads REST endpoint and Google OAuth server (tests and screenshots only). It keeps customers,
campaign budgets, campaigns, ad groups, keywords, ads, assets, campaign assets, asset groups and their links in
memory the way the Google Ads API v25 REST interface does, answers in the shapes of tests/fixtures/google/*.json,
and can be told to fail. No request leaves the machine.

    f = FakeGoogleAds()
    f.customer("1112223334", "Ember Shop")                 # a client account
    f.customer("9998887776", "Agency MCC", manager=True)    # a manager, with clients under it:
    f.customer("5556667778", "Client B", parent="9998887776")
    f.code("good-code", sub="10871", email="owner@example.com", access=["1112223334", "9998887776"])
    adhttp.TRANSPORT = f
    f.fail("googleAds:mutate", "google/error_policy_finding.json")    # the next mutate answers that error
    f.fail("googleAds:mutate", "lost")       # the mutate is applied, then the answer is lost (a timeout)

What it checks, like Google: the OAuth code exchange (PKCE verifier, redirect URI), refresh and revoke; bearer
tokens; access to a customer (directly, or through a manager named in login-customer-id); a mutate is atomic
(any error leaves nothing) and resolves temporary ids in order; a new campaign must be PAUSED and its name and
its budget's name unique; a responsive search ad has 3 to 15 headlines (30 characters) and 2 to 4 descriptions
(90); Performance Max with brand guidelines needs exactly 1 business name and 1 to 5 logos linked at campaign
level in the same mutate, and an asset group needs 3 headlines, 1 long headline, 2 descriptions, 1 landscape
and 1 square image. GAQL: FROM customer, customer_client, campaign, ad_group_ad, asset_group, asset, with the
WHERE forms the adapter uses (=, !=, LIKE, level > 0).
"""
import base64
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

V = "v25"


def gerr(http, status, category, code, message, index=None, extra=None):
    """A Google Ads error body: {"error": {code, message, status, details: [GoogleAdsFailure]}}."""
    e = {"errorCode": {category: code}, "message": message}
    if index is not None:
        e["location"] = {"fieldPathElements": [{"fieldName": "mutate_operations", "index": index}]}
    if extra:
        e.update(extra)
    return (http, {"error": {"code": http, "message": "Request contains an invalid argument." if http == 400 else message,
                             "status": status, "details": [{
                                 "@type": f"type.googleapis.com/google.ads.googleads.{V}.errors.GoogleAdsFailure",
                                 "errors": [e], "requestId": "fakeReq" + str(abs(hash(message)) % 10 ** 8)}]}})


class Refuse(Exception):
    def __init__(self, answer):
        super().__init__("refused")
        self.answer = answer


class FakeGoogleAds:
    def __init__(self):
        self.customers = {}          # id -> {name, currency, tz, manager, test, status, parent}
        self.grants = {}             # code -> {sub, email, scope, access, refresh}
        self.refresh = {}            # refresh token -> sub
        self.tokens = {}             # access token -> sub
        self.access_of = {}          # sub -> [customer ids reachable directly]
        self.store = {}              # resource name -> object
        self.calls = []
        self.failures = []           # [suffix, response, times]
        self.mutates = 0             # successful googleAds:mutate calls
        self._ids = itertools.count(20000000001)
        self._tok = itertools.count(1)
        self.scope_of = {}           # sub -> granted scope string (the Data Manager API checks its scope)
        self.ingested = []           # Data Manager events:ingest bodies that were accepted

    # ------------------------------------------------------------ test controls
    def customer(self, cid, name, currency="USD", tz="America/Chicago", manager=False, test=False, status="ENABLED",
                 parent=None):
        self.customers[cid] = {"name": name, "currency": currency, "tz": tz, "manager": manager, "test": test,
                               "status": status, "parent": parent}
        return self

    def code(self, code, sub="108700000000000000001", email="owner@example.com", access=(), scope=None, refresh=True):
        self.grants[code] = {"sub": sub, "email": email, "access": list(access), "refresh": refresh,
                             "scope": scope or "openid https://www.googleapis.com/auth/userinfo.email "
                                               "https://www.googleapis.com/auth/adwords"}
        self.access_of[sub] = list(access)
        self.scope_of[sub] = self.grants[code]["scope"]
        return self

    def grant(self, refresh_token, sub, access=(), scope=None):
        """A refresh token as if a connect had already happened (tests that seed ad_connections directly)."""
        self.refresh[refresh_token] = sub
        self.access_of[sub] = list(access)
        self.scope_of[sub] = scope or ("openid https://www.googleapis.com/auth/userinfo.email "
                                       "https://www.googleapis.com/auth/adwords")
        return self

    def tracking(self, cid, owner=None, terms=False, ec_leads=False):
        """customer.conversion_tracking_setting: `owner` a manager that manages this customer's conversions
        (cross-account conversion tracking), the customer data terms and enhanced conversions for leads."""
        self.customers[cid].update(conv_owner=owner, terms=terms, ec_leads=ec_leads)
        return self

    def conversion_action(self, cid, name, typ="WEBPAGE", category="SUBMIT_LEAD_FORM", status="ENABLED", primary=True):
        """An existing conversion action on customer `cid` (made in the Google Ads UI, or by an earlier sync)."""
        oid = str(next(self._ids))
        rn = f"customers/{cid}/conversionActions/{oid}"
        self.store[rn] = {"kind": "conversion_action", "id": oid, "rn": rn, "customer": cid, "name": name, "type": typ,
                          "category": category, "status": status, "primary": primary}
        return self.store[rn]

    def fail(self, suffix, response, times=1):
        self.failures.append([suffix, response, times])
        return self

    def objects(self, kind=None, include_removed=False):
        return [o for o in self.store.values() if (kind is None or o["kind"] == kind)
                and (include_removed or o.get("status") != "REMOVED")]

    def find(self, suffix=""):
        return [c for c in self.calls if c["path"].endswith(suffix)]

    # ------------------------------------------------------------ transport
    def __call__(self, method, url, params=None, data=None, headers=None, timeout=None, files=None):
        u = urlsplit(url)
        body = data
        if isinstance(data, str):
            try:
                body = json.loads(data)
            except ValueError:
                body = {}
        call = {"method": method, "host": u.netloc, "path": u.path, "params": dict(params or {}),
                "data": body if isinstance(body, dict) else dict(body or {}), "headers": dict(headers or {}), "raw": data}
        self.calls.append(call)
        for f in self.failures:
            if f[2] > 0 and u.path.endswith(f[0]):
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
            status = int((b.get("error") or {}).get("code") or 400) if isinstance(b.get("error"), dict) else \
                (400 if b.get("error") else 200)
        return adhttp.Response(status, {}, b)

    def _route(self, method, u, call):
        try:
            if u.netloc == "oauth2.googleapis.com":
                return self._oauth(u.path, call["data"])
            sub = self._auth(call["headers"])
            if u.netloc == "datamanager.googleapis.com":
                return self._datamanager(sub, u.path, method, call)
            path = u.path[len(f"/{V}"):] if u.path.startswith(f"/{V}") else u.path
            if path == "/customers:listAccessibleCustomers" and method == "GET":
                return adhttp.Response(200, {}, {"resourceNames": [f"customers/{c}" for c in self.access_of.get(sub, [])]})
            m = re.fullmatch(r"/customers/(\d{10})/(googleAds:search|googleAds:mutate|assets:mutate|campaigns:mutate|"
                             r"conversionActions:mutate)", path)
            if not m or method != "POST":
                raise AssertionError(f"no fake Google answer for {method} {u.path}")
            cid, op = m.groups()
            self._access(sub, cid, call["headers"].get("login-customer-id"))
            if op == "googleAds:search":
                return adhttp.Response(200, {}, self._search(cid, call["data"]["query"]))
            if op == "googleAds:mutate":
                return adhttp.Response(200, {}, self._mutate(cid, call["data"]))
            if op == "assets:mutate":
                return adhttp.Response(200, {}, self._assets(cid, call["data"]))
            if op == "conversionActions:mutate":
                return adhttp.Response(200, {}, self._conversion_actions(cid, call["data"]))
            return adhttp.Response(200, {}, self._campaigns(cid, call["data"]))
        except Refuse as e:
            return self._answer(e.answer)

    # ------------------------------------------------------------ OAuth
    def _oauth(self, path, data):
        if path == "/revoke":
            self.refresh.pop(data.get("token"), None)
            return adhttp.Response(200, {}, {})
        assert path == "/token", path
        if data.get("grant_type") == "authorization_code":
            g = self.grants.pop(data.get("code"), None)
            assert data.get("code_verifier") and data.get("redirect_uri", "").endswith("/connect/google/callback")
            if g is None:
                return adhttp.Response(400, {}, {"error": "invalid_grant", "error_description": "Bad Request"})
            access = f"ya29.fake-access-{next(self._tok)}-secretvalue"
            self.tokens[access] = g["sub"]
            out = {"access_token": access, "expires_in": 3599, "scope": g["scope"], "token_type": "Bearer",
                   "id_token": "eyJhbGciOiJub25lIn0." + base64.urlsafe_b64encode(json.dumps(
                       {"sub": g["sub"], "email": g["email"], "iss": "https://accounts.google.com"}).encode()).decode()
                   .rstrip("=") + ".sig"}
            if g["refresh"]:
                rt = f"1//fake-refresh-{next(self._tok)}-secretvalue"
                self.refresh[rt] = g["sub"]
                out["refresh_token"] = rt
            return adhttp.Response(200, {}, out)
        if data.get("grant_type") == "refresh_token":
            sub = self.refresh.get(data.get("refresh_token"))
            if sub is None:
                return adhttp.Response(400, {}, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            access = f"ya29.fake-access-{next(self._tok)}-secretvalue"
            self.tokens[access] = sub
            return adhttp.Response(200, {}, {"access_token": access, "expires_in": 3599, "token_type": "Bearer"})
        raise AssertionError(f"grant_type {data.get('grant_type')}")

    def _auth(self, headers):
        h = headers.get("Authorization") or ""
        sub = self.tokens.get(h[7:]) if h.startswith("Bearer ") else None
        if sub is None:
            raise Refuse("google/error_oauth_token_expired.json")
        return sub

    # ------------------------------------------------------------ Data Manager API (events:ingest)
    def _datamanager(self, sub, path, method, call):
        """POST /v1/events:ingest as the Data Manager API reference describes it (read 2026-11-09, not yet against
        a live account): the datamanager scope, destinations the login can reach, HEX-encoded SHA-256 identifiers,
        and an UPLOAD_CLICKS conversion action as productDestinationId."""
        assert method == "POST" and path == "/v1/events:ingest", path
        if "https://www.googleapis.com/auth/datamanager" not in (self.scope_of.get(sub) or "").split():
            raise Refuse("google/datamanager_error_scope.json")
        body = call["data"]
        bad = lambda why: Refuse((400, {"error": {"code": 400, "message": why, "status": "INVALID_ARGUMENT"}}))  # noqa: E731
        if body.get("encoding") != "HEX" or not body.get("destinations") or not body.get("events"):
            raise bad("encoding, destinations and events are required")
        for d in body["destinations"]:
            op, login = d["operatingAccount"], (d.get("loginAccount") or {}).get("accountId")
            if op.get("accountType") != "GOOGLE_ADS" or not re.fullmatch(r"\d{10}", op.get("accountId") or ""):
                raise bad("operatingAccount must be a GOOGLE_ADS customer id")
            self._access(sub, op["accountId"], login)
            ca = self.store.get(f"customers/{op['accountId']}/conversionActions/{d.get('productDestinationId')}")
            if not ca or ca["kind"] != "conversion_action":
                raise Refuse("google/datamanager_error_destination.json")
            if ca["type"] != "UPLOAD_CLICKS":
                raise bad("The conversion action can't receive imported conversions.")
        for ev in body["events"]:
            if not ev.get("transactionId") or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ev.get("eventTimestamp") or ""):
                raise bad("transactionId and eventTimestamp are required")
            ids = (ev.get("userData") or {}).get("userIdentifiers") or []
            if not ids and not (ev.get("adIdentifiers") or {}).get("gclid"):
                raise bad("An event needs user identifiers or a click identifier.")
            for i in ids:
                (k, v), = i.items()
                assert k in ("emailAddress", "phoneNumber") and re.fullmatch(r"[0-9a-f]{64}", v), "HEX SHA-256 only"
        if not body.get("validateOnly"):
            self.ingested.append(body)
        return adhttp.Response(200, {}, fixture("google/datamanager_ingest_ok.json"))

    def _tree(self, root):
        out = []
        for cid, c in self.customers.items():
            p, level = c["parent"], 1
            while p:
                if p == root:
                    out.append((cid, level))
                    break
                p, level = self.customers.get(p, {}).get("parent"), level + 1
        return out

    def _access(self, sub, cid, login):
        mine = self.access_of.get(sub, [])
        if login:
            if login not in mine or (cid != login and cid not in {c for c, _ in self._tree(login)}):
                raise Refuse(gerr(403, "PERMISSION_DENIED", "authorizationError", "USER_PERMISSION_DENIED",
                                  "User doesn't have permission to access customer."))
        elif cid not in mine:
            raise Refuse(gerr(403, "PERMISSION_DENIED", "authorizationError", "USER_PERMISSION_DENIED",
                              "User doesn't have permission to access customer. Note: If you're accessing a client "
                              "customer, the manager's customer id must be set in the 'login-customer-id' header."))
        if self.customers.get(cid, {}).get("status", "ENABLED") != "ENABLED":
            raise Refuse(gerr(403, "PERMISSION_DENIED", "authorizationError", "CUSTOMER_NOT_ENABLED",
                              "The customer account can't be accessed because it is not yet enabled or has been deactivated."))

    # ------------------------------------------------------------ GAQL
    def _search(self, cid, query):
        res = re.search(r"\bFROM\s+(\w+)", query).group(1)
        where = (re.search(r"\bWHERE\s+(.+?)(?:\s+LIMIT\s+\d+)?$", query, re.S) or [None, ""])[1]
        conds = re.findall(r"([\w.]+)\s*(=|!=|LIKE|>)\s*('(?:[^'\\]|\\.)*'|\d+)", where)

        def ok(row_fields: dict):
            for f, op, v in conds:
                v = v[1:-1].replace("\\'", "'") if v.startswith("'") else int(v)
                have = row_fields.get(f)
                if op == "=" and str(have) != str(v):
                    return False
                if op == "!=" and str(have) == str(v):
                    return False
                if op == ">" and not (int(have or 0) > v):
                    return False
                if op == "LIKE":
                    rx = "^" + re.sub(r"\[(.)\]|%|_|[^%_\[]+", lambda m: re.escape(m.group(1)) if m.group(1) else
                                      ".*" if m.group(0) == "%" else "." if m.group(0) == "_" else re.escape(m.group(0)), v) + "$"
                    if not re.match(rx, str(have or "")):
                        return False
            return True
        rows = []
        if res == "customer":
            c = self.customers[cid]
            rows.append({"customer": {"resourceName": f"customers/{cid}", "id": cid, "descriptiveName": c["name"],
                                      "currencyCode": c["currency"], "timeZone": c["tz"], "manager": c["manager"],
                                      "testAccount": c["test"], "status": c["status"],
                                      "conversionTrackingSetting": {
                                          "conversionTrackingId": str(self._conv_id(cid)),
                                          "googleAdsConversionCustomer": f"customers/{c.get('conv_owner') or cid}",
                                          **({"crossAccountConversionTrackingId": str(self._conv_id(c['conv_owner']))}
                                             if c.get("conv_owner") else {}),
                                          "acceptedCustomerDataTerms": bool(c.get("terms")),
                                          "enhancedConversionsForLeadsEnabled": bool(c.get("ec_leads"))}}})
        elif res == "customer_client":
            for kid, level in self._tree(cid):
                k = self.customers[kid]
                if ok({"customer_client.level": level}):
                    rows.append({"customerClient": {"resourceName": f"customers/{cid}/customerClients/{kid}", "id": kid,
                                                    "descriptiveName": k["name"], "currencyCode": k["currency"],
                                                    "timeZone": k["tz"], "manager": k["manager"], "testAccount": k["test"],
                                                    "status": k["status"], "level": str(level)}})
        elif res == "campaign":
            for o in self._mine(cid, "campaign"):
                if ok({"campaign.name": o["name"], "campaign.status": o["status"], "campaign.resource_name": o["rn"]}):
                    rows.append({"campaign": {"resourceName": o["rn"], "id": o["id"], "name": o["name"], "status": o["status"]}})
        elif res == "ad_group_ad":
            for ad in self._mine(cid, "ad"):
                grp = self.store[ad["adGroup"]]
                camp = self.store[grp["campaign"]]
                if ok({"campaign.resource_name": camp["rn"]}):
                    rows.append({"campaign": {"resourceName": camp["rn"], "id": camp["id"], "status": camp["status"],
                                              "servingStatus": "SERVING"},
                                 "adGroup": {"resourceName": grp["rn"], "id": grp["id"], "name": grp["name"]},
                                 "adGroupAd": {"resourceName": ad["rn"], "status": ad["status"], "ad": {"id": ad["id"]},
                                               "policySummary": {"approvalStatus": "UNKNOWN", "reviewStatus": "REVIEW_IN_PROGRESS"}}})
        elif res == "asset_group":
            for ag in self._mine(cid, "asset_group"):
                camp = self.store[ag["campaign"]]
                if ok({"campaign.resource_name": camp["rn"]}):
                    rows.append({"campaign": {"resourceName": camp["rn"], "id": camp["id"], "status": camp["status"],
                                              "servingStatus": "SERVING"},
                                 "assetGroup": {"resourceName": ag["rn"], "id": ag["id"], "name": ag["name"],
                                                "status": ag["status"], "primaryStatus": "PENDING"}})
        elif res == "conversion_action":
            for a in self._mine(cid, "conversion_action"):
                if ok({"conversion_action.status": a["status"], "conversion_action.name": a["name"]}):
                    rows.append({"conversionAction": self._action_row(a)})
        elif res == "asset":
            for a in self._mine(cid, "asset"):
                if ok({"asset.name": a.get("name") or "", "asset.resource_name": a["rn"]}):
                    rows.append({"asset": {"resourceName": a["rn"], "id": a["id"], "name": a.get("name")}})
        else:
            raise AssertionError(f"GAQL FROM {res}")
        return {"results": rows, "fieldMask": "x"} if rows else {"fieldMask": "x"}

    def _mine(self, cid, kind):
        return [o for o in self.store.values() if o["kind"] == kind and o["customer"] == cid]

    # ------------------------------------------------------------ mutates
    def _new(self, cid, kind, path, fields, temp, made):
        oid = str(next(self._ids))
        rn = f"customers/{cid}/{path}/{oid}"
        o = dict(fields, kind=kind, id=oid, rn=rn, customer=cid)
        if temp:
            made["temps"][temp] = rn
        made["new"][rn] = o
        return o

    def _ref(self, rn, made, kind, i):
        rn = made["temps"].get(rn, rn)
        o = made["new"].get(rn) or self.store.get(rn)
        if o is None or o["kind"] != kind:
            raise Refuse(gerr(400, "INVALID_ARGUMENT", "mutateError", "RESOURCE_NOT_FOUND",
                              f"Resource was not found: {rn}", i))
        return o

    def _mutate(self, cid, body):
        assert body.get("partialFailure") is False and body.get("validateOnly") is False
        made = {"temps": {}, "new": {}, "camp_assets": [], "ag_assets": []}
        answers = []
        names = {(o["kind"], o["name"]) for o in self._mine(cid, "campaign") + self._mine(cid, "budget")
                 if o.get("status") != "REMOVED"}
        for i, op in enumerate(body["mutateOperations"]):
            (kind, inner), = op.items()
            c = inner["create"]
            temp = c.get("resourceName") if re.search(r"/-\d+$", c.get("resourceName") or "") else None
            if kind == "campaignBudgetOperation":
                if ("budget", c["name"]) in names:
                    raise Refuse(gerr(400, "INVALID_ARGUMENT", "campaignBudgetError", "DUPLICATE_NAME",
                                      "Campaign budget with the given name already exists.", i))
                if int(c["amountMicros"]) <= 0 or int(c["amountMicros"]) % 10000:
                    raise Refuse("google/error_budget_too_small.json")
                names.add(("budget", c["name"]))
                o = self._new(cid, "budget", "campaignBudgets", {"name": c["name"], "amount": c["amountMicros"]}, temp, made)
                answers.append({"campaignBudgetResult": {"resourceName": o["rn"]}})
            elif kind == "campaignOperation":
                assert c["status"] == "PAUSED", "every new campaign is PAUSED"
                assert c["containsEuPoliticalAdvertising"] == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
                assert c["advertisingChannelType"] in ("SEARCH", "PERFORMANCE_MAX")
                if ("campaign", c["name"]) in names:
                    raise Refuse(gerr(400, "INVALID_ARGUMENT", "campaignError", "DUPLICATE_CAMPAIGN_NAME",
                                      "A campaign with this name already exists.", i))
                self._ref(c["campaignBudget"], made, "budget", i)
                names.add(("campaign", c["name"]))
                o = self._new(cid, "campaign", "campaigns", {"name": c["name"], "status": "PAUSED", "fields": c}, temp, made)
                answers.append({"campaignResult": {"resourceName": o["rn"]}})
            elif kind == "campaignCriterionOperation":
                self._ref(c["campaign"], made, "campaign", i)
                o = self._new(cid, "criterion", "campaignCriteria", {"fields": c, "campaign": made["temps"].get(c["campaign"], c["campaign"])}, None, made)
                answers.append({"campaignCriterionResult": {"resourceName": o["rn"]}})
            elif kind == "adGroupOperation":
                camp = self._ref(c["campaign"], made, "campaign", i)
                o = self._new(cid, "ad_group", "adGroups", {"name": c["name"], "status": c["status"], "campaign": camp["rn"]},
                              temp, made)
                answers.append({"adGroupResult": {"resourceName": o["rn"]}})
            elif kind == "adGroupCriterionOperation":
                grp = self._ref(c["adGroup"], made, "ad_group", i)
                kw = c["keyword"]
                assert kw["matchType"] in ("EXACT", "PHRASE", "BROAD") and kw["text"]
                o = self._new(cid, "keyword", "adGroupCriteria", {"text": kw["text"], "match": kw["matchType"],
                                                                  "adGroup": grp["rn"]}, None, made)
                answers.append({"adGroupCriterionResult": {"resourceName": o["rn"]}})
            elif kind == "adGroupAdOperation":
                grp = self._ref(c["adGroup"], made, "ad_group", i)
                rsa = c["ad"]["responsiveSearchAd"]
                hs, ds = rsa["headlines"], rsa["descriptions"]
                if not 3 <= len(hs) <= 15 or any(len(h["text"]) > 30 for h in hs):
                    raise Refuse(gerr(400, "INVALID_ARGUMENT", "adError", "TOO_FEW_HEADLINES" if len(hs) < 3 else "TOO_LONG",
                                      "Responsive search ad headlines are out of range.", i))
                if not 2 <= len(ds) <= 4 or any(len(d["text"]) > 90 for d in ds):
                    raise Refuse(gerr(400, "INVALID_ARGUMENT", "adError", "TOO_FEW_DESCRIPTIONS",
                                      "Responsive search ad descriptions are out of range.", i))
                assert c["ad"]["finalUrls"][0].startswith("https://")
                o = self._new(cid, "ad", "adGroupAds", {"status": c["status"], "adGroup": grp["rn"], "rsa": rsa,
                                                        "final_urls": c["ad"]["finalUrls"]}, None, made)
                o["rn"] = f"customers/{cid}/adGroupAds/{grp['id']}~{o['id']}"
                made["new"][o["rn"]] = made["new"].pop(f"customers/{cid}/adGroupAds/{o['id']}")
                answers.append({"adGroupAdResult": {"resourceName": o["rn"]}})
            elif kind == "assetOperation":
                o = self._new(cid, "asset", "assets", {"text": (c.get("textAsset") or {}).get("text"), "type": "TEXT"},
                              temp, made)
                answers.append({"assetResult": {"resourceName": o["rn"]}})
            elif kind == "campaignAssetOperation":
                camp = self._ref(c["campaign"], made, "campaign", i)
                a = self._ref(c["asset"], made, "asset", i)
                made["camp_assets"].append((camp["rn"], a["rn"], c["fieldType"]))
                answers.append({"campaignAssetResult": {"resourceName": f"{camp['rn']}~{a['id']}~{c['fieldType']}"}})
            elif kind == "assetGroupOperation":
                camp = self._ref(c["campaign"], made, "campaign", i)
                assert c["finalUrls"][0].startswith("https://")
                o = self._new(cid, "asset_group", "assetGroups", {"name": c["name"], "status": c["status"],
                                                                  "campaign": camp["rn"]}, temp, made)
                answers.append({"assetGroupResult": {"resourceName": o["rn"]}})
            elif kind == "assetGroupAssetOperation":
                ag = self._ref(c["assetGroup"], made, "asset_group", i)
                a = self._ref(c["asset"], made, "asset", i)
                made["ag_assets"].append((ag["rn"], a["rn"], c["fieldType"]))
                answers.append({"assetGroupAssetResult": {"resourceName": f"{ag['rn']}~{a['id']}~{c['fieldType']}"}})
            else:
                raise AssertionError(kind)
        self._pmax_rules(made)
        for rn, o in made["new"].items():
            self.store[rn] = o
        for camp, a, f in made["camp_assets"]:
            self.store[camp].setdefault("assets", []).append((a, f))
        for ag, a, f in made["ag_assets"]:
            self.store[ag].setdefault("assets", []).append((a, f))
        self.mutates += 1
        return {"mutateOperationResponses": answers}

    def _pmax_rules(self, made):
        for o in made["new"].values():
            if o["kind"] == "campaign" and o["fields"]["advertisingChannelType"] == "PERFORMANCE_MAX":
                links = [f for c, _, f in made["camp_assets"] if c == o["rn"]]
                if o["fields"].get("brandGuidelinesEnabled") and links.count("BUSINESS_NAME") != 1:
                    raise Refuse("google/error_pmax_business_name.json")
                if o["fields"].get("brandGuidelinesEnabled") and not 1 <= sum(f in ("LOGO", "LANDSCAPE_LOGO") for f in links) <= 5:
                    raise Refuse("google/error_pmax_logo.json")
            if o["kind"] == "asset_group":
                fs = [f for ag, _, f in made["ag_assets"] if ag == o["rn"]]
                for field, n, name in (("HEADLINE", 3, "headline"), ("LONG_HEADLINE", 1, "long_headline"),
                                       ("DESCRIPTION", 2, "description"), ("MARKETING_IMAGE", 1, "marketing_image"),
                                       ("SQUARE_MARKETING_IMAGE", 1, "square_marketing_image")):
                    if fs.count(field) < n:
                        raise Refuse(f"google/error_not_enough_{name}.json")

    # ------------------------------------------------------------ conversion actions (ConversionActionService)
    def _conv_id(self, cid):
        return 900000000 + int(cid) % 99999999

    def _action_row(self, a):
        """A conversion_action GAQL row the way v25 REST answers it; WEBPAGE actions carry tag_snippets in Google's
        format (the Google tag plus an event snippet with send_to AW-<conversion tracking id>/<label>)."""
        row = {"resourceName": a["rn"], "id": a["id"], "name": a["name"], "status": a["status"], "type": a["type"],
               "category": a["category"], "primaryForGoal": a["primary"]}
        if a["type"] == "WEBPAGE":
            aw = f"AW-{self._conv_id(a['customer'])}"
            label = a.setdefault("label", "Lb" + base64.urlsafe_b64encode(a["id"].encode()).decode().rstrip("=")[-14:])
            gst = (f"<!-- Google tag (gtag.js) -->\n<script async src=\"https://www.googletagmanager.com/gtag/js?id={aw}\">"
                   f"</script>\n<script>\n  window.dataLayer = window.dataLayer || [];\n  function gtag(){{dataLayer.push(arguments);}}"
                   f"\n  gtag('js', new Date());\n\n  gtag('config', '{aw}');\n</script>\n")
            extra = "\n      'value': 1.0,\n      'currency': 'USD'," if a["category"] == "PURCHASE" else ""
            ev = (f"<!-- Event snippet for {a['name']} conversion page -->\n<script>\n  gtag('event', 'conversion', {{\n"
                  f"      'send_to': '{aw}/{label}',{extra}\n      'transaction_id': ''\n  }});\n</script>\n")
            row["tagSnippets"] = [{"type": "WEBPAGE", "pageFormat": "HTML", "globalSiteTag": gst, "eventSnippet": ev},
                                  {"type": "WEBPAGE", "pageFormat": "AMP", "globalSiteTag": "<amp-analytics/>",
                                   "eventSnippet": "<amp-analytics/>"},
                                  {"type": "WEBPAGE_ONCLICK", "pageFormat": "HTML", "globalSiteTag": gst,
                                   "eventSnippet": ev.replace("conversion page", "click")}]
        return row

    def _conversion_actions(self, cid, body):
        """ConversionActionService.MutateConversionActions: creates only, all or nothing, unique names."""
        taken = {a["name"] for a in self._mine(cid, "conversion_action") if a["status"] != "REMOVED"}
        made = []
        for i, op in enumerate(body["operations"]):
            c = op["create"]
            assert c["type"] in ("WEBPAGE", "UPLOAD_CLICKS") and c["status"] == "ENABLED" and c["category"]
            assert c["countingType"] in ("ONE_PER_CLICK", "MANY_PER_CLICK") and isinstance(c["primaryForGoal"], bool)
            if c["name"] in taken:
                raise Refuse(gerr(400, "INVALID_ARGUMENT", "conversionActionError", "DUPLICATE_NAME",
                                  "Conversion action names must be unique.", i))
            taken.add(c["name"])
            made.append(c)
        out = []
        for c in made:
            a = self.conversion_action(cid, c["name"], typ=c["type"], category=c["category"], primary=c["primaryForGoal"])
            a["fields"] = c
            out.append({"resourceName": a["rn"]})
        return {"results": out}

    def _assets(self, cid, body):
        (op,) = body["operations"]
        c = op["create"]
        raw = base64.b64decode(c["imageAsset"]["data"])
        assert c["type"] == "IMAGE" and raw[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb")
        if any(a.get("name") == c["name"] for a in self._mine(cid, "asset")):
            raise Refuse(gerr(400, "INVALID_ARGUMENT", "assetError", "DUPLICATE_ASSET_NAME",
                              "An asset with this name already exists."))
        oid = str(next(self._ids))
        rn = f"customers/{cid}/assets/{oid}"
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(raw)) as im:
            size = im.size
        self.store[rn] = {"kind": "asset", "id": oid, "rn": rn, "customer": cid, "name": c["name"], "type": "IMAGE",
                          "size": size, "bytes": len(raw)}
        return {"results": [{"resourceName": rn}]}

    def _campaigns(self, cid, body):
        out = []
        for op in body["operations"]:
            u = op["update"]
            assert op["updateMask"] == "status"
            self.store[u["resourceName"]]["status"] = u["status"]
            out.append({"resourceName": u["resourceName"]})
        return {"results": out}
