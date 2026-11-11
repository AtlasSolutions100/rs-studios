"""Email priority (mailer.py, Nov 8 2026) on a throwaway database.

    python tests/test_email_priority.py

A fake Resend answers 200, 429 rate_limit_exceeded and 429 daily_quota_exceeded,
and a fake clock stands in for time.time / time.sleep in mailer.py. Covers:
kinds, pacing, the in-call rate-limit retry, the daily quota deferring
transactional mail and pausing nurture, the code that couldn't be sent (a
clear message, no silent lock-out), the deferred resend (oldest first, once
each, only while useful), nurture dropped at the quota, the nurture cap and
reserve, the nudges waiting unclaimed, and the /admin email row.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time as _time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_mailprio_")
os.environ["RS_SIGNUP_CONFIRM"] = "0"
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai", RS_PUBLIC_URL="https://campaignify.test")
for k in [k for k in os.environ if k.startswith("EMAIL_")] + ["FIRST_RUN_NUDGES"]:
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import login_security  # noqa: E402
import mailer  # noqa: E402
import nudges  # noqa: E402
import verify  # noqa: E402

UTC = timezone.utc
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        mailer.requests.post = _real_post
        shutil.rmtree(TMP, ignore_errors=True)
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


class Clock:
    def __init__(self):
        self.t = _time.time()
        self.slept = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += max(0.0, s)


CLOCK = Clock()
mailer._clock, mailer._sleep = CLOCK.time, CLOCK.sleep


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {"id": "re_1"}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


POSTS, SCRIPT = [], []


def ok():
    return Resp(200, {"id": f"re_{len(POSTS)}"})


def rate():
    return Resp(429, {"statusCode": 429, "name": "rate_limit_exceeded", "message": "Too many requests."},
                {"retry-after": "1"})


def quota():
    return Resp(429, {"statusCode": 429, "name": "daily_quota_exceeded",
                      "message": "You have reached your daily email sending quota."})


def fake_post(url, json=None, headers=None, timeout=0):
    POSTS.append({"t": CLOCK.t, "subject": json["subject"], "to": json["to"][0], "text": json.get("text", ""),
                  "key": (headers or {}).get("Idempotency-Key")})
    CLOCK.t += 0.01
    return SCRIPT.pop(0)() if SCRIPT else ok()


_real_post = mailer.requests.post
mailer.requests.post = fake_post


def reset():
    POSTS.clear()
    SCRIPT.clear()
    CLOCK.slept.clear()
    with db.connect() as conn:
        conn.execute("DELETE FROM email_log")
        conn.execute("DELETE FROM meta WHERE key IN ('mail_quota_hold','mail_pace')")
        conn.execute("DELETE FROM throttle")
        conn.commit()


def send(kind, to="a@example.com", subject=None):
    return mailer.send(frm='"Campaignify" <no-reply@mail.campaignify.ai>', to=to, subject=subject or f"{kind} mail",
                       html="<p>hi</p>", text="hi", tags={"kind": kind} if kind else {"workspace": 1})


def raises(fn):
    try:
        fn()
    except mailer.MailError as e:
        return e
    return None


def log_rows():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM email_log ORDER BY id")]


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


try:
    print("== kinds")
    for k in ("verify_email", "verify_signup", "password_reset", "account_exists", "login_fails", "ops_alert"):
        check(mailer.kind_class(k) == "transactional", f"{k} is transactional")
    for k in ("first_run_nudge_1", "first_run_nudge_2", "outreach"):
        check(mailer.kind_class(k) == "nurture", f"{k} is nurture")
    check(mailer.kind_class("") == "workspace", "no kind: a workspace's own email")

    print("== pacing")
    reset()
    os.environ["EMAIL_MAX_PER_SEC"] = "5"
    for i in range(8):
        send("password_reset", subject=f"pace {i}")
    gaps = [b["t"] - a["t"] for a, b in zip(POSTS, POSTS[1:])]
    check(len(POSTS) == 8 and min(gaps) >= 0.199, f"8 sends, never closer than 0.2 s (closest {min(gaps):.3f})")
    POSTS.clear()
    for i in range(4):
        send("", subject=f"ws {i}")
    gaps = [b["t"] - a["t"] for a, b in zip(POSTS, POSTS[1:])]
    check(min(gaps) >= 0.199, "workspace emails are paced too")
    os.environ.pop("EMAIL_MAX_PER_SEC")
    check(mailer.max_per_sec() == 3, "default 3 a second (Crowdcut's 4 plus these stay under Resend's 10)")

    print("== 429 rate limit: retried in the call")
    reset()
    SCRIPT[:] = [rate, rate, ok]
    check(send("verify_signup", subject="confirm") and len(POSTS) == 3, "two rate limits then accepted, 3 calls")
    check(CLOCK.slept.count(1.0) >= 2, "waited the retry-after header between tries")
    check(POSTS[0]["key"] is None or POSTS[0]["key"] == POSTS[2]["key"], "same request each try")
    reset()
    SCRIPT[:] = [rate, rate, rate]
    e = raises(lambda: send("verify_signup", to="s@example.com", subject="confirm and log in"))
    check(isinstance(e, mailer.MailDeferred) and e.queued, "still limited after 2 retries: MailDeferred, copy kept")
    r = log_rows()[-1]
    check(r["status"] == "deferred" and r["payload"] and r["retry_after"], "logged deferred with a retry time")
    SCRIPT[:] = [rate, rate, rate]
    e = raises(lambda: send("", to="contact@example.com", subject="flow email"))
    check(isinstance(e, mailer.MailDeferred) and not e.queued, "a workspace email gets MailDeferred (messaging keeps it queued)")

    print("== 429 daily_quota_exceeded: deferred, nurture paused")
    reset()
    SCRIPT[:] = [quota]
    e = raises(lambda: send("verify_signup", to="q@example.com", subject="Confirm your email to log in to Campaignify"))
    check(isinstance(e, mailer.MailDeferred) and e.queued and e.code == "quota_daily", "quota: MailDeferred, queued")
    r = log_rows()[-1]
    now = datetime.fromtimestamp(CLOCK.t, UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    check(r["status"] == "deferred" and r["retry_after"] == midnight.isoformat(timespec="seconds"),
          "deferred (not failed) until the next UTC midnight")
    check(mailer.hold_until() == midnight, "a quota hold until then")
    why = mailer.nurture_block()
    check(why and "quota" in why, f"nurture paused: {why}")
    n = len(POSTS)
    e = raises(lambda: send("first_run_nudge_1", to="n@example.com", subject="Your 2 free ads are waiting"))
    check(e and not isinstance(e, mailer.MailDeferred) and len(POSTS) == n, "a nudge is refused without calling Resend")
    check(log_rows()[-1]["status"] == "skipped", "logged as skipped")

    print("== a code that can't be sent: a clear message, no silent lock-out")
    c = client()
    SCRIPT[:] = [quota]
    r = c.post("/signup", data={"csrf": csrf(c), "email": "codeuser@example.com", "password": "longpassword"})
    check(r.status_code == 302, "sign-up still lands on Home with the quota spent")
    login_security.drain()
    u = user("codeuser@example.com")
    check(verify.code_live(u) and not verify.code_sent(u), "the code exists but its email didn't go")
    SCRIPT[:] = [quota]
    r = c.post("/app/verify/code/send", json={"auto": True}, headers={"X-CSRF": csrf(c)})
    body = r.get_json() or {}
    check(r.status_code == 503 and body.get("error") == "We couldn't send the code right now. Try again in a minute.",
          f"the code box doesn't claim it was sent; it says so plainly ({r.status_code} {body})")
    SCRIPT[:] = [quota]
    r = c.post("/app/verify/code/send", json={}, headers={"X-CSRF": csrf(c)})
    check(r.status_code == 503 and "We just sent one" not in json.dumps(r.get_json()),
          "a failed send doesn't count against the once-a-minute limit")
    SCRIPT[:] = [quota]
    r = c.post("/app/verify/resend", data={"csrf": csrf(c), "back": "/app"})
    page = c.get("/app").get_data(as_text=True)
    check("We couldn&#39;t send the code right now. Try again in a minute." in page
          or "We couldn't send the code right now. Try again in a minute." in page, "Resend shows the same message")
    # once mail goes again (an upgrade, or the reset) the next press works
    with db.connect() as conn:
        conn.execute("DELETE FROM meta WHERE key='mail_quota_hold'")
        conn.commit()
    POSTS.clear()
    r = c.post("/app/verify/code/send", json={}, headers={"X-CSRF": csrf(c)})
    check(r.status_code == 200 and (r.get_json() or {}).get("sent"), "with mail back, the next press sends a code")
    code = re.search(r"code is (\d{6})", POSTS[-1]["text"]).group(1)
    r = c.post("/app/verify/code", json={"code": code}, headers={"X-CSRF": csrf(c)})
    check(r.status_code == 200, "and that code confirms the email")
    check(verify.code_sent(user("codeuser@example.com")), "code_sent() true once it went")

    print("== deferred resend: oldest first, once each, only while useful")
    reset()
    SCRIPT[:] = [quota]
    raises(lambda: send("first_run_nudge_2", to="five@example.com", subject="nudge five"))
    r = log_rows()[-1]
    check(r["status"] == "deferred" and r["payload"] is None, "a nudge that finds the quota: deferred with no copy (dropped)")
    SCRIPT[:] = [quota]
    raises(lambda: send("verify_signup", to="one@example.com", subject="link one"))
    SCRIPT[:] = [quota]
    raises(lambda: send("password_reset", to="two@example.com", subject="reset two"))
    SCRIPT[:] = [quota]
    raises(lambda: send("verify_email", to="three@example.com", subject="code three"))
    SCRIPT[:] = [quota]
    raises(lambda: send("first_run_nudge_2", to="four@example.com", subject="nudge four"))
    rows = log_rows()
    check(rows[-1]["status"] == "skipped", "nurture during the hold is skipped, not sent")
    SCRIPT.clear()      # the skipped nudge never reached the fake
    POSTS.clear()
    st = mailer.retry_deferred(now=now + timedelta(minutes=10))
    check(st["resent"] == 0 and not POSTS, "nothing resent while the hold is on")
    # After the daily reset, and at least 61 minutes on: in the last hour before UTC midnight the reset comes sooner
    # than the 1-hour password email stops being worth sending, and this check used to fail then (seen Nov 11, 23:07 UTC).
    after = max(midnight + timedelta(minutes=2), now + timedelta(minutes=61))
    st = mailer.retry_deferred(now=after)
    subjects = [p["subject"] for p in POSTS]
    check(subjects == ["link one"], f"after the reset: the confirm link goes ({subjects})")
    check(st["dropped"] >= 2, "the 15-minute code and the 1-hour reset from hours ago are dropped, not sent")
    check("nudge five" not in subjects, "the deferred nudge is not resent")
    check(POSTS[0]["key"] is None or POSTS[0]["key"].endswith("-r"), "a resend uses its own idempotency key")
    POSTS.clear()
    st = mailer.retry_deferred(now=after + timedelta(minutes=1))
    check(st["resent"] == 0 and not POSTS, "once each")
    check(mailer.nurture_block(now=after) is None, "nurture allowed again")

    reset()
    SCRIPT[:] = [rate, rate, rate]
    raises(lambda: send("verify_email", to="six@example.com", subject="code A"))
    SCRIPT[:] = [rate, rate, rate]
    raises(lambda: send("verify_email", to="six@example.com", subject="code B"))
    SCRIPT[:] = [rate, rate, rate]
    raises(lambda: send("ops_alert", to="ops@example.com", subject="alert"))
    check("transactional" in (mailer.nurture_block() or ""), "nurture waits while a transactional email waits")
    POSTS.clear()
    st = mailer.retry_deferred(now=datetime.fromtimestamp(CLOCK.t, UTC) + timedelta(minutes=2))
    check([p["subject"] for p in POSTS] == ["code B", "alert"], "the newer code replaces the older; then the alert, in order")
    check(mailer.nurture_block() is None, "then nurture may go")
    check(send("first_run_nudge_1", to="later@example.com"), "and a nudge goes after them")

    print("== nurture cap and transactional reserve")
    reset()
    os.environ.update(EMAIL_NURTURE_DAILY_MAX="2", EMAIL_DAILY_LIMIT="8", EMAIL_TRANSACTIONAL_RESERVE="3")
    send("first_run_nudge_1", to="c1@example.com")
    send("outreach", to="c2@example.com")
    e = raises(lambda: send("first_run_nudge_1", to="c3@example.com"))
    check(e and "nurture cap" in str(e), f"cap of 2 reached: {e}")
    os.environ["EMAIL_NURTURE_DAILY_MAX"] = "60"
    for i in range(3):
        send("password_reset", to=f"r{i}@example.com")
    e = raises(lambda: send("first_run_nudge_1", to="c4@example.com"))
    check(e and "kept for transactional" in str(e), "5 of 8 sent, 3 reserved: nurture stops")
    check(send("verify_email", to="must@example.com"), "a code still goes in the reserve")
    os.environ["EMAIL_DAILY_LIMIT"] = "0"
    check(mailer.nurture_block() is None, "EMAIL_DAILY_LIMIT=0: no daily limit")
    for k in ("EMAIL_NURTURE_DAILY_MAX", "EMAIL_DAILY_LIMIT", "EMAIL_TRANSACTIONAL_RESERVE"):
        os.environ.pop(k)
    check((mailer.daily_limit(), mailer.transactional_reserve(), mailer.nurture_daily_max()) == (100, 30, 60),
          "defaults 100, 30, 60")

    print("== first-run nudges wait, unclaimed, while nurture is paused")
    reset()
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": "nudgeme@example.com", "password": "longpassword"})
    login_security.drain()
    u = user("nudgeme@example.com")
    later = datetime.fromisoformat(u["created_at"]).replace(tzinfo=UTC) + timedelta(minutes=15)
    mailer._set_hold(datetime.now(UTC) + timedelta(hours=2), "quota_daily")
    POSTS.clear()
    n = nudges.run_once(appmod.app.secret_key, now=later)
    with db.connect() as conn:
        claimed = conn.execute("SELECT COUNT(*) FROM first_run_nudges WHERE user_id=?", (u["id"],)).fetchone()[0]
    check(n == 0 and not POSTS and claimed == 0, "nothing sent and the step is not claimed")
    mailer._clear_hold()
    n = nudges.run_once(appmod.app.secret_key, now=later)
    check(n >= 1 and "nudgeme@example.com" in [p["to"] for p in POSTS], "after the hold the nudge goes")

    print("== /admin: the Account email row")
    reset()
    SCRIPT[:] = [quota]
    raises(lambda: send("verify_signup", to="adm1@example.com"))
    with db.connect() as conn:
        conn.execute("DELETE FROM meta WHERE key='mail_quota_hold'")
        conn.commit()
    SCRIPT[:] = [lambda: Resp(422, {"name": "validation_error", "message": "bad"})]
    raises(lambda: send("password_reset", to="adm2@example.com"))
    send("password_reset", to="adm3@example.com")
    with db.connect() as conn:
        s = mailer.status(conn)
    check((s["sent"], s["failed"], s["deferred"], s["waiting"]) == (1, 1, 1, 1), f"1 sent, 1 failed, 1 deferred waiting ({s})")
    a = client()
    a.post("/signup", data={"csrf": csrf(a), "email": "boss@example.com", "password": "longpassword"})
    with db.connect() as conn:
        conn.execute("UPDATE users SET is_admin=1 WHERE email='boss@example.com'")
        conn.commit()
    html = a.get("/admin").get_data(as_text=True)
    check('id="email-health"' in html and "1 deferred" in html and "1 failed" in html,
          "/admin's System card shows deferred apart from failed")

    dep = open(os.path.join(ROOT, ".github", "workflows", "deploy.yml"), encoding="utf-8").read()
    for k in ("EMAIL_DAILY_LIMIT", "EMAIL_TRANSACTIONAL_RESERVE", "EMAIL_NURTURE_DAILY_MAX"):
        check(f"{k}: ${{{{ vars.{k} }}}}" in dep and f'"{k}"' in dep, f"deploy.yml forwards {k}")
finally:
    mailer.requests.post = _real_post
    login_security.drain()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\nall {passed} email priority checks passed")
