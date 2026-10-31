"""The Meta, TikTok, Reddit and X pixels (pixels.py, web/_pixels.html) and the server-side
copies of sign-up and purchase (conversions.py) on a throwaway database: nothing loads
until an id is set, where each event fires, the shared event ids, consent, and that no
personal data reaches an ad platform. Stripe and the ad platforms are faked.

    python tests/test_pixels.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_pixels_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET="whsec_test_only", RS_CAPI_SYNC="1")
PIXEL_VARS = ("META_PIXEL_ID", "TIKTOK_PIXEL_ID", "REDDIT_PIXEL_ID", "X_PIXEL_ID", "X_EVENT_SIGNUP", "X_EVENT_PURCHASE",
              "X_EVENT_VIEWCONTENT", "META_CAPI_TOKEN", "TIKTOK_EVENTS_TOKEN", "REDDIT_CAPI_TOKEN",
              "META_TEST_EVENT_CODE", "TIKTOK_TEST_EVENT_CODE", "REDDIT_CAPI_TEST")
for k in PIXEL_VARS + ("GADS_SIGNUP_LABEL", "GADS_PURCHASE_LABEL", "GADS_TAG_ID", "RS_SITE_URL"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import conversions  # noqa: E402
import db  # noqa: E402
import gads  # noqa: E402
import pixels  # noqa: E402

stripe = {}
billing._get = lambda path, params=None: stripe.get(path, {"data": []})
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client(**headers):
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.7.0.{_ip[0]}"
    c.environ_base["HTTP_USER_AGENT"] = "Mozilla/5.0 test"
    for k, v in headers.items():
        c.environ_base["HTTP_" + k.upper().replace("-", "_")] = v
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(c, email):
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT id, workspace_id FROM users WHERE email=?", (email,)).fetchone()
    return r, u["id"], u["workspace_id"]


SCRIPTS = ("connect.facebook.net", "analytics.tiktok.com", "redditstatic.com/ads", "static.ads-twitter.com")
PX_DATA = re.compile(r"var P = (\{.*?\}), XE = (\{.*?\}), EV = (\[.*?\]), VC = (true|false);")
GTAG_EVENT = re.compile(r"gtag\('event', 'conversion', (\{.*?\})\);")


def px(html):
    """(ids, x events, events, view_content) of the page's pixel block, or None."""
    m = PX_DATA.search(html)
    return None if not m else (json.loads(m[1]), json.loads(m[2]), json.loads(m[3]), m[4] == "true")


def loaded(html):
    return [s for s in SCRIPTS if s in html]


def queue_rows():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM conversion_queue ORDER BY id")]


IDS = {"META_PIXEL_ID": "1234567890123456", "TIKTOK_PIXEL_ID": "CTESTPIXEL0001", "REDDIT_PIXEL_ID": "a2_testpixel",
       "X_PIXEL_ID": "xpix1", "X_EVENT_SIGNUP": "tw-xpix1-sgn1", "X_EVENT_PURCHASE": "tw-xpix1-pur1"}
pub = client()

print("== no ids: nothing new loads")
u0 = client()
signup(u0, "noids@example.com")
for c, p in ((pub, "/"), (pub, "/pricing"), (pub, "/features"), (pub, "/signup"), (u0, "/app"), (u0, "/app/billing")):
    html = c.get(p).get_data(as_text=True)
    check(not loaded(html) and px(html) is None and "googletagmanager" in html, f"{p}: no pixel, the Google tag as before")
check(not pixels.enabled() and conversions.enabled_platforms() == [], "no pixel and no server-side platform")
check(not gads.wanted("sign_up") and not gads.wanted("purchase"), "nothing wants a conversion queued")
check(queue_rows() == [], "nothing queued server-side")

