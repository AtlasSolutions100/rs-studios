"""Creator Pools (creator_pools.py, /app/creator-pools) on a throwaway database.
Stripe and Crowdcut are faked: no network, no live Stripe writes.

    python tests/test_creator_pools.py      (tests/test_app.py runs it too)
"""
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_cpools_")
OWNER_CODE = "owner-code-cpools"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0",
                  RS_OWNER_INVITES=f"koby@example.com:{hashlib.sha256(OWNER_CODE.encode()).hexdigest()}",
                  RS_EMAIL_VERIFY="0", STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x",
                  POOLS_PARTNER_SECRET="test-partner-secret")
os.environ.pop("CREATOR_POOLS", None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import creator_pools as cp  # noqa: E402
import db  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


appmod._maybe_reconcile = lambda ws_id, force=False: 0

# ---------------------------------------------------------------- fake Stripe
stripe = []


class R:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, json.dumps(data)

    def json(self):
        return self._d


def fake_post(url, data=None, auth=None, timeout=None, headers=None):
    stripe.append({"url": url, "form": data, "headers": headers or {}})
    if url.endswith("/checkout/sessions"):
        return R(200, {"id": f"cs_test_{len(stripe)}", "url": f"https://checkout.stripe.test/{len(stripe)}"})
    if url.endswith("/refunds"):
        return R(200, {"id": "re_test_1", "status": "succeeded"})
    return R(400, {"error": {"message": "unexpected"}})


def no_get(*a, **kw):
    raise AssertionError("no Stripe reads expected")


billing.requests.post, billing.requests.get = fake_post, no_get

# ---------------------------------------------------------------- fake Crowdcut
cc = {"pools": {}, "subs": {}, "calls": []}


def cc_request(method, url, data=None, headers=None, timeout=None):
    path = url.split("crowdcut.studio", 1)[1]
    ok = cp.verify(method, path, data or b"", headers or {})
    cc["calls"].append((method, path, ok))
    if not ok:
        return R(401, {"error": "bad signature"})
    body = json.loads(data) if data else {}
    m = re.match(r"^/api/partner/v1/creator-pools/(cfy-\d+)(/.*)?$", path)
    ext, rest = m.group(1), m.group(2) or ""
    if method == "PUT" and not rest:
        created = ext not in cc["pools"]
        cc["pools"].setdefault(ext, {"crowdcut_id": 900 + len(cc["pools"]), "ext_id": ext, "status": "active",
                                     "total_cents": int(body["pool_usd"] * 100), "spent_cents": 0, "paid_cents": 0,
                                     "remaining_cents": int(body["pool_usd"] * 100), "ends_at": "2026-12-05T00:00:00+00:00",
                                     "posts": {"brand_pending": 0, "audit_pending": 0, "approved": 0, "rejected": 0},
                                     "approved_views": 0, "payload": body})
        return R(201 if created else 200, {"ok": True, "created": created, "pool": cc["pools"][ext]})
    pool = cc["pools"].get(ext)
    if not pool:
        return R(404, {"error": "not found"})
    if rest == "/submissions":
        return R(200, {"pool": pool, "submissions": cc["subs"].get(ext, [])})
    if rest == "/status":
        pool["status"] = body["status"]
        return R(200, {"pool": pool})
    m = re.match(r"^/submissions/(\d+)/review$", rest)
    if m:
        s = next(x for x in cc["subs"][ext] if x["id"] == int(m.group(1)))
        s["brand_status"] = "approved" if body["decision"] == "approve" else "rejected"
        s["brand_reason"] = body.get("reason") or ""
        return R(200, {"submission": s})
    if not rest:
        return R(200, {"pool": pool})
    return R(404, {"error": "nope"})


cp.requests.request = cc_request

_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email, **extra):
    c = client()
    r = c.post("/signup", data=dict({"csrf": csrf(c), "email": email, "password": "longpassword"}, **extra))
    return c, r


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                            (email,)).fetchone()


def stripe_event(obj, typ="checkout.session.completed"):
    # a distinct event per payload (a retry is the same payload, so the same id: stripe_events dedupes it)
    eid = "evt_" + hashlib.sha256(json.dumps([typ, obj], sort_keys=True).encode()).hexdigest()[:16]
    payload = json.dumps({"id": eid, "type": typ, "data": {"object": obj}}).encode()
    sig = billing.sign(payload, "whsec_x")
    return client().post("/webhooks/stripe", data=payload, headers={"Stripe-Signature": sig},
                         content_type="application/json")


