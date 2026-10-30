"""The setup assistant: a short conversation that sets a new workspace up
(brand -> first product -> photos -> approved claims -> audience -> goal and
platforms -> first ad, then optionally ad accounts and a landing page), and a
"Help me" guide on any screen afterwards.

It works only through tools that call the same functions the app's own pages
use (products.py, the import, the plan limits, compliance), scoped to the
member's workspace. Three hard rules are enforced here, not just asked for:
  * It never spends credits. Anything that costs credits (reading a product
    page with AI, making the first ad) becomes a card the member confirms;
    the confirmation runs server-side at the price on the card.
  * It never invents claims. save_claims only accepts lines the member typed
    in this conversation (or approves from the page import), and they must pass
    the compliance checks.
  * It pays its own way cheaply: the fast model, a free allowance per
    workspace (FREE_CREDITS), then cost x markup only after the member agrees.
"""
import json
import os
import re
from datetime import datetime, timezone

import catalog
import compliance
import db
import llm
import platforms
import plans
import products

MAX_STEPS = 6            # model calls per member message
KICKOFF = "(app) The seller opened the guide"
TURN_TOKENS = 700
KEEP_MESSAGES = 40
STEPS = ["brand", "product", "photos", "claims", "audience", "goal", "first_ad"]
OPTIONAL = ["ad_accounts", "page"]
GOALS = ["sales", "leads", "traffic", "awareness"]
LINKS = {"products": "brand_kit", "create": "create.index", "ad_accounts": "adhub.index", "pages": "pages.index",
         "billing": "billing_page", "library": "library", "planner": "campaigns.index", "copy": "copy.studio",
         "seo": "seo.index", "team": "team"}


def free_credits() -> int:
    env = os.environ.get("RS_ASSISTANT_FREE", "").strip()
    return int(env) if env.isdigit() else 60


SYSTEM = """You are Campaignify's setup guide. You help a seller go from an empty account to their first ad in a few
minutes, by calling tools that act on their workspace. They see your messages in a small chat panel.

Setup, in order (call get_setup_state first, and again after changes; skip anything already done):
1. Brand: ask its name; create_brand (or update_brand to rename the one they have).
2. First product: ask for the product's page link and offer to import it (propose_import: reading the page with AI
   costs a couple of credits and the member confirms on a card), or ask for its name, a one-line description and price
   (create_product).
3. Photos: call request_photo. Its card shows the photos found on their product page (they tick the ones to use)
   and an upload box, right here in the chat. Don't send them to another screen to pick photos.
4. Approved claims: claims are only what's printed on the label or what the seller is allowed to say. NEVER suggest,
   write, improve or guess claims. If the import found suggested claims, call review_suggested_claims so the seller
   approves the true ones. Otherwise ask them to type the claims exactly as on the label and save only what they
   typed (save_claims). Having no claims is fine.
5. Audience: ask who it's for and save it (update_product). You may tidy their wording, not invent a different audience.
6. Goal and platforms: ask, then set_goal.
7. First ad: propose_first_ad. An image ad is the cheapest start; a talking UGC video opens the Create flow. You never
   start anything that costs credits yourself: the seller confirms every spend on a card (pressing its button, or
   typing yes / make it while the card is open, which the app treats as pressing it).
   NEVER say an ad is being made, started, queued, on its way or done unless an "(app)" note in this conversation
   says it was started. Until then it has NOT started: point them to the Make it button on the card.
8. Optional, after the first ad: ad-account setup and a landing page (show_link).

What "set up" means: the seller is activated once their first ad exists. Everything before that is the shortest path
to it; skip or postpone anything that isn't on that path. After it, the checklist continues in this order: a landing
page to send people to, then an ad account to run the ad. Keep one goal per session: finish the step in front of them
before raising the next, and don't list everything left at once.

If get_setup_state has a "nudge" (the seller has stalled: an ad made days ago with nothing after it, or setup left
half done), open with it once in your own short words and offer its link with show_link. Don't repeat it if they
decline or move on.

How to talk: 1-3 short sentences per message, one question at a time, plain friendly words, no hype, no emojis.
Use offer_choices for quick answers when it helps. Don't repeat what a card already says. Never promise results,
sales or income. If they ask about something else in Campaignify, answer briefly and point to the right screen with
show_link. Messages that start with "(app)" are notes from the app about what the seller just did on a card."""


