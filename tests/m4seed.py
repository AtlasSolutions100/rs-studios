"""Shared seed for the M4 closed-loop tests (lead attribution, creative fatigue, weekly report): a throwaway database,
a Growth workspace with a connected Meta ad account (fake Graph, tests/fakegraph.py), a ready plan with 2 finished
ads, and helpers to launch it and to write Meta's daily numbers straight into ad_insights_daily.

    import m4seed as S        (first, before app: it sets the environment)

Nothing leaves the machine. The temp folder is removed at exit.
"""
import atexit
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="rs_m4_")
atexit.register(shutil.rmtree, TMP, True)
TOKEN = "EAAm4closedLoopTOKEN000777"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_PUBLIC_URL="https://campaignify.test", RS_SIGNUP_CONFIRM="0",
                  RS_DISK_MIN_GB="0.2", RS_LAUNCH_KIT="1", TOKEN_KEY=base64.b64encode(b"Q" * 32).decode(),
                  AD_RATE_TIER="full", META_APP_ID="1000000000000001", META_APP_SECRET="fake-app-secret-m4",
                  META_LOGIN_CONFIG_ID="424242424242", AD_LAUNCH="on")
for k in ("AD_LAUNCH_WORKSPACES", "TOKEN_KEYS_OLD", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "CONVERSIONS_API"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adhttp  # noqa: E402
import adinsights  # noqa: E402
import admanage  # noqa: E402
import adplatforms  # noqa: E402
import adpolicy  # noqa: E402
import db  # noqa: E402
import launches  # noqa: E402
from adplatforms import meta  # noqa: E402
from fakegraph import FakeGraph  # noqa: E402
from PIL import Image  # noqa: E402

T = [time.time()]
adhttp.CLOCK = launches.CLOCK = admanage.CLOCK = adinsights.CLOCK = lambda: T[0]
adhttp.SLEEP = lambda s: T.__setitem__(0, T[0] + max(0.0, s))
adhttp.BACKOFF = 0.01
appmod._maybe_reconcile = lambda ws_id, force=False: 0
TZ = "America/Chicago"
passed = [0]
_ip = [0]


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed[0] += 1
    print("  ok ", msg)


def later(s=31.0):
    T[0] += s


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.64.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchall()


def ex(sql, *args):
    with db.connect() as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def ws(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def html(r):
    return r.get_data(as_text=True)


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def signup(email, plan="growth"):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan=?, plan_status='active' WHERE id=?", (plan, u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
        u = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    return c, u


IMG = os.path.join(TMP, "ad1.png")
Image.new("RGB", (864, 1080), (200, 120, 40)).save(IMG)
VID = os.path.join(TMP, "ad2.mp4")
open(VID, "wb").write(b"\x00\x00\x00\x18ftypmp42fake video bytes for the fake graph")
Image.new("RGB", (720, 1280), (40, 120, 200)).save(VID + ".jpg")
OKC = {"ok": True, "warnings": [], "blocks": []}
PLAN = {"angles": [{"name": "Evening ritual"}, {"name": "Lasts the month"}],
        "copy": [{"platform": "meta", "kind": "ad", "angle": "all", "variants": [
            {"angle": "Evening ritual", "ok": True, "compliance": OKC, "fields": [
                {"name": "Primary text", "text": "Light it once. Smell it all week."},
                {"name": "Headline", "text": "Ember Candle, 50 hours"}]},
            {"angle": "Lasts the month", "ok": True, "compliance": OKC, "fields": [
                {"name": "Primary text", "text": "One candle, fifty hours."}, {"name": "Headline", "text": "Burns 50 hours"}]}]}],
        "final": {"summary": "Image and video first.", "budget_split": [{"platform": "meta", "share_percent": 100}]}}
BRIEF = {"offer": "Ember Candle", "goal": "leads", "audience": "Renters 25-40", "platforms": ["meta"],
         "link": "https://campaignify.test/p/ember", "budget_amount": 40, "budget_currency": "USD", "budget_period": "day",
         "budget_days": 14}


def make_plan(ws_id, user_id, name="Ember Candle", brief=None):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, plan_json, steps_json, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (ws_id, user_id, name, json.dumps(brief or BRIEF), "ready", json.dumps(PLAN), "[]", db.now(),
                            db.now())).lastrowid
        for tool, kind, path, angle, params in (("ad_image", "image", IMG, "Evening ritual", {}),
                                                ("ugc_video", "video", VID, "Lasts the month", {"ad_type": "testimonial"})):
            jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, "
                               "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (ws_id, user_id, tool, "b", json.dumps(params), "done", 43, db.now(), db.now())).lastrowid
            conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (jid, kind, "", path))
            conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, credits, status, "
                         "job_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (cid, ws_id, angle, "meta", tool, "b", 43, "queued", jid, db.now()))
        conn.commit()
    return cid