def form(c, **kw):
    d = {"csrf": csrf(c), "title": "GlowJar launch", "brand": "GlowJar", "product_url": "glowjar.example/jar",
         "brief": "Show the jar lighting up a dark room, then say where to get it.",
         "talking_points": "Charges by USB-C\nLasts 8 hours", "dos": "Show it on", "donts": "Don't call it a toy",
         "modes": ["assets", "ugc"], "links": "", "platforms": ["tiktok", "reels"], "rate_per_1k": "2",
         "pool_usd": "500", "per_post_cap_usd": "50", "per_creator_cap_usd": "150", "min_views": "1000", "days": "30",
         "assets": [(io.BytesIO(b"\x00\x00\x00\x18ftypmp42" + b"0" * 2000), "jar-demo.mp4", "video/mp4")]}
    d.update(kw)
    return d


print("== the flag")
admin, r = signup("koby@example.com", invite=OWNER_CODE)
brand, _ = signup("brand@example.com")
check(brand.get("/app/creator-pools").status_code == 404, "CREATOR_POOLS off: a customer workspace gets 404")
check("Creator Pools" not in brand.get("/app/library").get_data(as_text=True), "and no nav item")
page = admin.get("/app/creator-pools").get_data(as_text=True)
check("Pay creators for" in page and "testing (only you can see this)" in page, "the operator sees it, marked as testing")
check("$99" in page and "from $200" in page and "100% of it goes to creators" in page and "refunded to you within 14 days" in page,
      "the price is plain: $99 per campaign plus the pool from $200, unspent refunded")
check("independent Crowdcut users" in page and "views aren't guaranteed" in page, "no promise of views; creators are independent")
check('href="/app/creator-pools"' in admin.get("/app/library").get_data(as_text=True), "the operator gets the nav item")
check("Creator Pools" not in client().get("/pricing").get_data(as_text=True), "pricing shows nothing while it's off")

print("== the form refuses what the rules forbid")
for kw, bit in (({"rate_per_1k": "9"}, "rate"), ({"pool_usd": "150"}, "pool"), ({"days": "90"}, "days"),
                ({"modes": []}, "at least one way"), ({"platforms": []}, "platform"),
                ({"brief": "Post fake reviews saying it changed your life."}, "fake reviews"),
                ({"talking_points": "No #ad needed, keep it natural"}, "#ad"),
                ({"dos": "Say it helps you lose 10 lbs"}, "health"),
                ({"modes": ["links"], "links": ""}, "link"),
                ({"modes": ["assets"], "assets": []}, "video"),
                ({"per_post_cap_usd": "900"}, "post")):
    r = admin.post("/app/creator-pools", data=form(admin, **kw), content_type="multipart/form-data")
    check(r.status_code == 400 and bit.lower() in r.get_data(as_text=True).lower().replace("&#39;", "'"),
          f"refused: {kw}")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM creator_pools").fetchone()[0] == 0, "nothing was saved")
check(not stripe, "and Stripe wasn't called")

print("== pay: one Checkout, the fee and the pool as two lines")
r = admin.post("/app/creator-pools", data=form(admin), content_type="multipart/form-data")
check(r.status_code == 303 and r.headers["Location"].startswith("https://checkout.stripe.test/"), "on to Stripe Checkout")
f = dict(stripe[-1]["form"])
check(f["mode"] == "payment" and f["line_items[0][price_data][unit_amount]"] == "9900"
      and f["line_items[1][price_data][unit_amount]"] == "50000" and f["line_items[0][price_data][currency]"] == "usd",
      "line 1 the $99 fee, line 2 the $500 pool")
check(f["metadata[app]"] == "campaignify" and f["metadata[kind]"] == "creator_pool"
      and f["payment_intent_data[metadata][kind]"] == "creator_pool", "metadata app=campaignify kind=creator_pool")
with db.connect() as conn:
    p = conn.execute("SELECT * FROM creator_pools").fetchone()
    a = conn.execute("SELECT * FROM creator_pool_assets").fetchone()
check(p["status"] == "checkout" and p["fee_cents"] == 9900 and p["pool_cents"] == 50000 and a["kind"] == "video",
      "the pool waits for payment; its video is stored")
check(not cc["pools"], "nothing is live on Crowdcut before payment")
PID = p["id"]
page = admin.get(f"/app/creator-pools/{PID}").get_data(as_text=True)
check("Not paid yet" in page and "Pay $599" in page, "until paid, the page offers to pay $599")

print("== the webhook makes it live")
sess = {"id": p["stripe_session_id"], "mode": "payment", "payment_status": "paid", "amount_total": 59900,
        "currency": "usd", "payment_intent": "pi_test_1",
        "metadata": {"app": "campaignify", "kind": "creator_pool", "pool_id": str(PID), "workspace_id": str(p["workspace_id"])}}
