"""Free-month codes (compcodes.py, /admin/codes) on a throwaway database.
Stripe is never called: every request to it fails the test.

    python tests/test_compcodes.py      (tests/test_app.py runs it too)
"""
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_codes_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")  # sign-ups here go straight in; test_login.py checks the emailed-link step
OWNER_CODE = "owner-code-codes"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0",
                  RS_OWNER_INVITES=f"koby@example.com:{hashlib.sha256(OWNER_CODE.encode()).hexdigest()}",
                  RS_EMAIL_VERIFY="0", STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")

import app as appmod  # noqa: E402
import billing  # noqa: E402
import compcodes  # noqa: E402
import db  # noqa: E402
import plans  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


stripe_calls = []


def _no_stripe(*a, **kw):
    stripe_calls.append(a[:1])
    raise AssertionError("Stripe must not be called for a free-month code")


billing.requests.post = _no_stripe
billing.requests.get = _no_stripe
appmod._maybe_reconcile = lambda ws_id, force=False: 0   # the plan sync reads Stripe; not under test here

_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.0.{_ip[0]}"
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


def code_row(code):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM comp_codes WHERE code=?", (code,)).fetchone()


def make(admin, **kw):
    data = {"csrf": csrf(admin), "plan": "growth", "months": "1", "labels": "", "count": "", "redeem_by": ""}
    data.update(kw)
    r = admin.post("/admin/codes", data=data)
    batch = r.headers.get("Location", "").split("batch=")[-1]
    with db.connect() as conn:
        return r, conn.execute("SELECT * FROM comp_codes WHERE batch=? ORDER BY id", (batch,)).fetchall()


def shift(ws_id, **cols):
    """Move a workspace's comp dates (time travel for the end of the month)."""
    with db.connect() as conn:
        for k, v in cols.items():
            conn.execute(f"UPDATE workspaces SET {k}=? WHERE id=?", (v, ws_id))
        conn.commit()


PAST = "2020-01-01T00:00:00+00:00"

print("== codes")
for _ in range(200):
    c = compcodes.new_code()
    assert len(c) == 13 and c.startswith("CFY-") and c[8] == "-", c
    assert not set(c[4:].replace("-", "")) & set("01OIL"), c
check(True, "codes look like CFY-XXXX-XXXX with no 0/O/1/I/L")
check(compcodes.normalize(" cfy abcd-efgh ") == "CFY-ABCD-EFGH" and compcodes.normalize("abcdefgh") == "CFY-ABCD-EFGH"
      and compcodes.normalize("CFY-ABC") == "", "typed codes are normalised (case, spaces, dashes, the prefix optional)")
from datetime import datetime, timezone  # noqa: E402
check(compcodes.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1).day == 28
      and compcodes.add_months(datetime(2026, 11, 4, tzinfo=timezone.utc), 2).month == 1, "calendar months add up")

print("== the admin page")
admin, r = signup("koby@example.com", invite=OWNER_CODE)
check(r.status_code == 302, "owner signs up as an admin")
check(client().get("/admin/codes").status_code == 302, "logged out: /admin/codes redirects to log in")
member, _ = signup("member@example.com")
check(member.get("/admin/codes").status_code == 403, "a non-admin can't open the codes page")
check(member.post("/admin/codes", data={"csrf": csrf(member), "count": "3"}).status_code == 403,
      "a non-admin can't make codes")
check(member.get("/admin/codes.csv").status_code == 403, "a non-admin can't download codes")
check(admin.post("/admin/codes", data={"count": "2"}).status_code == 400, "making codes without CSRF is refused")
page = admin.get("/admin/codes").get_data(as_text=True)
check("Make codes" in page and "No codes yet" in page, "the admin sees the codes page")
check(b"/admin/codes" in admin.get("/admin").data, "/admin links to it")

r, batch = make(admin, labels="Acme Coffee · sam@acme.com\n\n  Bolt Bikes · jo@bolt.test  \nCedar Soap\n")
check(r.status_code == 302 and len(batch) == 3, "one code per non-empty label line")
check([b["label"] for b in batch] == ["Acme Coffee · sam@acme.com", "Bolt Bikes · jo@bolt.test", "Cedar Soap"]
      and len({b["code"] for b in batch}) == 3, "labels kept in order, codes all different")
