"""The welcome credits (plan_gate.grant_welcome, Koby Nov 13 2026) on a throwaway database. Not a free plan: a new
sign-up skips paying and the first run, confirms its email and gets WELCOME_CREDITS once, for its brand setup and
image ads. Videos wait for a plan; when the credits run out the plan step (with the 3-day trial) comes back.

    python tests/test_welcome_credits.py

  * nothing before the email is confirmed; 500 once it is (code or link), and a Google sign-up gets them at once
  * once per workspace and per person (email folded), never for a team member, a person who had a trial or paid
  * while it holds some: not sent to pick a plan, the plan page can be looked at and skipped, a banner shows what's left
  * a video is refused with the plan message; an image ad goes through
  * out of credits: the plan step again, with the "used your free credits" message
  * the first ads are offered paid from the welcome credits (two image ads, no video); the upgrade box opens
    once on the desk after them, and short of credits it's the upgrade, not a pack
  * WELCOME_CREDITS=0 puts the trial-only sign-up back
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_welcome_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  STRIPE_SECRET_KEY="sk_test_wc", STRIPE_WEBHOOK_SECRET="whsec_wc", PLANS_ONLY="1",
                  RS_SIGNUP_CONFIRM="0", WELCOME_CREDITS="500")
for k in ("RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "SIGNUP_CREDITS_ADS", "RS_STARTER_CREDITS", "UI_V2", "HOME_V2",
          "STRIPE_SETUP", "RS_EMAIL_VERIFY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import billing  # noqa: E402
import db  # noqa: E402
import first_ads  # noqa: E402
import plan_gate  # noqa: E402
import verify  # noqa: E402

billing._post = lambda path, form, idempotency_key=None: {"id": "cs_x", "url": "https://checkout.stripe.test/x"}
billing._get = lambda path, params=None: {"object": "list", "data": []}
appmod._maybe_reconcile = lambda ws_id, force=False: 0
import mailer  # noqa: E402
mailer.configured = lambda: True
mailer.send = lambda **kw: "em_1"

passed = 0


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
    c.environ_base["REMOTE_ADDR"] = f"10.21.{_ip[0] // 200}.{_ip[0] % 200 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"})
    assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:300])
    return c


def row(sql, *a):
    with db.connect() as conn:
        return conn.execute(sql, a).fetchone()


def run_sql(sql, *a):
    with db.connect() as conn:
        conn.execute(sql, a)
        conn.commit()


def ws_of(email):
    return row("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?", email)


def user_of(email):
    return row("SELECT * FROM users WHERE email=?", email)


def confirm(email):
    secret = appmod.app.secret_key
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        code = verify.new_code(conn, secret, u)
        conn.commit()
        return verify.check_code(conn, secret, u, code)[0]


print("== sign-up, then the email")
check(plan_gate.on() and plan_gate.welcome_credits() == 500, "plans only on, 500 welcome credits")
c = signup("Ana.Lopez+ads@gmail.com")
check(int(ws_of("ana.lopez+ads@gmail.com")["credits"]) == 0, "nothing before the email is confirmed")
check(confirm("ana.lopez+ads@gmail.com") == "ok", "the code confirms the email")
ws = ws_of("ana.lopez+ads@gmail.com")
check(int(ws["credits"]) == 500, "500 credits once it is confirmed")
check(row("SELECT COUNT(*) n FROM ledger WHERE workspace_id=? AND reason=?", ws["id"], plan_gate.WELCOME_REASON)["n"] == 1,
      "one ledger row, 'welcome credits'")
with db.connect() as conn:
    check(plan_gate.grant_welcome(conn, ws_of("ana.lopez+ads@gmail.com"), user_of("ana.lopez+ads@gmail.com")) == 0,
          "never twice for the same workspace")

print("== while it holds some")
r = c.get("/app/studio")
check(r.status_code != 302 or "/app/plan" not in (r.headers.get("Location") or ""), "not sent to pick a plan")
with db.connect() as conn:
    check(not plan_gate.needs_plan(conn, ws, user_of("ana.lopez+ads@gmail.com")), "needs_plan is False")
    check(plan_gate.welcome_left(conn, ws) == 500, "welcome_left reads what is left")
r = c.get("/app/plan")
html = r.get_data(as_text=True)
check(r.status_code == 200 and "data-plan-welcome" in html and "Skip for now" in html,
      "the plan page can be looked at, says what's free and offers Skip for now")
r = c.get("/app/billing")
check("data-welcome-banner" in r.get_data(as_text=True), "app pages show the welcome banner")

print("== video waits for a plan, images go")
with appmod.app.test_request_context("/app/studio"):
    from flask import g
    import core
    g.ws, g.user = ws_of("ana.lopez+ads@gmail.com"), user_of("ana.lopez+ads@gmail.com")
    with db.connect() as conn:
        try:
            core.queue_job(conn, "ugc_video", None, "a talking video", {}, 1.0, 300, quiet=True)
            check(False, "a video is refused")
        except plan_gate.PlanRequired as e:
            check(e.description == plan_gate.VIDEO_MESSAGE, "a video is refused with the plan message")
        jid = core.queue_job(conn, "ad_image", None, "an image ad", {}, 0.1, 22, quiet=True)
        check(jid is not None, "an image ad goes through, paid from the welcome credits")
    check(int(ws_of("ana.lopez+ads@gmail.com")["credits"]) == 478, "500 - 22 left")

print("== out of credits")
run_sql("UPDATE workspaces SET credits=10 WHERE id=?", ws["id"])
with appmod.app.test_request_context("/app/studio"):
    from flask import g
    g.ws, g.user = ws_of("ana.lopez+ads@gmail.com"), user_of("ana.lopez+ads@gmail.com")
    with db.connect() as conn:
        try:
            core.queue_job(conn, "ad_image", None, "an image ad", {}, 0.1, 22, quiet=True)
            check(False, "short of credits: the plan step")
        except plan_gate.PlanRequired as e:
            check(e.description == plan_gate.SPENT_MESSAGE, "short of credits: the plan step, 'used your free credits'")
run_sql("UPDATE workspaces SET credits=0 WHERE id=?", ws["id"])
r = c.get("/app/studio")
check(r.status_code == 302 and "/app/plan" in r.headers["Location"], "at 0 credits every page sends to the plan step")
r = c.get("/app/plan")
check("Start 3-day trial" in r.get_data(as_text=True), "and the plan step offers the 3-day trial")

print("== once per person, never for a team member or a past trial")
signup("analopez@gmail.com")             # the same Gmail inbox: dots and +tags folded
check(confirm("analopez@gmail.com") == "ok", "a second account confirms")
check(int(ws_of("analopez@gmail.com")["credits"]) == 0, "the same person gets no second 500")
signup("ben@example.com")
run_sql("INSERT INTO plan_trials (workspace_id, email_norm, plan, subscription_id, credits, started_at) "
        "VALUES (999, 'ben@example.com', 'starter', 'sub_old', 0, ?)", db.now())
confirm("ben@example.com")
check(int(ws_of("ben@example.com")["credits"]) == 0, "a person who had a trial gets none")
signup("cara@example.com")
run_sql("UPDATE users SET role='member' WHERE email='cara@example.com'")
confirm("cara@example.com")
check(int(ws_of("cara@example.com")["credits"]) == 0, "only a workspace owner gets them")

print("== Google sign-ups are confirmed already")
import accounts  # noqa: E402
with appmod.app.test_request_context("/auth/google/callback"):
    from flask import request
    with db.connect() as conn:
        out = accounts.create_self_serve_account(conn, "dee@example.com", "Dee", "x", request, "10.9.9.9",
                                                 auth_provider="google", verified=True)
        conn.commit()
check(int(ws_of("dee@example.com")["credits"]) == 500, "a Google sign-up gets the 500 at once")

print("== the first ads, paid from the welcome credits")
os.environ["UI_V2"] = "1"
with appmod.app.test_request_context("/app/welcome/first"), db.connect() as conn:
    check(first_ads.on(), "the first ads step is on (new look)")
    dws, du = ws_of("dee@example.com"), user_of("dee@example.com")
    check(first_ads.open_for(conn, dws, du) and first_ads.paid(conn, dws, du),
          "offered on the welcome credits, paid from them")
    check(first_ads.left(conn, dws["id"], du) == {"image": 2, "talk": 0}, "two image ads, no talking video")
os.environ.pop("UI_V2")

print("== the upgrade box")
import plan_gate_routes  # noqa: E402
with appmod.app.test_request_context("/app/desk"):
    from flask import g
    g.ws, g.user, g.viewing = ws_of("dee@example.com"), user_of("dee@example.com"), None
    ctx = plan_gate_routes._upgrade_box()
check(ctx.get("upgrade_box") and not ctx["upgrade_box"]["auto"], "the box is on the page, closed before any ad")
run_sql("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)", ws_of("dee@example.com")["id"], user_of("dee@example.com")["id"], "ad_image", "x",
        "{}", "done", 22, db.now(), db.now())
with appmod.app.test_request_context("/app/desk"):
    from flask import g
    g.ws, g.user, g.viewing = ws_of("dee@example.com"), user_of("dee@example.com"), None
    ctx = plan_gate_routes._upgrade_box()
check(ctx["upgrade_box"]["auto"], "after the first ads it opens by itself on the desk")
check(ctx["upgrade_box"]["trials"] == {"starter": 3, "growth": 3}, "with the 3-day trial on Starter and Growth")
with appmod.app.test_request_context("/app/desk"):
    from flask import g
    g.ws, g.user, g.viewing = ws_of("dee@example.com"), user_of("dee@example.com"), None
    ctx = plan_gate_routes._upgrade_box()
check(not ctx["upgrade_box"]["auto"], "only once")
check(plan_gate.upgrade_instead(ws_of("dee@example.com")), "short of credits with no plan: upgrade, not a pack")

print("== the sign-up offer")
with appmod.app.test_request_context("/"):
    o = plan_gate.offer()
check(o["credits"] == 500 and not o["card"] and o["cta"] == "Start free", "the offer says 500 free credits, no card")
html = client().get("/signup").get_data(as_text=True)
check("500 free credits" in html and "No card" in html, "the sign-up page says so")

print("== WELCOME_CREDITS=0")
os.environ["WELCOME_CREDITS"] = "0"
signup("eve@example.com")
confirm("eve@example.com")
check(int(ws_of("eve@example.com")["credits"]) == 0, "no welcome credits")
check(plan_gate.offer()["card"] is True, "the offer is the trial again")
os.environ["WELCOME_CREDITS"] = "500"

print(f"\nall {passed} welcome-credit checks passed")