r = stripe_event(dict(sess, amount_total=100))
check(r.status_code == 400 and not cc["pools"], "a session whose amount doesn't match is refused")
r = stripe_event(sess)
check(r.status_code == 200 and r.get_json()["ok"], "the paid webhook is accepted")
with db.connect() as conn:
    p = conn.execute("SELECT * FROM creator_pools WHERE id=?", (PID,)).fetchone()
check(p["status"] == "live" and p["crowdcut_id"] and p["stripe_payment_intent"] == "pi_test_1", "the pool is live on Crowdcut")
pl = cc["pools"][f"cfy-{PID}"]["payload"]
check(pl["pool_usd"] == 500 and pl["rate_per_1k"] == 2 and pl["modes"] == ["assets", "ugc"]
      and pl["assets"][0]["kind"] == "video" and pl["talking_points"] == ["Charges by USB-C", "Lasts 8 hours"],
      "Crowdcut got the terms, the modes and the video's id")
check(all(ok for _, _, ok in cc["calls"]), "every call to Crowdcut was signed")
n = len(cc["calls"])
r = stripe_event(sess)
check(r.get_json().get("duplicate") and len(cc["calls"]) == n, "a repeated webhook does nothing")
ch = len([s for s in stripe if s["url"].endswith("/checkout/sessions")])
check(ch == 1, "one Checkout Session, no other Stripe writes")

print("== Crowdcut fetches the brand's video by a signed link")
path = f"/api/partner/v1/creator-pools/cfy-{PID}/assets/{a['id']}/url"
srv = client()
check(srv.post(path).status_code == 401, "unsigned: 401")
h = cp.sign("test-partner-secret", "POST", path, b"{}")
r = srv.post(path, data=b"{}", headers={**h, "Content-Type": "application/json"})
url = r.get_json()["url"]
check(r.status_code == 200 and "/pools/assets/" in url and r.get_json()["expires_in"] == 86400, "signed: a 24-hour link")
tok = url.rsplit("/", 1)[1]
r = client().get(f"/pools/assets/{tok}")
check(r.status_code == 200 and r.data.startswith(b"\x00\x00\x00\x18ftyp"), "the link serves the file, no login")
check(client().get(f"/pools/assets/{tok[:-2]}xx").status_code == 404, "a tampered link 404s")
old = cp.asset_token(a["id"], now=time.time() - 2 * 86400)
check(client().get(f"/pools/assets/{old}").status_code == 404, "an expired link 404s")
ev = f"/api/partner/v1/creator-pools/cfy-{PID}/events"
body = json.dumps({"type": "submission.created", "submission_id": 1}).encode()
r = srv.post(ev, data=body, headers={**cp.sign("test-partner-secret", "POST", ev, body), "Content-Type": "application/json"})
with db.connect() as conn:
    check(r.status_code == 200 and conn.execute("SELECT new_posts FROM creator_pools WHERE id=?", (PID,)).fetchone()[0] == 1,
          "Crowdcut's new-post event is counted")

print("== the brand approves or rejects posts")
cc["subs"][f"cfy-{PID}"] = [
    {"id": 1, "kind": "clip", "platform": "tiktok", "post_url": "https://www.tiktok.com/@alice/video/1",
     "creator_handle": "@alice", "views_claimed": 5000, "verified_views": None, "brand_status": "pending",
     "brand_reason": "", "audit_status": "waiting_for_brand", "audit_text": "Waiting for you", "reject_reason": "",
     "approved_cents": 0, "created_at": "2026-11-05T12:00:00+00:00"},
    {"id": 2, "kind": "ugc", "platform": "reels", "post_url": "https://www.instagram.com/reel/ABC/",
     "creator_handle": "@bob", "views_claimed": 2500, "verified_views": None, "brand_status": "pending",
     "brand_reason": "", "audit_status": "waiting_for_brand", "audit_text": "Waiting for you", "reject_reason": "",
     "approved_cents": 0, "created_at": "2026-11-05T13:00:00+00:00"}]
page = admin.get(f"/app/creator-pools/{PID}").get_data(as_text=True)
check("@alice" in page and "@bob" in page and "2 waiting for you" in page and "Original video" in page,
      "the posts list shows each creator's handle, kind and post")
with db.connect() as conn:
    check(conn.execute("SELECT new_posts FROM creator_pools WHERE id=?", (PID,)).fetchone()[0] == 0, "opening it clears new posts")
