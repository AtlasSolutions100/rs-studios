"""Golden-path eval: the two north-star paths (campaignify/02_north_star.md section 2), run in a real browser
against a local server with every provider faked, timed per step.

    python tests/golden_path.py                    # both paths, prints the results table
    python tests/golden_path.py --path service     # one path
    python tests/golden_path.py --json out.json --markdown out.md --headed
    python tests/golden_path.py --service-path     # with RS_SERVICE_PATH=1 (the service entrance, roadmap M5.2)

Product path (store owner): sign up, save a card (nothing charged), confirm the email, paste the product page, make
the 2 ads with the free credits, pick a plan,
plan the campaign, make the creative batch, write the copy, publish a landing page, export the ads. Service path
(local HVAC): sign up, confirm, paste the website, make the first ads, pick a plan, plan for leads (Google Search and
Meta), write Google Search copy, make a quote form, publish a landing page with the form, add a follow-up flow, and
a visitor sends the form so a lead lands in Contacts. Connect and launch (Meta for the product path, Google Ads for
the service path) is not built yet (roadmap M2 and M3): those rows say "not built" and do not fail the run.

For every step it records wall clock (seconds, renders included) and user actions (clicks, fields typed, files
picked, keys pressed, pages typed into the address bar). It asserts:
  * every button that spends credits shows its price (N credits, or "free" for the starter credits) on the button or
    right next to it before it is pressed, and the step never charges more than the prices shown;
  * credits spent by a step without a priced button fail the step;
  * no step navigates the browser away from the app, except the known handoffs (KNOWN_HANDOFFS).

Known gaps (KNOWN_GAPS) are failures the roadmap already tracks. They are reported as "known gap" and do not fail the
run, so this output is the baseline. With RS_SERVICE_PATH off (the default): the service path stops at the first-ads
step because the first run needs a product photo (UX-13). With RS_SERVICE_PATH=1 (or --service-path) the first run
asks "What do you sell?", the service path picks "A service", imports the website and makes its 2 free ads without a
photo, so that gap is not expected and a failure there fails the run. The steps after a known gap still run and are
timed ("after the gap"), so their numbers are in the baseline too, but the path counts as blocked. A known gap that
starts passing is reported, so the entry can be removed.

With RS_NEXT_STEPS=1 in the environment (the Home step strip, roadmap M5.1) the run also checks the thread: after
every step Home's "Your campaign" strip must show exactly one primary button (read with a request, not counted as a
user action, and noted on the step), and "Pick a plan" follows the strip's button and the upgrade wall's button
instead of the sidebar, so a dead end after the free ads fails the step.

With RS_LAUNCH_KIT=1 (the launch kit on the plan page, roadmap M5.3) the product path gets one more step before
"Connect Meta and launch": open the plan, check one block per planned platform with the ads' export links and the
copy's Copy buttons, download one export, tick 2 items, and read the ticks back from a second browser (another
device) logged in to the same account.

With RS_PLAN_PAYWALL=1 (the out-of-credits paywall) both paths get one more step after the first ads: the free ads
leave the account out of credits, so Home's "Plan the campaign" opens the free campaign plan. The run makes it
(nothing charged), checks the part-locked page (the summary shown, the rest behind the lock) and then "Pick a plan"
follows that page's "Get an active subscription to unlock" button to Billing, where the free-month code stands in
for Stripe Checkout; the plan then has to unlock the free plan's full page.

The card step (RS_CARD_REQUIRED, always on here) comes right after sign-up, so Home redirects to /app/card until the
card is saved: the strip check skips that one step (noted), and every step after the card step is checked.

Every M2 flag at once (the combination the release runs):
    RS_NEXT_STEPS=1 RS_LAUNCH_KIT=1 RS_SERVICE_PATH=1 RS_PLAN_PAYWALL=1 python tests/golden_path.py

Exit code: 0 when every step passed or is a known gap or not built; 1 on any other failure.

Fakes: the generation API, result downloads, email, page fetches and the Anthropic API are replaced in process the
way tests/test_app.py does it (hf.submit/status/upload, worker.requests.get, mailer.requests.post, fetcher.fetch,
llm._anthropic). The worker thread runs as in production with a short poll and a fake render time
(--render-seconds). Stripe is not configured: the plan is picked with a free-month code on Billing, which stands in
for Stripe Checkout (a known handoff that is not exercised locally). The card step after sign-up (RS_CARD_REQUIRED=1,
card_on_file.py) runs against a fake Stripe in process (F.stripe): its setup-mode Checkout "page" sends the browser
straight back to the success URL with a saved test card, so nothing leaves 127.0.0.1 and Stripe is never called. Needs Playwright for Python with Chromium
(preinstalled in CI images and dev containers; set PLAYWRIGHT_BROWSERS_PATH if yours lives elsewhere).
"""
import argparse
import hashlib
import io
import json
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

def service_path_on():
    return os.environ.get("RS_SERVICE_PATH", "0").strip().lower() in ("1", "on", "true", "yes")


def known_gaps():
    gaps = {}   # (path, step): (finding id, a regex the step's error must match, why)
    if not service_path_on():
        # RS_SERVICE_PATH=1 (roadmap M5.2) closes this one: the service path then has to pass first_ads.
        gaps[("service", "first_ads")] = ("UX-13", r"Add one photo of your product|made as product ads",
                                          "The first run is product-only: it needs a product photo (or takes a "
                                          "site photo as one) and writes a product ad; a service business has no "
                                          "product (roadmap M5.2, RS_SERVICE_PATH=1 turns the service entrance on).")
    return gaps


KNOWN_GAPS = known_gaps()
# Places the browser may leave the app for: the platforms' own pages that an app can't (or mustn't) do for the user.
KNOWN_HANDOFFS = [
    (r"^https://checkout\.stripe\.com/", "Stripe Checkout (plan or pack payment)"),
    (r"^https://billing\.stripe\.com/", "Stripe Customer Portal"),
    (r"^https://(www\.)?facebook\.com/(business|ads)/|^https://business\.facebook\.com/", "Meta payment method / Business settings"),
    (r"^https://ads\.google\.com/", "Google Ads billing setup"),
]
NOT_BUILT = {
    "product": [("connect_launch", "Connect Meta and launch (paused)", "M2.1, M2.3")],
    "service": [("connect_launch", "Connect Google Ads and launch (paused)", "M3.1, M3.2")],
}
PW = "golden-path-pw-1"


def next_steps_on():
    return os.environ.get("RS_NEXT_STEPS", "").strip().lower() in ("1", "on", "true", "yes")


def paywall_on():
    return os.environ.get("RS_PLAN_PAYWALL", "").strip().lower() in ("1", "on", "true", "yes")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ====================================================================================== server with fakes

def boot(render_seconds):
    """Import the app on a throwaway database with every provider faked; serve it on 127.0.0.1. Returns base URL."""
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="rs_golden_")
    # RS_DISK_MIN_GB: the data is a throwaway folder; the disk guard (M1.4) would otherwise refuse every render on a
    # dev box or CI runner with under 5 GB free.
    os.environ.update(
        RS_DATA=tmp, RS_WORKER="0", HF_KEY="test:secret", RS_MARKUP="2.0", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
        RS_POLL_SECONDS="1", RS_DISK_MIN_GB="0.2", ANTHROPIC_API_KEY="sk-ant-test", RESEND_API_KEY="re_test",
        RS_MAIL_DOMAIN="mail.campaignify.test", RS_PUBLIC_URL=f"http://127.0.0.1:{port}", RS_CARD_REQUIRED="1",
        RS_OWNER_INVITES="nobody@example.com:" + hashlib.sha256(b"unused").hexdigest())
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET", "SIGNUP_CREDITS_ADS",
              "FIRST_RUN_NUDGES", "GROQ_API_KEY"):
        os.environ.pop(k, None)

    import app as appmod
    import fetcher
    import hf
    import llm
    import mailer
    import worker

    appmod._maybe_reconcile = lambda ws_id, force=False: 0
    F.install(hf, worker, mailer, fetcher, llm, render_seconds)
    F.stripe()
    os.environ["RS_WORKER"] = "1"   # the planner and research run in threads, as in production
    worker.start()

    from werkzeug.serving import make_server
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    logging.getLogger("campaignify").setLevel(logging.WARNING)   # the per-request JSON lines
    srv = make_server("127.0.0.1", port, appmod.app, threaded=True)
    threading.Thread(target=srv.serve_forever, name="golden-http", daemon=True).start()
    return f"http://127.0.0.1:{port}", srv


