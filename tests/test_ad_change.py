"""The ad viewer's change (Koby, Nov 14 2026): a finished picture is made again with a change the member types,
starting from that picture, for the price of one picture shown first (jobs_routes.change, worker image_prompt /
build_inputs). On a throwaway database.

    python tests/test_ad_change.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_adchange_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0", UI_V2="1")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "PLANS_ONLY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY", "AD_LAUNCH", "AD_LAUNCH_WORKSPACES"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import desk  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (400, 400), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.15.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def member(email):
    import onboarding
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.execute("UPDATE users SET email_verified_at=?, name='Kay Lee' WHERE email=?", (db.now(), email))
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (user(email)["workspace_id"],))
        conn.commit()
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": "Ember Candle", "photos": (png(), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    return c


def job(email, tool, brief, hook, status="done", days_ago=0.0):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    with db.connect() as conn:
        u = user(email)
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                           "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (u["workspace_id"], u["id"], tool, brief,
                                                                     json.dumps({"hook": hook, "label": hook}), status, 12, 0.1, at, at)).lastrowid
        conn.commit()
    return jid


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def shown(body):
    return [int(x) for x in re.findall(r'<article class="litem[^"]*" data-job="(\d+)"', body)]



def picture(jid, w=1600, h=2000):
    """A finished image job's output, a real picture on disk."""
    from PIL import Image
    path = os.path.join(TMP, f"out-{jid}.png")
    Image.new("RGB", (w, h), (30, 160, 90)).save(path, "PNG")
    with db.connect() as conn:
        oid = conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?, 'image', ?)", (jid, path)).lastrowid
        conn.commit()
    return oid, path


import catalog  # noqa: E402
import worker  # noqa: E402

a = member("a@example.com")
b = member("b@example.com")
jid = job("a@example.com", "ad_image", "A candle on a desk", "Light it once")
oid, path = picture(jid)
vid = job("a@example.com", "ugc_video", "A talking video", "Hi there")
price = catalog.price_credits("ad_image", batch=1)

print("== the card says what the viewer needs")
body = page(a, "/app/library")
card = re.search(rf'<article class="litem[^"]*" data-job="{jid}"[^>]*>', body).group(0)
check(f'data-change="/app/jobs/{jid}/change"' in card and f'data-change-price="{price}"' in card and "data-view=" in card,
      "a finished picture carries its outputs, the change address and the price of one picture")
check("data-view-open" in body and 'aria-label="Open big"' in body, "each finished card has an Open big button for the big view")

print("== making the change")
def change(c, j, **kw):
    d = dict(note="Show it on a phone, not a watch", output=oid, total=price); d.update(kw)
    return c.post(f"/app/jobs/{j}/change", json=d, headers={"X-CSRF": csrf(c), "Accept": "application/json"})
bal0 = None
with db.connect() as conn:
    bal0 = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (user("a@example.com")["workspace_id"],)).fetchone())
r = change(a, jid, total=price + 1)
check(r.status_code == 409 and r.get_json()["total"] == price, "a stale price is refused and the right one returned")
r = change(a, jid, note="no")
check(r.status_code == 400 and "what to change" in r.get_json()["error"], "a note too short says to say what to change")
r = change(b, jid)
check(r.status_code == 404, "another workspace's ad can't be changed")
r = change(a, vid)
check(r.status_code == 400, "a presenter video has no change (its script and person make it); it has new takes instead")
r = change(a, jid)
new = r.get_json()["job"]
check(r.status_code == 200 and r.get_json()["ok"] and "highlight=" in r.get_json()["library"], "the change starts and points at the Library")
with db.connect() as conn:
    nj = conn.execute("SELECT * FROM jobs WHERE id=?", (new,)).fetchone()
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (nj["workspace_id"],)).fetchone()
    bal1 = db.balance(ws)
p = json.loads(nj["params_json"])
check(nj["tool"] == "ad_image" and nj["status"] == "pending" and nj["credits"] == price and bal0 - bal1 == price
      and nj["parent_job_id"] == jid, "one new picture, charged the price shown, linked to the one it changes")
check(p["batch"] == 1 and p["edit_from"] == oid and p["change_note"] == "Show it on a phone, not a watch"
      and p["label"].endswith(" · changed"), "it starts from that picture, with the note, labelled changed")

print("== what the model is told")
seen = {}
worker.hf.upload = lambda data, ctype: seen.setdefault("url", "https://files.example/start.png")
with db.connect() as conn:
    inp = worker.build_inputs(conn, nj)
