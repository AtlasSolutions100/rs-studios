"""Admin outreach emails (/admin/outreach) on a throwaway database with the
email service stubbed: nothing leaves the machine.

    python tests/test_outreach.py      (tests/test_app.py runs it too)
"""
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_outreach_")
OWNER_CODE = "owner-code-outreach"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0",
                  RS_OWNER_INVITES=f"koby@example.com:{hashlib.sha256(OWNER_CODE.encode()).hexdigest()}",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai", RS_PUBLIC_URL="https://campaignify.test",
                  RS_EMAIL_VERIFY="0")
for k in ("RS_OUTREACH_FROM", "RS_POSTAL_ADDRESS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402
import outreach  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.7.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


sent = []
fail = {"on": False}


class _Resp:
    def __init__(self, ok=True):
        self.status_code = 200 if ok else 422
        self.text = "{}" if ok else '{"message":"bad address"}'

    def json(self):
        return {"id": f"re_out{len(sent)}"}


def _post(url, json=None, headers=None, timeout=0):
    if fail["on"]:
        return _Resp(False)
    sent.append({"url": url, "body": json, "headers": headers})
    return _Resp()


mailer.requests.post = _post

ADDR = "30 N Gould St Ste R, Sheridan, WY 82801"
ok_body = "Hi Sam,\n\nI made a free sample ad for Acme.\nIt's attached as a link: https://campaignify.ai/s/abc\n\nKoby"


def form(c, **kw):
    d = {"csrf": csrf(c), "to": "sam@acme.com", "brand": "Acme", "subject": "A free ad for Acme", "body": ok_body,
         "do": "send"}
    d.update(kw)
    return d


def log_count():
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM outreach_log").fetchone()[0]


print("== access")
a = client()
r = a.post("/signup", data={"csrf": csrf(a), "email": "koby@example.com", "password": "longpassword", "name": "Koby",
                            "invite": OWNER_CODE})
check(r.status_code == 302, "owner signs up as an admin")
m = client()
r = m.post("/signup", data={"csrf": csrf(m), "email": "member@example.com", "password": "longpassword"})
check(r.status_code == 302, "a self-serve account signs up")
sent.clear()
check(client().get("/admin/outreach").status_code == 302, "logged out: /admin/outreach sends you to log in")
check(m.get("/admin/outreach").status_code == 403, "a non-admin can't open outreach")
check(m.post("/admin/outreach", data=form(m)).status_code == 403 and not sent, "a non-admin can't send")
check(m.post("/admin/outreach/suppress", data={"csrf": csrf(m), "entry": "x@y.com"}).status_code == 403,
      "a non-admin can't edit the do-not-contact list")
r = a.get("/admin/outreach")
page = r.get_data(as_text=True)
check(r.status_code == 200 and "New email" in page and "Do not contact" in page and "Send test to me" in page,
      "the admin sees the outreach page")
check(b"/admin/outreach" in a.get("/admin").data, "/admin links to it")
check(a.post("/admin/outreach", data={k: v for k, v in form(a).items() if k != "csrf"}).status_code == 400 and not sent,
      "a send without CSRF is refused")
check(a.post("/admin/outreach/suppress", data={"entry": "x@y.com"}).status_code == 400, "suppress without CSRF refused")

print("== the email")
r = a.post("/admin/outreach", data=form(a))
check(r.status_code == 302 and len(sent) == 1, "a send goes out")
e = sent[-1]["body"]
check(e["from"] == '"Koby Stull" <hello@mail.campaignify.ai>' and e["reply_to"] == "hello@mail.campaignify.ai"
      and e["to"] == ["sam@acme.com"] and e["subject"] == "A free ad for Acme", "from Koby at hello@, replies to hello@")
check(e["text"].startswith(ok_body) and e["text"].count(ADDR) == 1 and e["text"].count("Reply 'no thanks'") == 1,
      "text: the body as typed, then the postal address and the opt-out line once")
check(e["html"].count("<p ") == 4 and "Acme.<br>It&#x27;s" in e["html"] and ADDR in e["html"]
      and "no thanks" in e["html"], "html: one paragraph per blank line, plus the footer")
check(sent[-1]["headers"].get("Idempotency-Key", "").startswith("outreach-"), "sent with an idempotency key")
with db.connect() as conn:
    row = conn.execute("SELECT * FROM outreach_log ORDER BY id DESC").fetchone()
check(row["status"] == "sent" and row["provider_id"] == "re_out1" and row["to_addr"] == "sam@acme.com"
      and row["brand"] == "Acme" and row["subject"] == "A free ad for Acme" and row["created_at"],
      "the send is logged with the provider's id")
page = a.get("/admin/outreach").get_data(as_text=True)
check("sam@acme.com" in page and "re_out1" in page and "1 of 20" in page, "the log and today's count show on the page")

text, html_ = outreach.compose("Hi <b>there</b> & co\n\nKoby\n\n--\nCampaignify · " + ADDR + "\n"
                               "Not interested? Reply 'no thanks' and I won't email again.")
check(text.count(ADDR) == 1 and text.count("no thanks") == 1 and "&lt;b&gt;there&lt;/b&gt; &amp; co" in html_
      and "<b>" not in html_, "footer not duplicated when the body already has it; HTML is escaped")
text, _ = outreach.compose("Hi\n\nCampaignify, " + ADDR.replace(" ", "  "))
check(text.count("Sheridan") == 1 and text.count("no thanks") == 1,
      "address already in the body (any spacing): only the opt-out line is added")
os.environ["RS_OUTREACH_FROM"] = '"Koby Stull" <hello@campaignify.ai>'
check(outreach.from_header() == '"Koby Stull" <hello@campaignify.ai>' and outreach.reply_to() == "hello@campaignify.ai",
      "RS_OUTREACH_FROM replaces the From; replies go to hello@ that domain")
os.environ.pop("RS_OUTREACH_FROM")

print("== placeholders")
n = len(sent)
r = a.post("/admin/outreach", data=form(a, to="lee@bolt.com", subject="Hi {first_name}"))
check(r.status_code == 200 and "{first_name}" in r.get_data(as_text=True) and len(sent) == n,
      "an unfilled {placeholder} in the subject is refused")
r = a.post("/admin/outreach", data=form(a, to="lee@bolt.com", body="Hi Lee,\n\nYour ad for { brand } is ready."))
check("{ brand }" in r.get_data(as_text=True) and len(sent) == n, "and in the body")
check('value="lee@bolt.com"' in r.get_data(as_text=True) and "Your ad for" in r.get_data(as_text=True),
      "a refused send keeps what you typed")
r = a.post("/admin/outreach", data=form(a, to="lee@bolt.com", body="Hi {name}", do="test"))
check(len(sent) == n, "a test send with a placeholder is refused too")
r = a.post("/admin/outreach", data=form(a, to="lee@bolt.com, max@bolt.com"))
check(len(sent) == n and "one email address" in r.get_data(as_text=True), "only one address in To")

print("== test send")
r = a.post("/admin/outreach", data=form(a, to="lee@bolt.com", do="test"))
check(len(sent) == n + 1 and sent[-1]["body"]["to"] == ["koby@example.com"] and "Test sent" in r.get_data(as_text=True),
      "Send test to me goes to the logged-in admin, not the brand")
check('value="lee@bolt.com"' in r.get_data(as_text=True), "after a test the form is still filled in")
check(ADDR in sent[-1]["body"]["text"], "the test has the footer too")
with db.connect() as conn:
    check(outreach.last_sent_to(conn, "lee@bolt.com") is None and outreach.sent_last_day(conn) == 1,
          "a test doesn't count toward the 14 days or the daily cap")
r = a.post("/admin/outreach", data=form(a, to="lee@bolt.com"))
check(r.status_code == 302 and sent[-1]["body"]["to"] == ["lee@bolt.com"], "then the real send to the brand goes out")

print("== do not contact")
r = a.post("/admin/outreach/suppress", data={"csrf": csrf(a), "entry": "  No@Thanks.com ", "note": "replied no thanks"})
r = a.post("/admin/outreach/suppress", data={"csrf": csrf(a), "entry": "https://www.blocked.co/about"})
r = a.post("/admin/outreach/suppress", data={"csrf": csrf(a), "entry": "not an address"}, follow_redirects=True)
check("Add an email address" in r.get_data(as_text=True), "a bad entry is refused")
with db.connect() as conn:
    entries = {s["entry"] for s in outreach.suppressions(conn)}
check(entries == {"no@thanks.com", "blocked.co"}, "addresses and domains are stored normalized")
check("no@thanks.com" in a.get("/admin/outreach").get_data(as_text=True), "the list shows on the page")
n, logged = len(sent), log_count()
r = a.post("/admin/outreach", data=form(a, to="NO@thanks.com"))
check(r.status_code == 200 and "do-not-contact" in r.get_data(as_text=True) and len(sent) == n,
      "a suppressed address is refused")
r = a.post("/admin/outreach", data=form(a, to="ceo@blocked.co"))
check("do-not-contact" in r.get_data(as_text=True) and len(sent) == n, "an address at a suppressed domain is refused")
r = a.post("/admin/outreach", data=form(a, to="ceo@shop.blocked.co", follow_up="1"))
check("do-not-contact" in r.get_data(as_text=True) and len(sent) == n, "a subdomain too, even as a follow-up")
check(log_count() == logged, "refused sends aren't logged as sends")
r = a.post("/admin/outreach", data=form(a, to="other@thanks.com"))
check(r.status_code == 302 and len(sent) == n + 1, "another address at a domain with one suppressed address still sends")

print("== 14 days between emails to one address")
n = len(sent)
r = a.post("/admin/outreach", data=form(a, to="Sam@Acme.com", subject="Following up"))
check(r.status_code == 200 and "14 days" in r.get_data(as_text=True) and len(sent) == n,
      "the same address again within 14 days is refused (case doesn't matter)")
r = a.post("/admin/outreach", data=form(a, to="sam@acme.com", subject="Following up", follow_up="1"))
check(r.status_code == 302 and len(sent) == n + 1, "unless Follow-up is ticked")
with db.connect() as conn:
    conn.execute("UPDATE outreach_log SET created_at=? WHERE to_addr='lee@bolt.com'", (outreach._ago(days=15),))
    conn.commit()
r = a.post("/admin/outreach", data=form(a, to="lee@bolt.com"))
check(r.status_code == 302 and len(sent) == n + 2, "after 14 days the address can be emailed again")

print("== provider error")
fail["on"] = True
r = a.post("/admin/outreach", data=form(a, to="err@fail.com"))
fail["on"] = False
with db.connect() as conn:
    row = conn.execute("SELECT * FROM outreach_log WHERE to_addr='err@fail.com'").fetchone()
check(row and row["status"] == "failed" and "422" in row["error"] and "refused it" in r.get_data(as_text=True),
      "a provider error is logged with the error and shown")
r = a.post("/admin/outreach", data=form(a, to="err@fail.com"))
check(r.status_code == 302, "a failed send doesn't block retrying the address")

print("== daily cap")
with db.connect() as conn:
    used = outreach.sent_last_day(conn)
    old = outreach._ago(hours=25)
    for i in range(outreach.DAILY_CAP - used):
        conn.execute("INSERT INTO outreach_log (to_addr, subject, status, created_at) VALUES (?,?,?,?)",
                     (f"filler{i}@x{i}.com", "s", "sent", db.now()))
    conn.execute("INSERT INTO outreach_log (to_addr, subject, status, created_at) VALUES ('old@y.com','s','sent',?)", (old,))
    conn.commit()
    check(outreach.sent_last_day(conn) == outreach.DAILY_CAP == 20, "20 sends in the last 24 hours")
n = len(sent)
r = a.post("/admin/outreach", data=form(a, to="new@brand.com"))
check(r.status_code == 200 and "daily cap" in r.get_data(as_text=True) and len(sent) == n, "the 21st is refused")
r = a.post("/admin/outreach", data=form(a, to="new@brand.com", do="test"))
check(len(sent) == n + 1 and sent[-1]["body"]["to"] == ["koby@example.com"], "a test send to yourself still works at the cap")
with db.connect() as conn:
    conn.execute("UPDATE outreach_log SET created_at=? WHERE to_addr LIKE 'filler%'", (old,))
    conn.commit()
r = a.post("/admin/outreach", data=form(a, to="new@brand.com"))
check(r.status_code == 302 and len(sent) == n + 2, "sends older than 24 hours don't count")

print("== log")
with db.connect() as conn:
    for i in range(60):
        conn.execute("INSERT INTO outreach_log (to_addr, subject, status, created_at) VALUES (?,?,?,?)",
                     (f"bulk{i}@z.com", "s", "sent", old))
    conn.commit()
page = a.get("/admin/outreach").get_data(as_text=True)
check(page.count("<tr><td class=\"muted\">") == 50 and "bulk59@z.com" in page and "bulk9@z.com" not in page,
      "the page shows the last 50 sends")

print("== email off")
os.environ.pop("RESEND_API_KEY")
n = len(sent)
r = a.post("/admin/outreach", data=form(a, to="off@brand.com"))
check("isn't connected" in r.get_data(as_text=True) and len(sent) == n, "with email not connected nothing is sent")
os.environ["RESEND_API_KEY"] = "re_test"

print(f"\nall {passed} outreach checks passed ({TMP})")