class F:
    """The fakes. Same approach as tests/test_app.py, plus a schema-filling Anthropic fake that also drives the
    planner's tool loop and answers for either fixture business."""
    emails = []
    render_seconds = 3
    results = {}

    # ---- fixture businesses (fetched by the importer)
    PRODUCT_URL = "https://hearth.example/products/ember-candle"
    PRODUCT_PAGE = """<!doctype html><html><head><title>Ember Candle | Hearth & Co</title>
<meta name="description" content="A soy wax candle with amber and cedar. 50 hour burn.">
<meta property="og:image" content="https://cdn.hearth.example/ember-1.jpg">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Ember Candle",
"description":"A soy wax candle with amber and cedar. Hand poured in small batches. 50 hour burn time.",
"image":["https://cdn.hearth.example/ember-1.jpg","https://cdn.hearth.example/ember-2.jpg","https://cdn.hearth.example/ember-3.jpg"],
"brand":{"@type":"Brand","name":"Hearth & Co"},"offers":{"@type":"Offer","price":"34.00","priceCurrency":"USD"}}</script>
</head><body><h1>Ember Candle</h1><p>Amber and cedar. Soy wax. 50 hour burn. Hand poured in small batches.</p></body></html>"""
    # A typical small service site: business details and a logo, no product photo.
    SERVICE_URL = "https://northside-hvac.example/"
    SERVICE_PAGE = """<!doctype html><html><head><title>Northside Heating & Air | AC repair and furnace service in Plano, TX</title>
<meta name="description" content="Same-day AC repair, furnace tune-ups and new system installs in Plano and Allen. Free quotes.">
<meta property="og:image" content="https://northside-hvac.example/img/logo.png"><meta name="theme-color" content="#1f4e79">
<style>.btn{background:#f2a900;color:#1f4e79}</style>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"HVACBusiness","name":"Northside Heating & Air",
"description":"AC repair, furnace tune-ups and new system installs in Plano and Allen, Texas.",
"logo":"https://northside-hvac.example/img/logo.png","telephone":"+1-972-555-0142",
"address":{"@type":"PostalAddress","addressLocality":"Plano","addressRegion":"TX"},"areaServed":["Plano","Allen","Frisco"]}</script>
</head><body><img src="/img/logo.png" alt="Northside Heating & Air logo"><h1>AC repair in Plano, same day</h1>
<img src="/img/tech-at-work.jpg" alt="A technician checking an outdoor unit"><img src="/img/service-van.jpg" alt="Our van">
<img src="/img/new-install.jpg" alt="A new furnace install">
<p>Licensed technicians. Upfront prices. Free quotes on new systems.</p><a href="/quote">Get a free quote</a></body></html>"""

    BIZ = {
        "ember": {"name": "Ember Candle", "brand": "Hearth & Co",
                  "hooks": ["Your whole evening, in one candle.", "The room smells like a cabin in 10 seconds.",
                            "Stop buying candles that quit after a week."],
                  "body": "I light this when I get home and the whole room changes. Amber, cedar, a little smoke. "
                          "Fifty hours of burn, so one candle lasts the month.",
                  "line": "Ember Candle, 50 hours of amber and cedar"},
        "northside": {"name": "Northside Heating & Air", "brand": "Northside Heating & Air",
                      "hooks": ["AC out in July? We come the same day.", "Your furnace tune-up, booked in 2 minutes.",
                                "A free quote before anyone touches a screwdriver."],
                      "body": "Licensed techs in Plano and Allen. Upfront prices before any work starts. Book a "
                              "visit or ask for a free quote on a new system.",
                      "line": "Same-day AC repair in Plano"},
    }

    @classmethod
    def biz(cls, text):
        return cls.BIZ["northside"] if ("Northside" in text or "HVAC" in text or "quote" in text.lower()
                                        and "candle" not in text.lower()) else cls.BIZ["ember"]

    # ---- generation API
    @classmethod
    def submit(cls, model, inputs, webhook_url=None, **kw):   # kw: idempotency_key (hf.py, M1.9)
        rid = f"req{len(cls.results) + 1}"
        cls.results[rid] = {"at": time.time(), "model": model, "inputs": inputs}
        return rid

    @classmethod
    def status(cls, rid):
        r = cls.results.get(rid)
        if not r:
            return {"status": "failed"}
        if time.time() - r["at"] < cls.render_seconds:
            return {"status": "in_progress"}
        if "ads-studio" in r["model"] or "image" in r["model"]:
            n = int((r["inputs"] or {}).get("batch_size") or 1)
            return {"status": "completed", "images": [{"url": f"https://cdn.example/{rid}-{i}.png"} for i in range(n)]}
        return {"status": "completed", "video": {"url": f"https://cdn.example/{rid}.mp4"}}

    @staticmethod
    def png(w=864, h=1080, rgb=(200, 120, 40)):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (w, h), rgb)
        ImageDraw.Draw(img).rectangle([w * 0.25, h * 0.3, w * 0.75, h * 0.75], fill=(245, 236, 220))
        b = io.BytesIO()
        img.save(b, "PNG")
        return b.getvalue()

    cards = 0

    @classmethod
    def stripe(cls):
        """The card step's Stripe calls, faked (billing.py's setup-mode Checkout). Stripe keys stay unset, so the
        card step is switched on by hand (card_on_file.active) and everything else still sees Stripe as off."""
        import billing
        import card_on_file

        def no_stripe(*a, **kw):
            raise AssertionError("the golden path never calls Stripe")

        billing._get = billing._post = no_stripe
        card_on_file.active = card_on_file.required
        sessions = {}

        def customer(*, email, metadata, idempotency_key):
            return f"cus_golden_{metadata['workspace_id']}"

        def setup_session(*, customer, success_url, cancel_url, metadata, client_reference_id, idempotency_key):
            cls.cards += 1
            sid = f"cs_golden_card_{cls.cards}"
            sessions[sid] = {"id": sid, "mode": "setup", "status": "complete", "customer": customer,
                             "metadata": {k: str(v) for k, v in metadata.items()},
                             "setup_intent": {"id": f"seti_{sid}", "status": "succeeded", "payment_method": {
                                 "id": f"pm_{sid}", "card": {"fingerprint": f"fp_golden_{cls.cards}", "brand": "visa",
                                                             "last4": "4242"}}}}
            # Stripe's page, done: straight back to the success URL, as after a saved card
            return {"id": sid, "url": success_url.replace("{CHECKOUT_SESSION_ID}", sid)}

        billing.create_customer, billing.create_setup_session = customer, setup_session
        billing.get_setup_session = lambda sid: json.loads(json.dumps(sessions[sid]))

    @classmethod
    def install(cls, hf, worker, mailer, fetcher, llm, render_seconds):
        cls.render_seconds = render_seconds
        video = open(os.path.join(ROOT, "static/site/candle.mp4"), "rb").read()
        hf.submit, hf.status = cls.submit, cls.status
        hf.upload = lambda data, ctype: f"https://files.example/{random.randint(1, 10 ** 9)}.png"
        hf.estimate = lambda model_id, inputs: None
        hf.estimate_detail = lambda model_id, inputs: (None, "faked")
        hf.probe = lambda max_age=600: "ok"

        class Resp:
            status_code = 200

            def __init__(self, url):
                self.content = video if url.endswith(".mp4") else cls.png(rgb=(random.randint(40, 220), 120, 60))

            def raise_for_status(self):
                pass

        worker.requests.get = lambda url, timeout=0, **kw: Resp(url)

        class MailResp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"id": f"re_{len(cls.emails)}"}

        def mail_post(url, json=None, headers=None, timeout=0, data=None, **kw):
            cls.emails.append(json or {})
            return MailResp()

        mailer.requests.post = mail_post

        def fetch(url, accept=None):
            if "hearth.example" in url:
                return {"url": url, "status": 200, "content_type": "text/html", "text": cls.PRODUCT_PAGE}
            if "northside-hvac.example" in url:
                return {"url": url, "status": 200, "content_type": "text/html", "text": cls.SERVICE_PAGE}
            return {"url": url, "status": 404, "content_type": "text/html", "text": "<title>Not found</title>"}

        fetcher.fetch = fetch
        fetcher.fetch_bytes = lambda url, max_bytes=0, accept=None: {
            "url": url, "status": 200, "content_type": "image/png", "data": cls.png(800, 800, (240, 232, 220))}
        llm._anthropic = cls.anthropic

    # ---- Anthropic
    @staticmethod
    def _reply(name, inp, tin=2000, tout=600):
        return {"model": "claude-sonnet-4-6", "stop_reason": "tool_use", "usage": {"input_tokens": tin, "output_tokens": tout},
                "content": [{"type": "tool_use", "id": f"tu_{random.randint(1, 10 ** 9)}", "name": name, "input": inp}]}

    @classmethod
    def _sample(cls, schema, key, b, depth=0):
        if "enum" in schema:
            return schema["enum"][0]
        t = schema.get("type")
        if t == "object":
            return {k: cls._sample(v, k, b, depth + 1) for k, v in (schema.get("properties") or {}).items()}
        if t == "array":
            return [cls._sample(schema.get("items") or {"type": "string"}, key, b, depth + 1) for _ in range(3 if depth < 2 else 2)]
        if t == "integer":
            return 2
        if t == "number":
            return 7
        if t == "boolean":
            return True
        k = key.lower()
        if "hook" in k:
            return b["hooks"][0]
        if "headline" in k or k in ("title", "name"):
            return b["line"]
        if "url" in k or "source" in k:
            return "https://example.org/"
        return b["body"][:120]

    @classmethod
    def _copy(cls, prompt):
        import platforms
        m = re.search(r"Write (\d+) distinct variants of (.+?) for (.+?)\.\n", prompt)
        n = int(m.group(1)) if m else 3
        kind = next((k for k, v in platforms.KINDS.items() if m and v["name"].lower() == m.group(2)), "ad")
        plat = next((k for k, v in platforms.PLATFORMS.items() if m and v["name"] == m.group(3)), "meta")
        b = cls.biz(prompt)
        texts = b["hooks"] + [b["body"], b["line"]]
        out = []
        for i in range(n):
            fields = []
            for j, (fname, rec, hard) in enumerate(platforms.fields_for(kind, plat)):
                lim = hard or rec or 200
                if fname == "FAQ":
                    text = "Q: How fast can you come?\nA: Usually the same day.\nQ: Is the quote free?\nA: Yes." \
                        if b is cls.BIZ["northside"] else "Q: How long does it burn?\nA: About 50 hours.\nQ: What wax?\nA: Soy."
                elif fname == "Hashtags":
                    text = "#home #local"
                else:
                    text = texts[(i + j) % len(texts)]
                    if len(text) > lim:
                        text = text[:lim].rsplit(" ", 1)[0].rstrip(",.")
                fields.append({"name": fname, "text": text})
            out.append({"angle": ["Problem and fix", "A moment of use", "Direct benefit", "Curiosity", "Local"][i % 5],
                        "fields": fields})
        return {"variants": out}

    @classmethod
    def anthropic(cls, body, timeout=None):
        tools = {t.get("name"): t for t in body.get("tools") or []}
        forced = (body.get("tool_choice") or {}).get("name")
        text = json.dumps(body.get("messages") or [])[:20000] + str(body.get("system") or "")[:2000]
        b = cls.biz(text)
        if forced == "submit_copy":
            prompt = body["messages"][0]["content"]
            prompt = prompt if isinstance(prompt, str) else " ".join(p.get("text", "") for p in prompt if isinstance(p, dict))
            return cls._reply(forced, cls._copy(prompt))
        if forced and forced in tools:
            return cls._reply(forced, cls._sample(tools[forced].get("input_schema") or {}, "", b))
        if "finish" in tools and "propose_ad" in tools:
            return cls._planner(body, tools, b)
        return {"model": "claude-sonnet-4-6", "stop_reason": "end_turn", "usage": {"input_tokens": 500, "output_tokens": 50},
                "content": [{"type": "text", "text": "Done."}]}

    @classmethod
    def _planner(cls, body, tools, b):
        step = sum(1 for m in body["messages"] if m["role"] == "assistant")
        service = b is cls.BIZ["northside"]
        pres = tools["propose_ad"]["input_schema"]["properties"]["presenter"]["enum"]
        if step == 0:
            return cls._reply("get_brand_and_catalog", {})
        if step == 1:
            return cls._reply("save_angles", {"angles": [
                {"name": "Same day" if service else "Evening ritual", "insight": "People want it fixed today." if service
                 else "People want a signal that the work day is over.", "audience": "Homeowners in Plano and Allen"
                 if service else "Renters 25-40 who work from home", "why": "Speed is the first thing people ask about."
                 if service else "Scent changes a room fastest."},
                {"name": "Free quote" if service else "Lasts the month", "insight": "Nobody wants a surprise bill."
                 if service else "Cheap candles tunnel and die.", "audience": "Owners with a system over 12 years old"
                 if service else "Repeat candle buyers"}]})
        if step == 2:
            return cls._reply("save_creative", {"angle": "Same day" if service else "Evening ritual",
                                                "format": "UGC talking video", "hooks": b["hooks"], "script": b["body"],
                                                "on_screen_text": ["Same day", "Free quote"] if service else ["50 hour burn"]})
        if step == 3:
            ads = [
                {"angle": "Same day" if service else "Evening ritual", "platform": "meta", "tool": "ad_image",
                 "brief": "A technician's van outside a brick house at dusk, porch light on." if service else
                 "The lit candle on a wooden side table at dusk, a wool blanket beside it.", "aspect": "4:5", "versions": 2},
                {"angle": "Free quote" if service else "Lasts the month", "platform": "meta", "tool": "ad_image",
                 "brief": "A clean new outdoor AC unit beside a fence, morning light." if service else
                 "Three candles in a row on a shelf, one lit.", "aspect": "9:16", "versions": 2},
            ]
            if not service:
                ads += [{"angle": "Evening ritual", "platform": "meta", "tool": "ugc_video", "duration": 15, "presenter": pres[0],
                         "brief": "A presenter at home lights the candle and talks about the 50 hour burn."},
                        {"angle": "Lasts the month", "platform": "meta", "tool": "ugc_video", "duration": 15, "presenter": pres[1],
                         "brief": "A presenter holds the candle and says it lasts the month."}]
            return {"model": body["model"], "stop_reason": "tool_use", "usage": {"input_tokens": 3000, "output_tokens": 500},
                    "content": [{"type": "tool_use", "id": f"tu_p{i}", "name": "propose_ad", "input": a} for i, a in enumerate(ads)]}
        if step == 4:
            return cls._reply("write_copy", {"platform": "google" if service else "meta", "kind": "ad",
                                             "angle": "all", "variants": 2})
        if step == 5 and service:
            return cls._reply("write_copy", {"platform": "meta", "kind": "ad", "angle": "all", "variants": 2})
        plats = ["google", "meta"] if service else ["meta", "google"]
        extra = {}
        if "search_keywords" in tools["finish"]["input_schema"]["properties"] and service:
            # the model fills the keyword list when the schema asks for it (RS_SERVICE_PATH with Google picked)
            extra["search_keywords"] = [{"term": t, "match": m} for t, m in (
                ("ac repair plano", "exact"), ("ac repair allen", "exact"), ("ac repair near me", "phrase"),
                ("furnace tune up plano", "exact"), ("furnace tune up near me", "phrase"), ("hvac company plano", "phrase"),
                ("new ac unit cost", "phrase"), ("ac installation plano", "exact"), ("heating and air plano", "phrase"),
                ("air conditioner not cooling", "broad"), ("northside heating and air", "exact"))]
        return cls._reply("finish", {**extra,
            "summary": ("Leads first: Google Search for people already looking, Meta lead ads in the service area."
                        if service else "Image ads on Meta first, then the talking video once a hook wins."),
            "offer": "Free quote on a new system, same-day repair." if service else "Ember Candle, $34, 50 hour burn.",
            "budget_split": [{"platform": plats[0], "share_percent": 60, "note": "First test"},
                             {"platform": plats[1], "share_percent": 40, "note": "Second"}],
            "kpis": ["Cost per lead under $40", "Booked calls"] if service else ["Cost per purchase", "Hook rate"],
            "test_plan": "Run 2 weeks, keep the winning angle.",
            "checklist": [{"title": "Set up conversion tracking", "platform": plats[0]},
                          {"title": "Upload the ads", "platform": "meta"}]}, tin=3000, tout=300)


