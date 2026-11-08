"""Credit holds for AI runs (credits.py, BILL-2): the quote leaves the balance
before the provider is called; parallel runs can't spend the same credits; the
ledger shows held / used / back rows; crashed runs give their hold back.
The Anthropic API is faked: no request leaves the machine.

    python tests/test_credit_holds.py      (tests/test_app.py runs it too)
"""
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_holds_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_MARKUP="2.0",
                  RS_EMAIL_VERIFY="0", ANTHROPIC_API_KEY="sk-ant-test")
for k in ("ANTHROPIC_MODEL", "LLM_USD_IN_PER_MTOK", "LLM_USD_OUT_PER_MTOK", "LLM_USD_PER_WEB_SEARCH", "GROQ_API_KEY",
          "CREDIT_HOLDS", "RS_PLANNER_CAP"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import competitors  # noqa: E402
import creator  # noqa: E402
import credits  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import planner  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def _no_network(*a, **kw):
    raise AssertionError("no real HTTP in these tests")


llm.requests.post = _no_network
appmod._maybe_reconcile = lambda ws_id, force=False: 0
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


def signup(email):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    return c


def login(email):
    c = client()
    r = c.post("/login", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    return c


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                            (email,)).fetchone()


def ws_row(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def set_balance(ws_id, pack, plan=0):
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET credits=?, plan_credits=? WHERE id=?", (pack, plan, ws_id))
        conn.commit()


def ledger(ws_id, n=10):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC LIMIT ?", (ws_id, n)).fetchall()


def holds(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM credit_holds WHERE workspace_id=? ORDER BY id", (ws_id,)).fetchall()


a = signup("a@example.com")
b = signup("b@example.com")
a.get("/app/competitors")
b.get("/app/competitors")
wa, wb = ws_of("a@example.com"), ws_of("b@example.com")
assert wa["brand_id"] and wb["brand_id"]

print("== reserve, settle, release")
set_balance(wa["id"], 100)
h = credits.reserve(wa["id"], 40, "AI ad copy")
check(h is not None and h.id and db.balance(ws_row(wa["id"])) == 60, "a hold takes the quote out of the balance up front")
led = ledger(wa["id"], 1)[0]
check(led["delta"] == -40 and led["reason"] == "AI ad copy: held up to 40" and led["hold_id"] == h.id,
      "ledger row for the hold, tied to it")
check(credits.settle(h, 15) == 15 and db.balance(ws_row(wa["id"])) == 85, "settle keeps what it used, gives the rest back")
led = ledger(wa["id"], 1)[0]
check(led["delta"] == 25 and led["reason"] == "AI ad copy: used 15, 25 back" and led["hold_id"] == h.id,
      "ledger row for the settle")
n_led = len(ledger(wa["id"], 500))
check(credits.settle(h, 15) == 15 and db.balance(ws_row(wa["id"])) == 85 and len(ledger(wa["id"], 500)) == n_led,
      "settling twice changes nothing")
h = credits.reserve(wa["id"], 30, "AI script")
check(credits.settle(h, 999) == 30 and db.balance(ws_row(wa["id"])) == 55, "never more than the hold, even if it ran over")
check(ledger(wa["id"], 1)[0]["reason"] == "AI script: used all 30 held", "a fully used hold says so")
h = credits.reserve(wa["id"], 20, "AI flow draft")
check(credits.release(h) == 20 and db.balance(ws_row(wa["id"])) == 55, "release gives the whole hold back")
check(ledger(wa["id"], 1)[0]["reason"] == "AI flow draft: nothing used, 20 back", "ledger row for the release")
check(credits.release(h) == 0 and db.balance(ws_row(wa["id"])) == 55, "releasing twice changes nothing")
check([x["status"] for x in holds(wa["id"])] == ["settled", "settled", "released"], "hold rows record each outcome")

print("== refuse instead of under-collect")
set_balance(wa["id"], 10)
check(credits.reserve(wa["id"], 11, "AI ad copy") is None and db.balance(ws_row(wa["id"])) == 10,
      "not enough: no hold, nothing taken")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET billing_hold=1 WHERE id=?", (wa["id"],))
    conn.commit()
check(credits.reserve(wa["id"], 5, "AI ad copy") is None, "a workspace on a billing hold can't reserve")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET billing_hold=0 WHERE id=?", (wa["id"],))
    conn.commit()

print("== buckets: plan credits are used first, pack credits come back first")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='starter' WHERE id=?", (wa["id"],))
    conn.commit()
set_balance(wa["id"], 50, plan=50)
h = credits.reserve(wa["id"], 80, "AI campaign planning")
w = ws_row(wa["id"])
check(w["plan_credits"] == 0 and w["credits"] == 20, "the hold takes plan credits first")
credits.settle(h, 40)
w = ws_row(wa["id"])
check(w["credits"] == 50 and w["plan_credits"] == 10, "the unused part goes back to pack first (it never expires)")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override=NULL WHERE id=?", (wa["id"],))
    conn.commit()
set_balance(wa["id"], 0, plan=30)
h = credits.reserve(wa["id"], 30, "AI ad copy")
credits.release(h)
w = ws_row(wa["id"])
check(w["credits"] == 30 and w["plan_credits"] == 0, "with no plan on any more, plan credits come back as pack credits")

print("== parallel holds can't overspend (threads)")
set_balance(wa["id"], 100)
got, errors = [], []
gate = threading.Barrier(20)


def grab():
    try:
        gate.wait()
        got.append(credits.reserve(wa["id"], 30, "AI ad copy"))
    except Exception as e:  # noqa: BLE001
        errors.append(e)


ts = [threading.Thread(target=grab) for _ in range(20)]
for t in ts:
    t.start()
for t in ts:
    t.join()
ok = [x for x in got if x is not None]
w = ws_row(wa["id"])
check(not errors and len(ok) == 3 and w["credits"] == 10, f"20 runs at once on 100 credits: 3 holds of 30, 10 left ({len(ok)})")
check(sum(x["delta"] for x in ledger(wa["id"], 500) if x["hold_id"] in {o.id for o in ok}) == -90,
      "the ledger matches the holds")
for x in ok:
    credits.settle(x, 30)
check(ws_row(wa["id"])["credits"] == 10, "and settling them leaves the balance right")

print("== parallel AI requests: one provider call, the rest refused with 402")
with db.connect() as conn:
    pid = conn.execute("INSERT INTO products (workspace_id, brand_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
                       (wb["id"], wb["brand_id"], "Glow serum", db.now(), db.now())).lastrowid
    conn.commit()
calls = []


def fake_angles(body, timeout=None):
    calls.append(body)
    time.sleep(0.3)      # long enough for the other requests to arrive while this one runs
    out = {"angles": [{"title": "Two-minute mornings", "hook": "One drop, done.", "insight": "Mornings are short."}]}
    return {"model": body["model"], "stop_reason": "tool_use", "usage": {"input_tokens": 500, "output_tokens": 100},
            "content": [{"type": "tool_use", "id": "t1", "name": "submit_angles", "input": out}]}


llm._anthropic = fake_angles
quote = creator.estimate_angles()
set_balance(wb["id"], quote)
browsers = [b] + [login("b@example.com") for _ in range(3)]
answers = []


go = threading.Barrier(len(browsers))


def ask(c):
    tok = csrf(c)
    go.wait()
    t0 = time.time()
    r = c.post("/app/create/angles", json={"product": pid, "type": "ugc"}, headers={"X-CSRF": tok})
    answers.append((r.status_code, r.get_json(), t0, time.time()))


ts = [threading.Thread(target=ask, args=(c,)) for c in browsers]
for t in ts:
    t.start()
for t in ts:
    t.join()
codes = sorted(x[0] for x in answers)
check(len(calls) == 1 and codes == [200, 402, 402, 402], f"4 at once with credits for 1: 1 call, 3 refused ({codes})")
first_done = min(x[3] for x in answers if x[0] == 200)
check(all(x[2] < first_done for x in answers), "all 4 were sent while the first was still running")
done = next(x[1] for x in answers if x[0] == 200)
w = ws_row(wb["id"])
check(done["charged"] >= 1 and db.balance(w) == quote - done["charged"] >= 0, "the one that ran paid what it used")
refused = next(x[1] for x in answers if x[0] == 402)
check("credits" in refused["error"], "the refused ones say why")

print("== nothing else can spend a running competitor run's credits")
need = competitors.quote(1)
set_balance(wb["id"], need)
probe = {}


def fake_research(body, timeout=None):
    names = [t.get("name") for t in body.get("tools") or []]
    if "submit_competitor" in names and "spent" not in probe:
        with db.connect() as conn:       # an image ad queued in another tab mid-run
            probe["spent"] = db.adjust_credits(conn, wb["id"], -1, "probe")
            conn.commit()
        probe["balance"] = db.balance(ws_row(wb["id"]))
    raise llm.LLMError("529: overloaded")


llm._anthropic = fake_research
r = b.post("/app/competitors", data={"csrf": csrf(b), "competitor": ["Rival Glow"], "quote": str(need)})
check(probe.get("spent") is False and probe.get("balance") == 0, "while it runs, the whole quote is held: other spends fail")
check(db.balance(ws_row(wb["id"])) == need, "the run failed, so the hold came back in full")
with db.connect() as conn:
    rep = conn.execute("SELECT * FROM competitor_reports WHERE workspace_id=? ORDER BY id DESC", (wb["id"],)).fetchone()
hs = holds(wb["id"])
check(hs[-1]["ref"] == f"competitor:{rep['id']}" and hs[-1]["status"] == "released", "its hold was tied to it and released")
set_balance(wb["id"], need - 1)
n = len(holds(wb["id"]))
r = b.post("/app/competitors", data={"csrf": csrf(b), "competitor": ["Rival Glow"], "quote": str(need)})
check(len(holds(wb["id"])) == n and "up to" in b.get("/app/competitors").get_data(as_text=True),
      "one credit short: refused before any hold or call")

print("== interrupted runs give their hold back")
set_balance(wb["id"], 500)
h = credits.reserve(wb["id"], 200, "Competitor research")
with db.connect() as conn:
    rid = conn.execute("INSERT INTO competitor_reports (workspace_id, brand_id, status, inputs_json, steps_json, created_at, "
                       "updated_at) VALUES (?,?,?,?,?,?,?)", (wb["id"], wb["brand_id"], "running",
                                                              json.dumps({"hold": h.id}), "[]", "2020-01-01T00:00:00+00:00",
                                                              "2020-01-01T00:00:00+00:00")).lastrowid
    conn.commit()
credits.link(h, f"competitor:{rid}")
check(db.balance(ws_row(wb["id"])) == 300, "held while it runs")
b.get("/app/competitors")
check(db.balance(ws_row(wb["id"])) == 500 and holds(wb["id"])[-1]["status"] == "released",
      "a competitor run whose thread died gives the hold back when it's marked failed")

print("== stale holds (a crash) go back after 30 minutes")
set_balance(wb["id"], 100)
h = credits.reserve(wb["id"], 60, "AI landing page copy")
check(credits.release_stale() == 0, "a fresh hold stays")
with db.connect() as conn:
    conn.execute("UPDATE credit_holds SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (h.id,))
    conn.commit()
check(credits.release_stale() == 1 and db.balance(ws_row(wb["id"])) == 100, "an old one is released")
check(ledger(wb["id"], 1)[0]["reason"] == "AI landing page copy: the run stopped, 60 back", "and says why")
check(credits.settle(h, 12) == 12 and db.balance(ws_row(wb["id"])) == 88, "a run that finishes after that still pays")
credits._last_sweep[0] = 0
credits.maybe_release_stale()
check(credits._last_sweep[0] > 0, "the worker's sweep runs (and never raises)")
import worker  # noqa: E402
import inspect  # noqa: E402
check("credits.maybe_release_stale()" in inspect.getsource(worker.start), "the worker loop calls the sweep")

print("== the planner holds its cap and settles it")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='starter' WHERE id=?", (wb["id"],))
    conn.commit()


def fake_planner(body, timeout=None):
    return {"model": body["model"], "stop_reason": "tool_use", "usage": {"input_tokens": 3000, "output_tokens": 300},
            "content": [{"type": "tool_use", "id": "tu_f", "name": "finish",
                         "input": {"summary": "Test plan.", "checklist": [{"title": "Launch"}]}}]}


llm._anthropic = fake_planner
cap = planner.cap_credits()
set_balance(wb["id"], cap + 5)
r = b.post("/app/campaigns", data={"csrf": csrf(b), "offer": "Glow serum", "goal": "sales"})
with db.connect() as conn:
    c = conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY id DESC", (wb["id"],)).fetchone()
h = holds(wb["id"])[-1]
check(c["status"] == "ready" and h["ref"] == f"campaign:{c['id']}" and h["status"] == "settled"
      and h["amount"] == cap and h["charged"] == c["credits"] > 0, "the plan's cap was held, then settled at what it used")
check(db.balance(ws_row(wb["id"])) == cap + 5 - c["credits"], "balance down by what it used")
rows = ledger(wb["id"], 2)
check(rows[1]["reason"] == f"AI campaign planning: held up to {cap}" and rows[0]["delta"] == cap - c["credits"],
      "ledger: held, then the unused part back")
set_balance(wb["id"], cap - 1)
n = len(holds(wb["id"]))
r = b.post(f"/app/campaigns/{c['id']}/revise", data={"csrf": csrf(b), "notes": "Shorter."})
check(len(holds(wb["id"])) == n and db.balance(ws_row(wb["id"])) == cap - 1, "short of the cap: no revise, nothing held")
set_balance(wb["id"], 1000)
with db.connect() as conn:
    conn.execute("UPDATE campaigns SET status='planning', updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (c["id"],))
    conn.commit()
h = credits.reserve(wb["id"], 100, "AI campaign planning", ref=f"campaign:{c['id']}")
b.get(f"/app/campaigns/{c['id']}")
check(db.balance(ws_row(wb["id"])) == 1000 and holds(wb["id"])[-1]["status"] == "released",
      "a planning run whose thread died gives its hold back")

print("== CREDIT_HOLDS=off charges after the run, as before")
os.environ["CREDIT_HOLDS"] = "off"
set_balance(wa["id"], 50)
n = len(holds(wa["id"]))
h = credits.reserve(wa["id"], 40, "AI ad copy")
check(h is not None and h.id is None and db.balance(ws_row(wa["id"])) == 50 and len(holds(wa["id"])) == n,
      "no hold taken")
check(credits.reserve(wa["id"], 60, "AI ad copy") is None, "still refused when the balance is short of the quote")
check(credits.settle(h, 7) == 7 and db.balance(ws_row(wa["id"])) == 43 and ledger(wa["id"], 1)[0]["reason"] == "AI ad copy",
      "charged what it used after the call")
os.environ.pop("CREDIT_HOLDS")

print("== the billing page lists hold rows")
set_balance(wa["id"], 100)
h = credits.reserve(wa["id"], 9, "AI ad copy")
credits.settle(h, 4)
html = a.get("/app/billing").get_data(as_text=True)
check("AI ad copy: held up to 9" in html and "AI ad copy: used 4, 5 back" in html, "held and settled rows show on Billing")

print(f"\nall {passed} checks passed ({TMP})")