def _schema(props: dict, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


S = {"type": "string"}
TOOLS = [
    {"name": "get_setup_state", "description": "What's set up so far: brand, active product (photos, claims, audience), "
     "goal, platforms, first ad, credits, plan, and the screen the seller is on.", "input_schema": _schema({})},
    {"name": "create_brand", "description": "Add a brand and make it active (the plan limits how many).",
     "input_schema": _schema({"name": S, "about": S, "website": S}, ["name"])},
    {"name": "update_brand", "description": "Change the active brand's name, about, website, tone or default audience.",
     "input_schema": _schema({"name": S, "about": S, "website": S, "tone": S, "audience": S})},
    {"name": "create_product", "description": "Add a product to the active brand and make it active. No claims here.",
     "input_schema": _schema({"name": S, "description": S, "price": S, "audience": S, "url": S, "tags": S}, ["name"])},
    {"name": "update_product", "description": "Change the active product's name, description, price, offer, audience, "
     "link or tags. Not claims.", "input_schema": _schema({"name": S, "description": S, "price": S, "offer": S,
                                                           "audience": S, "url": S, "tags": S})},
    {"name": "save_claims", "description": "Save approved claims the seller typed in this chat, word for word. Lines "
     "they didn't type are refused.", "input_schema": _schema({"claims": {"type": "array", "items": S}}, ["claims"])},
    {"name": "propose_import", "description": "Show a card offering to import a product from its page link (AI read, "
     "a couple of credits). Nothing happens until the seller confirms.", "input_schema": _schema({"url": S}, ["url"])},
    {"name": "review_suggested_claims", "description": "Show the claims the page import suggested so the seller can "
     "approve the true ones.", "input_schema": _schema({})},
    {"name": "request_photo", "description": "Show an upload card for product photos.", "input_schema": _schema({})},
    {"name": "set_goal", "description": "Save the campaign goal and the platforms they'll run on.",
     "input_schema": _schema({"goal": {"type": "string", "enum": GOALS},
                              "platforms": {"type": "array", "items": {"type": "string", "enum": platforms.SOCIAL + ["google"]}}},
                             ["goal"])},
    {"name": "propose_first_ad", "description": "Show a card for the first ad: an image ad (made when the seller "
     "confirms, at the price shown) or a talking UGC video (opens Create, set up for this product).",
     "input_schema": _schema({"kind": {"type": "string", "enum": ["image", "ugc"]},
                              "template": {"type": "string", "enum": [t["id"] for t in catalog.first_run_templates()]}},
                             ["kind"])},
    {"name": "show_link", "description": "Show a button to a screen in Campaignify.",
     "input_schema": _schema({"target": {"type": "string", "enum": list(LINKS) + ["product"]}, "label": S}, ["target"])},
    {"name": "offer_choices", "description": "Show up to 4 quick replies the seller can tap.",
     "input_schema": _schema({"choices": {"type": "array", "items": S}}, ["choices"])},
]
CARD_TOOLS = {"propose_import", "review_suggested_claims", "request_photo", "propose_first_ad", "show_link", "offer_choices"}


# ---------------------------------------------------------------- state

def state(conn, ws, setup: dict, screen: str = "") -> dict:
    b, p = products.load_active(conn, ws)
    photos = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (p["id"],)).fetchone()[0] if p else 0
    nb = len(products.brands(conn, ws["id"]))
    made = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=?", (ws["id"],)).fetchone()[0]
    hub = conn.execute("SELECT COUNT(*) FROM adhub_progress WHERE workspace_id=?", (ws["id"],)).fetchone()[0]
    pages_n = conn.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=?", (ws["id"],)).fetchone()[0]
    claims = products.claims_list(p["claims"]) if p else []
    done = {"brand": bool(b and (b["brand_name"] or "").strip()), "product": p is not None, "photos": photos > 0,
            "claims": bool(claims) or bool(setup.get("no_claims")),
            "audience": bool(p and ((p["audience"] or "").strip() or (b["audience"] or "").strip())),
            "goal": bool(setup.get("goal")), "first_ad": made > 0, "ad_accounts": hub > 0, "page": pages_n > 0}
    return {"done": done, "next": next((s for s in STEPS if not done[s]), None),
            "brand": {"name": b["brand_name"] if b else "", "count": nb, "plan_allows": plans.limit(ws, "brands")},
            "product": None if p is None else {"name": p["name"], "description": p["description"] or "",
                                               "price": p["price"] or "", "audience": p["audience"] or "",
                                               "url": p["url"] or "", "photos": photos, "approved_claims": claims,
                                               "suggested_claims_waiting": len(products.pending(p))},
            "goal": setup.get("goal"), "platforms": setup.get("platforms") or [],
            "credits": db.balance(ws), "plan": plans.plan(ws)["name"], "ads_made": made, "screen": screen,
            "nudge": nudge(conn, ws, done),
            "ai_import_credits": _import_quote(), "first_image_ad_credits": catalog.price_credits("ad_image", batch=1),
            "talking_video_credits_10s": catalog.price_credits("ugc_video", duration=10)}


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - t).days)