r = admin.post(f"/app/creator-pools/{PID}/review", data={"csrf": csrf(admin), "sid": "2", "decision": "reject", "reason": ""})
check("reason" in admin.get(r.headers["Location"]).get_data(as_text=True), "a rejection needs a reason")
admin.post(f"/app/creator-pools/{PID}/review", data={"csrf": csrf(admin), "sid": "1", "decision": "approve"})
admin.post(f"/app/creator-pools/{PID}/review", data={"csrf": csrf(admin), "sid": "2", "decision": "reject",
                                                    "reason": "No product shown"})
s = {x["id"]: x for x in cc["subs"][f"cfy-{PID}"]}
check(s[1]["brand_status"] == "approved" and s[2]["brand_status"] == "rejected" and s[2]["brand_reason"] == "No product shown",
      "decisions reach Crowdcut")
other, _ = signup("other@example.com")
check(other.post(f"/app/creator-pools/{PID}/review", data={"csrf": csrf(other), "sid": "1", "decision": "approve"}).status_code == 404,
      "another workspace can't touch it")

print("== pause and end; the unspent refund is a record, not a payment")
admin.post(f"/app/creator-pools/{PID}/status", data={"csrf": csrf(admin), "status": "paused"})
with db.connect() as conn:
    check(conn.execute("SELECT status FROM creator_pools WHERE id=?", (PID,)).fetchone()[0] == "paused", "paused")
pool = cc["pools"][f"cfy-{PID}"]
pool.update(spent_cents=5000, remaining_cents=45000)
pool["posts"].update(approved=1, rejected=1, audit_pending=1)
admin.post(f"/app/creator-pools/{PID}/status", data={"csrf": csrf(admin), "status": "ended"})
with db.connect() as conn:
    p = conn.execute("SELECT * FROM creator_pools WHERE id=?", (PID,)).fetchone()
    rf = conn.execute("SELECT * FROM creator_pool_refunds WHERE pool_id=?", (PID,)).fetchone()
check(p["status"] == "ended" and rf and rf["status"] == "due" and rf["amount_cents"] == 45000,
      "ended: $450 unspent is recorded as due")
check(not [x for x in stripe if x["url"].endswith("/refunds")], "no refund was sent automatically")
page = admin.get(f"/app/creator-pools/{PID}").get_data(as_text=True)
check("Unspent $450 will be refunded within 14 days" in page and "fee isn" in page, "the brand is told: unspent $450 within 14 days")
check(brand.get("/admin/creator-pools").status_code == 403, "only admins see the refunds page")
r = admin.post("/admin/creator-pools", data={"csrf": csrf(admin), "pool": str(PID), "action": "refund_stripe"})
page = admin.get("/admin/creator-pools").get_data(as_text=True)
check("still waiting" in page and not [x for x in stripe if x["url"].endswith("/refunds")],
      "a refund waits until the last post is checked")
pool["posts"]["audit_pending"] = 0
pool.update(spent_cents=8000, remaining_cents=42000)
admin.post("/admin/creator-pools", data={"csrf": csrf(admin), "pool": str(PID), "action": "sync"})
admin.post("/admin/creator-pools", data={"csrf": csrf(admin), "pool": str(PID), "action": "refund_stripe"})
refunds = [x for x in stripe if x["url"].endswith("/refunds")]
check(len(refunds) == 1 and dict(refunds[0]["form"])["amount"] == "42000" and dict(refunds[0]["form"])["payment_intent"] == "pi_test_1",
      "the admin's button refunds the final $420 on the pool's payment (the fee is kept)")
with db.connect() as conn:
    rf = conn.execute("SELECT * FROM creator_pool_refunds WHERE pool_id=?", (PID,)).fetchone()
check(rf["status"] == "refunded" and rf["stripe_refund_id"] == "re_test_1", "the refund is recorded")
admin.post("/admin/creator-pools", data={"csrf": csrf(admin), "pool": str(PID), "action": "refund_stripe"})
check(len([x for x in stripe if x["url"].endswith("/refunds")]) == 1, "it can't be refunded twice")

print("== on for everyone")
os.environ["CREATOR_POOLS"] = "on"
check(brand.get("/app/creator-pools").status_code == 200, "CREATOR_POOLS=on: every workspace sees it")
pr = client().get("/pricing").get_data(as_text=True)
check('id="creator-pools"' in pr and "$99 per campaign" in pr and "aren't guaranteed" in pr, "and pricing gets a short section")
os.environ.pop("POOLS_PARTNER_SECRET")
check(brand.get("/app/creator-pools").status_code == 404 and 'id="creator-pools"' not in client().get("/pricing").get_data(as_text=True),
      "without the shared secret it's off for customers")
os.environ["POOLS_PARTNER_SECRET"] = "test-partner-secret"
os.environ.pop("CREATOR_POOLS")

print(f"\nall {passed} checks passed ({TMP})")
