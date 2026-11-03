"""The first-party funnel log (site_events.py, web/_beacon.html, POST /e) on a throwaway database:
the beacon on the public pages and the sign-up page (and nowhere else), the endpoint's whitelist,
bot / size / origin / consent / rate limits, first touch copied onto every step, the server-side
steps written once, and test traffic kept but matched by test_traffic_sql. Stripe is faked.

    python tests/test_site_events.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_events_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET="whsec_test_only", RS_CAPI_SYNC="1")
for k in ("META_PIXEL_ID", "TIKTOK_PIXEL_ID", "REDDIT_PIXEL_ID", "X_PIXEL_ID", "META_CAPI_TOKEN", "TIKTOK_EVENTS_TOKEN",
          "REDDIT_CAPI_TOKEN", "GADS_SIGNUP_LABEL", "GADS_PURCHASE_LABEL", "FUNNEL_TEST_SOURCES", "FUNNEL_TEST_CLICK_IDS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import plans  # noqa: E402
import site_events  # noqa: E402

billing._get = lambda path, params=None: {"data": []}
sessions = []
billing.create_checkout_session = lambda **kw: sessions.append(kw) or {"id": f"cs_test_pack{len(sessions)}",
                                                                       "url": "https://checkout.stripe.test/p"}
billing.create_subscription_session = lambda **kw: sessions.append(kw) or {"id": f"cs_test_plan{len(sessions)}",
                                                                           "url": "https://checkout.stripe.test/s"}
hf.submit = lambda *a, **k: {"request_id": "req_test", "status": "queued"}
passed = 0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client(ua=UA, **headers):
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    if ua is not None:
        c.environ_base["HTTP_USER_AGENT"] = ua
    for k, v in headers.items():
        c.environ_base["HTTP_" + k.upper().replace("-", "_")] = v
    return c


def csrf(c):
    c.get("/login")
    with c.session_transaction() as s:
        return s["csrf"]


def rows(where="1", params=()):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM site_events WHERE {where} ORDER BY id", params)]


def post(c, body, **kw):
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return c.post("/e", data=data, content_type=kw.pop("content_type", "text/plain"), **kw)


def give_vid(c, vid):
    c.set_cookie(site_events.VID_COOKIE, vid, domain="localhost")


BEACON = re.compile(r"var C = (\{.*?\});")


def beacon(html):
    m = BEACON.search(html)
    return json.loads(m.group(1)) if m else None


# ---------------------------------------------------------------- the table
with db.connect() as conn:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(site_events)")]
    idx = {r[1]: [c[2] for c in conn.execute(f"PRAGMA index_info({r[1]})")]
           for r in conn.execute("PRAGMA index_list(site_events)")}
check(cols == ["id", "ts", "vid", "name", "path", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
               "click_id", "click_type", "referrer_host", "detail", "workspace_id"], "site_events has the contract's columns")
check(["ts"] in idx.values() and ["name", "ts"] in idx.values(), "indexes on ts and (name, ts)")

# ---------------------------------------------------------------- the beacon on the pages
pub = client()
for path, view in (("/", "lp_view"), ("/pricing", "lp_view"), ("/features", "lp_view"),
                   ("/ai-ugc-ad-generator", "lp_view"), ("/signup", "signup_view")):
    r = pub.get(path)
    html = r.get_data(as_text=True)
    b = beacon(html)
    check(r.status_code == 200 and b and b["view"] == view and b["url"] == "/e" and b["cta"] == (view == "lp_view")
          and html.count("var C = ") == 1 and "</html>" in html,
          f"{path} renders (200) with the beacon: {view}{' + CTA clicks' if view == 'lp_view' else ''}")
html = pub.get("/").get_data(as_text=True)
check('data-cta="hero"' in html and 'data-cta="nav"' in html and 'data-cta="pricing-free"' in html,
      "the landing page's sign-up CTAs carry their names")
check(all(beacon(pub.get(p).get_data(as_text=True)) is None for p in ("/privacy", "/terms", "/login",
                                                                      "/signup?invite=abc123")),
      "no beacon on legal pages, login or invite sign-ups")
optout = client()
optout.set_cookie("cfy_ads", "0", domain="localhost")
check(beacon(client(**{"Sec-GPC": "1"}).get("/").get_data(as_text=True)) is None
      and beacon(client(**{"DNT": "1"}).get("/pricing").get_data(as_text=True)) is None
      and beacon(client(**{"CF-IPCountry": "DE"}).get("/").get_data(as_text=True)) is None
      and beacon(optout.get("/").get_data(as_text=True)) is None,
      "no beacon under GPC, Do Not Track, a consent-region country or the pixels' cfy_ads=0")

# ---------------------------------------------------------------- the endpoint
c = client()
give_vid(c, "abcdef0123456789abcdef01")
r = post(c, {"name": "lp_view", "path": "/pricing?email=a@b.co#x", "ref": "News.YCombinator.com"})
ev = rows()
check(r.status_code == 204 and len(ev) == 1 and ev[0]["name"] == "lp_view" and ev[0]["path"] == "/pricing"
      and ev[0]["vid"] == "abcdef0123456789abcdef01" and ev[0]["referrer_host"] == "news.ycombinator.com"
      and ev[0]["workspace_id"] is None and ev[0]["detail"] is None and ev[0]["ts"].endswith("+00:00"),
      "lp_view is written: vid, path without its query, the referring host, no workspace")
post(c, {"name": "lp_view", "path": "/features", "ref": ""})
check(rows()[-1]["referrer_host"] == "news.ycombinator.com", "the vid's first referrer sticks to its later views")
r = post(c, {"name": "cta_click", "path": "/", "detail": "hero <script>alert(1)</script>" + "x" * 300})
d = rows()[-1]["detail"]
check(r.status_code == 204 and d.startswith("hero scriptalert1/script") and len(d) <= 120, "cta_click keeps a cleaned, capped label")
n0 = len(rows())
for bad in ({"name": "signup"}, {"name": "paid"}, {"name": "first_ad"}, {"name": "checkout"}, {"name": "x"}, {}):
    check(post(c, bad).status_code == 400, f"{bad.get('name') or 'no name'}: refused, only browser steps are accepted")
check(post(c, "not json").status_code == 400 and post(c, "[1,2]").status_code == 400, "a body that isn't a JSON object is refused")
check(post(c, {"name": "lp_view", "path": "/", "pad": "x" * 3000}).status_code == 413, "a body over 2 KB is refused")
for ua in ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
           "Mozilla/5.0 (Linux; Android 6.0.1) AppleWebKit/537.36 HeadlessChrome/120.0 Safari/537.36",
           "facebookexternalhit/1.1", "curl/8.4.0", "python-requests/2.31", "", None):
    check(post(client(ua=ua), {"name": "lp_view", "path": "/"}).status_code == 204, f"bot {ua!r}: answered 204")
check(post(client(), {"name": "lp_view", "path": "/"}, headers={"Origin": "https://evil.example"}).status_code == 204
      and post(client(), {"name": "lp_view", "path": "/"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 204,
      "cross-site posts are answered 204")
check(post(client(**{"Sec-GPC": "1"}), {"name": "lp_view", "path": "/"}).status_code == 204, "GPC: answered 204")
check(len(rows()) == n0, "... and none of those wrote a row")
r = client().post("/e", json={"name": "signup_view", "path": "/signup"}, headers={"Origin": "http://localhost"})
check(r.status_code == 204 and rows()[-1]["name"] == "signup_view", "a same-origin application/json post works too (no CSRF token)")

rl = client()
give_vid(rl, "ratelimit0000000000000001")
codes = [post(rl, {"name": "lp_view", "path": "/"}).status_code for _ in range(site_events.RATE_PER_MIN + 1)]
check(codes[:-1] == [204] * site_events.RATE_PER_MIN and codes[-1] == 429, "rate limit: 30 a minute, then 429")
other = client()
other.environ_base["REMOTE_ADDR"] = rl.environ_base["REMOTE_ADDR"]
check(post(other, {"name": "lp_view", "path": "/"}).status_code == 429, "the limit holds per IP too (a new vid doesn't reset it)")
with db.connect() as conn:
    stored = json.dumps([list(r) for r in conn.execute("SELECT * FROM site_events")])
check(rl.environ_base["REMOTE_ADDR"] not in stored and "10.9." not in stored, "no IP address is stored")

# ---------------------------------------------------------------- first touch through the whole funnel
v = client()
vid = "f1r5770uch0000000000aaaa"
give_vid(v, vid)
r = v.get("/?utm_source=meta&utm_medium=paid&utm_campaign=cfy_launch&utm_content=ad_7&utm_term=ugc&fbclid=FBCLICK1")
check("cfy_ft=" in r.headers.get("Set-Cookie", ""), "an ad visit gets the first-touch cookie (gads.py)")
post(v, {"name": "lp_view", "path": "/", "ref": "l.facebook.com"})
post(v, {"name": "cta_click", "path": "/", "detail": "hero"})
v.get("/signup")
post(v, {"name": "signup_view", "path": "/signup"})
ATTR = {"utm_source": "meta", "utm_medium": "paid", "utm_campaign": "cfy_launch", "utm_content": "ad_7", "utm_term": "ugc",
        "click_id": "FBCLICK1", "click_type": "fbclid"}
mine = rows("vid=?", (vid,))
check([e["name"] for e in mine] == ["lp_view", "cta_click", "signup_view"]
      and all(all(e[k] == want for k, want in ATTR.items()) for e in mine) and mine[1]["detail"] == "hero",
      "browser steps carry the first touch: utm_*, click id + type")
r = v.post("/signup", data={"csrf": csrf(v), "email": "funnel@example.com", "password": "longpassword1"})
check(r.status_code == 302, "sign-up works")
with db.connect() as conn:
    u = conn.execute("SELECT id, workspace_id, first_touch FROM users WHERE email='funnel@example.com'").fetchone()
ws = u["workspace_id"]
sg = rows("name='signup' AND workspace_id=?", (ws,))
check(len(sg) == 1 and sg[0]["vid"] == vid and all(sg[0][k] == want for k, want in ATTR.items())
      and sg[0]["detail"] in ("starter", "ad_offer"),
      "signup: written server-side once, with the workspace, the vid and the stored first touch")
check("funnel@example.com" not in json.dumps(rows()), "no email in site_events")

# first ad
r = v.post("/app/generate", data={"csrf": csrf(v), "tool": "ad_image", "brief": "Serum on a vanity", "batch": "1"})
with db.connect() as conn:
    njobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (ws,)).fetchone()[0]
fa = rows("name='first_ad' AND workspace_id=?", (ws,))
check(njobs >= 1 and len(fa) == 1 and fa[0]["detail"] == "ad_image" and fa[0]["vid"] == vid
      and all(fa[0][k] == want for k, want in ATTR.items()), "first_ad: the first generation queued, same first touch")
v.post("/app/generate", data={"csrf": csrf(v), "tool": "ad_image", "brief": "Serum on a shelf", "batch": "1"})
with db.connect() as conn:
    njobs2 = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (ws,)).fetchone()[0]
check(njobs2 > njobs and len(rows("name='first_ad' AND workspace_id=?", (ws,))) == 1, "a second generation adds no first_ad")

# checkout + paid (pack)
v.post("/app/billing/checkout", data={"csrf": csrf(v), "pack": "starter"})
sid = f"cs_test_pack{len(sessions)}"
co = rows("name='checkout' AND workspace_id=?", (ws,))
check(len(co) == 1 and co[0]["detail"] == "pack:starter" and co[0]["utm_content"] == "ad_7",
      "checkout: a pack checkout session created, same first touch")


def deliver(obj, typ="checkout.session.completed"):
    body = json.dumps({"id": "evt_" + str(obj.get("id")), "type": typ, "data": {"object": obj}}).encode()
    return client().post("/webhooks/stripe", data=body, content_type="application/json",
                         headers={"Stripe-Signature": billing.sign(body, "whsec_test_only")})


with db.connect() as conn:
    pur = conn.execute("SELECT * FROM purchases WHERE stripe_session_id=?", (sid,)).fetchone()
sess_obj = {"id": sid, "object": "checkout.session", "mode": "payment", "amount_total": pur["amount_cents"],
            "currency": "usd", "payment_status": "paid", "payment_intent": "pi_1",
            "metadata": {"app": "campaignify", "purchase_id": str(pur["id"]), "workspace_id": str(ws), "pack": "starter"}}
deliver(dict(sess_obj, payment_status="unpaid"))
check(not rows("name='paid'"), "an unpaid checkout writes no paid step")
check(deliver(sess_obj).get_json().get("credited"), "the paid webhook credits the pack")
deliver(sess_obj)
pd = rows("name='paid' AND workspace_id=?", (ws,))
check(len(pd) == 1 and pd[0]["detail"] == "pack:starter" and pd[0]["vid"] == vid and pd[0]["path"] is None
      and all(pd[0][k] == want for k, want in ATTR.items()), "paid: from the webhook once (a retry adds nothing), same first touch")

# plan: checkout, then only the first invoice counts as paid
v.post("/app/billing/subscribe", data={"csrf": csrf(v), "plan": "growth"})
check(rows("name='checkout' AND workspace_id=?", (ws,))[-1]["detail"] == "plan:growth", "checkout: a plan checkout session")


def plan_invoice(inv_id, ws_id, code, reason):
    p = plans.PLANS[code]
    meta = {"app": "campaignify", "kind": "plan", "workspace_id": str(ws_id), "plan": code}
    return {"id": inv_id, "object": "invoice", "status": "paid", "currency": "usd", "customer": "cus_1",
            "subscription": "sub_ev1", "subscription_details": {"metadata": meta}, "billing_reason": reason,
            "amount_paid": p["usd"] * 100,
            "lines": {"data": [{"amount": p["usd"] * 100, "price": {"id": p["price_id"]}, "period": {"end": 1893456000}}]}}


deliver(plan_invoice("in_ev_1", ws, "growth", "subscription_create"), "invoice.paid")
deliver(plan_invoice("in_ev_1", ws, "growth", "subscription_create"), "invoice.paid")
deliver(plan_invoice("in_ev_2", ws, "growth", "subscription_cycle"), "invoice.paid")
pp = rows("name='paid' AND detail='plan:growth'")
check(len(pp) == 1 and pp[0]["workspace_id"] == ws and pp[0]["utm_campaign"] == "cfy_launch",
      "paid: a plan's first invoice once; a retry and a renewal add nothing")

# ---------------------------------------------------------------- organic visitor, invite join, consent
o = client()
give_vid(o, "0rgan1c00000000000000000")
post(o, {"name": "lp_view", "path": "/", "ref": "www.google.com"})
post(o, {"name": "lp_view", "path": "/pricing", "ref": ""})
o.post("/signup", data={"csrf": csrf(o), "email": "organic@example.com", "password": "longpassword1"})
osg = rows("vid='0rgan1c00000000000000000' AND name='signup'")
check(len(osg) == 1 and osg[0]["referrer_host"] == "www.google.com" and osg[0]["utm_source"] is None
      and osg[0]["click_id"] is None, "an organic sign-up is credited to its first referrer")

g_ = client(**{"Sec-GPC": "1"})
give_vid(g_, "gpc00000000000000000000a")
g_.post("/signup", data={"csrf": csrf(g_), "email": "private@example.com", "password": "longpassword1"})
with db.connect() as conn:
    pws = conn.execute("SELECT workspace_id FROM users WHERE email='private@example.com'").fetchone()[0]
psg = rows("name='signup' AND workspace_id=?", (pws,))
check(len(psg) == 1 and psg[0]["vid"] is None, "GPC: the sign-up still counts, with no visitor id")

with db.connect() as conn:
    conn.execute("INSERT INTO invites (code, workspace_id, created_at) VALUES ('evjoin1', ?, ?)", (ws, db.now()))
    conn.commit()
n_sign = len(rows("name='signup'"))
j = client()
j.post("/signup", data={"csrf": csrf(j), "email": "teammate@example.com", "password": "longpassword1", "invite": "evjoin1"})
with db.connect() as conn:
    joined = conn.execute("SELECT workspace_id FROM users WHERE email='teammate@example.com'").fetchone()
check(joined and joined[0] == ws and len(rows("name='signup'")) == n_sign, "joining a team by invite isn't a sign-up")

# a logged-in member sees no beacon on the app
check(beacon(v.get("/app").get_data(as_text=True)) is None and beacon(v.get("/pricing").get_data(as_text=True)) is None,
      "no beacon for logged-in members")

# ---------------------------------------------------------------- test traffic: kept, matched, left out
p = client()
give_vid(p, "pr0be000000000000000000a")
p.get("/?utm_source=probe&utm_campaign=hq_check")
post(p, {"name": "lp_view", "path": "/"})
t = client()
give_vid(t, "t3st123000000000000000aa")
t.get("/pricing?ttclid=test123")
post(t, {"name": "lp_view", "path": "/pricing"})
P = client()
give_vid(P, "pr0beupper00000000000000")
P.get("/?utm_source=Probe")
post(P, {"name": "lp_view", "path": "/"})
sql, params = site_events.test_traffic_sql()
with db.connect() as conn:
    flagged = {r["vid"] for r in conn.execute(f"SELECT vid FROM site_events WHERE {sql}", params)}
    kept = conn.execute(f"SELECT COUNT(*) FROM site_events WHERE NOT {sql}", params).fetchone()[0]
check(flagged == {"pr0be000000000000000000a", "t3st123000000000000000aa", "pr0beupper00000000000000"}
      and rows("vid='t3st123000000000000000aa'")[0]["click_type"] == "ttclid",
      "test traffic is written and test_traffic_sql matches it: utm_source=probe (any case), click id test123")
check(kept == len(rows()) - 3, "everything else is real traffic")

print(f"\nall {passed} checks passed ({TMP})")