def nudge(conn, ws, done: dict | None = None) -> dict | None:
    """One free, rule-based suggestion for a seller who has stalled (no AI): {"id", "text", "label", "target"}.
    `target` is a key of LINKS. None when there's nothing worth saying."""
    if done is None:
        done = state(conn, ws, {})["done"]
    last = conn.execute("SELECT created_at FROM jobs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (ws["id"],)).fetchone()
    if last is None:
        age = _days_since(ws["created_at"])
        if age is not None and age >= 1 and done.get("product"):
            return {"id": "first_ad", "text": "Your product is in, but there's no ad yet. An image ad takes about a minute.",
                    "label": "Make the first ad", "target": "create"}
        return None
    idle = _days_since(last["created_at"])
    if idle is None or idle < 2:
        return None
    made = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND status='done'", (ws["id"],)).fetchone()[0]
    ads = f"{made} ad" + ("" if made == 1 else "s")
    copies = conn.execute("SELECT COUNT(*) FROM copy_runs WHERE workspace_id=?", (ws["id"],)).fetchone()[0]
    if made and not copies:
        return {"id": "captions", "text": f"You made {ads} {idle} days ago. Want captions and ad copy to post with it?",
                "label": "Write captions", "target": "copy"}
    if made and not done.get("page"):
        return {"id": "page", "text": f"You have {ads} ready. A short landing page gives the ad somewhere to send people.",
                "label": "Build a page", "target": "pages"}
    if made and not done.get("ad_accounts"):
        return {"id": "ad_accounts", "text": f"You have {ads} ready. The ad-account checklist gets it running.",
                "label": "Set up ad accounts", "target": "ad_accounts"}
    return None


def _import_quote():
    import product_import
    return product_import.estimate()


# ---------------------------------------------------------------- claims guard

def _words(t):
    return [w for w in re.findall(r"[a-z0-9%$.]+", (t or "").lower()) if len(w) > 2 or any(c.isdigit() for c in w)]


def typed_by_member(claim: str, member_texts: list[str]) -> bool:
    """True when the claim's words come from one thing the member typed."""
    want = _words(claim)
    if not want:
        return False
    for t in member_texts:
        have = set(_words(t))
        if sum(1 for w in want if w in have) / len(want) >= 0.85:
            return True
    return False


def member_texts(messages) -> list[str]:
    out = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str) and not m["content"].startswith("(app)"):
            out.append(m["content"])
    return out


