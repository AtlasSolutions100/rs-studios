"""Quality wave 1 (the Campaignify audit's bugs and quick wins), on a throwaway database:

  * the planner and the copy studio never invent a link: the product's saved page (then the brand's website) is the
    fallback destination, "no link was given" is said out loud, and any address not from the brief, product or brand
    becomes "[your link]" in copy and in the finished plan
  * MCP: an asset's and a proposal's type comes from params.ad_type first; a snapshot lists the competitors the
    research found when the member typed none, and reads the "website" key
  * presenter videos keep the product on the table by default (holding is still selectable) and ask for the
    unpolished phone look; every photo-to-video prompt ends with the keep-the-product line
  * the first run: two free image ads are two different templates that suit the product; the free video preview is
    a voiceover over the product, at the same cost as before
  * the customer-voice rule catches first-person use and ownership ("I take this mug on every hike")
  * scripts: hook types are named in the prompt and come back with each hook
  * download, export and "make more like this" are logged per job with its ad type and template
  * "Your ads are ready": one email when a member's first batch finishes
  * one presenter-to-voice map

No network: the vendor, the model and the email provider are faked.

    python tests/test_quality_wave1.py      (tests/test_app.py runs it too)
"""
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
TMP = tempfile.mkdtemp(prefix="rs_qw1_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("FIRST_RUN_NUDGES", "SIGNUP_CREDITS_ADS", "ADS_READY_EMAIL", "RS_STARTER_CREDITS", "RS_FREE_PREVIEW",
          "RS_TALKING", "RS_SERVICE_PATH", "OPENAI_API_KEY"):
    os.environ.pop(k, None)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.campaignify.ai", RS_PUBLIC_URL="https://campaignify.test",
                  ANTHROPIC_API_KEY="sk-ant-test", MCP_SERVER="1", RS_DISK_MIN_GB="0.2")

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import compliance  # noqa: E402
import copywriter  # noqa: E402
import create_routes  # noqa: E402
import creator  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import llm  # noqa: E402
import mailer  # noqa: E402
import mcp_tools  # noqa: E402
import planner  # noqa: E402
import preview  # noqa: E402
import ready_email  # noqa: E402
import site_events  # noqa: E402
import worker  # noqa: E402

passed = 0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
VOICE = "AI presenters can't speak as customers or claim results."


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
    c.environ_base["REMOTE_ADDR"] = f"10.21.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.environ_base["HTTP_USER_AGENT"] = UA
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def png(n=64):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (n, n), (200, 120, 40)).save(b, "PNG")
    b.seek(0)
    return b


def signup(email, verified=True, plan=None):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if verified:
            conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        if plan:
            conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, u["workspace_id"]))
        conn.commit()
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return c, u