print("== ids set: every pixel loads on the site and the app")
os.environ.update(IDS)
u = client()
signup(u, "ids@example.com")
u.get("/app")   # the sign-up page view
for c, p in ((pub, "/"), (pub, "/pricing"), (pub, "/features"), (pub, "/ai-ugc-ad-generator"), (pub, "/privacy"),
             (pub, "/signup"), (pub, "/login"), (u, "/app")):
    html = c.get(p).get_data(as_text=True)
    d = px(html)
    check(d and len(loaded(html)) == 4 and d[0] == {"meta": "1234567890123456", "tiktok": "CTESTPIXEL0001",
                                                    "reddit": "a2_testpixel", "x": "xpix1"},
          f"{p} loads Meta, TikTok, Reddit and X with their ids")
html = pub.get("/").get_data(as_text=True)
check("fbq('track', 'PageView')" in html and "ttq.page()" in html and "rdt('track', 'PageVisit')" in html
      and "twq('config', P.x)" in html, "a page view on every pixel")
check("fbq('set', 'autoConfig', false, P.meta)" in html, "Meta's automatic form and button scraping is off")
check("useDecimalCurrencyValues: true" in html, "Reddit reads values as dollars")
check(px(html)[1] == {"SignUp": "tw-xpix1-sgn1", "Purchase": "tw-xpix1-pur1", "ViewContent": ""},
      "X gets its per-event ids (ViewContent unset)")
check(not px(html)[3] and px(pub.get("/pricing").get_data(as_text=True))[3],
      "ViewContent only on /pricing")
check(px(pub.get("/").get_data(as_text=True))[2] == [], "no conversion on a plain visit")
for k, v in IDS.items():
    os.environ.pop(k)
for k in ("META_PIXEL_ID", "REDDIT_PIXEL_ID"):
    os.environ[k] = IDS[k]
html = pub.get("/").get_data(as_text=True)
check(loaded(html) == ["connect.facebook.net", "redditstatic.com/ads"] and px(html)[0] ==
      {"meta": IDS["META_PIXEL_ID"], "tiktok": "", "reddit": IDS["REDDIT_PIXEL_ID"], "x": ""},
      "only the pixels whose ids are set load")
os.environ["META_PIXEL_ID"] = "off"
os.environ["REDDIT_PIXEL_ID"] = "a2_x'); alert(1); ('"
check(px(pub.get("/").get_data(as_text=True)) is None, "\"off\" and a malformed id load nothing")
os.environ.update(IDS)

print("== consent and pages that never load them")
for hdr in ({"Sec-GPC": "1"}, {"DNT": "1"}, {"CF-IPCountry": "FR"}, {"X-Country": "GB"}):
    check(px(client(**hdr).get("/").get_data(as_text=True)) is None, f"no pixels with {hdr}")
o = client()
o.set_cookie("cfy_ads", "0")
check(px(o.get("/pricing").get_data(as_text=True)) is None, "no pixels once the browser said no (cfy_ads=0)")
html = pub.get("/").get_data(as_text=True)
check("navigator.globalPrivacyControl === true" in html and '"Europe/"' in html and "cfy_ads" in html,
      "the browser skips them under GPC/DNT or a European time zone and remembers it")
for p in ("/signup?invite=abc123", "/pricing?q=jane", "/?utm_source=x&email=jane%40example.com",
          "/?utm_source=jane@example.com", "/login?next=/app"):
    check(px(pub.get(p).get_data(as_text=True)) is None, f"{p}: an address that could carry personal data loads none")
check(px(pub.get("/?utm_source=meta&utm_campaign=cfy_launch&fbclid=IwAR0abc").get_data(as_text=True)) is not None,
      "campaign tags and click ids are fine")
with appmod.app.test_request_context("/admin"):
    check(not pixels.page_ok(appmod.request), "never on admin pages")
with appmod.app.test_request_context("/s/sometoken"):
    check(not pixels.page_ok(appmod.request), "never on share links")
member_tpls = ("share.html", "page_public.html", "form_public.html", "c_base.html")
check(all("_pixels" not in open(os.path.join(ROOT, "web", t)).read() for t in member_tpls),
      "members' pages, forms, communities and share links never include them")

