"""The campaign desk (desk.py, web/v2/desk.html, static/desk.js): one sentence in, the whole campaign back as rows,
priced before anything is made. On a throwaway database, every vendor and the text model faked:

  * new look off: no desk, no desk routes, Home unchanged
  * new look on: the desk has its own tab; Home is Studio on the first run and the desk once something was made
  * the plan: the ads come from Studio's Ask (one model call, counted against the same daily cap); the copy, the
    page, the follow-up and the launch step are added by word rules; a part the plan doesn't include is shown
    with the plan that has it and can't be ticked; at the cap, or with no ads asked for, the rest still comes back
  * pricing again never calls the model; a row is cut down to what its kind may set; five ad parts at most
  * making it: nothing starts without the total that was shown; the ads go through Studio's own start, the copy
    is held and settled, the page and its form are drafts, the follow-up is switched off and starts from that
    form; what was made is kept and shown again
  * a part the member's plan doesn't include is never made, whatever the request says

    python tests/test_desk.py      (tests/test_app.py runs it too)
"""
import html as htmllib
import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_desk_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "RS_CARD_REQUIRED", "SIGNUP_CREDITS_ADS", "CLAIMS_CHECK", "ANTHROPIC_API_KEY",
          "GROQ_API_KEY", "STRIPE_SECRET_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import copywriter  # noqa: E402
