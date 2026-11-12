"""Site admins (site_admins.py, the admin page's Site admins card), on a throwaway database with email faked:

  * only a site admin reaches it: a member gets 403, a visitor the login page, a POST with no CSRF token nothing
  * add: the whole email of an existing, confirmed account, with the box ticked; anything less changes nothing and
    says why (no box, no such account, not confirmed, already an admin, part of an address)
  * the new admin opens the admin page and can preview the new look; nobody else's access changes
  * remove: another admin's access, never your own; then the admin page is closed to them again
  * never silent: every site admin and the person concerned get one email, and the log line carries ids, no address
  * a few changes an hour for each admin

    python tests/test_site_admins.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import logging
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_admins_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test",
                  RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "PLANS_ONLY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY", "AD_LAUNCH", "RS_OWNER_INVITES", "RS_FIRST_OWNER_EMAIL"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402
import site_admins  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


sent = []
mailer.configured = lambda: True
mailer.send = lambda **kw: sent.append(kw) or f"em_{len(sent)}"
logged = []


class Keep(logging.Handler):
    def emit(self, record):
        logged.append(record.getMessage())


logging.getLogger("campaignify.site_admins").addHandler(Keep())
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.14.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def member(email, name, confirmed=True):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"}).status_code == 302
    with db.connect() as conn:
        conn.execute("UPDATE users SET email_verified_at=?, name=? WHERE email=?", (db.now() if confirmed else None, name, email))
        conn.commit()
    return c


def admins():
    with db.connect() as conn:
        return [r["email"] for r in site_admins.listed(conn)]


def change(c, **data):
    r = c.post("/admin/admins", data=dict(data, csrf=csrf(c)), follow_redirects=True)
    return r.status_code, htmllib.unescape(r.get_data(as_text=True))


print("== who reaches it")
koby = member("kay@example.com", "Kay Owner")
travis = member("tee@example.com", "Tee Partner")
other = member("mo@example.com", "Mo Member")
late = member("late@example.com", "Late Signup", confirmed=False)
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1 WHERE email='kay@example.com'")
    conn.commit()
check(travis.get("/admin").status_code == 403 and travis.post(
    "/admin/admins", data={"csrf": csrf(travis), "action": "add", "email": "tee@example.com", "sure": "1"}).status_code == 403
    and admins() == ["kay@example.com"], "a member can't open the admin page or make themself an admin")
r = appmod.app.test_client().post("/admin/admins", data={"action": "add", "email": "tee@example.com", "sure": "1"})
check(r.status_code in (302, 400, 403) and admins() == ["kay@example.com"], "a visitor changes nothing")
r = koby.post("/admin/admins", data={"action": "add", "email": "tee@example.com", "sure": "1"})
check(r.status_code in (400, 403) and admins() == ["kay@example.com"], "an admin's POST with no CSRF token changes nothing")
body = htmllib.unescape(koby.get("/admin").get_data(as_text=True))
card = body[body.index('id="admins"'):body.index("<h2>Invites</h2>")]
check("Site admins" in card and "kay@example.com" in card and ">You<" in card and "Remove" not in card,
      "the card lists the site admins; your own row has no Remove")
check("every workspace, its credits and plan, and the prices" in card and 'name="sure" value="1" required' in card,
      "it says what a site admin can do, and asks for a tick")

print("== add")
code, page = change(koby, action="add", email="tee@example.com")
check(admins() == ["kay@example.com"] and "Tick the box to confirm." in page, "no tick: nothing changes, and it says why")
code, page = change(koby, action="add", email="nobody@example.com", sure="1")
check(admins() == ["kay@example.com"] and "No account has that email." in page, "no such account: nothing changes")
code, page = change(koby, action="add", email="tee@", sure="1")
check(admins() == ["kay@example.com"], "part of an address matches nobody")
code, page = change(koby, action="add", email="%@example.com", sure="1")
check(admins() == ["kay@example.com"], "and a wildcard is only text")
code, page = change(koby, action="add", email="late@example.com", sure="1")
check(admins() == ["kay@example.com"] and "has not confirmed that email yet" in page,
      "an account whose email was never confirmed is refused")
check(not sent and not logged, "nothing was emailed or logged for a refusal")
code, page = change(koby, action="add", email="  Tee@Example.com ", sure="1")
check(admins() == ["kay@example.com", "tee@example.com"] and "tee@example.com is now a site admin." in page
      and "?ui=v2" in page, "the whole address, capitals or not: that account is a site admin, and the page says how to see the new look")
check(sorted(m["to"] for m in sent) == ["kay@example.com", "tee@example.com"], "one email to each site admin, the new one included")
m = sent[0]
check(m["subject"] == "Site admin added on Campaignify" and "tee@example.com is now a site admin on Campaignify." in m["text"]
      and "Changed by kay@example.com." in m["text"] and "?ui=v2" in m["text"] and m["tags"] == {"kind": "site_admin_change"},
      "it says who was added, who did it, and how to see the new look")
ids = (user("tee@example.com")["id"], user("kay@example.com")["id"])
check(logged == [f"site admin added: user {ids[0]} by user {ids[1]}"] and "@" not in logged[0],
      "the log line carries the two user ids and no address")
check(not user("mo@example.com")["is_admin"] and not user("late@example.com")["is_admin"], "nobody else's access changed")
code, page = change(koby, action="add", email="tee@example.com", sure="1")
check("tee@example.com is already a site admin." in page and len(sent) == 2, "adding them again says so and emails nobody")

print("== the new admin")
check(travis.get("/admin").status_code == 200, "opens the admin page")
r = travis.get("/app/library?ui=v2")
check('data-ui="v2"' in travis.get("/app/library").get_data(as_text=True), "and can preview the new look with ?ui=v2")
check('data-ui="v2"' not in other.get("/app/library?ui=v2").get_data(as_text=True)
      and 'data-ui="v2"' not in other.get("/app/library").get_data(as_text=True), "a member still can't")

print("== remove")
code, page = change(koby, action="remove", user=str(user("kay@example.com")["id"]))
check(admins() == ["kay@example.com", "tee@example.com"] and "You can't remove your own admin access." in page,
      "nobody removes their own access")
code, page = change(koby, action="remove", user=str(user("mo@example.com")["id"]))
check(admins() == ["kay@example.com", "tee@example.com"] and len(sent) == 2, "removing someone who isn't an admin does nothing")
before = len(sent)
code, page = change(travis, action="remove", user=str(user("kay@example.com")["id"]))
check(admins() == ["tee@example.com"] and "kay@example.com is no longer a site admin." in page, "one admin removes another")
check(sorted(m["to"] for m in sent[before:]) == ["kay@example.com", "tee@example.com"]
      and sent[-1]["subject"] == "Site admin removed on Campaignify", "and both are told")
check(koby.get("/admin").status_code == 403, "the admin page is closed to them again")
code, page = change(travis, action="add", email="kay@example.com", sure="1")
check(admins() == ["kay@example.com", "tee@example.com"], "and they can be added back")
code, page = change(koby, action="nonsense")
check(admins() == ["kay@example.com", "tee@example.com"], "an unknown action does nothing")

print("== a few an hour")
with db.connect() as conn:
    conn.execute("DELETE FROM throttle")
    conn.commit()
for i in range(site_admins.PER_HOUR // 2):
    change(koby, action="add", email="mo@example.com", sure="1")
    change(koby, action="remove", user=str(user("mo@example.com")["id"]))
check(not user("mo@example.com")["is_admin"], f"{site_admins.PER_HOUR} changes went through")
code, page = change(koby, action="add", email="mo@example.com", sure="1")
check(not user("mo@example.com")["is_admin"] and "a lot of admin changes in an hour" in page, "the next one waits")
code, page = change(travis, action="add", email="mo@example.com", sure="1")
check(user("mo@example.com")["is_admin"] == 1, "(the limit is each admin's own)")

print("== email off")
mailer.configured = lambda: False
n = len(sent)
code, page = change(travis, action="remove", user=str(user("mo@example.com")["id"]))
check(not user("mo@example.com")["is_admin"] and len(sent) == n, "with email off the change still happens, and nothing is sent")
src = open(os.path.join(ROOT, "site_admins.py")).read() + open(os.path.join(ROOT, "web", "admin.html")).read()
check(not re.search(r"[\w.+-]+@(?!example\.)[\w-]+\.(com|ai|io|net)", src), "no address is written in the source")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nsite admins: {passed} checks passed")