print("== sign-up")
gads_before = os.environ.pop("GADS_SIGNUP_LABEL", None)
c = client()
r, uid, _ = signup(c, "pixel-signup@example.com")
html = c.get(r.headers["Location"]).get_data(as_text=True)
evs = px(html)[2]
check(evs == [{"name": "SignUp", "event_id": f"signup-{uid}"}],
      "the first page after sign-up fires SignUp with event id signup-<user id> (Google label unset)")
check(GTAG_EVENT.findall(html) == [], "and no Google conversion without its label")
check("pixel-signup" not in json.dumps(evs) and "pixel-signup@example.com" not in html.split("var P = ")[1].split("</script>")[0],
      "no email in the event")
check(px(c.get("/app").get_data(as_text=True))[2] == [], "only once")
os.environ["GADS_SIGNUP_LABEL"] = "SiGnUpLabel_1"
c = client()
r, uid2, _ = signup(c, "both-signup@example.com")
html = c.get(r.headers["Location"]).get_data(as_text=True)
g_ev = [json.loads(x) for x in GTAG_EVENT.findall(html)]
check(g_ev == [{"send_to": "AW-18543041154/SiGnUpLabel_1", "transaction_id": f"signup-{uid2}"}]
      and px(html)[2] == [{"name": "SignUp", "event_id": f"signup-{uid2}"}],
      "with the Google label too: one sign-up, the same id on Google and the pixels")
os.environ.pop("GADS_SIGNUP_LABEL")
c2 = client()
c2.post("/signup", data={"csrf": csrf(c2), "email": "bad@", "password": "x"})
check(px(c2.get("/signup").get_data(as_text=True))[2] == [], "a failed sign-up fires nothing")
check(queue_rows() == [], "no tokens: nothing queued server-side")

print("== purchase")
sessions = []
billing.create_checkout_session = lambda **kw: sessions.append(kw) or {"id": f"cs_test_pack{len(sessions)}",
                                                                       "url": "https://checkout.stripe.test/p"}
billing.create_subscription_session = lambda **kw: sessions.append(kw) or {"id": f"cs_test_plan{len(sessions)}",
                                                                           "url": "https://checkout.stripe.test/s"}
b = client()
_, buid, bws = signup(b, "pixel-buyer@example.com")
b.get("/app")
b.post("/app/billing/checkout", data={"csrf": csrf(b), "pack": "starter"})
sid = f"cs_test_pack{len(sessions)}"
html = b.get(f"/app/billing?checkout=success&session_id={sid}").get_data(as_text=True)
check(px(html)[2] == [{"name": "Purchase", "event_id": f"purchase-{sid}", "value": 29.0, "currency": "USD"}],
      "pack checkout success fires Purchase with value, currency and event id purchase-<session id>")
check(px(b.get(f"/app/billing?checkout=success&session_id={sid}").get_data(as_text=True))[2] == [],
      "a reload doesn't fire it again")
other = client()
signup(other, "pixel-other@example.com")
other.get("/app")
b.post("/app/billing/checkout", data={"csrf": csrf(b), "pack": "pro"})
sid2 = f"cs_test_pack{len(sessions)}"
check(px(other.get(f"/app/billing?checkout=success&session_id={sid2}").get_data(as_text=True))[2] == [],
      "another workspace can't fire someone else's checkout")
check(px(b.get(f"/app/billing?checkout=cancel&session_id={sid2}").get_data(as_text=True))[2] == [],
      "a canceled checkout fires nothing")
os.environ["GADS_PURCHASE_LABEL"] = "PuRcHaSe_2"
r = b.post("/app/billing/subscribe", data={"csrf": csrf(b), "plan": "growth"})
psid = f"cs_test_plan{len(sessions)}"
stripe[f"/checkout/sessions/{psid}"] = {"id": psid, "status": "complete", "payment_status": "paid", "amount_total": 4950,
                                        "currency": "usd", "metadata": {"app": "campaignify", "workspace_id": str(bws)}}