# ====================================================================================== recorder

class StepFailed(Exception):
    pass


class Step:
    def __init__(self, path, key, title):
        self.path, self.key, self.title = path, key, title
        self.actions = 0
        self.seconds = 0.0
        self.status = "pass"
        self.error = ""
        self.notes = []
        self.paid = []          # [(label, credits shown or 0 for free, "free"|"credits")]
        self.charged = 0
        self.left_app = []
        self.after_gap = False
        self.gap = None

    def row(self):
        return {"path": self.path, "step": self.key, "title": self.title, "status": self.status,
                "seconds": round(self.seconds, 1), "actions": self.actions, "charged": self.charged,
                "paid_buttons": [{"label": lbl, "shown": shown, "kind": kind} for lbl, shown, kind in self.paid],
                "left_app": self.left_app, "after_gap": self.after_gap, "gap": self.gap, "error": self.error,
                "notes": self.notes}


PRICE_RE = re.compile(r"(\d[\d,]*)\s*credits?\b", re.I)
FREE_RE = re.compile(r"\bfree\b", re.I)


class Run:
    """Drives one path in one browser context and records every step."""

    def __init__(self, base, browser, path, args):
        self.base, self.path, self.args = base, path, args
        self.ctx = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        self.ctx.route("**/*", self._route)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(20000)
        self.page.on("framenavigated", self._nav)
        self.ctx.on("page", self._popup)
        self.steps = []
        self.cur = None
        self.gap_hit = None
        self.ws_id = None
        self.blocked_hosts = set()

    # ---- hermetic browser: nothing leaves 127.0.0.1 (fonts, analytics); off-app navigations are recorded
    def _route(self, route):
        url = route.request.url
        if url.startswith(self.base) or url.startswith("data:") or url.startswith("blob:"):
            return route.continue_()
        self.blocked_hosts.add(re.sub(r"^(\w+://[^/]+).*$", r"\1", url))
        if route.request.is_navigation_request() and route.request.frame == self.page.main_frame:
            self._leave(url)
        return route.abort()

    def _leave(self, url):
        hand = next((label for rx, label in KNOWN_HANDOFFS if re.search(rx, url)), None)
        if self.cur is not None:
            self.cur.left_app.append({"url": url, "handoff": hand})
            if not hand:
                self.cur.notes.append(f"left the app: {url}")

    def _nav(self, frame):
        if frame == self.page.main_frame and not frame.url.startswith(self.base) and frame.url not in ("about:blank", ""):
            self._leave(frame.url)

    def _popup(self, page):
        page.on("framenavigated", lambda fr: (fr == page.main_frame and not fr.url.startswith(self.base)
                                              and fr.url != "about:blank" and self._leave(fr.url)))

    # ---- user actions (each one counts)
    def act(self, n=1):
        if self.cur:
            self.cur.actions += n

    def goto(self, path):
        """Typing an address (counts as one action)."""
        self.act()
        self.page.goto(self.base + path)

    def click(self, loc):
        loc = self._loc(loc)
        loc.scroll_into_view_if_needed()
        self.act()
        loc.click()

    def fill(self, loc, text):
        self.act()
        self._loc(loc).fill(text)

    def check(self, loc, on=True):
        loc = self._loc(loc)
        if loc.is_checked() != on:
            self.act()
            label = loc.locator("xpath=ancestor::label[1]")
            if label.count():   # styled chips: the label is what people click
                label.click()
            else:
                loc.set_checked(on)
            if loc.is_checked() != on:
                raise StepFailed("a checkbox didn't change")

    def select(self, loc, value):
        self.act()
        self._loc(loc).select_option(value)

    def _loc(self, loc):
        return self.page.locator(loc).first if isinstance(loc, str) else loc

    def nav(self, href):
        """Click a link in the app's sidebar (opening its folded group first if needed)."""
        link = self.page.locator(f"nav.navs a.nav[href='{href}']").first
        if not link.is_visible():
            grp = self.page.locator(f"nav.navs details:has(a.nav[href='{href}']) > summary").first
            self.click(grp)
        self.click(link)
        self.page.wait_for_load_state()

    def paid_click(self, loc, label=None):
        """Press a button that spends credits. Its price has to be on it or right next to it (the button, its
        parent or grandparent) before the press: 'N credits', or 'free' for the starter credits."""
        loc = self._loc(loc)
        loc.scroll_into_view_if_needed()
        texts = loc.evaluate("""b => { const out = [b.innerText || b.value || '']; let e = b.parentElement;
                                for (let i = 0; i < 2 && e; i++, e = e.parentElement) out.push(e.innerText || '');
                                return out; }""")
        label = label or " ".join(texts[0].split())[:60]
        shown, kind = None, None
        for t in texts:
            m = PRICE_RE.search(t)
            if m:
                shown, kind = int(m.group(1).replace(",", "")), "credits"
                break
            if FREE_RE.search(t):
                shown, kind = 0, "free"
                break
        if shown is None:
            raise StepFailed(f"paid button '{label}' shows no credit price before it is pressed")
        self.cur.paid.append((label, shown, kind))
        self.act()
        loc.click()

    # ---- the database, for checks and waits (never a user action)
    def q(self, sql, *a):
        import db
        with db.connect() as conn:
            return conn.execute(sql, a).fetchall()

    def ledger_mark(self):
        return (self.q("SELECT COALESCE(MAX(id),0) FROM ledger")[0][0]) if self.ws_id else 0

    def spent_since(self, mark):
        if not self.ws_id:
            return 0
        rows = self.q("SELECT delta, reason, job_id FROM ledger WHERE workspace_id=? AND id>?", self.ws_id, mark)
        out = 0
        for r in rows:
            if r["delta"] < 0 and "expire" not in (r["reason"] or ""):
                out -= r["delta"]
            elif r["delta"] > 0 and r["job_id"] is not None:
                out -= r["delta"]   # a refund
        return out

    def wait_until(self, fn, what, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.4)
        raise StepFailed(f"timed out waiting for {what}")

    def wait_jobs(self, since_id, n_min=1, timeout=120):
        def done():
            rows = self.q("SELECT status, error FROM jobs WHERE workspace_id=? AND id>?", self.ws_id, since_id)
            if len(rows) < n_min or any(r["status"] in ("pending", "running") for r in rows):
                return None
            return rows
        rows = self.wait_until(done, "renders", timeout)
        bad = [r for r in rows if r["status"] != "done"]
        if bad:
            raise StepFailed(f"{len(bad)} of {len(rows)} renders ended {bad[0]['status']}"
                             + (f" ({' '.join((bad[0]['error'] or '').split())[:160]})" if bad[0]["error"] else ""))
        return rows

    def last_job_id(self):
        return self.q("SELECT COALESCE(MAX(id),0) FROM jobs")[0][0]

    def text(self):
        return self.page.inner_text("main") if self.page.locator("main").count() else self.page.inner_text("body")

    # ---- steps
    def step(self, key, title, fn):
        st = Step(self.path, key, title)
        st.after_gap = self.gap_hit is not None
        self.steps.append(st)
        self.cur = st
        mark = self.ledger_mark()
        t0 = time.monotonic()
        try:
            fn(st)
        except Exception as e:  # noqa: BLE001  record and go on to the next step (or stop the path)
            st.status, st.error = "fail", (str(e).splitlines() or [repr(e)])[0][:300]
            if os.environ.get("GOLDEN_DEBUG"):
                traceback.print_exc()
            if self.args.shots:
                os.makedirs(self.args.shots, exist_ok=True)
                try:
                    self.page.screenshot(path=os.path.join(self.args.shots, f"{self.path}-{key}.png"), full_page=True)
                except Exception:  # noqa: BLE001
                    pass
        st.seconds = time.monotonic() - t0
        if next_steps_on() and st.status == "pass" and self.ws_id:
            try:
                self.strip_check(st)
            except StepFailed as e:
                st.status, st.error = "fail", str(e)
        st.charged = self.spent_since(mark)
        if st.status == "pass":
            shown = sum(p[1] for p in st.paid)
            if st.charged and not st.paid:
                st.status, st.error = "fail", f"spent {st.charged} credits with no priced button pressed"
            elif st.charged > shown and not any(p[2] == "free" for p in st.paid):
                st.status, st.error = "fail", f"charged {st.charged} credits, more than the {shown} shown"
            elif any(p[2] == "free" for p in st.paid) and st.charged:
                st.notes.append(f"labelled free; used {st.charged} of the starter credits")
            bad = [x["url"] for x in st.left_app if not x["handoff"]]
            if bad:
                st.status, st.error = "fail", f"left the app: {bad[0]}"
        gap = KNOWN_GAPS.get((self.path, key))
        if gap and st.status == "fail" and re.search(gap[1], st.error):
            st.status, st.gap = "known_gap", gap[0]
            st.notes.append(gap[2])
            self.gap_hit = self.gap_hit or key
        elif gap and st.status == "pass":
            st.notes.append(f"{gap[0]} passes now: remove it from KNOWN_GAPS")
        self.cur = None
        try:   # each step starts on a settled page
            self.page.wait_for_load_state(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        return st

    def strip_check(self, st):
        """Home's "Your campaign" strip (RS_NEXT_STEPS): one primary button, whatever step the account is on."""
        r = self.ctx.request.get(f"{self.base}/app")
        if re.search(r"/app/card(\?|$)", r.url):
            # the card step comes right after sign-up (RS_CARD_REQUIRED): Home is the card page until it's saved
            st.notes.append("Home is the card step until the card is saved (strip checked from the next step)")
            return
        html = r.text()
        m = re.search(r'<section class="panel nsteps"[^>]*data-current="(\w+)".*?</section>', html, re.S)
        if not m:
            if "data-first-run" in html:
                return   # before the first ad Home is the first-ad form only
            raise StepFailed("Home has no 'Your campaign' strip")
        prim = [x for x in re.findall(r'class="btn( [^"]*)?"', m.group(0)) if "dark" not in (x or "")]
        label = re.search(r"data-ns-primary>([^<]+)<", m.group(0))
        st.notes.append(f"Home next step: {m.group(1)} ('{label.group(1) if label else ''}', {len(prim)} primary)")
        if len(prim) != 1:
            raise StepFailed(f"Home's strip shows {len(prim)} primary buttons, not 1")

    # ---------------------------------------------------------------- shared steps
    def email_code(self, email):
        for m in reversed(F.emails):
            to = m.get("to")
            to = to if isinstance(to, list) else [to]
            mm = re.search(r"code: (\d{6})", m.get("subject") or "")
            if email in to and mm:
                return mm.group(1)
        return None

    def s_signup(self, st, email):
        self.goto("/")
        self.click("a[data-cta='hero']")
        self.page.wait_for_url("**/signup**")
        self.fill("input[name=email]", email)
        self.fill("input[name=password]", PW)
        self.click("form button:has-text('Create account')")
        self.wait_until(lambda: re.search(r"/app(/card)?(\?|$)", self.page.url)
                        or self.page.locator("[data-signup-sent]").count(), "the page after Create account", 20)
        if self.page.locator("[data-signup-sent]").count():
            # Sign-up waits for its emailed link (login_security.confirm_first): open it, as the person would.
            self.wait_until(lambda: self.email_link(email), "the sign-up email", 10)
            self.goto(self.email_link(email))
        self.page.wait_for_url(re.compile(r"/app(/card)?(\?|$)"))
        u = self.q("SELECT * FROM users WHERE email=?", email)
        if not u:
            raise StepFailed("no account was made")
        self.ws_id = u[0]["workspace_id"]

    def s_card(self, st):
        """The card step after sign-up (RS_CARD_REQUIRED): nothing charged, the starter credits land after it."""
        if "/app/card" not in self.page.url:
            st.notes.append("no card step (RS_CARD_REQUIRED off)")
            return
        before = self.q("SELECT credits FROM workspaces WHERE id=?", self.ws_id)[0][0]
        if "Why we ask" not in self.text():
            raise StepFailed("the card page doesn't say why we ask")
        self.click("[data-card-add]")
        self.page.wait_for_url(re.compile(r"/app(\?|$)"))
        ws = self.q("SELECT * FROM workspaces WHERE id=?", self.ws_id)[0]
        if not ws["card_saved_at"]:
            raise StepFailed("the card wasn't saved")
        if ws["credits"] <= before:
            raise StepFailed("the starter credits didn't land after the card")
        st.notes.append(f"card saved with a fake Stripe; {ws['credits'] - before} starter credits landed, 0 charged")

    def email_link(self, email):
        """The /verify link (path and query) of the newest email to this address."""
        for m in reversed(F.emails):
            to = m.get("to")
            to = to if isinstance(to, list) else [to]
            mm = re.search(r"https?://[^/\s]+(/verify\?t=[\w\-.]+)", m.get("text") or "")
            if email in to and mm:
                return mm.group(1)
        return None

    def s_verify(self, st, email):
        if self.q("SELECT email_verified_at FROM users WHERE email=?", email)[0][0]:
            return   # the sign-up link already confirmed it
        self.wait_until(lambda: self.email_code(email), "the confirmation email", 10)
        # Opening the box asks the server to send a code unless the last one still works: read the inbox after that.
        with self.page.expect_response(lambda resp: resp.request.method == "POST" and "/verify" in resp.url):
            self.click("[data-verify-open]")
        code = self.email_code(email)
        self.fill("dialog .vcode", code)
        self.click("dialog [data-vok]")
        verified = lambda: self.q("SELECT email_verified_at FROM users WHERE email=?", email)[0][0]  # noqa: E731
        refused = lambda: self.page.locator("dialog [data-verr]:visible").count()  # noqa: E731
        self.wait_until(lambda: verified() or refused(), "the email to be confirmed", 10)
        if not verified():
            raise StepFailed("the code was refused: " + self.page.locator("dialog [data-verr]").inner_text())
        self.page.wait_for_selector("[data-verify-banner]", state="detached", timeout=5000)

    def s_brand_kit(self, st, url, want_name, sells=None):
        if sells and self.page.locator(f"input[name=sells][value={sells}]").count():
            self.check(f"input[name=sells][value={sells}]", True)   # "What do you sell?" (RS_SERVICE_PATH)
        elif sells == "service" and service_path_on():
            raise StepFailed("RS_SERVICE_PATH is on but the first run doesn't ask 'What do you sell?'")
        self.fill("#purl", url)
        self.click("[data-import]")
        self.page.wait_for_function("() => (document.querySelector('#pn')||{}).value", timeout=15000)
        name = self.page.input_value("#pn")
        brand_name = self.page.input_value("#bn")
        photos = self.page.locator("[data-photos] input[name=photo_id]").count()
        prod = self.q("SELECT * FROM products WHERE workspace_id=? ORDER BY id DESC", self.ws_id)
        claims, logo, colors = 0, False, 0
        if prod:
            try:   # suggested claims wait for approval in claims_pending (the importer never approves them)
                claims = len(json.loads(prod[0]["claims_pending"] or "[]"))
                site = json.loads(prod[0]["import_json"] or "{}").get("site") or {}
                logo, colors = bool(site.get("logo_photo_id")), len(site.get("colors") or [])
            except Exception:  # noqa: BLE001
                pass
        kit = f", logo {'yes' if logo else 'no'}, {colors} color(s)" if sells == "service" else ""
        st.notes.append(f"filled: name '{name}', brand '{brand_name}'{kit}, {photos} photo(s), {claims} suggested claim(s)"
                        f" (target: name, colors, logo, 3 photos, 5 claims)")
        if want_name not in name:
            raise StepFailed(f"the importer filled '{name}', expected '{want_name}'")

    def s_first_ads(self, st):
        before = self.last_job_id()
        self.paid_click("[data-go]")
        self.page.wait_for_load_state()
        main = self.text()
        if "/app?made=" not in self.page.url:
            flash = self.page.locator(".flash").all_inner_texts()
            raise StepFailed("the first ads didn't start: " + (" ".join(flash) or main[:120]).strip())
        self.wait_jobs(before, 1)
        self.page.wait_for_selector("text=/ready/i", timeout=20000)
        if self.path == "service":
            # A service's first ads must be made as a service ad: a site photo (a van, a technician) passed off as
            # the "product" makes an ad told to keep the product's packaging exactly as photographed.
            import db
            import worker
            with db.connect() as conn:
                job = conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND id>? ORDER BY id LIMIT 1",
                                   (self.ws_id, before)).fetchone()
                prompt = worker.image_prompt(conn, job) if job["tool"] == "ad_image" else ""
            if "a service business" not in prompt:
                raise StepFailed("the first ads were made as product ads (a site photo used as the product photo)")
            st.notes.append(f"made as a service ad, {len(json.loads(job['params_json']).get('photo_ids') or [])} "
                            "reference image(s)")

    def s_free_plan(self, st):
        """RS_PLAN_PAYWALL: out of credits after the free ads, the planner's door opens on the free campaign plan."""
        nxt = self.page.locator("[data-next-steps] [data-ns-primary]")
        if next_steps_on():
            if not nxt.count() or "/app/campaigns" not in (nxt.first.get_attribute("href") or ""):
                raise StepFailed("after the free ads Home's primary button doesn't lead to the plan")
            st.notes.append(f"followed Home's '{nxt.first.inner_text().strip()}'")
            self.click(nxt.first)
            self.page.wait_for_load_state()
        else:
            self.nav("/app/campaigns")
        if "/app/free-plan" not in self.page.url:
            raise StepFailed("out of credits with the paywall on, the planner didn't open the free plan "
                             f"({self.page.url})")
        form = self.page.locator("form[data-free-plan-form]")
        if not form.count():
            raise StepFailed("the free plan page has no form: " + self.text()[:160])
        if not self.page.input_value("#offer"):
            self.fill("#offer", "Our main offer")
        self.click("form[data-free-plan-form] button:has-text('Make my free plan')")
        self.page.wait_for_url(re.compile(r"/app/free-plan/\d+"))
        cid = int(re.search(r"/app/free-plan/(\d+)", self.page.url).group(1))
        row = self.wait_until(lambda: (lambda r: r if r and r[0]["status"] != "planning" else None)(
            self.q("SELECT status, error FROM campaigns WHERE id=?", cid)), "the free plan", 60)[0]
        if row["status"] != "ready":
            raise StepFailed(f"the free plan ended '{row['status']}' ({row['error']})")
        self.page.goto(self.page.url)
        self.page.wait_for_selector("[data-locked]", timeout=15000)
        held = self.page.locator("[data-locked] li").count()
        if not self.page.locator("[data-part-summary]").count() or not self.page.locator("[data-unlock]").count():
            raise StepFailed("the free plan page doesn't show its summary and the unlock button")
        self.free_plan_url = self.page.url
        st.notes.append(f"free plan made (nothing charged): summary shown, {held} part(s) behind the lock")

    def s_pick_plan(self, st, code):
        nxt = self.page.locator("[data-next-steps] [data-ns-primary]")
        made = self.q("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", self.ws_id)[0][0]
        free_url = getattr(self, "free_plan_url", None)
        if paywall_on() and free_url:
            # the paywall's own way to the plans: the free plan page's "Get an active subscription to unlock"
            if not self.page.url.startswith(free_url):
                self.page.goto(free_url)
            wall = self.page.locator("[data-unlock]")
            st.notes.append("followed the free plan's 'Get an active subscription to unlock'")
            self.click(wall.first)
            self.page.wait_for_url("**/app/billing**")
            self._apply_code(st, code)
            self.page.goto(free_url)
            if "/app/campaigns/" not in self.page.url:
                raise StepFailed("the plan didn't unlock the free plan's full page")
            st.notes.append("the plan unlocked the free plan's full page")
            return
        if next_steps_on() and made:
            # The thread: Home's one primary button after the free ads, then the wall's button (no dead end).
            if not nxt.count() or "/app/campaigns" not in (nxt.first.get_attribute("href") or ""):
                raise StepFailed("after the free ads Home's primary button doesn't lead to the plan")
            st.notes.append(f"followed Home's '{nxt.first.inner_text().strip()}'")
            self.click(nxt.first)
            self.page.wait_for_load_state()
        else:
            self.nav("/app/campaigns")
        wall = self.page.locator("[data-wall] a.btn:not(.dark)") if next_steps_on() else self.page.locator("a:has-text('See plans')")
        if not wall.count():
            st.notes.append("no upgrade wall (already on a plan)")
            return
        self.click(wall.first)
        self.page.wait_for_url("**/app/billing**")
        self._apply_code(st, code)

    def _apply_code(self, st, code):
        st.notes.append("free-month code on Billing stands in for Stripe Checkout (a known handoff, not run locally)")
        self.fill("#comp-code", code)
        self.click("#code button:has-text('Apply code')")
        self.page.wait_for_load_state()
        ws = self.q("SELECT comp_plan FROM workspaces WHERE id=?", self.ws_id)[0]
        if not ws["comp_plan"]:
            raise StepFailed("the plan didn't switch on: " + " ".join(self.page.locator(".flash").all_inner_texts()))

    def s_plan(self, st, goal, platforms_on, budget, link):
        self.nav("/app/campaigns")
        self.select("#goal", goal)
        for k in ("meta", "tiktok", "youtube", "google", "x", "linkedin", "pinterest", "snapchat", "reddit"):
            self.check(f"input[name=platforms][value={k}]", k in platforms_on)
        self.fill("#budget", budget)
        self.fill("#link", link)
        self.paid_click("form.copyform button:has-text('Plan my campaign')")
        self.page.wait_for_url(re.compile(r"/app/campaigns/\d+"))
        cid = int(re.search(r"/app/campaigns/(\d+)", self.page.url).group(1))
        row = self.wait_until(lambda: (lambda r: r if r and r[0]["status"] != "planning" else None)(
            self.q("SELECT status, error FROM campaigns WHERE id=?", cid)), "the plan", 60)[0]
        if row["status"] != "ready":
            raise StepFailed(f"the planner ended '{row['status']}' ({row['error']})")
        self.page.wait_for_selector("h2:has-text('The plan')", timeout=15000)   # the page reloads itself when it's ready
        self.campaign_url = self.page.url
        main = self.text()
        sec = self.page.locator("section.camp-sec:has(h2:has-text('Launch checklist'))")
        checklist = sec.inner_text() if sec.count() else ""
        yn = {True: "yes", False: "no"}
        note = (f"plan: {self.page.locator('#ads .adrow').count()} ads proposed, budget split {yn['Budget split' in main]}, "
                f"copy {yn['Copy for each platform' in main]}")
        if "google" in platforms_on:
            kw = self.page.locator("#keywords table.kwlist tbody tr").count()
            note += f", search keyword list {yn[bool(kw)]}" + (f" ({kw} terms)" if kw else "")
        if goal == "leads":
            lead_items = bool(re.search(r"(?i)\bform\b", checklist) and re.search(r"(?i)\bflow\b", checklist))
            links = sec.locator("a[href='/app/pages'], a[href='/app/forms'], a[href='/app/flows']").count() if sec.count() else 0
            note += f", lead page/form/follow-up in the checklist {yn[lead_items]}" + (f" ({links} linked)" if links else "")
        st.notes.append(note)
        return main

    def s_creative(self, st, videos=True):
        if not self.page.url.startswith(self.campaign_url):
            self.page.goto(self.campaign_url)   # the plan page is where the previous step left them
        boxes = self.page.locator("#adform input[name=ad]")
        for i in range(boxes.count()):
            self.check(boxes.nth(i), True if videos else boxes.nth(i).is_checked())
        before = self.last_job_id()
        n = self.page.locator("#adform input[name=ad]:checked").count()
        self.paid_click("#genbtn")
        self.page.wait_for_load_state()
        rows = self.wait_jobs(before, n, timeout=180)
        kinds = self.q("SELECT tool, COUNT(*) n FROM jobs WHERE workspace_id=? AND id>? GROUP BY tool", self.ws_id, before)
        outs = self.q("SELECT COUNT(*) FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE j.workspace_id=? AND j.id>?",
                      self.ws_id, before)[0][0]
        st.notes.append(f"{len(rows)} jobs, {outs} finished ads ({', '.join(f'{k[1]} {k[0]}' for k in kinds)}); "
                        f"target 2 talking videos + 4 image ads")

    def s_copy(self, st, platform):
        self.nav("/app/copy")
        if self.page.locator("select[name=platform]").count():
            self.select("select[name=platform]", platform)
        self.paid_click("form button:has-text('Write it')")
        self.page.wait_for_url(re.compile(r"/app/copy/\d+"))
        main = self.text()
        if "Checks passed" not in main:
            raise StepFailed("no variant passed the checks")
        st.notes.append(f"{main.count('Checks passed')} variants with checks passed ({platform})")

    def s_page(self, st, title, form_name=None):
        self.nav("/app/pages")
        self.fill("form.panel input[name=title]", title)
        self.click("form.panel button:has-text('New page')")
        self.page.wait_for_url(re.compile(r"/app/pages/\d+"))
        pid = int(re.search(r"/app/pages/(\d+)", self.page.url).group(1))
        if not self.page.locator("details.aidraft[open]").count():
            self.click("details.aidraft > summary")
        self.paid_click("details.aidraft form button")
        self.click("dialog[open] button:has-text('Draft the copy')")
        self.page.wait_for_load_state()
        self.page.wait_for_selector("text=Drafted the copy", timeout=15000)
        if self.page.locator("input[name=b0_media]").count() > 1:
            self.click(self.page.locator("input[name=b0_media]").nth(1))
        if form_name:
            self.click("button[name=action][value='add:form']")
            self.page.wait_for_load_state()
            sel = self.page.locator("select[name$=_form]").last
            self.act()
            sel.select_option(label=form_name)
        self.click("button[name=action][value=publish]")
        self.page.wait_for_load_state()
        p = self.q("SELECT slug, status FROM pages WHERE id=?", pid)[0]
        if p["status"] != "published":
            raise StepFailed("the page didn't publish: " + " ".join(self.page.locator(".flash").all_inner_texts()))
        self.page_slug = p["slug"]
        r = self.ctx.request.get(f"{self.base}/p/{p['slug']}")
        if r.status != 200:
            raise StepFailed(f"the live page answers {r.status}")
        st.notes.append(f"live at /p/{p['slug']}")

    def s_export(self, st):
        self.nav("/app/library")
        dl = self.page.locator("a[download], a:has-text('Download')").first
        if not dl.count():
            raise StepFailed("no Download link in the library")
        with self.page.expect_download(timeout=15000) as d:
            self.click(dl)
        f = d.value
        size = os.path.getsize(f.path()) if f.path() else 0
        if size < 100:
            raise StepFailed("the download is empty")
        st.notes.append(f"downloaded {f.suggested_filename} ({size // 1024} KB)")

    def s_launch_kit(self, st, email, platforms):
        """RS_LAUNCH_KIT: one screen holds the launch; ticks persist across devices."""
        self.nav("/app/campaigns")
        self.click(self.page.locator("a.runrow").first)
        self.page.wait_for_url(re.compile(r"/app/campaigns/\d+"))
        kit = self.page.locator("[data-launch-kit]")
        if not kit.count():
            raise StepFailed("the plan page has no launch kit")
        missing = [p for p in platforms if not self.page.locator(f"#kit-{p}").count()]
        if missing:
            raise StepFailed("no launch-kit block for " + ", ".join(missing))
        exports = self.page.locator("#kit-meta .lk-ex a[download]")
        copies = self.page.locator("[data-launch-kit] [data-copy]").count()
        if not exports.count():
            raise StepFailed("the Meta block has no export links")
        with self.page.expect_download(timeout=30000) as d:
            self.click(exports.first)
        size = os.path.getsize(d.value.path()) if d.value.path() else 0
        if size < 100:
            raise StepFailed("the export is empty")
        boxes = self.page.locator("[data-launch-kit] .lk-item:not(.done) .lk-box")
        keys = []
        for i in range(2):
            b = boxes.first
            keys.append(b.locator("xpath=ancestor::li[1]").get_attribute("id"))
            with self.page.expect_response(lambda r: "/launch" in r.url and r.request.method == "POST"):
                self.click(b)
        other = self.ctx.browser.new_context()
        try:   # another device: a fresh browser, logged in to the same account (not counted as user actions)
            o = other.new_page()
            other.route("**/*", lambda rt: rt.continue_() if rt.request.url.startswith(self.base) else rt.abort())
            o.goto(f"{self.base}/login")
            o.fill("input[name=email]", email)
            o.fill("input[name=password]", PW)
            o.click("form button.btn")
            o.wait_for_load_state()
            o.goto(self.page.url.split("#")[0])
            seen = [k for k in keys if o.locator(f"li#{k}.done").count()]
        finally:
            other.close()
        if len(seen) != len(keys):
            raise StepFailed(f"ticks didn't persist to a second browser ({len(seen)} of {len(keys)})")
        st.notes.append(f"{self.page.locator('[data-launch-kit] .lk-block').count()} platform blocks, "
                        f"{exports.count()} Meta export links, {copies} Copy buttons; export {size // 1024} KB; "
                        f"{len(keys)} ticks seen from a second browser")

    # ---------------------------------------------------------------- service-only steps
    def s_form(self, st, name):
        self.nav("/app/forms")
        self.fill("input[name=name]", name)
        self.click("button:has-text('New form')")
        self.page.wait_for_url(re.compile(r"/app/forms/\d+"))
        self.click("button[name=action][value='add:phone']")
        self.page.wait_for_load_state()
        self.click("button[name=action][value=save]")
        self.page.wait_for_load_state()
        self.form_id = int(re.search(r"/app/forms/(\d+)", self.page.url).group(1))
        self.form_code = self.q("SELECT code FROM forms WHERE id=?", self.form_id)[0]["code"]

    def s_flow(self, st):
        self.nav("/app/flows")
        self.click(self.page.locator("button.nf-tpl:has-text('Lead magnet')").first)
        self.page.wait_for_url(re.compile(r"/app/flows/\d+"))
        fid = int(re.search(r"/app/flows/(\d+)", self.page.url).group(1))
        self.click("button[data-turnon]")
        self.page.wait_for_load_state()
        f = self.q("SELECT status FROM flows WHERE id=?", fid)[0]
        if f["status"] != "on":
            raise StepFailed("the flow didn't turn on: " + " ".join(self.page.locator(".flash").all_inner_texts())[:200])
        self.flow_id = fid
        st.notes.append("template 'Lead magnet', trigger: any form submitted; emails become tasks until sending is set up")

    def s_lead(self, st):
        """A visitor (not the user: these actions are not counted) sends the form on the live page."""
        visitor = self.ctx.browser.new_context()
        try:
            v = visitor.new_page()
            visitor.route("**/*", lambda rt: rt.continue_() if rt.request.url.startswith(self.base) else rt.abort())
            v.goto(f"{self.base}/p/{self.page_slug}")
            frm = v.locator("form.pp-form").first
            if not frm.count():
                raise StepFailed("the live page has no form")
            t0 = time.monotonic()
            for inp in frm.locator("input[name^=f_], textarea[name^=f_]").all():
                kind = inp.get_attribute("type") or "text"
                if kind == "checkbox":
                    inp.check()
                else:
                    inp.fill({"email": "lead@example.org", "tel": "972-555-0199"}.get(kind, "Pat Lee"))
            for box in frm.locator("input[name^=_consent]").all():
                box.check()
            time.sleep(max(0.0, 2.5 - (time.monotonic() - t0)))   # the form refuses bots that send in under 2 s
            frm.locator("button.pp-btn").click()
            v.wait_for_load_state()
            if not v.locator(".pp-ok").count():
                raise StepFailed("the form didn't send: " + v.inner_text("body")[:160].replace("\n", " "))
        finally:
            visitor.close()
        c = self.wait_until(lambda: self.q("SELECT * FROM contacts WHERE workspace_id=? AND email=?", self.ws_id,
                                           "lead@example.org"), "the lead in Contacts", 10)[0]
        runs = self.q("SELECT COUNT(*) FROM flow_runs WHERE flow_id=?", getattr(self, "flow_id", 0))[0][0]
        st.notes.append(f"lead in Contacts (source: {c['source'] or 'none'}); {'in the follow-up flow' if runs else 'NOT in the follow-up flow'}; "
                        "cost per lead needs the ad platform connection (M3)")
        if not runs:
            raise StepFailed("the lead didn't enter the follow-up flow")


