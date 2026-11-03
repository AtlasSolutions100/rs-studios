"""The first run, from sign-up to the first ad (the 4 sign-ups / 0 ads leak), on a throwaway database:

  * /signup shows "What happens next" (desktop panel); the confirmation email's subject is the code
  * Home starts on image ads (2 free ones), the confirm-email banner under the form, the preview as a later choice
  * "Paste your product page" reads the store with the existing importer (no AI, nothing charged) and hands the
    form name, brand and photo; a store that refuses apps gets a by-hand message
  * ad-grant accounts (500 credits) get the 5-second product video pre-selected, behind the inline code box
  * the first submit makes 2 image ads (one 43-credit job), Home shows them rendering with the unlock-video box
  * first-run events are whitelisted in /e and verify_completed is written once
  * nudge emails at 10 minutes and 24 hours with a magic link (/m) that logs in and confirms; FIRST_RUN_NUDGES=0

    python tests/test_first_run.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_firstrun_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai", RS_PUBLIC_URL="https://campaignify.test")
os.environ.pop("FIRST_RUN_NUDGES", None)
os.environ.pop("SIGNUP_CREDITS_ADS", None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import mailer  # noqa: E402
import nudges  # noqa: E402
import site_events  # noqa: E402
import verify  # noqa: E402

passed = 0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
STARTER = catalog.starter_credits()
R = catalog.rates()
TWO = catalog.price_credits("ad_image", batch=2)


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.7.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.environ_base["HTTP_USER_AGENT"] = UA
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def png(n=64):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (n, n), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


sent = []


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": f"re_{len(sent)}"}


mailer.requests.post = lambda url, json=None, headers=None, timeout=0: (sent.append(json) or _Resp())


def event(c, name, detail=None):
    return c.post("/e", data=json.dumps({"name": name, "path": "/app", "detail": detail}), content_type="text/plain")


def events(ws_id):
    with db.connect() as conn:
        return [(r["name"], r["detail"]) for r in conn.execute(
            "SELECT name, detail FROM site_events WHERE workspace_id=? ORDER BY id", (ws_id,))]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def ws_of(email):
    u = user(email)
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()


print("== sign-up page and the confirmation email")
c = client()
html = c.get("/signup").get_data(as_text=True)
check("What happens next" in html and "Paste your product page" in html and "Pick a template" in html
      and "ready in about a minute" in html and 'class="auth wide"' in html,
      "/signup carries the 3-step 'What happens next' panel (desktop column)")
check("What happens next" not in c.get("/signup?invite=nope").get_data(as_text=True), "invite sign-ups don't get the panel")
r = c.post("/signup", data={"csrf": csrf(c), "email": "first@example.com", "password": "longpassword"})
check(r.status_code == 302 and r.headers["Location"].endswith("/app"), "sign-up lands on Home")
code = re.search(r"^Your Campaignify code: (\d{6})$", sent[-1]["subject"])
check(code and code.group(1) in sent[-1]["text"], "the confirmation email's subject is 'Your Campaignify code: 123456'")
code = code.group(1)
ws1 = ws_of("first@example.com")

print("== Home: the form first, image ads first")
home = c.get("/app").get_data(as_text=True)
check('data-first-run' in home and 'value="image" checked' in home and 'value="preview" checked' not in home
      and 'value="video"' not in home, "a plain sign-up starts on image ads (no video kind: the grant isn't theirs)")
check("Make my 2 free ads" in home and "Make your 2 free ads" in home, "the button says 'Make my 2 free ads'")
check("after you confirm your email" in home and 'value="preview"' in home,
      "the free talking-video preview stays, labelled 'after you confirm your email'")
check(home.index('action="/app/start"') < home.index("data-verify-banner") and "data-vinline" in home,
      "the confirm-email banner sits under the form; the code box is inline in the form")
check('"inline": true' in home, "app.js is told the code box is inline (no pop-up on this page)")
check('id="purl"' in home and "data-import" in home and 'data-import-url="/app/start/import"' in home
      and home.index('id="purl"') < home.index('id="pn"'), "'Paste your product page' is the first field")

print("== paste your product page")
PAGE = """<!doctype html><html><head><title>Ember Candle | Hearth & Co</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Ember Candle",
"description":"A soy wax candle with amber and cedar.","image":["https://cdn.hearth.example/ember-1.jpg"],
"brand":{"@type":"Brand","name":"Hearth & Co"}}</script></head><body><h1>Ember Candle</h1></body></html>"""
fetched = []


def fake_fetch(url, accept=None):
    fetched.append(url)
    if "hearth" in url:
        return {"url": url, "status": 200, "content_type": "text/html", "text": PAGE}
    if "private" in url:
        raise fetcher.FetchError("That address points to a private or internal network.")
    return {"url": url, "status": 403, "content_type": "text/html", "text": ""}


fetcher.fetch = fake_fetch
fetcher.fetch_bytes = lambda url, max_bytes=0, accept=None: {"url": url, "status": 200, "content_type": "image/png",
                                                             "data": png(400).getvalue()}
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
r = c.post("/app/start/import", json={"url": "hearth.example/p/ember"}, headers={"X-CSRF": csrf(c)})
d = r.get_json()
check(r.status_code == 200 and d["ok"] and d["name"] == "Ember Candle" and d["brand"] == "Hearth & Co"
      and d["photo"] and d["photo"]["url"].startswith("/photo/"), "the importer answers with name, brand and the first photo")
check(ws_of("first@example.com")["credits"] == STARTER and not any("anthropic" in u for u in fetched),
      "no AI read on the first run: nothing charged, the free credits stay for the ads")
with db.connect() as conn:
    prods = conn.execute("SELECT * FROM products WHERE workspace_id=?", (ws1["id"],)).fetchall()
    brand = conn.execute("SELECT brand_name FROM brands WHERE workspace_id=?", (ws1["id"],)).fetchone()[0]
check(len(prods) == 1 and prods[0]["name"] == "Ember Candle" and brand == "Hearth & Co" and prods[0]["primary_photo_id"],
      "the product is saved with its brand and photo (Products > the same import)")
os.environ.pop("ANTHROPIC_API_KEY", None)
r = c.post("/app/start/import", json={"url": "https://blocked.example/p/x"}, headers={"X-CSRF": csrf(c)})
check(r.status_code == 422 and r.get_json()["manual"] and "Type the name and upload a photo" in r.get_json()["error"],
      "a store that refuses apps: a by-hand message, upload stays the fallback")
r = c.post("/app/start/import", json={"url": "https://private.example/"}, headers={"X-CSRF": csrf(c)})
check(r.status_code == 422 and "private" in r.get_json()["error"], "a private address is refused (the SSRF guard)")
check(c.post("/app/start/import", json={}, headers={"X-CSRF": csrf(c)}).status_code == 400, "no link: 400")

print("== the first submit: 2 image ads, rendering on Home")
photo_id = d["photo"]["id"]
r = c.post("/app/start", data={"csrf": csrf(c), "kind": "image", "product_name": "Ember Candle", "template": "hero",
                               "photo_id": str(photo_id)})
check(r.status_code == 302 and r.headers["Location"].startswith("/app?made="), "the first submit goes back to Home")
with db.connect() as conn:
    jobs = conn.execute("SELECT * FROM jobs WHERE workspace_id=?", (ws1["id"],)).fetchall()
    prods = conn.execute("SELECT * FROM products WHERE workspace_id=?", (ws1["id"],)).fetchall()
check(len(jobs) == 1 and jobs[0]["tool"] == "ad_image" and jobs[0]["credits"] == TWO
      and json.loads(jobs[0]["params_json"])["batch"] == 2 and jobs[0]["product_id"] == prods[0]["id"] and len(prods) == 1,
      f"one job for 2 image ads ({TWO} credits) on the imported product (no duplicate product)")
check(ws_of("first@example.com")["credits"] == STARTER - TWO, "paid from the starter credits")
home = c.get(r.headers["Location"]).get_data(as_text=True)
check("data-made" in home and 'data-live-cards="batch"' in home and "Making 1 of 1" in home and "about a minute" in home,
      "Home shows the batch rendering, live")
check("Confirm your email to unlock video ads" in home and "data-verify-open" in home and "data-first-run" not in home,
      "the 'confirm your email to unlock video' box sits under the progress; the form is gone")
check("data-made" in c.get("/app").get_data(as_text=True), "while the only batch renders, plain /app shows it too")

print("== first-run events")
for name, detail in (("first_run_viewed", "image"), ("first_run_kind_chosen", "preview"), ("first_run_imported", "photo"),
                     ("verify_box_opened", "preview"), ("first_output_viewed", "live")):
    check(event(c, name, detail).status_code == 204, f"/e takes {name}")
check(event(c, "made_up_event").status_code == 400, "/e still refuses names off the whitelist")
got = events(ws1["id"])
check(("first_run_viewed", "image") in got and ("first_run_kind_chosen", "preview") in got and ("first_output_viewed", "live") in got
      and ("verify_box_opened", "preview") in got and ("first_run_imported", "photo") in got,
      "the events are written against the member's workspace with their detail")
check(got[0] == ("signup", "starter") and ("first_ad", "ad_image") in got, "next to the server-side signup and first_ad steps")
r = c.post("/app/verify/code", json={"code": code}, headers={"X-CSRF": csrf(c)})
check(r.status_code == 200 and events(ws1["id"]).count(("verify_completed", None)) == 1, "a right code writes verify_completed")
c.post("/app/verify/code", json={"code": code}, headers={"X-CSRF": csrf(c)})
check(events(ws1["id"]).count(("verify_completed", None)) == 1, "once per workspace")
home = c.get("/app?made=%s" % jobs[0]["batch_id"]).get_data(as_text=True)
check("Confirm your email to unlock video ads" not in home and "data-made" in home, "confirmed: the unlock box is gone")

print("== ad-grant accounts: the free video ad first")
a = client()
a.get("/ai-ugc-ad-generator?gclid=Ad-77")
sent_before = len(sent)
r = a.post("/signup", data={"csrf": csrf(a), "email": "adbuyer@example.com", "password": "longpassword"})
check(r.status_code == 302 and ws_of("adbuyer@example.com")["credits"] == 500, "an ad click sign-up gets the 500-credit grant")
home = a.get("/app").get_data(as_text=True)
check('value="video" checked' in home and "Your free video ad" in home and ">included<" in home
      and "Make my free video ad" in home and f"{R['video5']} of your 500 free credits" in home,
      "the 5-second product video is pre-selected, 'Your free video ad (included)'")
check('data-verify-video="video"' in home and "Your video starts the moment you paste the code." in home
      and "Confirm your email first, with the code we sent you." in home,
      "the inline code box says the video starts the moment the code is pasted")
r = a.post("/app/start", data={"csrf": csrf(a), "kind": "video", "product_name": "Ember Candle", "photo": (png(), "e.png")},
           content_type="multipart/form-data")
with db.connect() as conn:
    njobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (ws_of("adbuyer@example.com")["id"],)).fetchone()[0]
check(r.status_code == 403 and njobs == 0 and 'class="vbanner hot"' in r.get_data(as_text=True)
      and r.get_data(as_text=True).index('action="/app/start"') < r.get_data(as_text=True).index('class="vbanner hot"'),
      "unconfirmed: the video waits for the code (form kept, nothing queued, banner still under the form)")
code2 = re.search(r"code: (\d{6})", sent[sent_before]["subject"]).group(1)
check(a.post("/app/verify/code", json={"code": code2}, headers={"X-CSRF": csrf(a)}).status_code == 200, "the code confirms")
r = a.post("/app/start", data={"csrf": csrf(a), "kind": "video", "product_name": "Ember Candle", "photo": (png(), "e.png")},
           content_type="multipart/form-data")
with db.connect() as conn:
    vj = conn.execute("SELECT * FROM jobs WHERE workspace_id=?", (ws_of("adbuyer@example.com")["id"],)).fetchone()
check(r.status_code == 302 and vj["tool"] == "photo_to_video" and vj["credits"] == R["video5"]
      and json.loads(vj["params_json"])["duration"] == 5 and "Ember Candle" in vj["brief"]
      and ws_of("adbuyer@example.com")["credits"] == 500 - R["video5"],
      f"the free video ad is a 5-second product video for {R['video5']} credits, paid from the grant")
check("Videos" in a.get(r.headers["Location"]).get_data(as_text=True), "Home shows it rendering (videos take minutes)")
p = client()
p.post("/signup", data={"csrf": csrf(p), "email": "plain@example.com", "password": "longpassword"})
r = p.post("/app/start", data={"csrf": csrf(p), "kind": "video", "product_name": "X", "photo": (png(), "e.png")},
           content_type="multipart/form-data")
check(r.status_code == 400 and "on this account. Make image ads instead." in r.get_data(as_text=True), "a plain account can't pick the video kind")

print("== nudge emails: 10 minutes and 24 hours, a magic link, a kill switch")
secret = appmod.app.secret_key
now = datetime.now(timezone.utc)
with db.connect() as conn:
    check(nudges.due(conn, now) == [], "nothing is due the minute after sign-up")
    due = nudges.due(conn, now + timedelta(minutes=11))
check([(u["email"], s) for u, s in due] == [("plain@example.com", 1)],
      "10 minutes on, only the sign-up with no job is due (not the two that made ads)")
sent_before = len(sent)
check(nudges.run_once(secret, now + timedelta(minutes=11)) == 1 and len(sent) == sent_before + 1
      and sent[-1]["to"] == ["plain@example.com"] and sent[-1]["subject"] == "Your 2 free ads are waiting"
      and sent[-1]["from"] == '"Campaignify" <no-reply@mail.campaignify.ai>', "the first nudge: 'Your 2 free ads are waiting'")
link = re.search(r"https://campaignify\.test/m\?t=[\w.\-]+", sent[-1]["text"])
check(link and link.group(0) in sent[-1]["html"] and "2 image ads" in sent[-1]["text"] and "$" not in sent[-1]["text"],
      "with a magic link to the app and costs in credits only")
check(nudges.run_once(secret, now + timedelta(minutes=30)) == 0, "not twice")
check(nudges.run_once(secret, now + timedelta(hours=25)) == 1 and sent[-1]["subject"] == "Still here: your 2 free ads",
      "a second nudge a day later")
check(nudges.run_once(secret, now + timedelta(hours=26)) == 0, "and that's all")
g = client()
g.get("/pricing?gclid=Ad-78")
g.post("/signup", data={"csrf": csrf(g), "email": "grantnudge@example.com", "password": "longpassword"})
nudges.run_once(secret, now + timedelta(minutes=11))
check(sent[-1]["to"] == ["grantnudge@example.com"] and sent[-1]["subject"] == "Your free video ad is waiting"
      and "5-second product video" in sent[-1]["text"], "an ad-grant account is told its free video ad is waiting")
os.environ["FIRST_RUN_NUDGES"] = "0"
k = client()
k.post("/signup", data={"csrf": csrf(k), "email": "killed@example.com", "password": "longpassword"})
n_sent = len(sent)
check(nudges.run_once(secret, now + timedelta(hours=30)) == 0 and len(sent) == n_sent, "FIRST_RUN_NUDGES=0 sends nothing")
os.environ.pop("FIRST_RUN_NUDGES")
with db.connect() as conn:
    old = now - timedelta(days=9)
    conn.execute("UPDATE users SET created_at=? WHERE email='killed@example.com'", (old.replace(microsecond=0).isoformat(),))
    conn.commit()
    check(not any(u["email"] == "killed@example.com" for u, _ in nudges.due(conn, now + timedelta(hours=30))),
          "accounts older than a week are never mailed (a deploy doesn't nudge old sign-ups)")
# the magic link logs in, confirms the email and opens Home
m = client()
r = m.get(link.group(0).replace("https://campaignify.test", ""))
u = user("plain@example.com")
check(r.status_code == 302 and r.headers["Location"].endswith("/app") and u["email_verified_at"]
      and m.get("/app").status_code == 200 and "data-first-run" in m.get("/app").get_data(as_text=True),
      "the magic link logs in, confirms the address (it reached that inbox) and opens the first-ad form")
check(("verify_completed", None) in events(u["workspace_id"]), "and writes verify_completed")
r = client().get("/m?t=not-a-token")
check(r.status_code == 302 and r.headers["Location"].endswith("/login"), "a bad link just goes to Log in")
with appmod.app.test_request_context():
    from itsdangerous import URLSafeTimedSerializer
    stale = URLSafeTimedSerializer(secret, salt=nudges.SALT).dumps({"u": u["id"], "e": "someone-else@example.com"})
check(client().get(f"/m?t={stale}").headers["Location"].endswith("/login"), "a link for a changed email doesn't log in")
check(verify.is_verified(user("plain@example.com")) and site_events.CLIENT_EVENTS[:3] == ("lp_view", "cta_click", "signup_view"),
      "the old client events keep their place (HQ's contract)")

print(f"\nall {passed} first-run checks passed ({TMP})")