html = b.get(f"/app/billing?plan=success&session_id={psid}").get_data(as_text=True)
g_ev = [json.loads(x) for x in GTAG_EVENT.findall(html)]
check(px(html)[2] == [{"name": "Purchase", "event_id": f"purchase-{psid}", "value": 49.5, "currency": "USD"}]
      and g_ev == [{"send_to": "AW-18543041154/PuRcHaSe_2", "value": 49.5, "currency": "USD", "transaction_id": psid}],
      "plan checkout: one purchase at what Stripe charged, on Google and on every pixel")
os.environ.pop("GADS_PURCHASE_LABEL")
check(gads.event_id("purchase", "cs_abc") == "purchase-cs_abc" and gads.event_id("sign_up", "signup-7") == "signup-7",
      "event ids: purchase-<Checkout Session id>, signup-<user id>")
check(queue_rows() == [], "no tokens: still nothing queued server-side")

print("== server-side copies (Meta CAPI, TikTok Events API, Reddit CAPI)")
os.environ["META_CAPI_TOKEN"] = "EAAtesttoken"
check(conversions.enabled_platforms() == ["meta"], "a platform is on only with its token and its pixel id")
os.environ.pop("META_PIXEL_ID")
check(conversions.enabled_platforms() == [], "a token without the pixel id does nothing")
os.environ["META_PIXEL_ID"] = IDS["META_PIXEL_ID"]
os.environ.update(TIKTOK_EVENTS_TOKEN="tt-test-token", REDDIT_CAPI_TOKEN="rd-test-token")
check(conversions.enabled_platforms() == ["meta", "tiktok", "reddit"], "Meta, TikTok and Reddit; X is pixel-only")

s = client()
s.set_cookie("_fbp", "fb.1.1700000000000.123456789")
s.set_cookie("_ttp", "ttp-cookie-1")
s.get("/?utm_source=tiktok&ttclid=TTCLID_123&utm_campaign=cfy1")
r, suid, sws = signup(s, "server-signup@example.com")
rows = queue_rows()
check(sorted(x["platform"] for x in rows) == ["meta", "reddit", "tiktok"] and
      all(x["event"] == "SignUp" and x["event_id"] == f"signup-{suid}" for x in rows),
      "sign-up queues SignUp for each platform with the pixel's event id")
check(px(s.get(r.headers["Location"]).get_data(as_text=True))[2] == [{"name": "SignUp", "event_id": f"signup-{suid}"}],
      "and the pixel fires the same id")
payload = json.loads(rows[0]["payload"])
check(payload["ctx"].get("fbp") == "fb.1.1700000000000.123456789" and payload["ctx"].get("ttp") == "ttp-cookie-1"
      and payload["ctx"].get("ip") and payload["ctx"].get("ua") == "Mozilla/5.0 test",
      "the browser context (IP, user agent, the platforms' own cookies) goes with it")
with db.connect() as conn:
    check(json.loads(conn.execute("SELECT first_touch FROM users WHERE id=?", (suid,)).fetchone()[0])["ttclid"] == "TTCLID_123",
          "the TikTok click id is kept on the account's first touch")
blob = json.dumps([json.loads(x["payload"]) for x in rows])
check("server-signup" not in blob and "@" not in blob and "?" not in payload["ctx"]["url"],
      "no email and no query string in what's queued")

sent = []


class Resp:
    status_code = 200
    text = '{"code":0}'

    def json(self):
        return {"code": 0}


conversions.requests.post = lambda url, headers=None, json=None, timeout=None: sent.append((url, headers, json)) or Resp()
with db.connect() as conn:
    st = conversions.flush(conn)
check(st == {"sent": 3, "failed": 0} and all(x["status"] == "sent" for x in queue_rows()), "the queue sends each row once")
by = {u.split("/")[2]: (u, h, j) for u, h, j in sent}
mu, _, mj = by["graph.facebook.com"]
check(mu.startswith(f"https://graph.facebook.com/v21.0/{IDS['META_PIXEL_ID']}/events")
      and mj["data"][0]["event_name"] == "CompleteRegistration" and mj["data"][0]["event_id"] == f"signup-{suid}"
      and mj["data"][0]["action_source"] == "website" and mj["data"][0]["user_data"]["fbp"].startswith("fb.1."),
      "Meta: CompleteRegistration with the event id, action_source website and _fbp")