# ====================================================================================== paths

def comp_code():
    import compcodes
    import db
    with db.connect() as conn:
        _, rows = compcodes.generate(conn, plan="growth", months=1, count=1)
    return rows[0]["code"]


def product_path(r):
    email = "owner@hearth.example"
    r.step("signup", "Sign up", lambda st: r.s_signup(st, email))
    r.step("card", "Save a card (nothing charged)", r.s_card)
    r.step("verify", "Confirm the email (code)", lambda st: r.s_verify(st, email))
    r.step("brand_kit", "Paste the product page (brand kit)", lambda st: r.s_brand_kit(st, F.PRODUCT_URL, "Ember Candle"))
    r.step("first_ads", "First ads (2 image ads with the free credits)", r.s_first_ads)
    if paywall_on():
        r.step("free_plan", "Free campaign plan when out of credits (paywall)", r.s_free_plan)
    code = comp_code()
    r.step("plan_pick", "Pick a plan", lambda st: r.s_pick_plan(st, code))
    r.step("planner", "Plan the campaign (Sales; Meta, Google)",
           lambda st: r.s_plan(st, "sales", ["meta", "google"], "$600 for two weeks", F.PRODUCT_URL))
    r.step("creative", "Creative batch from the plan", lambda st: r.s_creative(st, videos=True))
    r.step("copy", "Copy for Google Search", lambda st: r.s_copy(st, "google"))
    r.step("landing", "Landing page, published", lambda st: r.s_page(st, "Ember Candle"))
    r.step("export", "Export an ad (download)", r.s_export)
    if os.environ.get("RS_LAUNCH_KIT", "").strip().lower() in ("1", "on", "true", "yes"):
        r.step("launch_kit", "Launch kit on the plan page (ticks on a second device)",
               lambda st: r.s_launch_kit(st, email, ["meta", "google"]))