# ---------------------------------------------------------------- tools

def dispatch(name, inp, ctx):
    """Run one tool. ctx: conn, ws (row, refreshed), setup (dict), messages, cards (dict), screen."""
    conn, ws = ctx["conn"], ctx["ws"]
    try:
        if name == "get_setup_state":
            return state(conn, ws, ctx["setup"], ctx["screen"])
        if name == "create_brand":
            have = products.brands(conn, ws["id"])
            if len(have) == 1 and not (have[0]["brand_name"] or "").strip():
                products.update_brand(conn, ws["id"], have[0]["id"], {"brand_name": inp.get("name") or "",
                                                                      "about": inp.get("about") or "",
                                                                      "website": inp.get("website") or ""})
                return {"ok": True, "note": "named the workspace's first brand"}
            bid = products.create_brand(conn, ws, inp.get("name") or "", about=inp.get("about") or "",
                                        website=inp.get("website") or "")
            products.activate(conn, ws, brand_id=bid)
            return {"ok": True}
        if name == "update_brand":
            vals = {k2: inp[k1] for k1, k2 in (("name", "brand_name"), ("about", "about"), ("website", "website"),
                                                ("tone", "tone"), ("audience", "audience")) if inp.get(k1) is not None}
            products.update_brand(conn, ws["id"], ws["brand_id"], vals)
            return {"ok": True}
        if name == "create_product":
            form = {k: inp.get(k) or "" for k in ("name", "description", "price", "audience", "url", "tags")}
            pid = products.create_product(conn, ws, ws["brand_id"], form)
            products.activate(conn, ws, product_id=pid)
            return {"ok": True, "product_id": pid}
        if name == "update_product":
            if not ws["product_id"]:
                return {"error": "there's no product yet; create one first"}
            form = {k: inp[k] for k in ("name", "description", "price", "offer", "audience", "url", "tags")
                    if inp.get(k) is not None}
            products.update_product(conn, ws["id"], ws["product_id"], form)
            return {"ok": True}
        if name == "save_claims":
            if not ws["product_id"]:
                return {"error": "there's no product yet"}
            said = member_texts(ctx["messages"])
            take, refused = [], []
            for c in [str(x).strip()[:160] for x in inp.get("claims") or [] if str(x).strip()][:12]:
                if not typed_by_member(c, said):
                    refused.append(f"{c!r}: the seller didn't type this")
                elif not compliance.check(c)["ok"]:
                    refused.append(f"{c!r}: " + " ".join(compliance.check(c)["blocks"]))
                else:
                    take.append(c)
            if take:
                p = products.get_product(conn, ws["id"], ws["product_id"])
                have = products.claims_list(p["claims"])
                products.update_product(conn, ws["id"], p["id"], {"claims": "\n".join(have + [c for c in take if c not in have])})
            if not take and inp.get("claims") == []:
                ctx["setup"]["no_claims"] = True
            return {"ok": bool(take), "saved": take, "refused": refused,
                    **({"note": "Ask the seller to type the exact claims from the label."} if refused and not take else {})}
        if name == "set_goal":
            ctx["setup"]["goal"] = inp.get("goal") if inp.get("goal") in GOALS else "sales"
            ctx["setup"]["platforms"] = [x for x in inp.get("platforms") or [] if x in platforms.PLATFORMS][:6]
            return {"ok": True}
        if name in CARD_TOOLS:
            return _card(name, inp, ctx)
    except products.ProductError as e:
        return {"error": str(e)}
    return {"error": f"unknown tool {name}"}