tu, th, tj = by["business-api.tiktok.com"]
check(th["Access-Token"] == "tt-test-token" and tj["event_source_id"] == IDS["TIKTOK_PIXEL_ID"]
      and tj["data"][0]["event"] == "CompleteRegistration" and tj["data"][0]["event_id"] == f"signup-{suid}"
      and tj["data"][0]["user"]["ttclid"] == "TTCLID_123",
      "TikTok: CompleteRegistration with the event id and the ttclid")
ru, rh, rj = by["ads-api.reddit.com"]
check(ru.endswith(IDS["REDDIT_PIXEL_ID"]) and rj["events"][0]["event_type"]["tracking_type"] == "SignUp"
      and rj["events"][0]["event_metadata"]["conversion_id"] == f"signup-{suid}"
      and len(rj["events"][0]["user"]["ip_address"]) == 64, "Reddit: SignUp with conversion_id and a hashed IP")
check("server-signup" not in json.dumps(sent), "no email sent to any platform")
with db.connect() as conn:
    check(conversions.flush(conn) == {"sent": 0, "failed": 0}, "nothing is sent twice")

# purchase from the Stripe webhook, same id as the return page's pixels
s.get("/app")
s.post("/app/billing/checkout", data={"csrf": csrf(s), "pack": "starter"})
ssid = f"cs_test_pack{len(sessions)}"
with db.connect() as conn:
    pur = conn.execute("SELECT * FROM purchases WHERE stripe_session_id=?", (ssid,)).fetchone()
    ctx = json.loads(conn.execute("SELECT ctx_json FROM ad_conversions WHERE ref=?", (ssid,)).fetchone()[0])
check(ctx.get("fbp") and "?" not in ctx["url"], "checkout keeps the browser context for the webhook (no query string)")


def deliver(obj, typ="checkout.session.completed"):
    body = json.dumps({"id": "evt_1", "type": typ, "data": {"object": obj}}).encode()
    return client().post("/webhooks/stripe", data=body, content_type="application/json",
                         headers={"Stripe-Signature": billing.sign(body, "whsec_test_only")})


sess_obj = {"id": ssid, "object": "checkout.session", "mode": "payment", "amount_total": 2900, "currency": "usd",
            "payment_status": "paid", "payment_intent": "pi_1",
            "metadata": {"app": "campaignify", "purchase_id": str(pur["id"]), "workspace_id": str(sws), "pack": "starter"}}
check(deliver(dict(sess_obj, payment_status="unpaid")).status_code == 200 and
      not [x for x in queue_rows() if x["event"] == "Purchase"], "an unpaid checkout queues nothing")
check(deliver(sess_obj).get_json().get("credited"), "the paid webhook credits the pack")
prow = [x for x in queue_rows() if x["event"] == "Purchase"]
pp = json.loads(prow[0]["payload"])
check(len(prow) == 3 and all(x["event_id"] == f"purchase-{ssid}" for x in prow) and pp["value"] == 29.0
      and pp["currency"] == "USD", "the webhook queues Purchase for each platform: purchase-<session id>, $29 USD")
deliver(sess_obj)
check(len([x for x in queue_rows() if x["event"] == "Purchase"]) == 3, "a webhook retry queues nothing more")
check(px(s.get(f"/app/billing?checkout=success&session_id={ssid}").get_data(as_text=True))[2] ==
      [{"name": "Purchase", "event_id": f"purchase-{ssid}", "value": 29.0, "currency": "USD"}],
      "the return page's pixels fire the same id, so each platform counts it once")
sent.clear()
with db.connect() as conn:
    conversions.flush(conn)
mj = next(j for u, h, j in sent if "facebook" in u)
tj = next(j for u, h, j in sent if "tiktok" in u)
rj = next(j for u, h, j in sent if "reddit" in u)
check(mj["data"][0]["event_name"] == "Purchase" and mj["data"][0]["custom_data"] == {"value": 29.0, "currency": "USD"}
      and tj["data"][0]["event"] == "CompletePayment" and tj["data"][0]["properties"]["value"] == 29.0
      and rj["events"][0]["event_metadata"]["value_decimal"] == 29.0,
      "Purchase shapes: Meta Purchase, TikTok CompletePayment, Reddit Purchase, each with value + currency")