def service_path(r):
    email = "owner@northside-hvac.example"
    r.step("signup", "Sign up", lambda st: r.s_signup(st, email))
    r.step("card", "Save a card (nothing charged)", r.s_card)
    r.step("verify", "Confirm the email (code)", lambda st: r.s_verify(st, email))
    r.step("brand_kit", "Paste the website (brand kit)",
           lambda st: r.s_brand_kit(st, F.SERVICE_URL, "Northside", sells="service"))
    r.step("first_ads", "First ads (from the website, no product photo)" if service_path_on()
           else "First ads (the first run is product-only)", r.s_first_ads)
    if paywall_on():
        r.step("free_plan", "Free campaign plan when out of credits (paywall)", r.s_free_plan)
    code = comp_code()
    r.step("plan_pick", "Pick a plan", lambda st: r.s_pick_plan(st, code))
    r.step("planner", "Plan the campaign (Leads; Google, Meta)",
           lambda st: r.s_plan(st, "leads", ["google", "meta"], "$900 for the first month", F.SERVICE_URL))
    r.step("copy", "Copy for Google Search (RSA)", lambda st: r.s_copy(st, "google"))
    r.step("form", "Quote form", lambda st: r.s_form(st, "Free quote"))
    r.step("landing", "Landing page with the quote form, published",
           lambda st: r.s_page(st, "Free AC quote", form_name="Free quote"))
    r.step("flow", "Follow-up flow", r.s_flow)
    r.step("lead", "A visitor sends the form; the lead lands in Contacts", r.s_lead)


