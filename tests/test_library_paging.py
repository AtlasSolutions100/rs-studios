"""The Library loads its newest cards first and the rest as the member scrolls (app.library ?before=, part=1;
static/app.js libMore), and a picture's card uses a small webp (/media/<id>?thumb=1). On a throwaway database.

    python tests/test_library_paging.py      (tests/test_app.py runs it too)
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
TMP = tempfile.mkdtemp(prefix="rs_libpage_")
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


a = member("a@example.com")
b = member("b@example.com")
ids = [job("a@example.com", "ad_image", f"Ad number {i}", f"Hook {i}") for i in range(60)]
oid, path = picture(ids[-1])

print("== the first cards, then the rest as you scroll")
body = page(a, "/app/library")
first = shown(body)
check(first == ids[::-1][:appmod.LIB_PAGE], f"the first load shows the newest {appmod.LIB_PAGE}")
check("60 items" in body, "the count is of everything that matches, not just what loaded")
m = re.search(r'data-lib-more="([^"]+)"', body)
check(m and f"before={first[-1]}" in m.group(1) and "part=1" in m.group(1) and '>Show more</a>' in body,
      "the next cards' address is on the page, with a Show more link for when scripts don't run")
r = a.get(m.group(1), headers={"X-Requested-With": "fetch"})
d = r.get_json()
second = [int(x) for x in re.findall(r'data-job="(\d+)"', d["html"])]
check(second == ids[::-1][appmod.LIB_PAGE:2 * appmod.LIB_PAGE] and "before=" in d["next"] and "part=1" in d["next"],
      "the next cards come back as HTML, with the address of the ones after")
d = a.get(d["next"]).get_json()
third = [int(x) for x in re.findall(r'data-job="(\d+)"', d["html"])]
check(third == ids[::-1][2 * appmod.LIB_PAGE:] and d["next"] == "", "the last cards end the list")
check(len(set(first + second + third)) == 60, "every ad shows once")
nojs = page(a, f"/app/library?before={first[-1]}")
check(shown(nojs) == second and "Back to the newest" in nojs, "without scripts, Show more opens the next cards as a page")
f = page(a, "/app/library?q=Hook+5")
check(shown(f) == [ids[i] for i in range(59, 49, -1)] + [ids[5]] and "data-lib-more" not in f and "11 items" in f,
      "a search pages the same way (here everything fits)")
check(shown(page(b, f"/app/library?before={ids[-1] + 1}")) == [], "another workspace's ads never come in")

print("== thumbnails")
check(f'src="/media/{oid}?thumb=1"' in body and 'loading="lazy"' in body, "a picture's card asks for its small version, lazily")
r = a.get(f"/media/{oid}?thumb=1")
from PIL import Image
im = Image.open(io.BytesIO(r.data))
check(r.status_code == 200 and r.mimetype == "image/webp" and im.size == (appmod.THUMB_W, 600), "a small webp, the picture's own shape")
check(os.path.exists(f"{path}.w{appmod.THUMB_W}.webp") and len(r.data) < os.path.getsize(path), "kept beside the full file, and smaller")
check(a.get(f"/media/{oid}?thumb=1").data == r.data, "made once")
check(a.get(f"/media/{oid}").data == open(path, "rb").read(), "the full file is unchanged (details, download)")
check(b.get(f"/media/{oid}?thumb=1").status_code == 404, "another workspace can't fetch it")
small, spath = picture(ids[-2], 300, 300)
check(a.get(f"/media/{small}?thumb=1").data == open(spath, "rb").read(), "a picture already small is served as it is")

print(f"\nall {passed} library paging checks passed")
shutil.rmtree(TMP, ignore_errors=True)