b0 = batch[0]
check(b0["plan"] == "growth" and b0["months"] == 1 and not b0["redeemed_at"] and not b0["revoked"]
      and b0["redeem_by"][:10] == compcodes.iso(compcodes._now() + compcodes.timedelta(days=30))[:10],
      "defaults: Growth, 1 month, redeem by 30 days from now")
page = admin.get(r.headers["Location"]).get_data(as_text=True)
check("Just made (3)" in page and all(b["code"] in page for b in batch) and "Bolt Bikes" in page
      and "Download CSV" in page, "the new batch is shown with its labels and a CSV link")
csv_r = admin.get(f"/admin/codes.csv?batch={b0['batch']}")
body = csv_r.get_data(as_text=True)
check(csv_r.status_code == 200 and csv_r.mimetype == "text/csv" and "attachment" in csv_r.headers["Content-Disposition"]
      and body.splitlines()[0].startswith("code,label,plan,months,redeem_by,status")
      and len(body.strip().splitlines()) == 4 and b0["code"] in body and "open" in body, "CSV download of the batch")
r, counted = make(admin, count="2", months="2", plan="scale", redeem_by="2099-12-31")
check(len(counted) == 2 and all(c["label"] is None and c["months"] == 2 and c["plan"] == "scale"
                                and c["redeem_by"] == "2099-12-31T23:59:59+00:00" for c in counted),
      "a count makes unlabelled codes; plan, months and redeem-by are kept")
for bad, why in [({"count": "0"}, "nothing to make"), ({"count": "201"}, "too many"), ({"months": "0", "count": "1"}, "0 months"),
                 ({"months": "13", "count": "1"}, "13 months"), ({"plan": "free", "count": "1"}, "free plan"),
                 ({"redeem_by": "2001-01-01", "count": "1"}, "a past date"), ({"redeem_by": "soon", "count": "1"}, "not a date")]:
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM comp_codes").fetchone()[0]
    r, _ = make(admin, **bad)
    with db.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM comp_codes").fetchone()[0]
    check(r.status_code == 200 and before == after, f"refused: {why}")

print("== redeeming on Billing")
check(not plans.has(ws_of("member@example.com"), "seo"), "a free workspace has no SEO optimizer")
check(member.get("/app/seo").status_code == 402, "the SEO optimizer is locked on Free")
page = member.get("/app/billing").get_data(as_text=True)
check("Have a code?" in page and "CFY-XXXX-XXXX" in page, "Billing has a Have a code? box")
r = member.post("/app/billing/code", data={"csrf": csrf(member), "code": "CFY-ZZZZ-ZZZZ"})
check(r.status_code == 302 and "#code" in r.headers["Location"], "an unknown code goes back to the box")
check("know that code" in member.get("/app/billing").get_data(as_text=True), "the unknown code message is shown")
check(member.post("/app/billing/code", data={"code": b0["code"]}).status_code == 400, "redeeming without CSRF is refused")
ws_before = ws_of("member@example.com")
pack_before = ws_before["credits"]
r = member.post("/app/billing/code", data={"csrf": csrf(member), "code": b0["code"].lower().replace("-", " ")})
check(r.status_code == 302 and r.headers["Location"].endswith("/app/billing"), "a good code is applied (typed loosely)")
ws = ws_of("member@example.com")
check(plans.plan_code(ws) == "growth" and plans.has(ws, "seo") and plans.has(ws, "optimizer")
      and not plans.has(ws, "community"), "the workspace gets Growth's features (and not Scale's)")
check(member.get("/app/seo").status_code == 200, "the SEO optimizer opens")
check(ws["plan_credits"] == 6000 and ws["credits"] == pack_before, "6,000 plan credits land; pack credits untouched")
check(not ws["stripe_customer_id"] and not ws["stripe_subscription_id"] and ws["plan"] == "free"
      and not ws["plan_status"] and not ws["plan_override"], "no Stripe customer, subscription or admin comp")
