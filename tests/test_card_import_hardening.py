"""Re-audit L-2, L-3 and L-10 (campaignify/m2_reaudit.md):
  L-2  one-trial-per-person counts only confirmed emails: a card row gets its email_norm once its member confirms,
       and the email match looks only at confirmed rows and members, so a squatter can't use up someone's trial;
  L-3  users.email_norm, filled at sign-up, backfilled on start and indexed, replaces the scan of every Gmail user;
  L-10 the first-run import (page, photos, logo) shares one time budget, and only a few imports run at once.
Nothing leaves the machine: the slow site is a local socket and the fetcher's address checks are patched for it.

    python tests/test_card_import_hardening.py      (tests/test_app.py runs it too)
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_l2l3l10_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_MARKUP="2.0",
                  RS_EMAIL_VERIFY="1", RS_NUDGE_LOOP="0", RS_SIGNUP_CONFIRM="0", RS_STARTER_CREDITS="0",
                  RS_IMPORT_CONCURRENCY="2", RS_IMPORT_BUDGET_S="30")
for k in ("RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "ANTHROPIC_API_KEY",
          "SIGNUP_CREDITS_ADS"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import card_on_file  # noqa: E402
import db  # noqa: E402
import fetcher  # noqa: E402
import verify  # noqa: E402

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
    c.environ_base["REMOTE_ADDR"] = f"10.6.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword1"})
    assert r.status_code in (302, 303), r.status_code
    return c


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def save_card(email, fingerprint):
    """What the card step does when Stripe says the card is saved: credits wait on the workspace, then record_card."""
    u = user(email)
    with db.connect() as conn:
        card_on_file.defer(conn, u["workspace_id"], 44, "starter credits")
        conn.commit()
        return card_on_file.record_card(conn, u["workspace_id"], customer="cus_x", pm_id="pm_x", fingerprint=fingerprint,
                                        label="Visa ending 4242", email=u["email"], user=u)


def confirm(email):
    """The member types the right code (verify.check_code's write, then verify.completed)."""
    u = user(email)
    with db.connect() as conn:
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
        verify.completed(conn, u)


def trial_row(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM card_trials WHERE workspace_id=? ORDER BY id DESC",
                            (user(email)["workspace_id"],)).fetchone()


try:
    # ================================================================ L-3
    print("== L-3: users.email_norm, indexed")
    signup("Jo.Park+ads@GoogleMail.com")
    check(user("jo.park+ads@googlemail.com") is not None or user("Jo.Park+ads@GoogleMail.com") is not None, "sign up")
    u = user("jo.park+ads@googlemail.com") or user("Jo.Park+ads@GoogleMail.com")
    check(u["email_norm"] == "jopark@gmail.com", f"sign-up writes the normalized email ({u['email_norm']})")
    with db.connect() as conn:
        idx = [r["name"] for r in conn.execute("PRAGMA index_list(users)")]
        plan = " ".join(str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT workspace_id FROM users WHERE email_norm=? AND workspace_id!=?", ("a@b.c", 0)))
        conn.execute("UPDATE users SET email_norm=NULL WHERE id=?", (u["id"],))
        conn.commit()
    check("idx_users_email_norm" in idx and "idx_users_email_norm" in plan, "the lookup uses idx_users_email_norm")
    db.init()
    check(user(u["email"])["email_norm"] == "jopark@gmail.com", "a row without it (an older image wrote it) is backfilled on start")
    src = open(os.path.join(ROOT, "card_on_file.py")).read()
    check("email LIKE" not in src, "card_on_file no longer scans users by domain")

    # ================================================================ L-2
    print("== L-2: only confirmed emails use up a trial")
    # a squatter signs up with a variant of Dana's Gmail and saves their own card; they can't confirm Dana's inbox
    signup("dana.lee+promo@gmail.com")
    out = save_card("dana.lee+promo@gmail.com", "fp_squatter")
    row = trial_row("dana.lee+promo@gmail.com")
    check(out["credits"] == 44 and row["email_norm"] is None and row["user_id"] == user("dana.lee+promo@gmail.com")["id"],
          "an unconfirmed member's card row is kept without the email (card fingerprint and member only)")
    # Dana signs up for real, saves her card first (the card step comes before confirming)
    signup("danalee@gmail.com")
    out = save_card("danalee@gmail.com", "fp_dana")
    check(out["credits"] == 44 and out["reused"] is None,
          "Dana still gets her free credits: the squatter's unconfirmed address doesn't count")
    check(trial_row("danalee@gmail.com")["email_norm"] is None, "Dana's row has no email until she confirms")
    confirm("danalee@gmail.com")
    check(trial_row("danalee@gmail.com")["email_norm"] == "danalee@gmail.com",
          "confirming puts the address on the card row she saved before (verify.completed)")
    # now a third account on a variant of Dana's address and a new card: refused by email
    signup("d.a.n.a.l.e.e@gmail.com")
    out = save_card("d.a.n.a.l.e.e@gmail.com", "fp_third")
    check(out["credits"] == 0 and out["reused"] == "email", "a variant of a confirmed address gets no second trial")
    # the card fingerprint still counts with or without a confirmed email
    signup("someone.else@example.org")
    out = save_card("someone.else@example.org", "fp_squatter")
    check(out["credits"] == 0 and out["reused"] == "card", "a card that already had a trial still gets none")
    # the member path: a confirmed member who had starter credits blocks a variant even with no card row
    with db.connect() as conn:
        conn.execute("DELETE FROM card_trials WHERE email_norm='danalee@gmail.com'")
        conn.commit()
    with db.connect() as conn:
        check(card_on_file.trial_used(conn, -1, None, "Dana.Lee+x@gmail.com") == "email",
              "a confirmed member with starter credits counts (found by users.email_norm)")
        check(card_on_file.trial_used(conn, -1, None, "dana.lee+promo2@googlemail.com") == "email",
              "googlemail.com folds into gmail.com")
    with db.connect() as conn:   # the same, unconfirmed: doesn't count
        conn.execute("UPDATE users SET email_verified_at=NULL WHERE email='danalee@gmail.com'")
        conn.commit()
        check(card_on_file.trial_used(conn, -1, None, "danalee@gmail.com") is None,
              "an unconfirmed member with starter credits doesn't count")
    # ad-offer grants count only when that workspace's member with the address confirmed it
    signup("pat@example.net")
    pws = user("pat@example.net")["workspace_id"]
    with db.connect() as conn:
        conn.execute("INSERT INTO ad_signup_grants (workspace_id, email_norm, ip_hash, credits, created_at) VALUES (?,?,?,?,?)",
                     (pws, "pat@example.net", "x", 44, db.now()))
        conn.commit()
        check(card_on_file.trial_used(conn, -1, None, "Pat+1@example.net") is None, "an unconfirmed ad-offer grant doesn't count")
    confirm("pat@example.net")
    with db.connect() as conn:
        check(card_on_file.trial_used(conn, -1, None, "Pat+1@example.net") == "email", "a confirmed one does")
    # with no confirm step at all (RS_EMAIL_VERIFY=0) every address counts, as before
    os.environ["RS_EMAIL_VERIFY"] = "0"
    with db.connect() as conn:
        check(card_on_file.trial_used(conn, -1, None, "danalee@gmail.com") == "email",
              "RS_EMAIL_VERIFY=0: there's nothing to confirm, so addresses count as typed")
    os.environ["RS_EMAIL_VERIFY"] = "1"

    # ================================================================ L-10
    print("== L-10: one time budget per import, a cap on imports at once")
    # a site that answers a byte every 0.3 s, forever (the fetcher's address checks are pointed at it)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def drip(conn_):
        try:
            conn_.recv(4096)
            conn_.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100000\r\n\r\n")
            while not stop.is_set():
                conn_.sendall(b"x")
                time.sleep(0.3)
        except OSError:
            pass
        finally:
            conn_.close()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c_, _ = srv.accept()
            except OSError:
                continue
            threading.Thread(target=drip, args=(c_,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    import urllib3
    real = (fetcher.check_url, fetcher.resolve, fetcher._open)
    fetcher.check_url = lambda u: ("http", "slow.example", 80, "/", u)
    fetcher.resolve = lambda h, p: "127.0.0.1"
    fetcher._open = lambda scheme, host, p, ip: urllib3.HTTPConnectionPool("127.0.0.1", port, timeout=fetcher.TIMEOUT,
                                                                           retries=False, maxsize=1)
    try:
        t0 = time.monotonic()
        try:
            with fetcher.budget(1.5):
                fetcher.fetch("http://slow.example/")
            ok = False
        except fetcher.FetchError as e:
            ok = "too long" in str(e)
        took = time.monotonic() - t0
        check(ok and took < 4, f"a drip-fed page stops at the budget, not after {fetcher.TOTAL_SECONDS} s ({took:.1f} s)")
        t0 = time.monotonic()
        fails = 0
        with fetcher.budget(2):
            for _ in range(4):   # page, photos, logo: each one gets what's left, not a fresh 25 s
                try:
                    fetcher.fetch_bytes("http://slow.example/a.png")
                except fetcher.FetchError:
                    fails += 1
        took = time.monotonic() - t0
        check(fails == 4 and took < 4.5, f"4 fetches inside one 2 s budget take about 2 s together ({took:.1f} s)")
        with fetcher.budget(10):
            with fetcher.budget(1):
                inner = fetcher._BUDGET.get() - time.monotonic()
            outer = fetcher._BUDGET.get() - time.monotonic()
        check(inner <= 1.01 and 9 < outer <= 10.01 and fetcher._BUDGET.get() is None,
              "budgets nest (the tighter one wins) and end with their block")
    finally:
        fetcher.check_url, fetcher.resolve, fetcher._open = real
        stop.set()
        srv.close()

    # the import route: at most RS_IMPORT_CONCURRENCY at once, the rest get a 503 with the by-hand way
    import product_routes
    check(appmod.IMPORT_CONCURRENCY == 2 and appmod.IMPORT_BUDGET_S == 30, "RS_IMPORT_CONCURRENCY and RS_IMPORT_BUDGET_S are read")
    gate, inside, budgets = threading.Event(), [], []
    real_import = product_routes.import_url

    def slow_import(conn, url, brand_id, use_ai=False):
        b = fetcher._BUDGET.get()
        budgets.append(None if b is None else b - time.monotonic())
        inside.append(url)
        gate.wait(10)
        raise fetcher.FetchError("The site took too long to answer.")

    product_routes.import_url = slow_import
    appmod.IMPORT_WAIT_S = 0.3
    try:
        cs = [signup(f"importer{i}@example.com") for i in range(3)]
        for i in range(3):
            confirm(f"importer{i}@example.com")
        results = [None, None]

        def go(i):
            r = cs[i].post("/app/start/import", json={"url": "https://shop.example/p/1"},
                           headers={"X-CSRF": csrf(cs[i])})
            results[i] = r.status_code

        ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        deadline = time.time() + 5
        while len(inside) < 2 and time.time() < deadline:
            time.sleep(0.05)
        check(len(inside) == 2, "2 imports run at once")
        r = cs[2].post("/app/start/import", json={"url": "https://shop.example/p/2"}, headers={"X-CSRF": csrf(cs[2])})
        body = r.get_json() or {}
        check(r.status_code == 503 and body.get("manual") and "Try again in a minute" in body.get("error", ""),
              "a third import at the same time gets 503 and the by-hand way, not a held thread")
        gate.set()
        for t in ts:
            t.join(10)
        check(results == [422, 422], f"the 2 running imports finish normally ({results})")
        check(all(b is not None and 29 < b <= 30 for b in budgets), "each import ran inside its 30 s budget")
        r = cs[2].post("/app/start/import", json={"url": "https://shop.example/p/3"}, headers={"X-CSRF": csrf(cs[2])})
        check(r.status_code == 422, "once they finish, the slot is free again (the semaphore is released)")
    finally:
        product_routes.import_url = real_import

    print(f"\nall {passed} checks passed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