# plan checkout through the webhook
s.post("/app/billing/subscribe", data={"csrf": csrf(s), "plan": "starter"})
plsid = f"cs_test_plan{len(sessions)}"
deliver({"id": plsid, "object": "checkout.session", "mode": "subscription", "amount_total": 3900, "currency": "usd",
         "payment_status": "paid", "subscription": "sub_t1", "customer": "cus_t1",
         "metadata": {"app": "campaignify", "kind": "plan", "workspace_id": str(sws), "plan": "starter"}})
check(sorted(x["platform"] for x in queue_rows() if x["event_id"] == f"purchase-{plsid}") == ["meta", "reddit", "tiktok"]
      and json.loads(next(x for x in queue_rows() if x["event_id"] == f"purchase-{plsid}")["payload"])["value"] == 39.0,
      "a plan checkout queues Purchase at what Stripe charged")
deliver({"id": "cs_not_ours_1", "mode": "payment", "amount_total": 100, "currency": "usd", "payment_status": "paid",
         "metadata": {"app": "crowdcut"}})
check(not [x for x in queue_rows() if "cs_not_ours" in x["event_id"]], "someone else's checkout queues nothing")

# failures are retried, never raised
with db.connect() as conn:
    conversions.flush(conn)
before = len(queue_rows())
gp = client(**{"Sec-GPC": "1"})
signup(gp, "gpc@example.com")
check(len(queue_rows()) == before, "a GPC sign-up isn't sent server-side either")


class Bad(Resp):
    status_code = 500
    text = "boom access_token=SECRET123"


conversions.requests.post = lambda *a, **k: Bad()
x = client()
signup(x, "retry@example.com")
with db.connect() as conn:
    conversions.flush(conn)
fails = [r for r in queue_rows() if r["status"] == "failed"]
check(len(fails) == 3 and all("SECRET123" not in (r["error"] or "") for r in fails),
      "a platform error is kept for a retry, without the token")
conversions.requests.post = lambda *a, **k: Resp()
with db.connect() as conn:
    conversions.flush(conn)
check(all(r["status"] == "sent" for r in queue_rows()), "and goes out on the retry")
os.environ["META_TEST_EVENT_CODE"] = "TEST123"
check(conversions.build("meta", {"event": "SignUp", "event_id": "signup-1", "ts": 1, "ctx": {}})[2]["test_event_code"]
      == "TEST123", "META_TEST_EVENT_CODE sends to Meta's test events")
os.environ.pop("META_TEST_EVENT_CODE")

print("== deploy settings")
dy = open(os.path.join(ROOT, ".github", "workflows", "deploy.yml")).read()
for k in ("META_PIXEL_ID", "TIKTOK_PIXEL_ID", "REDDIT_PIXEL_ID", "X_PIXEL_ID", "X_EVENT_SIGNUP", "X_EVENT_PURCHASE",
          "X_EVENT_VIEWCONTENT", "META_TEST_EVENT_CODE", "TIKTOK_TEST_EVENT_CODE"):
    check((f"{k}: ${{{{ vars.{k} }}}}" in dy or f"{k}: ${{{{ vars.{k} || '" in dy) and f'"{k}"' in dy,
          f"{k} is a repo variable sent to the server")
for k in ("META_CAPI_TOKEN", "TIKTOK_EVENTS_TOKEN", "REDDIT_CAPI_TOKEN"):
    check(f"{k}: ${{{{ secrets.{k} }}}}" in dy and f'"{k}"' in dy, f"{k} is a secret sent to the server")
priv = pub.get("/privacy").get_data(as_text=True)
check(all(x in priv for x in ("Meta", "TikTok", "Reddit", "X (Twitter)")), "the privacy policy names the pixels")

print(f"\nall {passed} pixel checks passed ({TMP})")