def _card(name, inp, ctx):
    """Cards are shown in the chat; anything that costs credits runs only when
    the seller confirms it (assistant_routes.action)."""
    conn, ws = ctx["conn"], ctx["ws"]
    card = {"kind": name, "status": "open"}
    if name == "propose_import":
        try:
            import fetcher
            fetcher.check_url(inp.get("url") or "")
        except Exception as e:  # noqa: BLE001
            return {"error": f"that link won't work: {e}"}
        card.update(url=str(inp["url"]).strip()[:500], credits=_import_quote() if llm.anthropic_ready() else 0)
    elif name == "review_suggested_claims":
        p = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
        pend = products.pending(p) if p else []
        if not pend:
            return {"error": "there are no suggested claims waiting; ask the seller to type theirs"}
        card.update(claims=pend, product=p["id"])
    elif name == "request_photo":
        if not ws["product_id"]:
            return {"error": "create the product first"}
        card.update(product=ws["product_id"])
    elif name == "propose_first_ad":
        p = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
        if p is None:
            return {"error": "create the product first"}
        if inp.get("kind") == "ugc":
            if not catalog.capabilities()["talking"]:
                return {"error": "talking videos aren't available; propose an image ad"}
            card.update(ad="ugc", product=p["id"], credits=catalog.price_credits("ugc_video", duration=10))
        else:
            tpl = next((t for t in catalog.first_run_templates() if t["id"] == inp.get("template")), None) \
                or catalog.first_run_templates()[0]
            card.update(ad="image", template=tpl["id"], template_name=tpl["name"], product=p["id"],
                        credits=catalog.price_credits("ad_image", batch=1))
        card["product_name"] = p["name"]
    elif name == "show_link":
        tgt = inp.get("target")
        if tgt not in LINKS and tgt != "product":
            return {"error": "unknown screen"}
        card.update(target=tgt, label=str(inp.get("label") or "")[:60])
    elif name == "offer_choices":
        card.update(choices=[str(c)[:60] for c in inp.get("choices") or [] if str(c).strip()][:4])
    ctx["new_cards"].append(card)
    if name == "propose_first_ad" and card.get("ad") == "image":
        return {"ok": True, "shown": True, "note": "Nothing is made yet. It starts only when the seller presses Make it "
                "(or types yes / make it); an (app) note will tell you. Until then don't say it's being made."}
    if name == "request_photo":
        p = products.get_product(conn, ws["id"], ws["product_id"])
        own, others = products.import_candidates(p)
        found = own or others
        return {"ok": True, "shown": True, "photos_found_on_page": len(found),
                "note": "The card shows those page photos to tick, plus an upload box. Wait for them."}
    if name in ("offer_choices", "show_link"):
        return {"ok": True, "shown": True}
    return {"ok": True, "shown": True, "note": "The seller sees a card. Wait for them; don't call this again now."}


# ---------------------------------------------------------------- one turn

def trim(messages):
    """Keep the tail of the conversation, starting at a member's message so
    tool calls and their results stay paired."""
    if len(messages) <= KEEP_MESSAGES:
        return messages
    cut = len(messages) - KEEP_MESSAGES
    while cut < len(messages) and not (messages[cut]["role"] == "user" and isinstance(messages[cut]["content"], str)):
        cut += 1
    return messages[cut:]


_PROGRESS = (r"being (made|built|created|generated|rendered)|on (its|it's|the) way|in the works|(is|are|it's|its) "
             r"(generating|rendering|cooking|processing|queued|started|underway|running)|(i'?ve|i have|i) (started|"
             r"kicked off|queued|submitted|launched)|(making|building|creating|generating|rendering) (it|your (first )?"
             r"(ad|video)|the (ad|video)|that) now")
_DONE = r"(it'?s|it is|your (ad|video) is|the (ad|video) is) (done|ready|finished|live)"
_LIBRARY = r"(check|see|find) (it )?(in )?(the|your) library"
STARTED_RE = re.compile(rf"\b({_PROGRESS}|{_DONE}|{_LIBRARY})", re.I)
PROGRESS_RE, DONE_RE, LIBRARY_RE = (re.compile(rf"\b({x})", re.I) for x in (_PROGRESS, _DONE, _LIBRARY))
NOT_STARTED = ("Nothing has started yet. Press **Make it** on the card above (or type \"make it\") and I'll start "
               "it right away.")
