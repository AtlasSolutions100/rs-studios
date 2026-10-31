"""The vendor-price check (hf.parse_usd / hf.estimate) and how it gates generation
(worker.price_ok), on a throwaway database with the vendor faked.

What it pins down:
  * image ads and product videos are never priced live: they always start;
  * a talking video starts when the vendor's estimate is missing or unreadable
    (any reply shape, an error, a timeout) at our own table price, and the miss is
    recorded for the admin page; it's refused and refunded only when a readable
    estimate is over PRICE_TOLERANCE x our price;
  * the parser reads the old {"usd": n} reply and the text pricing descriptions,
    and never takes a credit count for dollars.

    python tests/test_vendor_price.py      (tests/test_app.py runs it too)
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_vendor_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_MARKUP="2.0")
os.environ.pop("RS_TALKING_USD_PER_SECOND", None)
os.environ.pop("RS_VIDEO_USD_PER_SECOND", None)

import app as appmod  # noqa: E402,F401  (creates the schema)
import catalog  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


class Resp:
    def __init__(self, status=200, text="", ctype="application/json"):
        self.status_code, self.text, self.headers = status, text, {"Content-Type": ctype}

    def json(self):
        return json.loads(self.text)


ESTIMATE = {"reply": None}   # what /estimate answers: a Resp, or an exception to raise
submitted = []


def fake_post(url, json=None, params=None, headers=None, timeout=0):  # noqa: A002
    if "/estimate/" in url:
        r = ESTIMATE["reply"]
        if isinstance(r, Exception):
            raise r
        return r
    submitted.append((url, json))
    return Resp(200, '{"request_id": "req%d"}' % len(submitted))


hf.requests.post = fake_post

# ---------------------------------------------------------------- the parser
print("== parser")
inp = {"duration": 10, "resolution": "720p"}
cases = [
    ('{"usd": 4.622}', 4.622, "the old reply"),
    ('{"usd": "4.6220"}', 4.622, "usd as a string"),
    ('{"usd": "$4.62"}', 4.62, "usd with a dollar sign"),
    ('{"cost": "4.6220", "currency": "USD"}', 4.622, "cost + currency USD"),
    ('[{"unit_price": "0.4622", "quantity": "10", "currency": "USD"}]', 4.622, "unit price x quantity"),
    ('{"description": "Seedance 2.5, 720p, 10 s: estimated cost $4.62 (74 credits)"}', 4.62, "a description field"),
    ('"Estimated cost: $4.62"', 4.62, "a JSON string"),
    ("Estimated cost: $4.62 (74 credits)", 4.62, "plain text"),
    ("This generation will cost 4.6220 USD.", 4.622, "text in USD"),
    ("$0.4622/s x 10s = $4.622", 4.622, "a worked sum: the result"),
    ("Price: $0.4622 per second", 4.622, "a per-second rate times our duration"),
    ("1080p: $1.1372/s, 720p: $0.4622/s, 480p: $0.2056/s", 4.622, "a price list: the rate for our resolution"),
    ("Total: $5.00 ($0.50/s x 10 s)", 5.0, "a total wins over its parts"),
]
for body, want, what in cases:
    got, how = hf.parse_usd(body, inp)
    check(got is not None and abs(got - want) < 1e-6, f"{what}: {body!r} -> {got} ({how})")
for body, what in [('{"credits": 74}', "a credit count"), ("74 credits", "credits in text"),
                   ("between $3 and $5", "a range"), ('{"status": "ok"}', "no price"), ("", "empty"),
                   ("$0.2056/s at 480p, $0.4622/s at 720p and $1.1372/s at 1080p", "an ambiguous list")]:
    check(hf.parse_usd(body, inp)[0] is None, f"{what} is not read as dollars: {body!r}")

# ---------------------------------------------------------------- the gate
print("== gate")
now = db.now()
with db.connect() as conn:
    ws = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES ('T', 100000, ?)", (now,)).lastrowid
    uid = conn.execute("INSERT INTO users (email, pw_hash, workspace_id, created_at, email_verified_at) "
                       "VALUES ('t@example.com','x',?,?,?)", (ws, now, now)).lastrowid
    ph = conn.execute("INSERT INTO photos (workspace_id, path, content_type, remote_url, created_at) "
                      "VALUES (?, '/nope.jpg', 'image/jpeg', 'https://cdn.example/p.jpg', ?)", (ws, now)).lastrowid
    conn.commit()

FORMATS = {
    "ad_image": ({"photo_ids": [ph], "batch": 1, "aspect": "9:16"}, catalog.cost_usd("ad_image", batch=1),
                 catalog.price_credits("ad_image", batch=1)),
    "photo_to_video": ({"photo_ids": [ph], "duration": 5}, catalog.cost_usd("photo_to_video", duration=5),
                       catalog.price_credits("photo_to_video", duration=5)),
    "ugc_video": ({"photo_ids": [ph], "duration": 10, "product": "Glow Serum"}, catalog.cost_usd("ugc_video", duration=10),
                  catalog.price_credits("ugc_video", duration=10)),
}


def run(tool, reply, ticks=worker.MAX_ATTEMPTS + 1):
    """Queue one `tool` job (credits taken), answer /estimate with `reply`, tick the
    worker. Returns (status, error, refunded?, estimate calls made)."""
    params, usd, credits = FORMATS[tool]
    with db.connect() as conn:
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                           (ws, uid, tool, "A product on a table", json.dumps(params), credits, usd, now, now)).lastrowid
        db.adjust_credits(conn, ws, -credits, "generation", jid)
        conn.commit()
    ESTIMATE["reply"] = reply
    calls = {"n": 0}
    real = hf.estimate_detail

    def counting(m, i):
        calls["n"] += 1
        return real(m, i)
    hf.estimate_detail = counting
    try:
        for _ in range(ticks):
            worker.tick()
            with db.connect() as conn:
                if conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] != "pending":
                    break
    finally:
        hf.estimate_detail = real
    with db.connect() as conn:
        j = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        refunded = conn.execute("SELECT COUNT(*) FROM ledger WHERE job_id=? AND delta>0", (jid,)).fetchone()[0] > 0
        conn.execute("UPDATE jobs SET status='done' WHERE id=? AND status='running'", (jid,))   # out of the poll loop
        conn.commit()
    return j["status"], j["error"], refunded, calls["n"]


UNREADABLE = {
    "text/plain pricing description": Resp(200, "Seedance 2.5 reference-to-video bills per second of output; "
                                                "see your plan for current rates.", "text/plain"),
    "JSON string description": Resp(200, json.dumps("Pricing is per second of output video, by resolution.")),
    "JSON list": Resp(200, json.dumps([{"model": "x", "pricing": "per second"}])),
    "JSON without a price": Resp(200, json.dumps({"description": "Billed per second of output.", "credits": 74})),
    "vendor 500": Resp(500, "upstream error"),
    "vendor 404": Resp(404, '{"detail": "Not Found"}'),
    "timeout": hf.requests.Timeout("read timed out"),
}
for tool in ("ad_image", "photo_to_video"):
    st, err, ref, n = run(tool, UNREADABLE["text/plain pricing description"])
    check(st == "running" and not ref and n == 0, f"{catalog.TOOLS[tool]['name']}: never priced live, always starts")
for what, reply in UNREADABLE.items():
    st, err, ref, n = run("ugc_video", reply)
    check(st == "running" and not ref and n == 1,
          f"talking video with {what}: starts at our table price, not refused or refunded ({st} {err})")
with db.connect() as conn:
    live = json.loads(conn.execute("SELECT value FROM meta WHERE key=?", (worker.ESTIMATE_META,)).fetchone()["value"])
check(live["ok"] is False and live["misses"] == len(UNREADABLE) and live["why"] and live["vendor"] is None,
      f"the misses are recorded for the admin warning ({live['misses']}, {live['why'][:60]})")

ours = FORMATS["ugc_video"][1]
st, err, ref, n = run("ugc_video", Resp(200, json.dumps({"description": f"Estimated cost: ${ours:.2f}"})))
check(st == "running" and not ref, "a readable estimate at our price starts")
with db.connect() as conn:
    live = json.loads(conn.execute("SELECT value FROM meta WHERE key=?", (worker.ESTIMATE_META,)).fetchone()["value"])
check(live["ok"] and live["misses"] == 0 and live["last_ok_at"], "a good estimate clears the warning")
n_sub = len(submitted)
st, err, ref, n = run("ugc_video", Resp(200, f"Estimated cost: ${ours * 1.5:.2f} (240 credits)", "text/plain"))
check(st == "failed" and ref and "price changed" in err and len(submitted) == n_sub,
      "a readable estimate over 1.25x our price: refused before submit, and refunded")
st, err, ref, n = run("ugc_video", Resp(200, json.dumps({"usd": ours * 1.2})))
check(st == "running" and not ref, "within the 1.25x tolerance: starts")
st, err, ref, n = run("ugc_video", Resp(200, '{"credits": 99999}'))
check(st == "running" and not ref, "a big credit count isn't mistaken for dollars (no false refusal)")

# a job stored without a cost is compared against the table price, not $0 (which refused everything)
with db.connect() as conn:
    jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                       "created_at, updated_at) VALUES (?,?,'ugc_video','x',?, 'pending', 0, 0, ?, ?)",
                       (ws, uid, json.dumps(FORMATS["ugc_video"][0]), now, now)).lastrowid
    conn.commit()
    check(abs(worker.table_cost(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()) - ours) < 1e-9,
          "a job without a stored cost uses today's table price")
ESTIMATE["reply"] = Resp(200, json.dumps({"usd": ours}))
worker.tick()
with db.connect() as conn:
    check(conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == "running",
          "so an estimate at the table price starts it")

# estimate() itself never raises, whatever comes back
for what, reply in UNREADABLE.items():
    ESTIMATE["reply"] = reply
    check(hf.estimate("m", inp) is None and hf.last_estimate["ok"] is False, f"estimate() with {what}: None, no raise")

# ---------------------------------------------------------------- admin
print("== admin")
from werkzeug.security import generate_password_hash  # noqa: E402

app = appmod.app
_ip = [0]


def client():
    _ip[0] += 1
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as sess:
        return sess["csrf"]


with db.connect() as conn:
    conn.execute("INSERT INTO users (email, pw_hash, is_admin, workspace_id, created_at, email_verified_at, role) "
                 "VALUES ('boss@example.com', ?, 1, ?, ?, ?, 'owner')", (generate_password_hash("longpassword1"), ws, now, now))
    conn.commit()
adm = client()
adm.post("/login", data={"csrf": csrf(adm), "email": "boss@example.com", "password": "longpassword1"})

# an ad click sign-up (500 credits), a plain one, and one that made a video
ad = client()
ad.get("/?utm_source=google&utm_medium=cpc&utm_campaign=launch&gclid=abc123")
ad.post("/signup", data={"csrf": csrf(ad), "email": "adclicker@gmail.com", "password": "longpassword1"})
plain = client()
plain.post("/signup", data={"csrf": csrf(plain), "email": "plainperson@example.org", "password": "longpassword1"})
with db.connect() as conn:
    adu = conn.execute("SELECT * FROM users WHERE email='adclicker@gmail.com'").fetchone()
    conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, created_at, "
                 "updated_at) VALUES (?,?, 'photo_to_video', 'x', '{}', 'done', 0, 0, ?, ?)",
                 (adu["workspace_id"], adu["id"], now, now))
    conn.commit()

anon = client()
check(anon.get("/admin/signups").status_code == 302, "sign-ups: logged-out visitors are sent to log in")
check(plain.get("/admin/signups").status_code == 403 and plain.get("/admin").status_code == 403,
      "sign-ups and the system card are admin-only")
html = adm.get("/admin/signups").get_data(as_text=True)
check("ad•••@gmail.com" in html and "pl•••@example.org" in html and "adclicker" not in html
      and "plainperson" not in html, "emails are masked")
check("google / cpc" in html and "campaign launch" in html and "Google click id" in html and "ad offer" in html
      and ">500<" in html.replace(" ", "").replace("\n", ""), "the ad sign-up shows its first touch and the 500-credit grant")
check(f">{catalog.starter_credits()}<" in html.replace(" ", ""), "the plain sign-up shows the normal starter credits")
check("(1 done)" in html, "generations are counted")
check("abc123" not in html, "click ids themselves aren't shown")
with db.connect() as conn:
    data = __import__("admin_funnel").signups(conn, 30)
check(data["totals"]["signups"] == 3 and data["totals"]["ad_grant"] == 1 and data["totals"]["paid_source"] == 1
      and data["totals"]["made"] >= 1, f"funnel totals leave the owner out ({data['totals']})")

# the price check against a text reply, and the system card
ESTIMATE["reply"] = Resp(200, "Seedance 2.5 reference-to-video bills per second of output.", "text/plain")
r = adm.post("/admin/prices", data={"csrf": csrf(adm)})
check(r.status_code == 302, "the price check survives an unreadable reply")
html = adm.get("/admin").get_data(as_text=True)
check("no answer" in html and "bills per second" in html, "a miss shows the vendor's own words")
check("got no readable price for any format" in html and "start at our table price" in html, "and the admin gets a warning")
check('id="health"' in html and "Vendor-price check" in html and "0 of" in html, "the system card shows the check")
for name in ("Google", "Meta", "TikTok", "Reddit", "X"):
    check(f"{name} on" in html or f"{name} off" in html, f"the system card lists the {name} pixel")
check("See sign-ups" in html, "the admin links to the sign-ups view")
ESTIMATE["reply"] = Resp(200, json.dumps({"description": "Estimated cost: $4.62 (74 credits)"}))
adm.post("/admin/prices", data={"csrf": csrf(adm)})
html = adm.get("/admin").get_data(as_text=True)
check("$4.620" in html and "read from text in description" in html and "got no readable price" not in html,
      "a readable text reply is priced and the warning clears")

print(f"\nall {passed} vendor-price checks passed ({TMP})")
