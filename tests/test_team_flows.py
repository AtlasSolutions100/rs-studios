"""Team and small flows (roadmap M5.9, 03b_ux_copy.md UX-18 to UX-20): the workspace name on the Team page, the
invite link kept on the Team page (with Copy) until it's used, and Creator Pools hidden from Free accounts that
have no product yet.

    python tests/test_team_flows.py      (tests/test_app.py runs it too)
"""
import os
import re
import shutil
import sys
import tempfile
from html import unescape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_team_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_MARKUP="2.0",
                  RS_EMAIL_VERIFY="0", RS_NUDGE_LOOP="0", RS_SIGNUP_CONFIRM="0", CREATOR_POOLS="on",
                  POOLS_PARTNER_SECRET="test-partner-secret")
for k in ("RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def page(c, path):
    return c.get(path).get_data(as_text=True)


def text(html):
    html = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", " ", html)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def signup(email, **extra):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1", **extra})
    assert r.status_code in (302, 303), r.status_code
    return c


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                            (email,)).fetchone()


try:
    # ================================================================ UX-18 workspace name
    print("== UX-18: workspace name")
    o = signup("sam.rivera@example.com")
    t = page(o, "/app/team")
    check("<h1>Your <span class=\"gold\">team</span></h1>" in t and "sam.rivera's studio" not in text(t),
          "a new workspace's Team page says 'Your team', not the email's local part")
    check('<label for="ws-name">Workspace name</label>' in t and 'id="ws-name" name="name" maxlength="60" required value=""' in t,
          "the owner gets a Workspace name field, empty while the name is the sign-up default")
    r = o.post("/app/team/name", data={"csrf": csrf(o), "name": "   "}, follow_redirects=True)
    check("Type a name for the workspace." in r.get_data(as_text=True) and ws_of("sam.rivera@example.com")["name"] == "sam.rivera's studio",
          "a blank name is refused and nothing changes")
    check(o.post("/app/team/name", data={"name": "Hearth & Co"}).status_code == 400, "rename needs CSRF")
    r = o.post("/app/team/name", data={"csrf": csrf(o), "name": "  Hearth   &  Co  " + "x" * 80}, follow_redirects=True)
    name = ws_of("sam.rivera@example.com")["name"]
    check(name.startswith("Hearth & Co x") and len(name) == 60, "spaces collapse and the name is cut at 60 characters")
    o.post("/app/team/name", data={"csrf": csrf(o), "name": "Hearth & Co"})
    t = page(o, "/app/team")
    check("Who's in <span class=\"gold\">Hearth &amp; Co</span>" in t and 'value="Hearth &amp; Co"' in t,
          "after a rename the heading and the field show the new name (escaped)")

    # ================================================================ UX-19 invite link stays on the Team page
    print("== UX-19: the invite link stays until it's used")
    with db.connect() as conn:   # seats: Growth has room for invites
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (ws_of("sam.rivera@example.com")["id"],))
        conn.commit()
    r = o.post("/app/team/invite", data={"csrf": csrf(o), "note": "Alex"})
    with db.connect() as conn:
        code = conn.execute("SELECT code FROM invites WHERE note='Alex'").fetchone()[0]
    check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/team?made={code}#invites"),
          "making an invite goes back to the Team page, at the invites")
    t = page(o, r.headers["Location"])
    link = f"signup?invite={code}"
    check("Invite link made. It works once and stays under Open invites until someone uses it." in text(t)
          and f"Invite link (works once)" not in t, "the flash says where the link is, not the link itself")
    m = re.search(r'<li class="invite new">.*?</li>', t, re.S)
    check(m is not None and 'readonly' in m.group(0) and link in m.group(0) and 'data-copy="inv-1"' in m.group(0)
          and "<b>New.</b>" in m.group(0), "the new link is a read-only field with a Copy button, marked New")
    check('<label class="sr" for="inv-1">Invite link for Alex</label>' in t, "the field has a label")
    page(o, "/app/library")
    t = page(o, "/app/team")
    check(link in t and 'class="invite new"' not in t, "the link is still on the Team page after going elsewhere")
    m1 = signup("alex@example.com", invite=code)
    t = page(o, "/app/team")
    check(link not in t and "alex@example.com" in t, "once it's used the link leaves Open invites and Alex is a member")
    check("Who's in <span class=\"gold\">Hearth &amp; Co</span>" in page(m1, "/app/team")
          and 'id="ws-name"' not in page(m1, "/app/team"), "members see the name but no rename field")
    check(m1.post("/app/team/name", data={"csrf": csrf(m1), "name": "Mine now"}).status_code == 403
          and ws_of("sam.rivera@example.com")["name"] == "Hearth & Co", "members can't rename the workspace")
    # a member's own email local part doesn't count as a name either
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET name=? WHERE id=?", ("alex's studio", ws_of("sam.rivera@example.com")["id"]))
        conn.commit()
    check("<h1>Your <span class=\"gold\">team</span></h1>" in page(o, "/app/team"), "any member's default name reads as 'Your team'")

    # ================================================================ UX-20 Creator Pools for Free accounts
    print("== UX-20: Creator Pools hidden from Free accounts with no product")
    f = signup("fresh@example.com")
    home = page(f, "/app")
    check("/app/creator-pools" not in home, "a new Free account with no product has no Creator Pools in the nav")
    cp = page(f, "/app/creator-pools")
    check("Add a product first" in cp and 'id="cpform"' not in cp, "its Creator Pools page asks for a product, no form")
    r = f.post("/app/creator-pools", data={"csrf": csrf(f), "title": "x"}, follow_redirects=True)
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM creator_pools WHERE workspace_id=?", (ws_of("fresh@example.com")["id"],)).fetchone()[0]
    check(n == 0 and "Add a product first." in r.get_data(as_text=True), "and a POST is refused (no draft, no checkout)")
    f.post("/app/brand", data={"csrf": csrf(f), "brand_name": "Vera Co", "product_name": "Vera Tea", "claims": "", "audience": ""})
    with db.connect() as conn:
        has = conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=? AND archived=0",
                           (ws_of("fresh@example.com")["id"],)).fetchone()[0]
    check(has >= 1, "the Free account adds a product")
    check("/app/creator-pools" in page(f, "/app") and 'id="cpform"' in page(f, "/app/creator-pools"),
          "with a product, Creator Pools is back in the nav and its form opens")
    p = signup("paid@example.com")
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override='starter' WHERE id=?", (ws_of("paid@example.com")["id"],))
        conn.commit()
    check("/app/creator-pools" in page(p, "/app"), "a paid plan sees Creator Pools even before a product")

    print(f"\nall {passed} checks passed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
