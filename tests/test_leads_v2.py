"""The leads screen in the new look (web/v2/leads.html, forms_routes._opened_lead): the board with the opened lead
beside it.

  * new look off: the board members have today, a select and Move on every card, no side panel
  * new look on: cards are links that open a lead; the newest lead of the first stage with anyone in it is open by
    default; ?lead= opens another; the opened card and its stage are marked
  * another workspace's lead can't be opened
  * the panel moves the lead and keeps it open; it sends an email through the usual path and comes back to the lead
  * a channel that can't be used says why instead of offering a form; an empty workspace has no panel

    python tests/test_leads_v2.py      (tests/test_app.py runs it too)
"""
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_leadsv2_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


mailer.configured = lambda: True
mailer.send = lambda **kw: "re_1"
mailer.hold_until = lambda: None
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan='growth', plan_status='active', postal_address='1 Main St, Austin, TX 78701', "
                     "messaging_accepted_at=?, sender_name='Ember Candle Co.' WHERE id=?", (db.now(), u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        db.adjust_credits(conn, u["workspace_id"], 500, "test top-up")
        conn.commit()
    return c, u["workspace_id"]


def add(ws, name, stage, consent="form", seen="2026-11-10T10:00:00+00:00"):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO contacts (workspace_id, email, name, stage, source, email_consent, first_seen, last_seen) "
                           "VALUES (?,?,?,?,?,?,?,?)", (ws, f"{name.split()[0].lower()}@example.com", name, stage, "Holiday page",
                                                        consent, seen, seen)).lastrowid
        conn.commit()
    return cid


a, wa = signup("a@example.com")
dana = add(wa, "Dana Whitfield", "lead", seen="2026-11-10T12:00:00+00:00")
marcus = add(wa, "Marcus Lee", "lead", seen="2026-11-09T12:00:00+00:00")
ben = add(wa, "Ben Schultz", "qualified")
nolist = add(wa, "Nora Quiet", "engaged", consent="none")
b, wb = signup("b@example.com")
theirs = add(wb, "Other Person", "lead")

print("== new look off")
old = a.get("/app/leads").get_data(as_text=True)
check('id="lead"' not in old and 'class="pipe-card"' in old and old.count(">Move</button>") == 4, "the board members have today: Move on every card, no side panel")

print("== new look on")
os.environ["UI_V2"] = "1"
page = a.get("/app/leads").get_data(as_text=True)
check('id="lead"' in page and page.count('class="ld-card"') == 4 and ">Move</button>" not in page, "cards are links and the side panel is there")
panel = page[page.index('id="lead"'):]
check("<h2>Dana Whitfield</h2>" in panel, "the newest lead of the first stage with anyone in it is open")
check(re.search(rf'<a class="ld-card" href="/app/leads\?lead={dana}#lead" aria-current=true>', page) is not None
      and len(re.findall(r'<a class="ld-card" [^>]*aria-current=true>', page)) == 1, "its card, and only its card, is marked as the open one")
check(re.search(r'<section class="ld-col here"[^>]*aria-labelledby="pc-lead"', page) is not None, "and so is its stage")
check(re.search(r'<button class="choice on" name="stage" value="lead" aria-current=true disabled>', panel) is not None,
      "the stage it is at is the pressed one")
page = a.get(f"/app/leads?lead={ben}").get_data(as_text=True)
panel = page[page.index('id="lead"'):]
check("<h2>Ben Schultz</h2>" in panel and re.search(r'<section class="ld-col here"[^>]*aria-labelledby="pc-qualified"', page) is not None,
      "?lead= opens another lead")
page = a.get(f"/app/leads?lead={theirs}").get_data(as_text=True)
check("Other Person" not in page and "<h2>Dana Whitfield</h2>" in page, "another workspace's lead can't be opened")

print("== from the panel")
r = a.post(f"/app/contacts/{ben}/stage", data={"csrf": csrf(a), "stage": "customer", "back": "board", "open": "1"})
check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/leads?lead={ben}"), "moving a lead comes back to the board with it still open")
page = a.get(f"/app/leads?lead={ben}").get_data(as_text=True)
panel = page[page.index('id="lead"'):]
check("Qualified to Won" in panel and re.search(r'class="ld-col here"[^>]*aria-labelledby="pc-customer"', page) is not None,
      "the move is on its timeline and its stage is marked")
check("Send email · 1 credit" in panel and 'name="back" value="board"' in panel, "the email form shows its price and returns to the board")
r = a.post(f"/app/contacts/{ben}/message", data={"csrf": csrf(a), "channel": "email", "subject": "Your order", "body": "Hi Ben, it is on its way.", "back": "board"})
check(r.status_code == 302 and r.headers["Location"].endswith(f"/app/leads?lead={ben}#lead"), "sending comes back to the opened lead")
with db.connect() as conn:
    m = conn.execute("SELECT * FROM messages WHERE contact_id=?", (ben,)).fetchone()
check(m is not None and m["flow_id"] is None and m["subject"] == "Your order", "the email is queued through the usual path")
panel = a.get(f"/app/leads?lead={ben}").get_data(as_text=True)
panel = panel[panel.index('id="lead"'):]
check("Your order, written by you" in panel, "and it shows on the timeline in the panel")
page = a.get(f"/app/leads?lead={nolist}").get_data(as_text=True)
panel = page[page.index('id="lead"'):]
check("Send email" not in panel and 'name="channel" value="email"' not in panel and "no email consent on record" in panel,
      "a lead who can't be emailed gets the reason, not a form")
r = a.post(f"/app/contacts/{theirs}/stage", data={"csrf": csrf(a), "stage": "customer", "back": "board", "open": "1"})
check(r.status_code == 404, "another workspace's lead can't be moved from the panel either")

c, wc = signup("c@example.com")
empty = c.get("/app/leads").get_data(as_text=True)
check('id="lead"' not in empty and "Nobody here yet." in empty and 'class="ld "' in empty, "an empty workspace has the board and no panel")
os.environ.pop("UI_V2")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