NOTHING_MADE = ("Nothing is being made yet. I can't start ads myself: they start when you press the button on a card "
                "(Make it, or Open Create for a video).")


def awaiting_make(cards: dict) -> bool:
    """True when an image first-ad card is open: the ad is offered, not made."""
    return any(c.get("kind") == "propose_first_ad" and c.get("ad") == "image" and c.get("status") == "open"
               for c in (cards or {}).values())


def guard_started(thread: dict, start: int, shown: list[dict], job_started: bool, in_flight: bool = True,
                  made: bool = True) -> list[dict]:
    """The guide may not tell the seller an ad is being made when the app
    didn't start one. Rewrites such lines from this turn (in the stored
    conversation too, so the model doesn't keep believing it).

    While an image Make-it card is open, any started/done talk is wrong. With
    no card open it's still wrong when nothing is in flight (in_flight: a
    pending or running job in the workspace) or, for "it's done", when nothing
    was ever made (made)."""
    if job_started:
        return shown
    if awaiting_make(thread.get("cards")):
        def false(t):
            return STARTED_RE.search(t)
        fix = NOT_STARTED
    elif in_flight and made:
        return shown
    else:
        def false(t):
            return ((not in_flight and PROGRESS_RE.search(t)) or (not made and DONE_RE.search(t))
                    or (not in_flight and not made and LIBRARY_RE.search(t)))
        fix = NOTHING_MADE
    fixed = False
    for m in thread["messages"][start:]:
        if m["role"] != "assistant":
            continue
        for b in m["content"]:
            if b.get("type") == "text" and false(b.get("text") or ""):
                b["text"] = fix     # never empty: the API refuses empty text blocks
                fixed = True
    if not fixed:
        return shown
    out, said = [], False
    for it in shown:
        if it.get("role") == "assistant" and false(it.get("text") or ""):
            if not said:
                out.append({"role": "assistant", "text": fix})
                said = True
            continue
        out.append(it)
    return out


def job_counts(conn, ws_id) -> tuple[bool, bool]:
    """(anything pending or running, anything finished) in the workspace."""
    r = conn.execute("SELECT COALESCE(SUM(status IN ('pending','running')),0), COALESCE(SUM(status='done'),0) FROM jobs "
                     "WHERE workspace_id=? AND deleted_at IS NULL", (ws_id,)).fetchone()
    return bool(r[0]), bool(r[1])


def run(conn, ws, thread: dict, screen: str, usage: llm.Usage, job_started: bool = False) -> list[dict]:
    """Answer the latest member message: model calls and tool runs until the
    model stops. Mutates thread (messages, setup, cards). Returns new display
    items. Raises llm.LLMError."""
    msgs = thread["messages"]
    start = len(msgs)
    setup = thread.setdefault("setup", {})
    cards = thread.setdefault("cards", {})
    shown = []
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    for _ in range(MAX_STEPS):
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
        resp = llm.chat(system=system, messages=_cached(trim(msgs)), tools=TOOLS, max_tokens=TURN_TOKENS, usage=usage,
                        temperature=0.4, model_id=llm.fast_model())
        content = [b for b in resp.get("content") or [] if b.get("type") in ("text", "tool_use")]
        msgs.append({"role": "assistant", "content": content})
        for b in content:
            if b["type"] == "text" and b.get("text", "").strip():
                shown.append({"role": "assistant", "text": b["text"].strip()})
        uses = [b for b in content if b["type"] == "tool_use"]
        if not uses:
            break
        results = []
        for tu in uses:
            ctx = {"conn": conn, "ws": ws, "setup": setup, "messages": msgs, "screen": screen, "new_cards": []}
            out = dispatch(tu.get("name"), tu.get("input") or {}, ctx)
            for card in ctx["new_cards"]:
                card["id"] = tu["id"]
                cards[tu["id"]] = card
                shown.append({"role": "card", "card": card})
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": json.dumps(out)[:6000],
                            **({"is_error": True} if "error" in out else {})})
            ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
        msgs.append({"role": "user", "content": results})
    if msgs and msgs[-1]["role"] == "user":
        # Out of steps mid-way: one last reply in words so the turn ends with the assistant.
        resp = llm.chat(system=system, messages=_cached(trim(msgs)), tools=TOOLS, tool_choice={"type": "none"},
                        max_tokens=TURN_TOKENS, usage=usage, temperature=0.4, model_id=llm.fast_model())
        content = [b for b in resp.get("content") or [] if b.get("type") == "text"] or \
            [{"type": "text", "text": "Done. What next?"}]
        msgs.append({"role": "assistant", "content": content})
        shown += [{"role": "assistant", "text": b["text"].strip()} for b in content if b.get("text", "").strip()]
    in_flight, made = job_counts(conn, ws["id"])
    return guard_started(thread, start, shown, job_started, in_flight, made)