def connect(ws_id, user_id, account="111000111", currency="USD"):
    with db.connect() as conn:
        cid = conn.execute(
            "INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, currency, timezone, "
            "page_id, page_name, instagram_actor_id, instagram_username, external_user_id, token_enc, scopes, status, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ws_id, "meta", account, "Ember Shop", currency, TZ, "500000001", "Ember Candle Co",
             "17841400000000001", "embercandle", "10200000000000001", adplatforms.seal_token(ws_id, TOKEN),
             " ".join(meta.SCOPES), "active", user_id, db.now(), db.now())).lastrowid
        conn.execute("INSERT INTO pixels (workspace_id, platform, connection_id, external_pixel_id, name, created_at) "
                     "VALUES (?,?,?,?,?,?)", (ws_id, "meta", cid, "700000002", "Ember site pixel", db.now()))
        conn.commit()
    return cid


G = FakeGraph(account="act_111000111")      # one Meta for every launch, so each campaign page reads its own ads
adhttp.TRANSPORT = G


def launched(ws_id, user, plan_id, name):
    """A launched (PAUSED) 2-ad campaign of the plan on the fake Graph. Returns (launch id, [ad ids], campaign id)."""
    adhttp.TRANSPORT = G
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM campaigns WHERE id=?", (plan_id,)).fetchone()
        crow = launches.connection(conn, ws_id)
        d = launches.draft(conn, ws(ws_id), c, crow)
        d["name"] = name
        spec, issues, card, _ = launches.review(conn, ws(ws_id), d, crow)
        assert spec is not None and not adpolicy.blocking(issues), [i.as_dict() for i in issues]
        card_id = launches.save_card(conn, ws_id, user["id"], c["id"], crow, spec, card, issues)
        lid, _ = launches.approve(conn, ws(ws_id), user, conn.execute("SELECT * FROM approval_cards WHERE id=?",
                                                                       (card_id,)).fetchone())
    for _ in range(30):
        if launches.run(lid) == "paused":
            break
        later(61)
    assert q("SELECT status FROM launches WHERE id=?", lid)[0]["status"] == "paused"
    ads = [r["external_id"] for r in q("SELECT external_id FROM launch_steps WHERE launch_id=? AND kind='ad' ORDER BY step_no",
                                       lid)]
    camp = q("SELECT external_campaign_id FROM launches WHERE id=?", lid)[0]["external_campaign_id"]
    return lid, ads, camp


def day(n):
    """The ad account's date n days ago."""
    from datetime import timedelta
    return (adinsights.today(TZ) - timedelta(days=n)).isoformat()


def insight(ws_id, conn_id, lid, ad_id, camp_id, n, *, spend_minor, imp, link, results=0, kind="lead", leads=0):
    """One ad-day as adinsights.store writes it (Meta's numbers, already parsed)."""
    with db.connect() as conn:
        conn.execute("INSERT INTO ad_insights_daily (workspace_id, connection_id, launch_id, platform, level, "
                     "external_object_id, campaign_external_id, day, currency, spend_minor, impressions, reach, clicks, "
                     "link_clicks, ctr, results, result_kind, leads, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(platform, external_object_id, day) DO UPDATE SET spend_minor=excluded.spend_minor, "
                     "impressions=excluded.impressions, link_clicks=excluded.link_clicks, ctr=excluded.ctr, "
                     "results=excluded.results, leads=excluded.leads",
                     (ws_id, conn_id, lid, "meta", "ad", str(ad_id), str(camp_id), day(n), "USD", spend_minor, imp,
                      int(imp * .85), int(link * 1.3), link, round(link * 100.0 / imp, 4) if imp else None, results, kind,
                      leads, db.now()))
        conn.commit()