import desk  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import llm  # noqa: E402
import studio_v2  # noqa: E402
import worker  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def png(w=400, h=400, rgb=(200, 120, 40)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(b, "PNG")
    b.seek(0)
    return b


def no_vendor(*a, **kw):
    raise AssertionError("this test never calls a vendor")


submitted = []
hf.submit = lambda model, inputs, webhook_url=None, idempotency_key=None: submitted.append(model) or f"req{len(submitted)}"
hf.upload = lambda data, ctype: "https://files.example/1.png"
hf.status = lambda rid: {"status": "in_progress"}
hf.cancel = lambda rid: hf.STARTED
worker.requests.get = no_vendor
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


def user(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def ws_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (user(email)["workspace_id"],)).fetchone()


def member(email, name="Ember Candle", credits=20000):
    """A signed-up, onboarded, email-confirmed member with one product (with a photo) and credits to spend."""
    import onboarding
    import products
    c = client()
    assert c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"}).status_code == 302
    with db.connect() as conn:
        onboarding.finish(conn, user(email))
        conn.execute("UPDATE users SET email_verified_at=? WHERE email=?", (db.now(), email))
        conn.commit()
    r = c.post("/app/products/new", data={"csrf": csrf(c), "name": name, "photos": (png(), "p.png")},
               content_type="multipart/form-data")
    assert r.status_code in (200, 302), r.status_code
    with db.connect() as conn:
        p = conn.execute("SELECT * FROM products WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                         (user(email)["workspace_id"],)).fetchone()
        if not conn.execute("SELECT 1 FROM photos WHERE product_id=?", (p["id"],)).fetchone():
            path = os.path.join(TMP, f"p-{p['id']}.png")
            open(path, "wb").write(png().getvalue())
            products.add_photo(conn, p["workspace_id"], p["id"], path, "p.png", "image/png")
        conn.execute("UPDATE products SET description=?, offer=?, claims=?, audience=? WHERE id=?",
                     ("A soy wax candle with amber and cedar.", "Free shipping this week",
                      "Burns for 40 hours\nHand poured in Texas", "People who light a candle every evening", p["id"]))
        if credits:
            db.adjust_credits(conn, p["workspace_id"], credits, "test top-up")
        conn.commit()
    return c, p["id"]




def jobs_of(email):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id", (user(email)["workspace_id"],)).fetchall()


def jpost(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF": csrf(c)})


def page(c, url):
    return htmllib.unescape(c.get(url).get_data(as_text=True))


def bal(email):
    return db.balance(ws_of(email))


def count(table, email):
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE workspace_id=?", (user(email)["workspace_id"],)).fetchone()[0]


def set_plan(email, code):
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (code, user(email)["workspace_id"]))
        conn.commit()


def by_kind(parts):
    out = {}
    for p in parts:
        out.setdefault(p["kind"], []).append(p)
    return out


calls, answer = [], [None]
SAID = "Holiday launch for the candle: image ads, the captions, a page to collect emails and a follow-up"
ADS = {"note": "Two looks for the holiday, one on the scent and one on the gift.", "parts": [
    {"make": "image", "text": "Your whole evening, in one candle", "count": 3},
    {"make": "image", "text": "The gift that smells like home", "count": 1}]}

print("== new look off")
a, pid = member("a@example.com")
r = a.get("/app/desk")
check(r.status_code == 302 and r.headers["Location"].rstrip("/").endswith("/app"), "no desk without the new look")
for url in ("/app/desk/plan", "/app/desk/quote", "/app/desk/run"):
    check(jpost(a, url, {"ask": SAID, "product": pid, "parts": [], "total": 0}).status_code == 404, f"{url} is not there")
check("data-desk" not in page(a, "/app") and "desk.js" not in page(a, "/app"), "Home is unchanged")

print("== new look on: where the desk lives")
os.environ["UI_V2"] = "1"
home = page(a, "/app")
check("data-desk" not in home and "CFY_STUDIO" in home, "the first run keeps Studio as Home")
check('href="/app/desk"' in home and "<span>Desk</span></a>" in home, "the desk has its own link above the tabs")
d = page(a, "/app/desk")
check("data-desk" in d and "What are we launching?" in d and "desk.js" in d, "the desk opens at /app/desk")
check(re.search(r'<a class="nav v2-desk on" href="/app/desk" aria-current=page>', d) is not None
      and re.search(r'<a class="nav v2-desk " href="/app/desk" >', home) is not None, "and it is marked as where you are, only on the desk")
check("Burns for 40 hours. Hand poured in Texas" in d and "Free shipping this week" in d
      and "People who light a candle every evening" in d, "the brand panel shows the claims, the offer and who it is for")
check("Start from an example" in d and d.count('data-try="') == len(desk.EXAMPLES) and "Launch kit" in d
      and 'data-try="Launch Ember Candle: three image ads, one presenter video, the ad copy, a page to collect emails and a follow-up"' in d,
      "example cards to start from, each with a sentence written for the product")
check(d.count('class="dk-ex-media"><img src="/static/showcase/') == len(desk.EXAMPLES) and 'src="/static/showcase/team-skincare.mp4"' in d,
      "every card shows a picture from our own gallery, and a clip where there is one")
check("New campaign" in d and ">Campaigns</h2>" in d and "None yet." in d, "the sidebar starts a campaign and lists them (none yet)")
boot = json.loads(re.search(r"window\.CFY_DESK = (\{.*?\});</script>", d, re.S).group(1))
check(boot["product"] == pid and boot["urls"]["run"] == "/app/desk/run" and boot["starters"]["image"]
      and {x["kind"] for x in boot["add"]} >= {"image", "copy", "page", "flow"}, "the page knows the product and what can be added")
check(all(x["locked"] == "Starter" for x in boot["add"] if x["kind"] in ("copy", "page", "flow")),
      "on the free plan the campaign tools are offered with the plan that has them")

print("== the plan")
llm.configured = lambda: True
llm.structured = lambda **kw: (calls.append(kw), answer[0])[1]
answer[0] = ADS
r = jpost(a, "/app/desk/plan", {"ask": "x", "product": pid})
check(r.status_code == 400 and not calls, "a sentence too short to plan from is refused before the model")
r = jpost(a, "/app/desk/plan", {"ask": "Ads that promise a guaranteed income of $5,000 a month", "product": pid})
check(r.status_code == 400 and not calls and "parts" not in r.get_json(), "a claim the ads can't make is refused before the model")
p = jpost(a, "/app/desk/plan", {"ask": SAID, "product": pid}).get_json()
k = by_kind(p["parts"])
check(len(calls) == 1 and p["planned"] and p["note"] == ADS["note"], "one model call, and its reason comes back")
check([x["name"] for x in k["image"]] == ["3 image ads", "Image ad"] and all(x["on"] and x["stage"] == "make" for x in k["image"]),
      "the ads it proposed are rows, ticked")
check([x["kind"] for x in p["parts"]] == ["image", "image", "copy", "page", "flow", "launch"], "the rows are in the order a campaign happens")
check(all(k[x][0]["locked"] == "Starter" and not k[x][0]["on"] and k[x][0]["label"] == "Starter plan" for x in ("copy", "page", "flow")),
      "free plan: the copy, the page and the follow-up are shown with their plan and can't be ticked")
one = catalog.price_credits("ad_image", batch=1)
sq = jpost(a, "/app/studio/quote", {"plan": ADS["parts"], "product": pid}).get_json()
check(p["ads"] == sq["total"] == p["total"] and k["image"][0]["price"] + k["image"][1]["price"] == sq["total"] and one,
      "the ads cost what Studio quotes for the same set")
check(p["left"] == studio_v2.ASK_DAILY - 1 and p["balance"] == bal("a@example.com") and p["short"] == 0, "the ask is counted, the balance is shown")
check(p["url"] == f"/app/desk/c/{p['thread']}#t{p['turn']}" and count("desk_threads", "a@example.com") == 1
      and count("desk_turns", "a@example.com") == 2, "the sentence started a campaign: a conversation with what was said and the plan")
check(count("desk_threads", "a@example.com") == 1 and "This continues a campaign" not in calls[-1]["prompt"],
      "a first sentence is planned with no earlier campaign in mind")

set_plan("a@example.com", "growth")
p = jpost(a, "/app/desk/plan", {"ask": SAID, "product": pid}).get_json()
k = by_kind(p["parts"])
check(k["copy"][0]["on"] and k["page"][0]["on"] and k["flow"][0]["on"] and not k["launch"][0]["on"],
      "on a plan that has them, what the sentence asked for is ticked and the launch step is offered unticked")
est = copywriter.estimate(3, "ad")
check(k["copy"][0]["price"] == est and k["copy"][0]["label"].startswith("Up to ") and p["copy"] == est and p["total"] == p["ads"] + est,
      "the copy is priced at the most it can cost, and that is in the total")
check(k["page"][0]["label"] == "In your plan" and "sign-up form" in k["page"][0]["name"] and "draft" in k["page"][0]["words"]
      and "switched off" in k["flow"][0]["words"], "the page and the follow-up say they are in the plan, a draft, switched off")
check(not any(x["href"] in ("/app/copy", "/app/pages", "/app/flows") for x in p["also"]), "a tool that is a row is not also a link")

answer[0] = {"note": "", "parts": []}
p2 = jpost(a, "/app/desk/plan", {"ask": "A page to collect emails and a follow-up", "product": pid}).get_json()
k2 = by_kind(p2["parts"])
check("image" not in k2 and k2["page"][0]["on"] and k2["flow"][0]["on"] and not k2["copy"][0]["on"] and "No ads in this one" in p2["note"]
      and p2["total"] == 0, "no ads asked for: the page and the follow-up come back, ticked, for no credits")
answer[0] = ADS
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (studio_v2._ask_key(ws_of("a@example.com")["id"]), str(studio_v2.ASK_DAILY)))
    conn.commit()
n = len(calls)
p3 = jpost(a, "/app/desk/plan", {"ask": SAID, "product": pid})
check(p3.status_code == 200 and len(calls) == n and "asks today" in p3.get_json()["note"] and by_kind(p3.get_json()["parts"])["page"][0]["on"],
      "at the daily cap the model isn't called, and the rest of the campaign still comes back")
with db.connect() as conn:
    conn.execute("DELETE FROM meta WHERE key=?", (studio_v2._ask_key(ws_of("a@example.com")["id"]),))
    conn.commit()

print("== pricing again")
n = len(calls)
parts = p["parts"]
parts[1]["on"] = False
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": parts}).get_json()
check(len(calls) == n and q["ads"] == k["image"][0]["price"] and q["total"] == q["ads"] + est and q["count"] == 4,
      "unticking a row re-prices with no model call")
parts[0].update(text="  A candle for every   evening ", count=2, price=1, name="hacked", stage="launch", locked="")
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": parts}).get_json()
first = q["parts"][0]
same = jpost(a, "/app/studio/quote", {"plan": [{"make": "image", "text": "A candle for every evening", "count": 2}], "product": pid}).get_json()
check(first["name"] == "2 image ads" and first["stage"] == "make" and first["text"] == "A candle for every evening"
      and first["price"] == same["total"] != 1,
      "an edit is priced from the words and the count; a name, a stage or a price sent by the browser counts for nothing")
many = [{"id": f"m{i}", "kind": "image", "on": True, "text": f"Headline {i}", "count": 1} for i in range(8)]
many += [{"kind": "copy", "on": True}, {"kind": "copy", "on": True, "n": 5}, {"kind": "poster", "on": True}, "junk",
         {"id": "b1", "kind": "voiceover", "on": True, "text": ""}]
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": many}).get_json()
kq = by_kind(q["parts"])
check(len(kq["image"]) + len(kq.get("voiceover", [])) == studio_v2.ASK_MAX_PARTS and len(kq["copy"]) == 1 and "poster" not in kq,
      "five ad parts at most, one of each of the rest, and nothing that isn't a part")
blank = jpost(a, "/app/desk/quote", {"product": pid, "parts": [{"id": "b1", "kind": "voiceover", "on": True, "text": ""}]}).get_json()
check(blank["parts"][0]["on"] is False and blank["parts"][0]["label"] == "Add the words" and blank["total"] == 0,
      "a part with no words yet waits, unticked and unpriced")
b, pid_b = member("b@example.com")
check(jpost(a, "/app/desk/quote", {"product": pid_b, "parts": parts}).status_code == 400, "another workspace's product is refused")

print("== the campaign is a conversation")
T, PLAN = p["thread"], p["turn"]
tp = page(a, f"/app/desk/c/{T}")
tb = json.loads(re.search(r"window\.CFY_DESK = (\{.*?\});</script>", tp, re.S).group(1))
check("<h1 id=\"dk-h\">Holiday launch for the candle</h1>" in tp and desk.platform_in("for Instagram and TikTok") == "tiktok"
      and desk.platform_in("a few image ads") == "", "the campaign is named from the sentence; a platform is read from it when it names one")
check(SAID in tp and "data-plan-wrap" in tp and tb["thread"] == T and tb["plan"]["turn"] == PLAN
      and [x["kind"] for x in tb["plan"]["parts"]] == [x["kind"] for x in p["parts"]] and tb["plan"]["note"] == ADS["note"],
      "its page shows what was said and the plan, still open to edit")
edit = [dict(x) for x in tb["plan"]["parts"]]
edit[0].update(text="A candle for every evening", count=2)
jpost(a, "/app/desk/quote", {"product": pid, "parts": edit, "turn": PLAN})
tb2 = json.loads(re.search(r"window\.CFY_DESK = (\{.*?\});</script>", page(a, f"/app/desk/c/{T}"), re.S).group(1))
check(tb2["plan"]["parts"][0]["text"] == "A candle for every evening" and tb2["plan"]["parts"][0]["name"] == "2 image ads",
      "an edit to the open plan is still there after a reload")
check(jpost(b, "/app/desk/plan", {"ask": "Two more image ads please", "product": pid_b, "thread": T}).status_code == 404
      and b.get(f"/app/desk/c/{T}").status_code == 302 and count("desk_turns", "a@example.com") == 8,
      "another workspace can neither read nor add to it")
check(f'href="/app/desk/c/{T}"' in page(a, "/app/desk") and "Your campaigns" in page(a, "/app/desk"),
      "the desk and the sidebar list the campaign")

print("== making it")
parts = p["parts"]
parts[1]["on"] = True
q = jpost(a, "/app/desk/quote", {"product": pid, "parts": parts}).get_json()
before = bal("a@example.com")
r = jpost(a, "/app/desk/run", {"product": pid, "ask": SAID, "parts": q["parts"]})
check(r.status_code == 409 and r.get_json().get("needs_quote") and not jobs_of("a@example.com"), "no total, nothing starts")
r = jpost(a, "/app/desk/run", {"product": pid, "ask": SAID, "parts": q["parts"], "total": q["total"] - 1})
check(r.status_code == 409 and r.get_json()["total"] == q["total"] and not jobs_of("a@example.com") and count("pages", "a@example.com") == 0,
      "a total that isn't the one worked out here starts nothing and makes nothing")
written = []
copywriter.write = lambda brief, kind, platform, b_, n_, usage: (written.append((kind, platform, n_, b_)), [{"Primary text": "A candle for the evening."}] * n_)[1]
q["parts"][-1]["on"] = True                                  # the launch step too
q2 = jpost(a, "/app/desk/quote", {"product": pid, "parts": q["parts"]}).get_json()
r = jpost(a, "/app/desk/run", {"product": pid, "ask": SAID, "parts": q2["parts"], "total": q2["total"], "thread": T, "turn": PLAN})
out = r.get_json()
check(r.status_code == 200 and out["ok"] and out["thread"] == T and out["url"] == f"/app/desk/c/{T}#t{out['turn']}",
      "the right total starts it, as the next turn of the campaign")
jobs = jobs_of("a@example.com")
check(len(jobs) == 2 and len({j["batch_id"] for j in jobs}) == 1, "both image parts start (three ads in all), as one batch")
check(bal("a@example.com") == before - q2["ads"], "the ads are charged at the price shown; the copy's hold is settled at what it used (nothing here)")
check(written and written[0][:3] == ("ad", "meta", 3) and written[0][3]["offer"] == "Free shipping this week" and written[0][3]["notes"] == SAID,
      "the copy is written for the platform from the product's offer and the sentence")
with db.connect() as conn:
    ws = user("a@example.com")["workspace_id"]
    pg = conn.execute("SELECT * FROM pages WHERE workspace_id=?", (ws,)).fetchone()
    fm = conn.execute("SELECT * FROM forms WHERE workspace_id=?", (ws,)).fetchone()
    fl = conn.execute("SELECT * FROM flows WHERE workspace_id=?", (ws,)).fetchone()
    cr = conn.execute("SELECT * FROM copy_runs WHERE workspace_id=?", (ws,)).fetchone()
    dr = conn.execute("SELECT * FROM desk_runs WHERE workspace_id=?", (ws,)).fetchone()
blocks = json.loads(pg["blocks_json"])
check(pg["status"] == "draft" and pg["title"] == "Ember Candle" and any(bk.get("type") == "form" and bk.get("form") == fm["id"] for bk in blocks),
      "the page is a draft with the new sign-up form on it")
import flows as flows_mod  # noqa: E402
tree = flows_mod.unpack(fl["graph_json"]) if hasattr(flows_mod, "unpack") else json.loads(fl["graph_json"])
trig = (tree.get("tree") or tree).get("trigger") or {}
check(fl["status"] == "draft" and trig.get("form_id") == fm["id"], "the follow-up is saved switched off and starts from that form")
check(cr is not None and cr["kind"] == "ad" and cr["platform"] == "meta" and cr["product_id"] == pid, "the copy is kept as a run of the Copy tool")
made = json.loads(dr["made_json"])
check([m["kind"] for m in made] == ["ads", "copy", "page", "form", "flow", "launch"] and dr["ask"] == SAID and dr["credits"] == q2["ads"]
      and made[0]["name"] == "3 ads" and made[-1]["state"] == "Next", "what was made is kept, in order, with the launch step as what's next")
done = page(a, out["url"])
check("Started. Here is each part." in done and f'/app/pages/{pg["id"]}' in done and f'/app/flows/{fl["id"]}' in done
      and f'/app/copy/{cr["id"]}' in done and "In this campaign" in done and SAID in done, "the campaign shows what was made, each part a link")
check("data-plan-wrap" not in done and "Made." in done and "2 being made" in done and "data-busy" in done and dr["thread_id"] == T,
      "the plan is closed and shown as it was made; the ads still being made are shown as that")
check(a.get(f"/app/desk?done={dr['id']}").headers["Location"].endswith(f"/app/desk/c/{T}"), "an address from before conversations still finds the campaign")

n, nthreads = len(calls), count("desk_threads", "a@example.com")
answer[0] = {"note": "Two more on the gift angle.", "parts": [{"make": "image", "text": "The gift that smells like home", "count": 2}]}
f = jpost(a, "/app/desk/plan", {"ask": "Two more image ads on the gift angle, and the captions for TikTok", "product": pid, "thread": T}).get_json()
fk = by_kind(f["parts"])
check(f["thread"] == T and len(calls) == n + 1 and "This continues a campaign" in calls[-1]["prompt"] and SAID in calls[-1]["prompt"]
      and "Already made: 3 ads" in calls[-1]["prompt"] and "only what the new request adds" in calls[-1]["prompt"],
      "a later sentence is planned knowing what the campaign already holds")
check([x["kind"] for x in f["parts"]] == ["image", "copy"] and fk["copy"][0]["on"] and count("desk_threads", "a@example.com") == nthreads
      and fk["copy"][0]["platform"] == "tiktok" and fk["copy"][0]["name"] == "Ad copy for TikTok",
      "and answers with only what it adds: the ads and the copy it asked for, not every stage again")
later = page(a, f["url"])
lb = json.loads(re.search(r"window\.CFY_DESK = (\{.*?\});</script>", later, re.S).group(1))
check(lb["plan"]["turn"] == f["turn"] and later.count("data-plan-wrap") == 1 and "Two more on the gift angle." in json.dumps(lb["plan"]),
      "the new plan is the one open; the one before stays as it was made")
g2 = jpost(a, "/app/desk/plan", {"ask": "Actually make it three product photos instead", "product": pid, "thread": T}).get_json()
again = page(a, g2["url"])
check("Not made. A later plan replaced it." in again and again.count("data-plan-wrap") == 1, "a plan left unmade is marked as replaced by the next one")
answer[0] = {"note": "", "parts": []}
nn = jpost(a, "/app/desk/plan", {"ask": "What should we name the dog", "product": pid, "thread": T}).get_json()
check(not nn["parts"] and "couldn't turn that into a part of the campaign" in page(a, nn["url"]),
      "a sentence that adds nothing gets a plain answer in the conversation")
answer[0] = ADS
with db.connect() as conn:
    wsid = user("a@example.com")["workspace_id"]
    conn.execute("INSERT INTO desk_runs (workspace_id, user_id, product_id, ask, made_json, credits, created_at) VALUES (?,?,?,?,?,?,?)",
                 (wsid, user("a@example.com")["id"], pid, "An older campaign from before", json.dumps([{"kind": "page", "name": "Page: Old", "state": "Draft", "href": "/app/pages/1"}]), 0, db.now()))
    conn.commit()
home2 = page(a, "/app/desk")
check("An older campaign from before" in home2 and count("desk_threads", "a@example.com") == nthreads + 1 and "Page: Old" in home2,
      "a campaign made before conversations existed becomes one, with what it made")
check("data-desk" in page(a, "/app") and "CFY_STUDIO" in page(a, "/app?mode=image") and "CFY_STUDIO" in page(a, "/app/studio"),
      "now that something was made, Home is the desk; a link that asks Studio for something still gets Studio")
check("Waiting for you" in home2 and "being made" in home2, "what is waiting is listed on the desk: the ads being made")

print("== the plan decides what can be made")
c, pid_c = member("c@example.com")
free = [{"id": "p1", "kind": "page", "on": True}, {"id": "p2", "kind": "flow", "on": True}, {"id": "p3", "kind": "copy", "on": True}]
q = jpost(c, "/app/desk/quote", {"product": pid_c, "parts": free}).get_json()
check(q["count"] == 0 and all(not x["on"] and x["locked"] for x in q["parts"]), "free plan: a request that ticks them anyway gets them back unticked")
r = jpost(c, "/app/desk/run", {"product": pid_c, "ask": SAID, "parts": free, "total": 0})
check(r.status_code == 400 and count("pages", "c@example.com") == 0 and count("flows", "c@example.com") == 0 and len(written) == 1,
      "and nothing is made")
r = jpost(c, "/app/desk/run", {"product": pid_c, "ask": SAID, "total": 10 ** 6,
                                 "parts": [{"id": "v1", "kind": "image", "on": True, "text": "A candle", "count": 4}] * 1})
check(r.status_code == 409 and not jobs_of("c@example.com"), "a made-up total starts nothing")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET credits=0, plan_credits=0 WHERE id=?", (user("c@example.com")["workspace_id"],))
    conn.commit()
q = jpost(c, "/app/desk/quote", {"product": pid_c, "parts": [{"id": "v1", "kind": "image", "on": True, "text": "A candle", "count": 4}]}).get_json()
r = jpost(c, "/app/desk/run", {"product": pid_c, "ask": SAID, "parts": q["parts"], "total": q["total"]})
check(q["short"] == q["total"] > 0 and r.status_code == 402 and r.get_json().get("billing") and not jobs_of("c@example.com"),
      "not enough credits: it says how many are missing and nothing starts")

print("== Studio's own Ask is the same call")
r = jpost(a, "/app/studio/ask", {"ask": SAID, "product": pid}).get_json()
check([x["name"] for x in r["plan"]] == ["3 image ads", "Image ad"] and r["quote"]["total"] == sq["total"], "the composer's Ask answers as before")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {passed} desk checks passed")