CONFIRM_WORDS = {"yes", "yep", "yeah", "yup", "ya", "y", "ok", "okay", "k", "sure", "confirm", "confirmed", "go", "ahead",
                 "do", "it", "make", "lets", "let", "s", "please", "pls", "sounds", "good", "great", "proceed", "start",
                 "run", "that", "one", "now", "perfect", "yes,", "send"}
CONFIRM_STRONG = {"yes", "yep", "yeah", "yup", "ya", "y", "ok", "okay", "sure", "confirm", "confirmed", "go", "make",
                  "proceed", "start", "run", "do", "send"}


def is_confirm(text: str) -> bool:
    """A short typed yes ('confirmed', 'yes make it', 'go ahead')."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    return 0 < len(words) <= 6 and set(words) <= CONFIRM_WORDS and bool(set(words) & CONFIRM_STRONG)


def confirmable_card(thread: dict):
    """The newest spend card, if it's still open: a typed yes answers it."""
    last = None
    for c in (thread.get("cards") or {}).values():
        if c.get("kind") == "propose_import" or (c.get("kind") == "propose_first_ad" and c.get("ad") == "image"):
            last = c
    return last if last and last.get("status") == "open" else None


def add_member(thread: dict, text: str):
    """Append the member's (or the app's) message, keeping roles alternating."""
    msgs = thread.setdefault("messages", [])
    if msgs and msgs[-1]["role"] == "user":
        prev = msgs[-1]["content"]
        if isinstance(prev, str):
            msgs[-1]["content"] = prev + "\n\n" + text
            return
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "Okay."}]})
    msgs.append({"role": "user", "content": text})


def estimate() -> int:
    """About what one reply costs (a few model calls on the fast model)."""
    return llm.estimate_credits(9000, 600, model_id=llm.fast_model())


def _cached(messages):
    if not messages:
        return messages
    msgs = [dict(m) for m in messages]
    last = msgs[-1]
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    content = [dict(b) for b in content]
    content[-1]["cache_control"] = {"type": "ephemeral"}
    last["content"] = content
    return msgs


def display(thread: dict) -> list[dict]:
    """The conversation as the panel shows it: member and assistant text, and
    the cards (with their current status)."""
    out, cards = [], thread.get("cards") or {}
    for m in thread.get("messages") or []:
        if m["role"] == "user" and isinstance(m["content"], str):
            if m["content"].startswith(KICKOFF):
                continue
            if m["content"].startswith("(app)"):
                out.append({"role": "note", "text": m["content"][5:].strip()})
            else:
                out.append({"role": "user", "text": m["content"]})
        elif m["role"] == "assistant":
            for b in m["content"]:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append({"role": "assistant", "text": b["text"].strip()})
                elif b.get("type") == "tool_use" and b.get("id") in cards:
                    out.append({"role": "card", "card": cards[b["id"]]})
    return out