check(not stripe_calls, "Stripe was never called")
row = code_row(b0["code"])
check(row["redeemed_at"] and row["workspace_id"] == ws["id"] and row["redeemed_by"] and row["comp_until"] == ws["comp_until"],
      "the redemption is recorded on the code (when, workspace, user, until)")
with db.connect() as conn:
    led = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC", (ws["id"],)).fetchone()
check(led["delta"] == 6000 and led["plan_delta"] == 6000 and b0["code"] in led["reason"], "and in the credits ledger")
until_text = compcodes.free_until_text(ws["comp_until"])
page = member.get("/app/billing").get_data(as_text=True)
check(f"Growth, free until {until_text}." in page and "No card on file, won't renew." in page,
      "Billing says: Growth, free until {date}. No card on file, won't renew.")
check("Have a code?" not in page and "Get Scale" in page, "the code box goes; the normal upgrade buttons stay")
check(compcodes.applied_message(row).startswith(f"Code applied. You're on Growth, free until {until_text}"),
      "the success message names the plan and the date")

print("== single use, one per workspace")
other, _ = signup("other@example.com")
r = other.post("/app/billing/code", data={"csrf": csrf(other), "code": b0["code"]})
check(not plans.has(ws_of("other@example.com"), "seo"), "a used code doesn't work on another workspace")
with db.connect() as conn:
    try:
        compcodes.redeem(conn, ws_of("other@example.com")["id"], None, b0["code"])
        raise SystemExit("FAIL: used code redeemed twice")
    except compcodes.Refused as e:
        check("already been used" in str(e), "used code: 'That code has already been used.'")
    try:
        compcodes.redeem(conn, ws["id"], None, batch[1]["code"])
        raise SystemExit("FAIL: second code on one workspace")
    except compcodes.Refused as e:
        check("one code per workspace" in str(e) or "already on Growth" in str(e),
              "a second code on the same workspace is refused")
check(not code_row(batch[1]["code"])["redeemed_at"], "and that second code stays unused")

print("== expiry and revoke")
with db.connect() as conn:
    conn.execute("UPDATE comp_codes SET redeem_by=? WHERE id=?", (PAST, batch[1]["id"]))
    conn.commit()
    try:
        compcodes.redeem(conn, ws_of("other@example.com")["id"], None, batch[1]["code"])
        raise SystemExit("FAIL: expired code worked")
    except compcodes.Refused as e:
        check("expired on 2020-01-01" in str(e), "an expired code is refused with its date")
check(compcodes.status(code_row(batch[1]["code"])) == "expired", "and shows as expired")
r = admin.post(f"/admin/codes/{batch[2]['id']}/revoke", data={"csrf": csrf(admin)})
check(r.status_code == 302 and code_row(batch[2]["code"])["revoked"] == 1, "the admin revokes an unused code")
check(member.post(f"/admin/codes/{batch[2]['id']}/revoke", data={"csrf": csrf(member)}).status_code == 403,
      "a non-admin can't revoke")
r = other.post("/app/billing/code", data={"csrf": csrf(other), "code": batch[2]["code"]})
check(not plans.has(ws_of("other@example.com"), "seo") and not code_row(batch[2]["code"])["redeemed_at"],
      "a revoked code doesn't work")
admin.post(f"/admin/codes/{b0['id']}/revoke", data={"csrf": csrf(admin)})
check(not code_row(b0["code"])["revoked"] and plans.has(ws_of("member@example.com"), "seo"),
      "a redeemed code can't be revoked (its month carries on)")
page = admin.get("/admin/codes").get_data(as_text=True)
check("redeemed" in page and "revoked" in page and "expired" in page and "member@example.com" in page,
      "the list shows each code's status and who used it")

print("== the end of the free month")
shift(ws["id"], comp_until=PAST)
check(plans.plan_code(ws_of("member@example.com")) == "free", "past comp_until the plan is Free at once")
member.get("/app/billing")          # any page settles it
ws = ws_of("member@example.com")
check(ws["comp_plan"] is None and ws["comp_until"] is None and ws["plan_credits"] == 0 and ws["credits"] == pack_before,
      "plan credits go, pack credits stay, the comp is cleared")