check(inp["image_urls"][0] == "https://files.example/start.png", "the picture being changed is the first reference")
check("Start from the first reference picture" in inp["prompt"] and "Change only this, asked for by the advertiser: Show it on a phone, not a watch" in inp["prompt"]
      and "Keep everything else the same" in inp["prompt"], "the prompt says to change only that and keep the rest")
with db.connect() as conn:
    conn.execute("UPDATE jobs SET params_json=? WHERE id=?", (json.dumps(dict(p, layout="headline")), new))
    conn.commit()
    lay = conn.execute("SELECT * FROM jobs WHERE id=?", (new,)).fetchone()
    seen.clear()
    try:
        inp2 = worker.build_inputs(conn, lay)
    except Exception as e:      # a text-layer ad builds its own scene; only the reference rule matters here
        inp2 = {"image_urls": [], "prompt": str(e)}
check("url" not in seen and "Start from the first reference picture" not in inp2.get("prompt", ""),
      "a text-layer ad is not started from its own picture (its words are set afterwards)")

print("== new takes")
import jobs_routes  # noqa: E402
def vary(c, j, **kw):
    return c.post(f"/app/jobs/{j}/vary", json=kw, headers={"X-CSRF": csrf(c), "Accept": "application/json"})
prices = {n: catalog.price_credits("ad_image", batch=n) for n in jobs_routes.VARY_COUNTS}
with db.connect() as conn:
    db.adjust_credits(conn, user("a@example.com")["workspace_id"], 5000, "test top-up")
    conn.commit()
body = page(a, "/app/library")
card = re.search(rf'<article class="litem[^"]*" data-job="{jid}"[^>]*>', body).group(0)
check(f'data-vary="/app/jobs/{jid}/vary"' in card and 'data-vary-kind="image"' in card and str(prices[3]) in card,
      "a picture's card carries the new-takes address and the price for 2, 3 or 4")
check(vary(a, jid, count=3, output=oid, total=prices[3] + 1).status_code == 409, "a stale price is refused")
check(vary(a, jid, count=9, output=oid, total=prices[3]).status_code == 400, "only 2, 3 or 4 at a time")
r = vary(a, jid, count=3, output=oid, total=prices[3])
with db.connect() as conn:
    vj = conn.execute("SELECT * FROM jobs WHERE id=?", (r.get_json()["job"],)).fetchone()
vp = json.loads(vj["params_json"])
check(r.status_code == 200 and vj["credits"] == prices[3] and vp["batch"] == 3 and vp["vary"] and vp["edit_from"] == oid
      and vp["label"].endswith(" · new takes") and vj["parent_job_id"] == jid, "3 new takes from that picture, at the price shown")
with db.connect() as conn:
    inp = worker.build_inputs(conn, vj)
check("Make a new take on the first reference picture" in inp["prompt"] and inp["batch_size"] == 3, "the model is asked for new takes on it")
vprice = jobs_routes.price_now(None, {"tool": "ugc_video", "params_json": json.dumps({"hook": "Hi there"}), "credits": 0, "cost_usd": 0})[0]
with db.connect() as conn:
    conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?, 'video', ?)", (vid, path))
    conn.commit()
r = vary(a, vid, total=vprice)
with db.connect() as conn:
    vv = conn.execute("SELECT * FROM jobs WHERE id=?", (r.get_json()["job"],)).fetchone()
check(r.status_code == 200 and vv["tool"] == "ugc_video" and vv["credits"] == vprice and json.loads(vv["params_json"])["vary_of"] == vid,
      "a presenter video: one more made with the same settings, at today's price")

print("== changing a product video")
pv = job("a@example.com", "photo_to_video", "The candle on a table, slow push in", "Slow glow")
with db.connect() as conn:
    conn.execute("UPDATE jobs SET params_json=? WHERE id=?", (json.dumps({"hook": "Slow glow", "duration": 5}), pv))
    conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?, 'video', ?)", (pv, path))
    conn.commit()
pprice = catalog.price_credits("photo_to_video", duration=5)
r = a.post(f"/app/jobs/{pv}/change", json={"note": "Show the label up close", "total": pprice}, headers={"X-CSRF": csrf(a), "Accept": "application/json"})
with db.connect() as conn:
    cv = conn.execute("SELECT * FROM jobs WHERE id=?", (r.get_json()["job"],)).fetchone()
check(r.status_code == 200 and cv["tool"] == "photo_to_video" and cv["credits"] == pprice
      and cv["brief"].endswith("Change from the last version, asked for by the advertiser: Show the label up close. Keep the product exactly as it is."),
      "a product video is made again with the change added to its scene, at the price shown")

print(f"\nall {passed} checks passed")
shutil.rmtree(TMP, ignore_errors=True)