PATHS = {"product": product_path, "service": service_path}


# ====================================================================================== report

def summarize(steps):
    out = {}
    for path in PATHS:
        ss = [s for s in steps if s.path == path]
        if not ss:
            continue
        gap = next((s for s in ss if s.status == "known_gap"), None)
        failed = [s for s in ss if s.status == "fail"]
        before = [s for s in ss if not s.after_gap and s.status != "known_gap"]
        out[path] = {
            "verdict": "fail" if failed else ("blocked (known gap " + gap.gap + " at " + gap.key + ")") if gap else "pass",
            "seconds": round(sum(s.seconds for s in ss), 1),
            "actions": sum(s.actions for s in ss),
            "seconds_to_gap": round(sum(s.seconds for s in before), 1) if gap else None,
            "actions_to_gap": sum(s.actions for s in before) if gap else None,
            "credits": sum(s.charged for s in ss),
            "paid_buttons": sum(len(s.paid) for s in ss),
            "left_app": sum(len(s.left_app) for s in ss),
        }
    return out


def markdown(steps, summary, render_seconds):
    lines = ["| Path | Step | Result | Wall clock (s) | User actions | Credits | Priced buttons | Notes |",
             "|---|---|---|---:|---:|---:|---|---|"]
    label = {"pass": "pass", "fail": "**FAIL**", "known_gap": "known gap", "not_built": "not built"}
    for s in steps:
        res = label[s.status] + (f" ({s.gap})" if s.gap else "") + (" · after the gap" if s.after_gap else "")
        paid = "; ".join(f"{lbl} ({'free' if k == 'free' else f'{shown:,} cr'})" for lbl, shown, k in s.paid) or ""
        notes = "; ".join([s.error] + s.notes if s.error else s.notes).replace("|", "/")
        secs = "" if s.status == "not_built" else f"{s.seconds:.1f}"
        acts = "" if s.status == "not_built" else str(s.actions)
        lines.append(f"| {s.path} | {s.title} | {res} | {secs} | {acts} | {s.charged or ''} | {paid.replace('|', '/')} | {notes} |")
    lines.append("")
    lines.append("| Path | Verdict | Wall clock (s) | User actions | To the gap (s / actions) | Credits spent | Priced buttons | Left the app |")
    lines.append("|---|---|---:|---:|---|---:|---:|---:|")
    for p, v in summary.items():
        gap = f"{v['seconds_to_gap']} / {v['actions_to_gap']}" if v["seconds_to_gap"] is not None else ""
        lines.append(f"| {p} | {v['verdict']} | {v['seconds']} | {v['actions']} | {gap} | {v['credits']:,} | "
                     f"{v['paid_buttons']} | {v['left_app']} |")
    lines.append("")
    lines.append(f"Fake render time {render_seconds} s per job; the worker polls every 1 s. Wall clock is this machine "
                 "with faked providers, so it measures the app's own steps and waits, not vendor render time. "
                 "North-star target per path: under 25 minutes of user time, under 60 minutes wall clock.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--path", choices=["product", "service", "both"], default="both")
    ap.add_argument("--render-seconds", type=float, default=3.0, help="fake render time per job (default 3)")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--json", help="write the results as JSON here")
    ap.add_argument("--markdown", help="write the results table (Markdown) here")
    ap.add_argument("--shots", help="save a full-page screenshot of every failed or known-gap step here")
    ap.add_argument("--service-path", action="store_true", help="run with RS_SERVICE_PATH=1 (roadmap M5.2)")
    args = ap.parse_args()
    global KNOWN_GAPS
    if args.service_path:
        os.environ["RS_SERVICE_PATH"] = "1"
    KNOWN_GAPS = known_gaps()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright for Python is required (pip install playwright; Chromium must be installed).")

    base, srv = boot(args.render_seconds)
    flags = " ".join(f"{k}={'on' if on else 'off'}" for k, on in (
        ("RS_SERVICE_PATH", service_path_on()), ("RS_NEXT_STEPS", next_steps_on()), ("RS_PLAN_PAYWALL", paywall_on()),
        ("RS_LAUNCH_KIT", os.environ.get("RS_LAUNCH_KIT", "").strip().lower() in ("1", "on", "true", "yes")),
        ("RS_CARD_REQUIRED", True)))
    print(f"== golden path against {base} (providers faked; {flags})")
    steps = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, args=["--disable-dev-shm-usage"])
        for path in (["product", "service"] if args.path == "both" else [args.path]):
            r = Run(base, browser, path, args)
            print(f"-- {path} path")
            try:
                PATHS[path](r)
            finally:
                for key, title, ms in NOT_BUILT[path]:
                    st = Step(path, key, title)
                    st.status, st.notes = "not_built", [f"roadmap {ms}"]
                    r.steps.append(st)
                for s in r.steps:
                    mark = {"pass": "ok  ", "fail": "FAIL", "known_gap": "gap ", "not_built": "todo"}[s.status]
                    print(f"  {mark} {s.title}: {s.seconds:.1f} s, {s.actions} actions"
                          + (f", {s.charged} credits" if s.charged else "") + (f" - {s.error}" if s.error else ""))
                if r.blocked_hosts:
                    print("  (outside requests the pages made, blocked to stay offline: "
                          + ", ".join(sorted(r.blocked_hosts)) + ")")
                steps += r.steps
                r.ctx.close()
        browser.close()
    srv.shutdown()
    import shutil
    shutil.rmtree(os.environ["RS_DATA"], ignore_errors=True)   # the throwaway database and media
    summary = summarize(steps)
    table = markdown(steps, summary, args.render_seconds)
    print()
    print(table)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"summary": summary, "steps": [s.row() for s in steps], "render_seconds": args.render_seconds},
                      fh, indent=1)
    if args.markdown:
        with open(args.markdown, "w") as fh:
            fh.write(table + "\n")
    failed = [s for s in steps if s.status == "fail"]
    if failed:
        print(f"\nFAIL: {len(failed)} step(s) failed: " + ", ".join(f"{s.path}/{s.key}" for s in failed))
        sys.exit(1)
    print("\nOK: every step passed, is a known gap, or is not built yet.")


if __name__ == "__main__":
    main()