def jobs_of(ws_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE workspace_id=? ORDER BY id", (ws_id,)).fetchall()


def events(ws_id):
    with db.connect() as conn:
        return [(r["name"], r["detail"]) for r in conn.execute(
            "SELECT name, detail FROM site_events WHERE workspace_id=? ORDER BY id", (ws_id,))]


sent = []


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": f"re_{len(sent)}"}


mailer.requests.post = lambda url, json=None, headers=None, timeout=0: (sent.append(json) or _Resp())


def _no_vendor(*a, **k):
    raise AssertionError("this test must never call the vendor")


# Nothing here may reach the vendor: uploads are faked, and a submit, status or estimate call fails the test.
hf.upload = lambda data, ctype: "https://files.example/up.png"
hf.submit = hf.status = hf.estimate = _no_vendor

try:
    # ------------------------------------------------------------------ 1. links
    print("== the destination link: the product's page, then the brand's site, else '[your link]'")
    kit = {"name": "Trail Co", "product": "Trail Mug", "url": "https://trailco.com/mug", "website": "https://trailco.com"}
    bt = copywriter.brief_text(kit, {"offer": "Trail Mug"})
    check("Destination link (the only web address you may use): https://trailco.com/mug" in bt,
          "no link in the brief: the product's saved page is the destination")
    check("https://shop.example/x" in copywriter.brief_text(kit, {"link": "https://shop.example/x"})
          and "trailco.com/mug" not in copywriter.brief_text(kit, {"link": "https://shop.example/x"}),
          "a link typed in the brief wins")
    check(copywriter.destination_link({"name": "T", "url": "", "website": "https://trailco.com"}, {}) == "https://trailco.com",
          "no product page: the brand's website")
    none = copywriter.brief_text({"name": "Trail Co", "product": "Trail Mug"}, {"offer": "Trail Mug"})
    check("Destination link: none was given" in none and "[your link]" in none and "Do not make one up" in none,
          "no link anywhere: the writer is told so and told to write [your link]")
    check("Never invent a web address" in copywriter.RULES and "[your link]" in copywriter.RULES
          and "Never invent a web address" in planner.SYSTEM and "no link was given" in planner.SYSTEM,
          "the rule is in the copy rules and in the planner's system prompt")

    allowed = copywriter.allowed_hosts(kit, {"notes": "Our sizing guide is at help.trailco.com/sizes"})
    for text, want, n in (
            ("Shop now at www.trailmugs-official.com today.", "Shop now at [your link] today.", 1),
            ("Order at https://trailco.com/mug?utm=x, ships free.", "Order at https://trailco.com/mug?utm=x, ships free.", 0),
            ("See TrailCo.com or help.trailco.com/sizes.", "See TrailCo.com or help.trailco.com/sizes.", 0),
            ("Visit getyourmug.io/shop and mugdeals.co!", "Visit [your link] and [your link]!", 2),
            ("Questions? hello@madeup-mail.com answers them.", "Questions? hello@madeup-mail.com answers them.", 0),
            ("Set the pixel up in business.facebook.com first.", "Set the pixel up in business.facebook.com first.", 0),
            ("Buy it in the shop.Shop now, it fits 12 oz. Use it daily.It works.",
             "Buy it in the shop.Shop now, it fits 12 oz. Use it daily.It works.", 0),
            ("Link in bio: [your link]", "Link in bio: [your link]", 0)):
        got = copywriter.strip_unknown_links(text, allowed)
        check(got == (want, n), f"post-check: {text!r} -> {got[0]!r}")
    check(copywriter.strip_unknown_links("Go to trailco.com", copywriter.allowed_hosts({"name": "x"}, {}))
          == ("Go to [your link]", 1), "with no link given, even a plausible domain is replaced")

    print("== copy studio output goes through the post-check")
    _structured = llm.structured

    def fake_copy(**kw):
        return {"variants": [{"angle": "Trail", "fields": [
            {"name": "Primary text", "text": "Hot for six hours. Get yours at www.trailmug-store.com."},
            {"name": "Headline", "text": "The mug for the trail"}]}]}

    llm.structured = fake_copy
    v = copywriter.write({"name": "Trail Co", "product": "Trail Mug"}, "ad", "meta", {"offer": "Trail Mug"}, 1)[0]
    joined = " ".join(f["text"] for f in v["fields"])
    check("trailmug-store" not in joined and "Get yours at [your link]." in joined and v.get("links_replaced") == 1,
          "an invented address in a copy variant becomes [your link], and the variant says so")
    v = copywriter.write(dict(kit, url="https://www.trailmug-store.com/"), "ad", "meta", {"offer": "Trail Mug"}, 1)[0]
    check("www.trailmug-store.com" in " ".join(f["text"] for f in v["fields"]) and "links_replaced" not in v,
          "the product's own address is left alone")
    llm.structured = _structured

    print("== the planner: the brief carries the fallback link, and finish can't keep an invented one")
    pc, pu = signup("planner@example.com", plan="starter")
    pws = pu["workspace_id"]
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET credits=5000 WHERE id=?", (pws,))
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (pws,)).fetchone()
        import products
        pid = products.save_kit(conn, ws, brand_name="Trail Co", product_name="Trail Mug", audience="Hikers")
        conn.execute("UPDATE products SET url='https://trailco.com/mug' WHERE id=?", (pid,))
        brand_id = conn.execute("SELECT brand_id FROM products WHERE id=?", (pid,)).fetchone()[0]
        cid = conn.execute("INSERT INTO campaigns (workspace_id, user_id, brand_id, product_id, name, brief_json, status, "
                           "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (pws, pu["id"], brand_id, pid, "Mug launch", json.dumps({"offer": "Trail Mug", "goal": "sales",
                                                                                    "platforms": ["meta"]}),
                            "planning", db.now(), db.now())).lastrowid
        cid2 = conn.execute("INSERT INTO campaigns (workspace_id, user_id, name, brief_json, status, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (pws, pu["id"], "No link", json.dumps({"offer": "Trail Mug", "goal": "sales", "platforms": ["meta"]}),
                             "planning", db.now(), db.now())).lastrowid
        conn.commit()
    prompts = []

    def fake_chat(*, system, messages, tools=None, tool_choice=None, max_tokens=2000, usage=None, temperature=0.7,
                  model_id=None, timeout=None):
        prompts.append(messages[0]["content"] if isinstance(messages[0]["content"], str)
                       else messages[0]["content"][0]["text"])
        return {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "save_angles", "input": {"angles": [
                {"name": "Trail mornings", "insight": "i", "audience": "a", "why": "w"}]}},
            {"type": "tool_use", "id": "t2", "name": "save_creative", "input": {
                "angle": "Trail mornings", "hooks": ["Cold coffee at the summit?"],
                "script": "Show the mug, then the link trailmug-official.com on screen.",
                "on_screen_text": ["trailmug-official.com"]}},
            {"type": "tool_use", "id": "t3", "name": "finish", "input": {
                "summary": "Send people to https://www.trailmug.shop/launch and retarget.",
                "offer": "A steel mug. See trailco.com/mug for the price.",
                "test_plan": "Two weeks. Landing page: gettrailmug.com/offer.",
                "kpis": ["Clicks to trailmug.co"],
                "budget_split": [{"platform": "meta", "share_percent": 100, "note": "to trailmugs.net"}],
                "checklist": [{"title": "Check the page loads", "detail": "Open https://trail-mug-co.com/checkout",
                               "platform": "all"},
                              {"title": "Install the pixel", "detail": "In business.facebook.com", "platform": "meta"}]}}]}

    _chat, llm.chat = llm.chat, fake_chat
    check(planner.run(cid, free=True) == "ready", "the plan finishes")
    check("Destination link (the only web address you may use): https://trailco.com/mug" in prompts[-1],
          "the planner's brief names the product's saved page as the link")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    plan, steps = json.loads(row["plan_json"]), json.loads(row["steps_json"])
    fin = plan["final"]
    blob = json.dumps(plan)
    check(all(bad not in blob for bad in ("trailmug.shop", "gettrailmug.com", "trailmug.co", "trailmugs.net",
                                          "trail-mug-co.com", "trailmug-official.com")),
          "no invented address survives anywhere in the plan (summary, test plan, KPIs, budget notes, checklist, creative)")
    check(fin["summary"] == "Send people to [your link] and retarget." and "trailco.com/mug" in fin["offer"]
          and "business.facebook.com" in blob and fin["checklist"][0]["detail"] == "Open [your link]",
          "they read [your link]; the product's own page and the ad platform's site are kept")
    check(any(s["title"] == "Took out a link that wasn't yours" for s in steps), "and the member is told in the plan's steps")
    planner.run(cid2, free=True)
    with db.connect() as conn:
        row2 = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid2,)).fetchone()
    check("Destination link: none was given" in prompts[-1]
          and "trailco.com/mug" not in json.loads(row2["plan_json"])["final"]["offer"]
          and "[your link]" in json.loads(row2["plan_json"])["final"]["offer"],
          "a plan with no product and no link: told there is none, and even trailco.com is replaced")
    llm.chat = _chat

    # ------------------------------------------------------------------ 2 + 3. MCP
    print("== MCP: asset and proposal types from params.ad_type; snapshot competitors")
    check(catalog.job_ad_type("ugc_video", {"ad_type": "voiceover"}) == "voiceover"
          and catalog.job_ad_type("ugc_video", '{"ad_type": "ugc"}') == "ugc"
          and catalog.job_ad_type("ugc_video", {}) == "ugc" and catalog.job_ad_type("ad_image", None) == "image"
          and catalog.job_ad_type("ugc_video", {"ad_type": "nonsense"}) == "ugc"
          and catalog.job_ad_type("video_upscale", "{bad json") == "video_upscale",
          "job_ad_type: params first, then the tool's type, then the tool")

    class Ctx:
        account_id = pws

        class _As:
            def __enter__(self):
                self.conn = db.connect().__enter__()
                return self.conn

            def __exit__(self, *a):
                self.conn.close()

        def as_user(self):
            return self._As()

        def must(self, method, path):
            return type("R", (), {"data": {"status": "ready"}})()

    with db.connect() as conn:
        for tool, prm in (("ugc_video", {"ad_type": "voiceover", "voice": "narrator_deep"}),
                          ("ugc_video", {"ad_type": "ugc", "presenter": "maya"}), ("ad_image", {"batch": 1})):
            conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                         "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (pws, pu["id"], tool, "x", json.dumps(prm), "done", 0, 0, db.now(), db.now()))
        for tool, prm in (("ugc_video", {"ad_type": "voiceover"}), ("ugc_video", {"ad_type": "ugc"}),
                          ("photo_to_video", {})):
            conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, brief, params_json, "
                         "credits, cost_usd, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (cid, pws, "a", "meta", tool, "b", json.dumps(prm), 1, 0.1, "proposed", db.now()))
        conn.commit()
    assets = mcp_tools.list_assets(Ctx(), {})["assets"]
    check([a["type"] for a in assets] == ["image", "ugc", "voiceover"],
          "list_assets: a voiceover job says voiceover, not ugc")
    props = mcp_tools.get_plan(Ctx(), {"campaign_id": cid})["proposed_ads"]
    check([a["type"] for a in props] == ["voiceover", "ugc", "product_video"] and all("params_json" not in a for a in props),
          "get_campaign_plan: proposals carry their real type (and not the raw params)")

    typed = json.dumps({"competitors": [{"name": "Rival One", "website": "https://rival1.example/"},
                                        {"name": "", "website": "https://rival2.example/"}]})
    found = json.dumps({"competitors": [{"name": "Found Co", "website": "https://found.example/"},
                                        {"name": "", "website": "https://nameless.example/"}]})
    check(mcp_tools.snapshot_competitors(typed, found) == ["Rival One", "https://rival2.example/"],
          "typed competitors: their names, and the website (not null) for one with no name")
    check(mcp_tools.snapshot_competitors(json.dumps({"competitors": []}), found) == ["Found Co", "https://nameless.example/"]
          and mcp_tools.snapshot_competitors("{}", found) == ["Found Co", "https://nameless.example/"],
          "a 'find them' run: the names the research found, from the report")
    check(mcp_tools.snapshot_competitors(None, None) == [] and mcp_tools.snapshot_competitors("{bad", "[]") == [],
          "nothing yet (still running) or unreadable JSON: an empty list")
    with db.connect() as conn:
        conn.execute("INSERT INTO competitor_reports (workspace_id, user_id, status, inputs_json, report_json, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?,?)", (pws, pu["id"], "ready", json.dumps({"competitors": []}), found,
                                                           db.now(), db.now()))
        conn.commit()
    _need, mcp_tools._need = mcp_tools._need, (lambda *a, **k: None)
    snaps = mcp_tools.list_snapshots(Ctx(), {})["snapshots"]
    mcp_tools._need = _need
    check(snaps[0]["competitors"] == ["Found Co", "https://nameless.example/"], "list_competitor_snapshots shows them")

    # ------------------------------------------------------------------ 4 + 5 + 11. prompts and the voice map
    print("== presenter prompt: product on the table, unpolished look; holding still selectable")
    tp = catalog.talking_prompt("ugc", "Trail Mug", "Meet the Trail Mug.", "maya", "warm_f")
    check("on the table beside them" in tp and "hands stay off the product" in tp and "holds the Trail Mug" not in tp,
          "default: the product stands on the table beside the presenter, hands off")
    for phrase in ("natural skin texture", "slight softness", "ordinary lived-in room", "handheld phone framing",
                   "No slow motion", "no studio lighting"):
        check(phrase in tp, f"unpolished look: {phrase}")
    check('"Meet the Trail Mug."' in tp and "curly dark hair" in tp and "warm, friendly female voice" in tp
          and "exactly as in the reference photos" in tp and "Lip movements match" in tp,
          "the script, presenter, voice and product-fidelity line are all still there")
    th = catalog.talking_prompt("ugc", "Trail Mug", "Hi.", "maya", "warm_f", placement="holding")
    check("holds the Trail Mug up to the camera" in th and "on the table beside" not in th and "natural skin texture" in th,
          "placement=holding: the old in-hand look, with the unpolished lines too")
    vo = catalog.talking_prompt("voiceover", "Trail Mug", "Hi.", voice_id="narrator_bright")
    vp = catalog.talking_prompt("voiceover", "Trail Mug", "Hi.", voice_id="narrator_bright", placement="product_only")
    check("hands using it" in vo and "No people and no hands in frame" in vp and "hands using it" not in vp
          and "narrator" in vp and "skin texture" not in vp, "voiceover: unchanged by default; product_only keeps hands out")

    print("== one presenter-to-voice map")
    check(set(catalog.PRESENTER_VOICE) == set(catalog.PRESENTER_BY_ID)
          and all(v in catalog.VOICE_BY_ID for v in catalog.PRESENTER_VOICE.values()),
          "every presenter has a default voice, and it is a real voice")
    check(all(create_routes._default_voice(p) == catalog.default_voice(p) for p in catalog.PRESENTER_BY_ID)
          and catalog.default_voice("jordan") == "chatty_m" and catalog.default_voice("nobody") == catalog.VOICES[0]["id"],
          "Create reads the same map; an unknown presenter gets the first voice")
    src = open(os.path.join(ROOT, "planner.py"), encoding="utf-8").read() + open(os.path.join(ROOT, "create_routes.py"),
                                                                             encoding="utf-8").read()
    check('"maya": "warm_f"' not in src and "catalog.default_voice(pr)" in src, "the planner and Create no longer carry a copy")

    print("== the fidelity line on every photo-to-video prompt")
    check(catalog.with_fidelity("A mug on a rock at dawn") == "A mug on a rock at dawn. " + catalog.KEEP_PRODUCT
          and catalog.with_fidelity("Mug turns. " + catalog.KEEP_PRODUCT).count(catalog.KEEP_PRODUCT) == 1
          and catalog.with_fidelity("") == catalog.KEEP_PRODUCT and "label" in catalog.KEEP_PRODUCT and "logo" in catalog.KEEP_PRODUCT,
          "with_fidelity appends it once, to any scene")
    fc, fu = signup("fidelity@example.com")
    with db.connect() as conn:
        f = os.path.join(TMP, "p.png")
        with open(f, "wb") as fh:
            fh.write(png().getvalue())
        ph = conn.execute("INSERT INTO photos (workspace_id, path, filename, content_type, remote_url, created_at) "
                          "VALUES (?,?,?,?,?,?)", (fu["workspace_id"], f, "p.png", "image/png", "https://files.example/p.png",
                                                    db.now())).lastrowid
        built = {}
        for label, brief in (("typed", "the mug steams on a windowsill"), ("template", catalog.fill(
                catalog.TEMPLATE_BY_ID["turn"]["brief"], {"product": "Trail Mug"}))):
            jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                               "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (fu["workspace_id"], fu["id"], "photo_to_video", brief, json.dumps({"photo_ids": [ph], "duration": 5}),
                                "pending", 0, 0, db.now(), db.now())).lastrowid
            built[label] = worker.build_inputs(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        ujid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (fu["workspace_id"], fu["id"], "ugc_video", "Meet the mug.", json.dumps(
                                {"photo_ids": [ph], "duration": 8, "ad_type": "ugc", "presenter": "maya", "voice": "warm_f",
                                 "product": "Trail Mug", "placement": "holding"}), "pending", 0, 0, db.now(), db.now())).lastrowid
        ugc_in = worker.build_inputs(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (ujid,)).fetchone())
        conn.commit()
    check(built["typed"]["prompt"] == "the mug steams on a windowsill. " + catalog.KEEP_PRODUCT,
          "a hand-typed scene gets the line")
    check(built["template"]["prompt"].endswith(catalog.KEEP_PRODUCT) and "slowly circles it" in built["template"]["prompt"]
          and built["template"]["image_url"] == "https://files.example/p.png", "so does a template's scene")
    check("holds the Trail Mug up to the camera" in ugc_in["prompt"], "the worker passes params.placement to the prompt")
    _exp_ok = True
    with appmod.app.test_request_context("/app/create/quote"):
        from flask import g
        with db.connect() as conn:
            g.user = conn.execute("SELECT * FROM users WHERE id=?", (fu["id"],)).fetchone()
            g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (fu["workspace_id"],)).fetchone()
            pidf = products.save_kit(conn, g.ws, brand_name="Trail Co", product_name="Trail Mug", audience="")
            conn.execute("UPDATE photos SET product_id=? WHERE id=?", (pidf, ph))
            conn.commit()
            g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (fu["workspace_id"],)).fetchone()
            base = {"type": "ugc", "product": pidf, "items": ["Meet the Trail Mug."], "presenters": ["marcus"],
                    "duration": 8, "aspects": ["9:16"]}
            _, plain = create_routes.expand(conn, base)
            _, held = create_routes.expand(conn, dict(base, placement="holding"))
            try:
                create_routes.expand(conn, dict(base, items=["I take this mug on every hike."]))
                refused = ""
            except create_routes.ConfigError as e:
                refused = str(e)
    check("placement" not in plain[0]["params"] and held[0]["params"]["placement"] == "holding"
          and plain[0]["params"]["voice"] == "calm_m", "Create: table by default, holding when asked; voice from the one map")
    check(VOICE in refused, "Create refuses a presenter script in a customer's voice: 'I take this mug on every hike.'")
    check(mcp_tools._ad_schema(("ugc",), True)["properties"]["placement"]["enum"] == ["table", "holding"] and mcp_tools._ad_config(
        {"ad_type": "ugc", "product_id": 1, "lines": ["x"], "placement": "holding"})["placement"] == "holding",
        "MCP: generate_video_ads accepts placement")
    check("holds your product" not in catalog.AD_TYPE_BY_ID["ugc"]["line"], "the ad type's one-line description matches")

    # ------------------------------------------------------------------ 7. customer voice
    print("== the customer-voice rule: first-person use, ownership and experience")
    for line in ("I take this mug on every hike.", "I've been using this for a month and love it.",
                 "I’ve used this every day since March.", "My skin has never looked better.", "It changed my life.",
                 "I swear by it.", "Every morning I drink this before work.", "I always bring it to the gym.",
                 "I bought this last year.", "I love this mug.", "I have been taking these daily.",
                 "I can't live without it.", "Honestly, it worked for me.", "I lost ten pounds.", "My results speak.",
                 "I switched to this and never looked back.", "I'm obsessed with this serum.",
                 "I use it every single night.", "I started using this in January."):
        check(VOICE in compliance.check(line)["warnings"] and not creator.ok(line, spoken=True), f"flagged: {line}")
    for line in ("Meet the Trail Mug. Here's what it is and why it's worth a look.", "I'll show you this in ten seconds.",
                 "Take this mug on every hike.", "We use only organic beans, every day.", "I think you'll like it.",
                 "If your skin feels dry, this is a simple cream.", "You can take it everywhere.",
                 "We've been roasting since 1990.", "Use it daily.", "Let me show you my favorite detail on the label.",
                 "I use a kettle every morning, and this sits right next to it.", "Here's why I'd put this on every desk.",
                 "Your mornings, sorted.", "It keeps coffee hot for six hours."):
        check(VOICE not in compliance.check(line)["warnings"] and creator.ok(line, spoken=True), f"fine: {line}")
    check(compliance.check("I take this mug on every hike.")["ok"],
          "it stays a warning for text in general (only spoken presenter lines are refused on it)")
    check(compliance.IDS[-1] == "ai_customer_voice" and len(compliance.IDS) == len(compliance.RULES),
          "the rule keeps its id for the launch pre-flight")
    check(VOICE not in compliance.check(preview.line_for("Trail Mug"))["warnings"], "the free preview's default line passes")

    # ------------------------------------------------------------------ 8. hook types
    print("== scripts: named hook types in the prompt, a type back with each hook")
    seen = {}

    def fake_script(**kw):
        seen.update(kw)
        return {"hooks": [{"type": "question", "text": "Still drinking cold coffee by ten?"},
                          {"type": "bold_claim", "text": "I lost 10 pounds with this mug."},
                          {"type": "pov", "text": "POV: the trailhead, and your coffee is still hot."},
                          {"type": "made_up_type", "text": "This mug holds twelve ounces."}],
                "body": "The Trail Mug keeps it hot. Take a look.", "on_screen": ["Still hot"]}

    llm.structured = fake_script
    out = creator.script({"name": "Trail Co", "product": "Trail Mug", "claims": ""}, "ugc", {"title": "Trail"}, 10, 4)
    for tid, (name, _) in creator.HOOK_TYPES.items():
        check(tid in seen["prompt"] and name in seen["prompt"], f"the prompt names the hook type: {name}")
    check(set(creator.HOOK_TYPES) == {"problem_callout", "bold_claim", "question", "pov", "before_after", "social_proof",
                                      "curiosity_gap", "demo_first"}, "eight hook types")
    hooks_schema = seen["schema"]["properties"]["hooks"]["items"]
    check(hooks_schema["properties"]["type"]["enum"] == list(creator.HOOK_TYPES) and hooks_schema["required"] == ["type", "text"],
          "the schema asks for {type, text} per hook")
    check(out["items"] == ["Still drinking cold coffee by ten?", "POV: the trailhead, and your coffee is still hot.",
                           "This mug holds twelve ounces."]
          and out["hook_types"] == [{"id": "question", "name": "Question"}, {"id": "pov", "name": "POV"},
                                    {"id": "", "name": ""}],
          "each hook that passes the rules comes back with its type; a dropped hook takes its type with it; an "
          "unknown type is blank")
    llm.structured = lambda **kw: {"hooks": ["Still drinking cold coffee by ten?"], "body": "Take a look."}
    old = creator.script({"name": "T", "product": "Trail Mug", "claims": ""}, "voiceover", {"title": "Trail"}, 10, 1)
    check(old["items"] == ["Still drinking cold coffee by ten?"] and old["hook_types"] == [{"id": "", "name": ""}],
          "hooks as plain strings (an older reply shape) still work")
    llm.structured = lambda **kw: {"headlines": ["Hot for hours"], "concept": "A mug on a rock."}
    check("hook_types" not in creator.script({"name": "T", "product": "Trail Mug", "claims": ""}, "image", {"title": "x"}),
          "image scripts are unchanged")
    llm.structured = _structured
    page = open(os.path.join(ROOT, "web", "create.html"), encoding="utf-8").read()
    check("data-hook-type" in page and "S.hookTypes = r.hook_types" in page, "Create shows the type beside each written hook")

    # ------------------------------------------------------------------ 6. the first run
    print("== first run: two different templates that suit the product")
    for name, want in (("Ember Candle", ["hero", "gift"]), ("Glow Serum", ["hero", "routine"]),
                       ("Trail Mug", ["hero", "inhand"]), ("Thing 3000", ["hero", "routine"]),
                       ("Dawn Coffee", ["hero", "two-pm"])):
        got = [t["id"] for t in catalog.first_run_pair(name, "hero")]
        check(got == want, f"{name}: the pick (hero) plus {want[1]}")
    check([t["id"] for t in catalog.first_run_pair("Glow Serum", "routine")] == ["routine", "flatlay"]
          and [t["id"] for t in catalog.first_run_pair("Thing 3000")] == ["hero", "routine"]
          and [t["id"] for t in catalog.first_run_pair("Thing 3000", "turn")] == ["hero", "routine"],
          "the second is never the pick; no pick (or a video template) falls back to two generic ones")
    check(all(len({t["id"] for t in catalog.first_run_pair(n, p)}) == 2 and all(t["tool"] == "ad_image" for t in
              catalog.first_run_pair(n, p)) for n in ("Ember Candle", "Dawn Coffee", "Thing", "Glow Serum", "Trail Mug")
              for p in [None] + [t["id"] for t in catalog.TEMPLATES]), "always two distinct image templates")
    one = catalog.price_credits("ad_image", batch=1)
    check(2 * one <= catalog.starter_credits() and abs(2 * catalog.cost_usd("ad_image", batch=1)
                                                     - catalog.cost_usd("ad_image", batch=2)) < 0.001,
          "two single jobs fit the starter credits and cost us the same as one job of two")
    c1, u1 = signup("first@example.com", verified=False)
    home = c1.get("/app").get_data(as_text=True)
    check("two different looks" in home.lower() and "A narrator says one line over shots of your product" in home
          and "A presenter holds your product" not in home and 'name="presenter"' not in home,
          "the form says so, and offers the preview as a voiceover (no presenter to pick)")
    r = c1.post("/app/start", data={"csrf": csrf(c1), "kind": "image", "product_name": "Ember Candle", "template": "hero",
                                    "photo": (png(), "candle.png")}, content_type="multipart/form-data")
    check(r.status_code == 302 and "/app?made=" in r.headers["Location"], "the first submit goes back to Home")
    js = jobs_of(u1["workspace_id"])
    check(len(js) == 2 and [j["template_id"] for j in js] == ["hero", "gift"]
          and all(j["tool"] == "ad_image" and j["credits"] == one and json.loads(j["params_json"])["batch"] == 1 for j in js)
          and js[0]["batch_id"] == js[1]["batch_id"] and js[0]["brief"] != js[1]["brief"] and "Ember Candle" in js[1]["brief"],
          "two jobs in one batch: the picked template and a different one that suits a candle, one ad each")
    with db.connect() as conn:
        bal = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (u1["workspace_id"],)).fetchone())
        b1 = conn.execute("SELECT * FROM batches WHERE id=?", (js[0]["batch_id"],)).fetchone()
    check(bal == catalog.starter_credits() - 2 * one and b1["credits"] == 2 * one
          and json.loads(b1["config_json"])["templates"] == ["hero", "gift"],
          "paid from the starter credits; the batch records both templates")
    check(events(u1["workspace_id"]).count(("first_ad", "ad_image")) == 1, "first_ad is still counted once")

    print("== first run: the free video preview is a voiceover over the product")
    c2, u2 = signup("preview@example.com")
    r = c2.post("/app/start", data={"csrf": csrf(c2), "kind": "preview", "product_name": "Trail Mug",
                                    "line": "I take this mug on every hike.", "photo": (png(), "mug.png")},
                content_type="multipart/form-data")
    check(r.status_code == 400 and "speak as customers" in r.get_data(as_text=True),
          "the preview line is refused in a customer's voice")
    r = c2.post("/app/start", data={"csrf": csrf(c2), "kind": "preview", "product_name": "Trail Mug", "line": "",
                                    "presenter": "jordan", "photo": (png(), "mug.png")}, content_type="multipart/form-data")
    check(r.status_code == 302 and "/app?made=" in r.headers["Location"], "the preview starts")
    pj = jobs_of(u2["workspace_id"])[0]
    pp = json.loads(pj["params_json"])
    check(pj["tool"] == "ugc_video" and pp["ad_type"] == "voiceover" and "presenter" not in pp
          and pp["voice"] == "narrator_bright" and pp["placement"] == "product_only" and pp["preview"]
          and pp["resolution"] == "480p" and pp["duration"] == 8 and pj["credits"] == 0,
          "it is a voiceover job: no presenter, a narrator, the product only, 8 s at 480p, free")
    check(abs(pj["cost_usd"] - 8 * 0.2056) < 0.001 and preview.cost_usd() <= round(8 * 0.2056, 4),
          "it costs us what the presenter preview did, no more")
    with db.connect() as conn:
        pin = worker.build_inputs(conn, pj)
        led = conn.execute("SELECT reason FROM ledger WHERE job_id=?", (pj["id"],)).fetchone()
        bt_ = conn.execute("SELECT * FROM batches WHERE id=?", (pj["batch_id"],)).fetchone()
    check("No people and no hands in frame" in pin["prompt"] and "A narrator, never seen on camera" in pin["prompt"]
          and "holds" not in pin["prompt"] and "Lip movements" not in pin["prompt"] and "Meet Trail Mug." in pin["prompt"],
          "the prompt: product on a table, a narrator, no presenter, no lip-sync, no hand on the product")
    check(led["reason"] == "free video preview" and bt_["ad_type"] == "voiceover"
          and catalog.job_ad_type(pj["tool"], pj["params_json"]) == "voiceover", "the ledger and the batch say what it is")
    html = c2.get(r.headers["Location"]).get_data(as_text=True)
    check("This is your free preview" in html and "Make it for real" in html, "Home still explains the preview and the upsell")
    with db.connect() as conn:
        fp = conn.execute("SELECT * FROM free_previews WHERE workspace_id=?", (u2["workspace_id"],)).fetchone()
    check(fp and fp["job_id"] == pj["id"], "and it is still one per workspace and email")

    # ------------------------------------------------------------------ 9. measurement
    print("== download, export and 'make more like this' are logged per job with ad type and template")
    with db.connect() as conn:
        jid = js[1]["id"]
        f = os.path.join(TMP, "out.png")
        with open(f, "wb") as fh:
            fh.write(png(1200).getvalue())
        conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id=?", (db.now(), jid))
        oid = conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?,?,?)", (jid, "image", f)).lastrowid
        conn.commit()
    before = events(u1["workspace_id"])
    check(c1.get(f"/media/{oid}").status_code == 200 and events(u1["workspace_id"]) == before,
          "viewing an ad logs nothing")
    r = c1.get(f"/media/{oid}?download=1")
    check(r.status_code == 200 and events(u1["workspace_id"])[-1] == ("asset_download", f"image:gift:job{jid}"),
          "a download logs asset_download with image:gift:job<id>")
    n = len(events(u1["workspace_id"]))
    c1.get(f"/media/{oid}?download=1", headers={"Range": "bytes=100-"})
    check(len(events(u1["workspace_id"])) == n, "a resumed chunk of the same download isn't counted again")
    c1.get(f"/app/create?batch={js[0]['batch_id']}")
    check(events(u1["workspace_id"])[-1] == ("more_like_this", f"image:hero:job{js[0]['id']}"),
          "'Make more like this' logs more_like_this for the batch's first job")
    n = len(events(u1["workspace_id"]))
    c1.get(f"/app/create?batch={js[0]['batch_id']}&rest=1")
    check(len(events(u1["workspace_id"])) == n, "'Make the rest' is not 'more like this'")
    check(c2.get(f"/media/{oid}?download=1").status_code == 404 and c2.get(f"/app/create?batch={js[0]['batch_id']}").status_code == 200
          and not [e for e in events(u2["workspace_id"]) if e[0] in site_events.ASSET_EVENTS],
          "another workspace's ad: nothing served, nothing logged")
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (u1["workspace_id"],))
        conn.commit()
    import adhub
    plat, spec = next((k, p["exports"][0]) for k, p in adhub.PLATFORMS.items() if p.get("exports"))
    r = c1.get(f"/app/export/{oid}/{plat}/{spec[0]}")
    last = events(u1["workspace_id"])[-1]
    check(r.status_code == 200 and last == ("asset_export", f"image:gift:job{jid}:{plat}/{spec[0]}"),
          f"an export logs asset_export with the platform and format ({last[1]})")
    with db.connect() as conn:
        vj = conn.execute("SELECT * FROM jobs WHERE id=?", (pj["id"],)).fetchone()
        row = conn.execute("SELECT * FROM site_events WHERE workspace_id=? AND name='asset_export'",
                           (u1["workspace_id"],)).fetchone()
    check(site_events.asset_detail(vj) == f"voiceover:none:job{pj['id']}", "a voiceover job with no template: voiceover:none:job<id>")
    check(row["vid"] is None or re.match(r"^[a-z0-9]{8,40}$", row["vid"]) and "@" not in (row["detail"] or ""),
          "nothing personal in the row")
    check(set(site_events.ASSET_EVENTS) <= set(site_events.SERVER_EVENTS)
          and not set(site_events.ASSET_EVENTS) & set(site_events.CLIENT_EVENTS),
          "they are server events: a browser can't post them")
    with db.connect() as conn:
        check(site_events.on_asset(conn, "paid", vj) is None and site_events.on_asset(conn, "asset_download", None) is None,
              "on_asset only writes its own three events, and never raises on a missing job")

    # ------------------------------------------------------------------ 10. the ready email
    print("== 'Your ads are ready': one email when a member's first batch finishes")
    sent.clear()
    with db.connect() as conn:
        j0 = conn.execute("SELECT * FROM jobs WHERE id=?", (js[0]["id"],)).fetchone()
        j1 = conn.execute("SELECT * FROM jobs WHERE id=?", (js[1]["id"],)).fetchone()
        check(ready_email.on_job_settled(conn, j1) is False and not sent,
              "one ad done, one still pending: nothing sent yet")
        conn.execute("UPDATE jobs SET status='running', request_id='r1' WHERE id=?", (j0["id"],))
        conn.commit()
        j0 = conn.execute("SELECT * FROM jobs WHERE id=?", (j0["id"],)).fetchone()
        check(worker._finish_done(conn, j0) and len(sent) == 1, "the worker finishing the last job sends it")
    m = sent[0]
    check(m["to"] == ["first@example.com"] and m["subject"] == "Your ads are ready"
          and f"https://campaignify.test/app/create/batch/{j0['batch_id']}" in m["text"]
          and f"/app/create/batch/{j0['batch_id']}" in m["html"] and "2 of them" in m["text"]
          and {"name": "kind", "value": "ads_ready"} in m["tags"] and "$" not in m["text"],
          "to the member who made it, with the count and a link to the batch")
    check(mailer.kind_class("ads_ready") == "transactional", "it is an account email (not nurture)")
    with db.connect() as conn:
        check(ready_email.on_job_settled(conn, j0) is False and len(sent) == 1, "never twice")
        check(conn.execute("SELECT step FROM first_run_nudges WHERE user_id=?", (u1["id"],)).fetchone()["step"]
              == ready_email.READY_STEP, "the send is recorded per member")
        # a second batch for the same member: no email
        b2 = conn.execute("INSERT INTO batches (workspace_id, user_id, ad_type, title, config_json, credits, created_at) "
                          "VALUES (?,?,?,?,?,?,?)", (u1["workspace_id"], u1["id"], "image", "More", "{}", 0, db.now())).lastrowid
        conn.execute("DELETE FROM first_run_nudges WHERE user_id=?", (u1["id"],))
        j2 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                          "created_at, updated_at, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (u1["workspace_id"], u1["id"], "ad_image", "x", "{}", "done", 0, 0, db.now(), db.now(), b2)).lastrowid
        conn.commit()
        check(ready_email.on_job_settled(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (j2,)).fetchone()) is False
              and len(sent) == 1, "only for the first batch")
    # the preview member: the job fails -> nothing; a video that finishes says "video"
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running', request_id='r2' WHERE id=?", (pj["id"],))
        conn.commit()
        vrow = conn.execute("SELECT * FROM jobs WHERE id=?", (pj["id"],)).fetchone()
        check(worker._fail(conn, vrow, "The generation failed.") and len(sent) == 1,
              "a first batch where every job failed: no 'ready' email")
        check(not conn.execute("SELECT 1 FROM first_run_nudges WHERE user_id=?", (u2["id"],)).fetchone(),
              "and nothing is recorded, so a later first batch can still send it")
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (pj["id"],))
        conn.commit()
        check(worker._finish_done(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (pj["id"],)).fetchone())
              and len(sent) == 2 and sent[1]["subject"] == "Your video is ready" and sent[1]["to"] == ["preview@example.com"],
              "one finished video: 'Your video is ready'")
    c3, u3 = signup("off@example.com")
    n0 = len(sent)      # (the sign-up sent its own confirmation email)
    os.environ["ADS_READY_EMAIL"] = "0"
    with db.connect() as conn:
        b3 = conn.execute("INSERT INTO batches (workspace_id, user_id, ad_type, title, config_json, credits, created_at) "
                          "VALUES (?,?,?,?,?,?,?)", (u3["workspace_id"], u3["id"], "image", "First", "{}", 0, db.now())).lastrowid
        j3 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                          "created_at, updated_at, batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (u3["workspace_id"], u3["id"], "ad_image", "x", "{}", "done", 0, 0, db.now(), db.now(), b3)).lastrowid
        conn.commit()
        row3 = conn.execute("SELECT * FROM jobs WHERE id=?", (j3,)).fetchone()
        check(ready_email.on_job_settled(conn, row3) is False and len(sent) == n0, "ADS_READY_EMAIL=0 switches it off")
        os.environ.pop("ADS_READY_EMAIL")

        def boom(*a, **k):
            raise RuntimeError("provider down")
        _post, mailer.requests.post = mailer.requests.post, boom
        check(ready_email.on_job_settled(conn, row3) is False, "a failing email provider never raises into the worker")
        mailer.requests.post = _post
        check(ready_email.on_job_settled(conn, row3) is False and len(sent) == n0,
              "and it is not sent twice later (claimed before the send)")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} checks passed")