check(member.get("/app/seo").status_code == 402, "the SEO optimizer is locked again")
check(not stripe_calls, "still no Stripe: nothing charged, nothing renewed")
with db.connect() as conn:
    led = conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC", (ws["id"],)).fetchone()
check(led["delta"] == -6000 and "ended" in led["reason"], "the ledger says the free month ended")
page = member.get("/app/billing").get_data(as_text=True)
check("free until" not in page and "Have a code?" not in page, "billing is back to Free; no second code box")

print("== more than one month")
multi, _ = signup("multi@example.com")
r = multi.post("/app/billing/code", data={"csrf": csrf(multi), "code": counted[0]["code"]})
mw = ws_of("multi@example.com")
check(plans.plan_code(mw) == "scale" and mw["plan_credits"] == 18000 and mw["comp_next_grant"]
      and mw["comp_next_grant"] < mw["comp_until"], "a 2-month Scale code: first month's credits, next grant set")
with db.connect() as conn:
    db.adjust_credits(conn, mw["id"], -5000, "spent")
    conn.commit()
shift(mw["id"], comp_next_grant=PAST)
with db.connect() as conn:
    check(compcodes.settle_all(conn) == 1, "the hourly sweep finds it")
mw = ws_of("multi@example.com")
check(mw["plan_credits"] == 18000 and (mw["comp_next_grant"] is None or mw["comp_next_grant"] > db.now())
      and plans.plan_code(mw) == "scale",
      "month 2: the credits are replaced with a fresh 18,000; still on Scale")
shift(mw["id"], comp_until=PAST)
with db.connect() as conn:
    compcodes.settle_all(conn)
mw = ws_of("multi@example.com")
check(plans.plan_code(mw) == "free" and mw["plan_credits"] == 0 and mw["comp_plan"] is None, "then back to Free")

print("== paid subscriptions")
paid, _ = signup("paid@example.com")
pw = ws_of("paid@example.com")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan='starter', plan_status='active', stripe_customer_id='cus_1', "
                 "stripe_subscription_id='sub_1', plan_credits=2000 WHERE id=?", (pw["id"],))
    conn.commit()
r, fresh = make(admin, count="2")
r = paid.post("/app/billing/code", data={"csrf": csrf(paid), "code": fresh[0]["code"]})
paid.get("/app/billing")
pw = ws_of("paid@example.com")
check(plans.plan_code(pw) == "starter" and pw["plan_credits"] == 2000 and not code_row(fresh[0]["code"])["redeemed_at"],
      "a workspace with a paid subscription keeps its plan; the code stays unused")
with db.connect() as conn:
    try:
        compcodes.redeem(conn, pw["id"], None, fresh[0]["code"])
        raise SystemExit("FAIL: paid sub redeemed")
    except compcodes.Refused as e:
        check("already pays for a plan" in str(e), "with a clear message: already pays for a plan")
check("Have a code?" not in paid.get("/app/billing").get_data(as_text=True), "and paid workspaces don't see the box")

print("== at sign-up")
c, r = signup("new@example.com", comp_code=fresh[0]["code"])
nw = ws_of("new@example.com")
check(r.status_code == 302 and plans.plan_code(nw) == "growth" and nw["plan_credits"] == 6000
      and not nw["stripe_customer_id"], "a code at sign-up: the new account starts on Growth, no card")
check(code_row(fresh[0]["code"])["workspace_id"] == nw["id"], "the code is used by that workspace")
c, r = signup("again@example.com", comp_code=fresh[0]["code"])
with db.connect() as conn:
    made = conn.execute("SELECT 1 FROM users WHERE email='again@example.com'").fetchone()
check(r.status_code == 200 and not made and "already been used" in r.get_data(as_text=True).replace("&#39;", "'"),
      "a used code at sign-up refuses the sign-up with the reason (no account made)")
check(b'name="comp_code"' in client().get("/signup").data, "the sign-up form has the code field")
check(fresh[1]["code"].encode() in client().get(f"/signup?code={fresh[1]['code']}").data, "/signup?code= prefills it")

print(f"\nall {passed} checks passed ({TMP})")
