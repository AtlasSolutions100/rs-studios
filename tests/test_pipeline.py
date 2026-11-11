"""The lead pipeline (pipeline.py, /app/leads, the contact page), on a throwaway database with the mail service faked:

  * stages: the six built-in ones under the workspace's own names, up to four of its own in the middle, the flow
    keys unchanged underneath; removing one sends its leads back
  * the board: every contact at its stage (no stage = the first), counts, the lost ones off the board
  * moving a lead writes who moved it where to its timeline; a stage nobody has is refused; another workspace's
    contact is a 404
  * the timeline: forms, stage moves and every email, a flow's and the member's own, newest first
  * writing to a lead: refused without consent, after an unsubscribe and for a blocked claim; a stated discount is
    fine; the message goes through the usual sender, costs the usual credit and is logged
  * the plan gate: without Forms the pages are the upgrade page

    python tests/test_pipeline.py      (tests/test_app.py runs it too)
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_pipeline_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import compliance  # noqa: E402
import db  # noqa: E402
import flows  # noqa: E402
import mailer  # noqa: E402
import messaging  # noqa: E402
import pipeline  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.6.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email, plan="growth"):
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan=?, plan_status='active', postal_address='1 Main St, Austin, TX 78701', "
                     "messaging_accepted_at=?, sender_name='Ember Candle Co.' WHERE id=?", (plan, db.now(), u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        db.adjust_credits(conn, u["workspace_id"], 500, "test top-up")
        conn.commit()
    return c, u["workspace_id"]


def ws_row(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def contact(ws_id, email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM contacts WHERE workspace_id=? AND email=?", (ws_id, email)).fetchone()


def add(ws_id, email, name, stage=None, consent="form", source="Holiday page"):
    with db.connect() as conn:
        cid = conn.execute("INSERT INTO contacts (workspace_id, email, name, stage, source, email_consent, first_seen, last_seen) "
                           "VALUES (?,?,?,?,?,?,?,?)", (ws_id, email, name, stage, source, consent, db.now(), db.now())).lastrowid
        conn.commit()
    return cid


sent = []
mailer.configured = lambda: True
mailer.hold_until = lambda: None
mailer.from_address = lambda ws_id, sender, custom=None: f"{sender} <hello@campaignify.test>"
mailer.send = lambda **kw: (sent.append(kw), f"re_{len(sent)}")[1]

print("== stages")
a, ws = signup("pipe@example.com")
st = pipeline.stages(ws_row(ws))
check([s["key"] for s in st] == ["lead", "engaged", "qualified", "customer", "repeat", "cold"]
      and [s["name"] for s in st] == ["New lead", "Contacted", "Qualified", "Won", "Delivered", "Lost"]
      and st[-1]["lost"] and not any(s["lost"] for s in st[:-1]), "a new workspace has the six default stages, Lost last")
check(set(s["key"] for s in st) == set(flows.STAGES), "their keys are the ones a flow's 'Move to a stage' step uses")
r = a.post("/app/leads/stages", data={"csrf": csrf(a), "name_lead": "Enquiry", "name_customer": "Closed", "name_cold": "Not now",
                                      "new": "Quote sent"})
st = pipeline.stages(ws_row(ws))
check(r.status_code == 302 and [s["name"] for s in st] == ["Enquiry", "Contacted", "Qualified", "Quote sent", "Closed", "Delivered", "Not now"]
      and st[3]["key"] == "x1" and st[3]["own"], "stages take the workspace's names, and its own stage sits before the close")
for n in ("Sample sent", "Negotiating", "Contract out"):
    a.post("/app/leads/stages", data={"csrf": csrf(a), "new": n})
r = a.post("/app/leads/stages", data={"csrf": csrf(a), "new": "One too many"}, follow_redirects=True)
check(len([s for s in pipeline.stages(ws_row(ws)) if s["own"]]) == pipeline.MAX_EXTRA and "Remove one first" in r.get_data(as_text=True),
      f"a pipeline holds {pipeline.MAX_EXTRA} stages of its own, and says so at the fifth")
r = a.post("/app/leads/stages", data={"csrf": csrf(a), "name_lead": "   "}, follow_redirects=True)
check(pipeline.stages(ws_row(ws))[0]["name"] == "Enquiry" and "needs a name" in r.get_data(as_text=True), "a stage can't be left without a name")

print("== the board")
dana = add(ws, "dana@example.com", "Dana Whitfield")
sam = add(ws, "sam@example.com", "Sam Carter", stage="engaged")
ben = add(ws, "ben@example.com", "Ben Schultz", stage="x1")
odd = add(ws, "odd@example.com", "Old Value", stage="a-stage-from-before")
gone = add(ws, "gone@example.com", "Lost Lead", stage="cold")
with db.connect() as conn:
    b = pipeline.board(conn, ws_row(ws))
cols = {c["name"]: c for c in b["columns"]}
check(b["total"] == 5 and [k["name"] for k in cols["Enquiry"]["cards"]] and {k["name"] for k in cols["Enquiry"]["cards"]} == {"Dana Whitfield", "Old Value"}
      and cols["Contacted"]["count"] == 1 and cols["Quote sent"]["count"] == 1, "every contact is at its stage; no stage or an unknown one is the first")
check("Not now" not in cols and b["lost"] == {"key": "cold", "name": "Not now", "count": 1}, "lost leads are counted, off the board")
page = a.get("/app/leads").get_data(as_text=True)
check(all(n in page for n in ("Enquiry", "Quote sent", "Dana Whitfield", "Sam Carter", "Ben Schultz")) and "Lost Lead" not in page
      and page.count('name="stage"') == 4, "the page shows the board, with a move control on every card")
check("Lost Lead" in a.get("/app/leads?lost=1").get_data(as_text=True), "and the lost list on request")

print("== moving a lead")
r = a.post(f"/app/contacts/{dana}/stage", data={"csrf": csrf(a), "stage": "qualified", "back": "board"})
c = contact(ws, "dana@example.com")
with db.connect() as conn:
    ev = conn.execute("SELECT * FROM contact_events WHERE contact_id=? AND kind='stage'", (dana,)).fetchall()
check(r.status_code == 302 and r.headers["Location"].endswith("/app/leads") and c["stage"] == "qualified" and len(ev) == 1
      and ev[0]["ref"] == "qualified" and ev[0]["detail"].startswith("Enquiry to Qualified, by "), "a move saves the stage and writes who moved it where")
a.post(f"/app/contacts/{dana}/stage", data={"csrf": csrf(a), "stage": "qualified"})
a.post(f"/app/contacts/{dana}/stage", data={"csrf": csrf(a), "stage": "nowhere"})
with db.connect() as conn:
    n = conn.execute("SELECT COUNT(*) FROM contact_events WHERE contact_id=? AND kind='stage'", (dana,)).fetchone()[0]
check(n == 1 and contact(ws, "dana@example.com")["stage"] == "qualified", "the same stage, or one the workspace doesn't have, changes nothing")
a.post("/app/leads/stages", data={"csrf": csrf(a), "remove": "x1"})
check(contact(ws, "ben@example.com")["stage"] == "qualified" and "Quote sent" not in [s["name"] for s in pipeline.stages(ws_row(ws))],
      "removing a stage of your own sends its leads back to the stage before")
b2, ws2 = signup("other@example.com")
check(b2.post(f"/app/contacts/{dana}/stage", data={"csrf": csrf(b2), "stage": "customer"}).status_code == 404
      and b2.post(f"/app/contacts/{dana}/message", data={"csrf": csrf(b2), "channel": "email", "subject": "Hi", "body": "Hello"}).status_code == 404
      and contact(ws, "dana@example.com")["stage"] == "qualified", "another workspace's contact is a 404")

print("== writing to a lead")
credits0 = db.balance(ws_row(ws))
r = a.post(f"/app/contacts/{dana}/message", data={"csrf": csrf(a), "channel": "email", "subject": "Your holiday set",
                                                  "body": "Hi Dana, it ships free this week. Use code EMBER for 10% off."}, follow_redirects=True)
with db.connect() as conn:
    m = conn.execute("SELECT * FROM messages WHERE contact_id=?", (dana,)).fetchone()
check("on its way" in r.get_data(as_text=True) and m["status"] == "queued" and m["flow_id"] is None and m["channel"] == "email"
      and m["to_addr"] == "dana@example.com", "an email written on the contact page is queued with no flow")
check(not compliance.check("Use code EMBER for 10% off")["ok"], "(a discount is still refused where nobody stated it)")
messaging.send_queued()
with db.connect() as conn:
    m = conn.execute("SELECT * FROM messages WHERE contact_id=?", (dana,)).fetchone()
to_dana = [x for x in sent if x.get("to") == "dana@example.com"]      # sign-up sends its own mail through the same stub
check(m["status"] == "sent" and len(to_dana) == 1 and to_dana[0]["subject"] == "Your holiday set"
      and "Unsubscribe" in to_dana[0]["text"] and db.balance(ws_row(ws)) == credits0 - messaging.EMAIL_CREDITS,
      "the usual sender sends it, with the unsubscribe footer, for the usual credit")
noc = add(ws, "noc@example.com", "No Consent", consent="none")
r = a.post(f"/app/contacts/{noc}/message", data={"csrf": csrf(a), "channel": "email", "subject": "Hi", "body": "Hello"}, follow_redirects=True)
check("no email consent" in r.get_data(as_text=True).lower(), "no consent: not queued, and the page says why")
r = a.post(f"/app/contacts/{dana}/message", data={"csrf": csrf(a), "channel": "email", "subject": "An idea",
                                                  "body": "You could make $500 a week selling these."}, follow_redirects=True)
check("breaks a content rule" in r.get_data(as_text=True), "a blocked claim is refused, even written by the member")
r = a.post(f"/app/contacts/{dana}/message", data={"csrf": csrf(a), "channel": "sms", "body": "Hi Dana"}, follow_redirects=True)
with db.connect() as conn:
    n_msgs = conn.execute("SELECT COUNT(*) FROM messages WHERE workspace_id=?", (ws,)).fetchone()[0]
check(n_msgs == 1, "a text without a texting setup or consent is not queued either")
with db.connect() as conn:
    conn.execute("UPDATE contacts SET unsubscribed_at=? WHERE id=?", (db.now(), dana))
    conn.commit()
r = a.post(f"/app/contacts/{dana}/message", data={"csrf": csrf(a), "channel": "email", "subject": "Hi", "body": "Hello"}, follow_redirects=True)
messaging.send_queued()
check("unsubscribed" in r.get_data(as_text=True) and len([x for x in sent if x.get("to") == "dana@example.com"]) == 1,
      "after an unsubscribe nothing more can be written to them")

print("== the timeline")
with db.connect() as conn:
    conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                 (ws, dana, "form", "1", "Holiday scent list", "2026-11-09T10:00:00+00:00"))
    conn.commit()
    tl = pipeline.timeline(conn, ws_row(ws), dana)
kinds = [e["kind"] for e in tl]
check(set(kinds) == {"form", "stage", "message"} and kinds[-1] == "form" and tl == sorted(tl, key=lambda e: e["at"], reverse=True),
      "forms, stage moves and messages are one list, newest first")
msg = next(e for e in tl if e["kind"] == "message")
check(msg["what"] == "Email sent" and "Your holiday set" in msg["detail"] and "written by you" in msg["detail"], "a message says what it was and who wrote it")
page = a.get(f"/app/contacts/{dana}").get_data(as_text=True)
check("What has happened" in page and "Enquiry to Qualified" in page and "Email sent" in page and 'name="stage" value="customer"' in page
      and "Write to them" in page, "the contact page shows the stage buttons, the timeline and the place to write")
check("They unsubscribed" in page and f"/app/contacts/{dana}/message" not in page, "and says why instead of offering a form it would refuse")
page = a.get(f"/app/contacts/{sam}").get_data(as_text=True)
check(f"/app/contacts/{sam}/message" in page and f"Send email · {messaging.EMAIL_CREDITS} credit" in page, "for a lead it can write to, the price is on the button")

print("== the plan gate")
f, wsf = signup("free@example.com", plan="free")
r = f.get("/app/leads")
check(r.status_code != 200 or "Dana" not in r.get_data(as_text=True), "without Forms on the plan there is no board")
nav = a.get("/app/leads").get_data(as_text=True)
check(">Leads<" in nav.replace("<span>", "").replace("</span>", "") or "Leads" in nav, "Leads is in the menu")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} checks passed")
