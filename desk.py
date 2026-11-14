"""The campaign desk (the new look only): one sentence in, a whole campaign back, priced before anything is made.

With the new look on, this is the app's home once a member has made something (their first run keeps Studio's own
opening move, the two free image ads). The sentence goes through Studio's Ask (studio_v2.ask_run: the same model call,
the same daily cap, the same claim rules), which answers with the ads. The rest of the campaign is added here by
word rules and the member's plan, with no model call:

    Make        the ads Ask proposed (image ads, a presenter video, a voiceover, a product video)
    Write       ad copy for one platform (copywriter.write, held at its estimate and settled at what it used)
    Capture     a page with a sign-up form, saved as a draft (pages.starter_blocks, the form's default fields)
    Follow up   an email follow-up from a template, saved switched off (flows.from_template)
    Launch      the hand-over to the ad accounts, as the step after the ads are done

Every part is a row the member can untick, edit in place or remove, and each row carries its price. The total is on
the button that runs it, and /app/desk/run starts nothing unless the total it is sent is the total it works out
(409 otherwise), exactly as Studio does.

A campaign is a conversation (desk_threads, desk_turns): what the member said, the plan that came back, what was
made from it, and whatever they ask for next ("add a TikTok version"), which goes through the same Ask with a short
account of the campaign so far and answers with only what is new. The desk's home starts one, from the sentence
box or from an example card; /app/desk/c/<id> is the conversation, with the newest plan still open to edit and
everything before it shown as it was. The campaigns are listed in the sidebar.

A part is a small dict the browser holds and sends back; clean_part() cuts it down to the fields its kind may set, so
what is priced and started is never more than that.
"""
import json
import logging
import re

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, url_for

import catalog
import compliance
import copywriter
import create_routes
import credits as credit_holds
import db
import flows
import llm
import pages
import pipeline
import plans
import platforms
import products
import studio_v2
import ui_v2
from core import brand, login_required, reserve_ai

log = logging.getLogger("desk")
bp = Blueprint("desk", __name__)

MAKES = studio_v2.ASK_MAKES
STAGES = [("make", "Make"), ("write", "Write"), ("capture", "Capture"), ("followup", "Follow up"), ("launch", "Launch")]
STAGE_OF = {"image": "make", "presenter": "make", "voiceover": "make", "product_video": "make",
            "copy": "write", "page": "capture", "flow": "followup", "launch": "launch"}
STAGE_ORDER = {key: i for i, (key, _name) in enumerate(STAGES)}
SPINE = ("copy", "page", "flow", "launch")                 # the rest of a campaign, in the order it happens
FEATURE = {"copy": "copy", "page": "pages", "flow": "flows", "launch": "adhub"}
ENDPOINT = {"copy": "copy.studio", "page": "pages.index", "flow": "flows.index", "launch": "adhub.index"}
ADD_NAMES = {"image": "Image ads", "presenter": "Presenter video", "voiceover": "Voiceover video",
             "product_video": "Product video", "copy": "Ad copy", "page": "A page with a sign-up form",
             "flow": "An email follow-up", "launch": "Launch"}
DEFAULT_FLOW = "lead_magnet"
MAX_PARTS = 12
THREADS_SHOWN = 8           # campaigns listed in the sidebar and on the desk
TURNS_SHOWN = 60            # turns of one conversation shown (the newest)
STORED = ("id", "kind", "on", "text", "count", "presenter", "situation", "platform", "n", "title", "template")

# "Start from an example": what the card says, the sentence it puts in the box ({name} is the product), and a
# picture or clip from our own gallery (static/showcase) to show the kind of thing it makes.
EXAMPLES = [
    ("Launch kit", "The whole campaign from one product.",
     "Launch {name}: three image ads, one presenter video, the ad copy, a page to collect emails and a follow-up",
     "team-skincare"),
    ("Presenter test", "Two presenters, two openings.",
     "Two presenter videos for {name}, each with a different opening line", "preset-talk-to-camera"),
    ("Product film", "The product on screen, no person.",
     "A product video and a voiceover video for {name}", "team-product-film"),
    ("Angle test", "Four headlines, four angles.",
     "Four image ads for {name}, each on a different angle", "compare"),
    ("Lead capture", "A page, a form and the emails after.",
     "A page to collect emails for {name}, with a follow-up", "doorstep"),
    ("Ad copy", "Every text field, sized for the platform.",
     "Ad copy for {name} for Meta", "callouts"),
]

# What the sentence asks for beyond ads: Studio's own word rules (studio_v2.ASK_ALSO), by the tool they point at, plus
# the few ways people say "page" and "follow-up" when they are describing a campaign and not naming a tool.
_ALSO_RX = {ep: rx for ep, _name, _line, rx, _feat in studio_v2.ASK_ALSO}
WANTS = {
    "copy": r"\b(copy|captions?|primary text|ad text|hooks?)\b",
    "page": _ALSO_RX["pages.index"] + r"|\b(sign[- ]?ups?|wait(?:ing)? ?list|lead forms?|a form|collect (?:names|emails|leads))\b",
    "flow": _ALSO_RX["flows.index"] + r"|\b(email (?:sequence|series)|emails? (?:after|over|to (?:new|everyone))|"
                                      r"(?:two|three|four|five|six|\d)[- ]emails?)\b",
    "launch": _ALSO_RX["adhub.index"],
}


# The platform a sentence names, for the copy it asks for ("the captions for TikTok").
PLATFORM_WORDS = [("tiktok", r"\btik ?tok\b"), ("youtube", r"\byoutube\b|\bshorts\b"), ("linkedin", r"\blinkedin\b"),
                  ("pinterest", r"\bpinterest\b"), ("snapchat", r"\bsnap(?:chat)?\b"), ("reddit", r"\breddit\b"),
                  ("x", r"\btwitter\b|\bon x\b|\bfor x\b"), ("meta", r"\bmeta\b|\bfacebook\b|\binstagram\b|\breels\b")]


def platform_in(text: str) -> str:
    """The first platform the sentence names that the copy tool writes for ("" when it names none)."""
    t = " ".join(str(text or "").lower().split())
    return next((k for k, rx in PLATFORM_WORDS if k in platforms.PLATFORMS and re.search(rx, t)), "")


def wanted(text: str) -> set:
    t = " ".join(str(text or "").lower().split())
    return {kind for kind, rx in WANTS.items() if re.search(rx, t)}


def is_home() -> bool:
    """Is the desk this member's home? Yes once they have made something, unless the link asks Studio for
    something (a mode, a batch to edit, a preset)."""
    if any(request.args.get(k) for k in ("mode", "batch", "preset", "made")):
        return False
    import first_ads
    if first_ads.on():              # the guided first run: the desk (and its guide) is home from the first minute
        return True
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (g.ws["id"],)).fetchone() is not None


def _has(kind: str) -> bool:
    """Is this kind of part something the app can do here at all (the tool is installed)?"""
    return kind in MAKES or ENDPOINT[kind] in current_app.view_functions


def _locked(kind: str) -> str:
    """The plan that includes this part, when the member's plan doesn't ("" when it does)."""
    feat = FEATURE.get(kind)
    return plans.required(feat)["name"] if feat and not plans.has(g.ws, feat) else ""


def flow_templates() -> list[dict]:
    """The follow-ups the desk offers: the email-only templates (a text needs its own setup and consent)."""
    return [{"id": t["id"], "name": t["name"], "line": t["line"]} for t in flows.TEMPLATES if t["channels"] == ["email"]]


def clean_part(raw, cast, have) -> dict | None:
    """One row, cut down to what its kind may set (None when it is not a part)."""
    if not isinstance(raw, dict) or raw.get("kind") not in STAGE_OF or not _has(raw["kind"]):
        return None
    kind = raw["kind"]
    part = {"id": re.sub(r"[^A-Za-z0-9]", "", str(raw.get("id") or ""))[:12], "kind": kind, "on": bool(raw.get("on"))}
    if kind in MAKES:
        if kind not in have:
            return None
        it = studio_v2.ask_item(dict(raw, make=kind), cast, have)
        if it is None:
            if kind == "presenter" and not cast:
                return None
            part.update(text="", on=False)              # no words yet: the row waits, unticked, until it has some
            if kind == "image":
                part["count"] = 1
            if kind == "presenter":
                part["presenter"] = cast[0]["id"]
        else:
            part.update({k: it[k] for k in ("text", "count", "presenter", "situation") if k in it})
    elif kind == "copy":
        part["platform"] = raw.get("platform") if raw.get("platform") in platforms.PLATFORMS else "meta"
        part["n"] = 5 if str(raw.get("n")) == "5" else 3
    elif kind == "page":
        part["title"] = studio_v2._clean(raw.get("title"), 120)
    elif kind == "flow":
        ok = {t["id"] for t in flow_templates()}
        part["template"] = raw.get("template") if raw.get("template") in ok else DEFAULT_FLOW
    if _locked(kind):
        part["on"] = False
    return part


def clean_parts(raw, cast=None, have=None) -> list[dict]:
    cast = cast if cast is not None else studio_v2.roster()
    have = have if have is not None else {m["id"] for m in studio_v2.available_modes()}
    out, makes, seen = [], 0, set()
    for x in (raw if isinstance(raw, list) else [])[:MAX_PARTS * 2]:
        p = clean_part(x, cast, have)
        if p is None or len(out) >= MAX_PARTS:
            continue
        if p["kind"] in MAKES:
            makes += 1
            if makes > studio_v2.ASK_MAX_PARTS:     # Studio starts at most this many parts from one confirmation
                continue
        elif p["kind"] in seen:                     # one of each of the rest
            continue
        seen.add(p["kind"])
        if not p["id"] or p["id"] in {o["id"] for o in out}:
            p["id"] = f"p{len(out) + 1}x"
        out.append(p)
    out.sort(key=lambda p: STAGE_ORDER[STAGE_OF[p["kind"]]])
    return out


def _make_item(part) -> dict:
    return {"make": part["kind"], "text": part["text"], "count": part.get("count"), "presenter": part.get("presenter"),
            "situation": part.get("situation")}


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def view(part, product, cast) -> dict:
    """The row as the member reads it: its stage, a name, the words, the price and what Edit can change."""
    kind = part["kind"]
    v = dict(part, stage=STAGE_OF[kind], locked=_locked(kind), price=None, label="", error="", words="")
    if kind in MAKES:
        v["words"] = part["text"]
        if not part["text"]:
            v.update(name=ADD_NAMES[kind], label="Add the words", on=False)
            return v
        it = studio_v2.ask_item(_make_item(part), cast)
        v["name"] = it["name"] if it else ADD_NAMES[kind]
        q = studio_v2.quote_parts([studio_v2._ask_part(it, product["id"])]) if it else {"error": "That can't be made right now."}
        if q.get("error"):
            v.update(error=q["error"], on=False, label="Can't be priced")
        else:
            v.update(price=q["total"], label=f"{q['total']:,} credits", videos=q["count"] if q.get("videos") else 0)
    elif kind == "copy":
        v["name"] = f"Ad copy for {platforms.PLATFORMS[part['platform']]['short']}"
        v["words"] = f"{part['n']} versions of every text field, in your voice, from your approved claims."
        v["price"] = copywriter.estimate(part["n"], "ad")
        v["label"] = f"Up to {_plural(v['price'], 'credit')}"
    elif kind == "page":
        title = part["title"] or product["name"]
        forms_on = plans.has(g.ws, "forms") and "forms.index" in current_app.view_functions
        v["name"] = "A page with a sign-up form" if forms_on else "A page for the offer"
        v["words"] = (f"“{title}”" + (", asking for a name and an email" if forms_on else "") +
                      ". Saved as a draft: you publish it.")
        v["label"] = "In your plan"
        v["title"] = title
    elif kind == "flow":
        t = flows.TEMPLATE_BY_ID[part["template"]]
        v["name"] = f"Email follow-up: {t['name']}"
        v["words"] = f"{t['line']} Saved switched off: you turn it on."
        v["label"] = "In your plan"
    elif kind == "launch":
        v["name"] = "Get the ads into your ad accounts"
        v["words"] = "The step after the ads are done. You pick the account, and nothing runs until you say so."
        v["label"] = "No credits"
    if v["locked"]:
        v.update(on=False, price=None, label=f"{v['locked']} plan")
    return v


def price(parts, product, cast=None) -> dict:
    """Every row with its price, and the total of the ticked ones. No model call: the ads are priced by Create's own
    quote (through Studio), the copy at the most it can cost."""
    cast = cast if cast is not None else studio_v2.roster()
    views = [view(p, product, cast) for p in parts]
    on = [v for v in views if v["on"]]
    ads = sum(v["price"] or 0 for v in on if v["kind"] in MAKES)
    copy_max = sum(v["price"] or 0 for v in on if v["kind"] == "copy")
    videos = sum(v.get("videos") or 0 for v in on)
    total, bal = ads + copy_max, db.balance(g.ws)
    return {"parts": views, "total": total, "ads": ads, "copy": copy_max, "count": len(on), "balance": bal,
            "short": max(0, total - bal), "pack": create_routes.suggest_pack(total - bal) if total > bal else None,
            "confirm": videos > create_routes.CONFIRM_VIDEOS, "videos": videos, "product": product["id"]}


def _product(conn, pid):
    p = products.get_product(conn, g.ws["id"], pid) if pid else None
    if p is None or p["brand_id"] not in products.unlocked_brand_ids(conn, g.ws):
        return None
    return p


def starters(conn, product) -> dict:
    """First words for a part the member adds by hand, from what the product already says (no model)."""
    scenes = studio_v2.scene_templates(conn, product["id"], n=1)
    script = studio_v2.starter_script(product)
    return {"image": (scenes[0]["headline"] if scenes else "") or product["name"],
            "presenter": script, "voiceover": script,
            "product_video": f"{product['name']} on a table in soft light, the camera moving in slowly."}


def examples(product) -> list[dict]:
    """The example cards, each with its picture or clip when the gallery has it."""
    import os
    name = product["name"] if product is not None else "your product"
    root = os.path.join(current_app.static_folder, "showcase")
    out = []
    for title, line, ask, media in EXAMPLES:
        card = {"title": title, "line": line, "ask": ask.format(name=name), "image": "", "video": ""}
        if os.path.exists(os.path.join(root, f"{media}.webp")):
            card["image"] = url_for("static", filename=f"showcase/{media}.webp")
        if os.path.exists(os.path.join(root, f"{media}.mp4")):
            card["video"] = url_for("static", filename=f"showcase/{media}.mp4")
        out.append(card)
    return out


# ---------------------------------------------------------------- campaigns as conversations

def _short(text: str, n: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 2].rsplit(" ", 1)[0].rstrip(" ,:;.-") + "\u2026"


def _thread(conn, tid):
    """The campaign, when it is this workspace's and still there."""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return None
    return conn.execute("SELECT * FROM desk_threads WHERE id=? AND workspace_id=? AND archived=0",
                        (tid, g.ws["id"])).fetchone()


def _new_thread(conn, product, said: str) -> int:
    t = db.now()
    head = str(said or "").split(":", 1)[0]               # "Holiday launch for the candle: three image ads, ..." -> its name
    title = _short(head if 12 <= len(head.strip()) < len(str(said or "")) else said) or "A campaign"
    return conn.execute("INSERT INTO desk_threads (workspace_id, user_id, product_id, title, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?)", (g.ws["id"], g.user["id"], product["id"], title, t, t)).lastrowid


def _turn(conn, tid: int, role: str, kind: str, body: dict) -> int:
    t = db.now()
    rid = conn.execute("INSERT INTO desk_turns (thread_id, workspace_id, role, kind, body_json, created_at) "
                       "VALUES (?,?,?,?,?,?)", (tid, g.ws["id"], role, kind, json.dumps(body), t)).lastrowid
    conn.execute("UPDATE desk_threads SET updated_at=? WHERE id=?", (t, tid))
    return rid


def _body(row) -> dict:
    try:
        b = json.loads(row["body_json"] or "{}")
        return b if isinstance(b, dict) else {}
    except ValueError:
        return {}


def _turns(conn, tid: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM (SELECT * FROM desk_turns WHERE thread_id=? AND workspace_id=? ORDER BY id DESC "
                        "LIMIT ?) ORDER BY id", (tid, g.ws["id"], TURNS_SHOWN)).fetchall()
    return [{"id": r["id"], "role": r["role"], "kind": r["kind"], "at": r["created_at"], "body": _body(r)} for r in rows]


def _stored(parts) -> list[dict]:
    """A plan's rows as they are kept in its turn: only what a row may set."""
    return [{k: p[k] for k in STORED if k in p} for p in parts]


def _snapshot(views) -> list[dict]:
    """The rows as they read, for showing a plan later exactly as it was."""
    return [{"stage": v["stage"], "name": v["name"], "words": v.get("words") or "", "label": v.get("label") or "",
             "on": bool(v["on"])} for v in views]


def _context(conn, thread) -> str:
    """What this campaign holds so far, in a few lines, for the model: what was asked, what the last plan had,
    what was made. Member text only ever reaches the model as quoted data here, as it does in the request itself."""
    asked, planned, made = [], [], []
    for t in _turns(conn, thread["id"]):
        b = t["body"]
        if t["kind"] == "said":
            asked.append(studio_v2._clean(b.get("text"), 300))
        elif t["kind"] == "plan":
            planned = [f"{r['name']} ({studio_v2._clean(r['words'], 120)})" for r in b.get("rows") or [] if r.get("on")]
        elif t["kind"] == "made":
            made += [m.get("name") or "" for m in b.get("made") or []]
    lines = [f"- The member asked: \"{a}\"" for a in asked[-3:]]
    if planned:
        lines.append("- The last plan had: " + "; ".join(planned[:8]))
    if made:
        lines.append("- Already made: " + "; ".join(x for x in made[-10:] if x))
    return "\n".join(lines)


def _adopt_runs(conn) -> None:
    """Campaigns made before the desk had conversations (desk_runs with no thread) become one each, so nothing a
    member made disappears from the list."""
    for r in conn.execute("SELECT * FROM desk_runs WHERE workspace_id=? AND thread_id IS NULL ORDER BY id LIMIT 20",
                          (g.ws["id"],)).fetchall():
        try:
            made = [m for m in json.loads(r["made_json"] or "[]") if isinstance(m, dict)]
        except ValueError:
            made = []
        cur = conn.execute("INSERT INTO desk_threads (workspace_id, user_id, product_id, title, created_at, updated_at) "
                           "VALUES (?,?,?,?,?,?)", (g.ws["id"], r["user_id"], r["product_id"], _short(r["ask"]) or "A campaign",
                                                    r["created_at"], r["created_at"]))
        for role, kind, body in (("member", "said", {"text": r["ask"] or ""}), ("desk", "made", {"made": made, "credits": r["credits"]})):
            conn.execute("INSERT INTO desk_turns (thread_id, workspace_id, role, kind, body_json, created_at) VALUES (?,?,?,?,?,?)",
                         (cur.lastrowid, g.ws["id"], role, kind, json.dumps(body), r["created_at"]))
        conn.execute("UPDATE desk_runs SET thread_id=? WHERE id=?", (cur.lastrowid, r["id"]))
    conn.commit()


def threads(conn, limit: int = THREADS_SHOWN) -> list[dict]:
    """The workspace's campaigns, the one touched last first, each with what it last made."""
    _adopt_runs(conn)
    out = []
    for t in conn.execute("SELECT * FROM desk_threads WHERE workspace_id=? AND archived=0 ORDER BY updated_at DESC, id DESC "
                          "LIMIT ?", (g.ws["id"], limit)).fetchall():
        last = conn.execute("SELECT body_json FROM desk_turns WHERE thread_id=? AND kind='made' ORDER BY id DESC LIMIT 1",
                            (t["id"],)).fetchone()
        body = _body(last) if last else {}
        made = [dict(m) for m in (body.get("made") or []) if isinstance(m, dict) and m.get("href")]
        if body.get("batch") and any(m.get("kind") == "ads" for m in made):
            # what was said when they started ("Being made") is checked against the jobs as they are now
            busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE batch_id=? AND workspace_id=? AND deleted_at IS NULL AND "
                                "status IN ('pending','running','made')", (body["batch"], g.ws["id"])).fetchone()[0]
            for m in made:
                if m.get("kind") == "ads":
                    m["state"] = "Being made" if busy else "Ready"
        out.append({"id": t["id"], "title": t["title"] or "A campaign", "at": t["updated_at"], "made": made,
                    "href": url_for("desk.thread", tid=t["id"])})
    return out


def _tiles(conn, batch_id) -> dict:
    """The ads one confirmation started: how many are still being made, and the finished ones to show."""
    if not batch_id:
        return {"busy": 0, "outs": []}
    jobs = conn.execute("SELECT id, status FROM jobs WHERE batch_id=? AND workspace_id=? AND deleted_at IS NULL ORDER BY id",
                        (batch_id, g.ws["id"])).fetchall()
    ids = [j["id"] for j in jobs]
    outs = conn.execute(f"SELECT id, kind FROM outputs WHERE job_id IN ({','.join('?' * len(ids))}) ORDER BY id LIMIT 8",
                        ids).fetchall() if ids else []
    return {"busy": sum(1 for j in jobs if j["status"] in ("pending", "running", "made")),
            "outs": [{"id": o["id"], "video": o["kind"] == "video"} for o in outs]}


# ---------------------------------------------------------------- the routes

@bp.route("/app/desk/plan", methods=["POST"])
@login_required
def plan():
    """The sentence -> the campaign, priced, as a turn of a conversation. Nothing is made here. With no `thread`
    this starts a campaign; with one it continues it: the model is told what the campaign holds and answers with
    what the new sentence adds."""
    if not ui_v2.on():
        return studio_v2._off()
    d = studio_v2._payload()
    text = studio_v2._clean(d.get("ask"), 600)
    with db.connect() as conn:
        thread = _thread(conn, d.get("thread")) if d.get("thread") else None
        if d.get("thread") and thread is None:
            return jsonify({"error": "That campaign isn't here any more. Start a new one."}), 404
        product = _product(conn, thread["product_id"] if thread is not None else d.get("product"))
        context = _context(conn, thread) if thread is not None else ""
    out, status = studio_v2.ask_run(text, product["id"] if product is not None else d.get("product"), context=context)
    if status == 400 and "step" not in out:             # too short, a claim the ads can't make, no product
        return jsonify(out), 400
    if product is None:
        return jsonify({"error": "Add your product first, then ask again."}), 400
    cast = studio_v2.roster()
    have = {m["id"] for m in studio_v2.available_modes()}
    want, raw = wanted(text), []
    if status == 200:
        note = out.get("note") or "Here is a campaign for that."
        raw = [dict(it, kind=it["make"], on=True) for it in out["plan"]]
    elif status == 429:
        note = (f"That's {studio_v2.ASK_DAILY} asks today, so I haven't planned the ads. Add them with Add a part, "
                "or ask again tomorrow.")
    elif status == 422:
        note = "No ads in this one. Add some with Add a part if you want them."
    elif status == 400:
        note = out.get("error") or ""
    else:
        note = "The ads didn't come back this time. Add them with Add a part, or plan it again."
    if thread is None:
        raw += [{"kind": k, "on": k in want} for k in SPINE]        # a new campaign shows every stage
    else:
        raw += [{"kind": k, "on": True} for k in SPINE if k in want]   # a later turn: only what it asks for
    for r in raw:
        if r["kind"] == "copy" and platform_in(text):
            r["platform"] = platform_in(text)
    for i, r in enumerate(raw):
        r["id"] = f"p{i + 1}"
    parts = clean_parts(raw, cast, have)
    ans = price(parts, product, cast)
    rows = {url_for(ENDPOINT[k]) for k in SPINE if _has(k)}          # already a row of the plan: not a link as well
    also = [a for a in (out.get("also") or studio_v2.ask_also(text)) if a["href"] not in rows]
    if thread is not None and not parts:
        note = (note if status in (429, 400) else "") or ("I couldn't turn that into a part of the campaign. Say what "
                                                          "to add: more image ads, a video, the ad copy, a page, a follow-up.")
    with db.connect() as conn:
        tid = thread["id"] if thread is not None else _new_thread(conn, product, text)
        _turn(conn, tid, "member", "said", {"text": text})
        if parts:
            turn = _turn(conn, tid, "desk", "plan", {"ask": text, "note": note, "skipped": out.get("skipped") or [],
                                                     "also": also, "parts": _stored(ans["parts"]),
                                                     "rows": _snapshot(ans["parts"]), "ran": False})
        else:
            turn = _turn(conn, tid, "desk", "note", {"text": note, "also": also})
        conn.commit()
    ans.update(note=note, skipped=out.get("skipped") or [], also=also, ask=text, planned=status == 200,
               left=out.get("left"), thread=tid, turn=turn, url=url_for("desk.thread", tid=tid) + f"#t{turn}")
    return jsonify(ans)


@bp.route("/app/desk/quote", methods=["POST"])
@login_required
def quote():
    """The rows as they stand now, re-priced (a tick, an edit, a part added or removed)."""
    if not ui_v2.on():
        return studio_v2._off()
    d = studio_v2._payload()
    with db.connect() as conn:
        product = _product(conn, d.get("product"))
    if product is None:
        return jsonify({"error": "Pick a product first."}), 400
    ans = price(clean_parts(d.get("parts")), product)
    if d.get("turn"):
        _keep_plan(d.get("turn"), ans["parts"])
    return jsonify(ans)


def _keep_plan(turn_id, views, ran: bool = False) -> None:
    """Write the rows of a plan that is still open back to its turn, so a reload shows the member's edits; `ran`
    closes it."""
    try:
        turn_id = int(turn_id)
    except (TypeError, ValueError):
        return
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM desk_turns WHERE id=? AND workspace_id=? AND kind='plan'",
                           (turn_id, g.ws["id"])).fetchone()
        if row is None:
            return
        body = _body(row)
        if body.get("ran"):
            return
        body.update(parts=_stored(views), rows=_snapshot(views), ran=bool(ran))
        conn.execute("UPDATE desk_turns SET body_json=? WHERE id=?", (json.dumps(body), turn_id))
        conn.commit()


def _write_copy(part, product, said) -> tuple[dict | None, str]:
    """Write the ad copy this row stands for: (what was made, "") or (None, why not). Held at the estimate before the
    writer runs and settled at what it used, as the Copy page does."""
    import copy_routes
    if not llm.configured():
        return None, "The copy wasn't written (AI writing isn't switched on), so it wasn't charged."
    b = brand(product["id"])
    brief = {"offer": (b.get("offer") or "")[:300], "audience": (b.get("audience") or "")[:200], "goal": "", "tone": "",
             "link": (b.get("url") or "")[:300], "notes": said[:1000]}
    if not compliance.check(brief["offer"], brief["audience"], brief["notes"])["ok"]:
        return None, "The copy wasn't written: the request asks for a claim it can't make. It wasn't charged."
    n, kind, platform = part["n"], "ad", part["platform"]
    hold = reserve_ai(copywriter.estimate(n, kind), "AI ad copy")
    if hold is None:
        return None, "The copy wasn't written: not enough credits left for it. It wasn't charged."
    usage = llm.Usage()
    try:
        variants = copywriter.write(b, kind, platform, brief, n, usage)
    except llm.LLMError as e:
        credit_holds.settle(hold, usage.credits if usage.calls else 0)
        return None, f"The copy didn't finish ({e}). Write it again from Copy."
    except Exception:
        credit_holds.release(hold, "the writer crashed")
        raise
    with db.connect() as conn:
        rid = copy_routes.save_run(conn, kind, platform, dict(brief, n=n), {"variants": variants}, usage,
                                   product_id=product["id"], hold=hold)
    return {"kind": "copy", "name": f"Ad copy for {platforms.PLATFORMS[platform]['short']}", "state": "Written",
            "href": url_for("copy.run", rid=rid)}, ""


def _make_page(conn, part, product) -> tuple[list, str, int | None]:
    """A draft page for the product, with a sign-up form when the plan has forms: (what was made, why not, form id)."""
    import forms_routes
    import pages_routes
    import secrets
    if conn.execute("SELECT COUNT(*) FROM pages WHERE workspace_id=?", (g.ws["id"],)).fetchone()[0] >= pages_routes.MAX_DRAFTS:
        return [], f"The page wasn't made: you have {pages_routes.MAX_DRAFTS} pages. Delete one to make another.", None
    title = part["title"] or product["name"]
    made, fid, t = [], None, db.now()
    blocks = pages.starter_blocks(brand(product["id"]))
    if plans.has(g.ws, "forms") and "forms.index" in current_app.view_functions:
        fid = conn.execute("INSERT INTO forms (workspace_id, code, name, fields_json, button_text, success_message, "
                           "webhook_secret, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (g.ws["id"], secrets.token_urlsafe(9), f"{title} sign-up"[:80],
                            json.dumps(forms_routes.DEFAULT_FIELDS), "Sign up", "Thanks! We'll be in touch.",
                            secrets.token_hex(16), t, t)).lastrowid
        block = pages.clean_block("form", {"heading": "Sign up", "form": fid}, lambda _id: False, lambda _id: True)
        if block:
            blocks.insert(1, block)
        made.append({"kind": "form", "name": "Sign-up form", "state": "Ready", "href": url_for("forms.edit", fid=fid)})
    pid = conn.execute("INSERT INTO pages (workspace_id, slug, title, description, blocks_json, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (g.ws["id"], pages_routes._unique_slug(conn, title), title, "",
                                                  json.dumps(blocks), t, t)).lastrowid
    conn.commit()
    made.insert(0, {"kind": "page", "name": f"Page: {title}", "state": "Draft", "href": url_for("pages.edit", pid=pid)})
    return made, "", fid


def _make_flow(conn, part, form_id) -> dict:
    """The follow-up from its template, switched off, set to start from the campaign's form when there is one."""
    import flows_routes
    t = flows.TEMPLATE_BY_ID[part["template"]]
    tree = flows.from_template(t["id"])
    if tree["trigger"].get("kind") == "form_submitted":
        forms = flows_routes._forms(conn)
        tree["trigger"]["form_id"] = form_id or (forms[0]["id"] if forms else None)
    fid = flows_routes._create(conn, t["name"], tree)
    return {"kind": "flow", "name": f"Follow-up: {t['name']}", "state": "Switched off",
            "href": url_for("flows.edit", fid=fid)}


@bp.route("/app/desk/run", methods=["POST"])
@login_required
def run():
    """Make the ticked parts. `total` must be the total the member was shown; when it isn't, nothing starts (409).
    The ads go first, through Studio (so a step Create asks for, or a refusal, comes back before anything else is
    made); then the copy, the page and its form, and the follow-up."""
    if not ui_v2.on():
        return studio_v2._off()
    d = studio_v2._payload()
    if d.get("total") in (None, ""):
        return jsonify({"error": "See the price first, then confirm it.", "needs_quote": True}), 409
    with db.connect() as conn:
        product = _product(conn, d.get("product"))
    if product is None:
        return jsonify({"error": "Pick a product first."}), 400
    cast = studio_v2.roster()
    q = price(clean_parts(d.get("parts"), cast), product, cast)
    on = [v for v in q["parts"] if v["on"]]
    if not on:
        return jsonify({"error": "Tick at least one part."}), 400
    if str(q["total"]) != str(d.get("total")):
        return jsonify({"error": f"The total changed to {q['total']:,} credits. Check it and confirm again.",
                        "total": q["total"]}), 409
    if q["short"]:
        return jsonify({"error": f"That needs {q['total']:,} credits and you have {q['balance']:,}. Untick a part or "
                                 "top up on Billing.", "billing": url_for("billing_page"), "pack": q["pack"]}), 402
    said = studio_v2._clean(d.get("ask"), 600)
    made, notes, spent, batch = [], [], 0, None
    ads = [v for v in on if v["kind"] in MAKES]
    if ads:
        body = {"plan": [_make_item(v) for v in ads], "product": product["id"], "ask": said, "total": q["ads"],
                "confirm_many": bool(d.get("confirm_many"))}
        parts = studio_v2.plan_parts(body)
        title, extra = studio_v2._title_for(body, parts)
        out = studio_v2.generate_parts(parts, body, title, extra)
        resp, status = out if isinstance(out, tuple) else (out, 200)
        data = resp.get_json(silent=True) or {}
        if status != 200 or not data.get("ok"):
            return out                                  # nothing has been made: Create's own answer
        n = sum((v.get("count") or 1) if v["kind"] == "image" else 1 for v in ads)
        made.append({"kind": "ads", "name": _plural(n, "ad"), "state": "Being made", "href": data["url"]})
        batch = data.get("batch")
        spent += q["ads"]                               # (a part Create refused is flashed by Studio itself)
    for v in on:
        if v["kind"] == "copy":
            done, why = _write_copy(v, product, said)
            if done:
                made.append(done)
            else:
                notes.append(why)
    with db.connect() as conn:
        form_id = None
        for v in on:
            if v["kind"] == "page":
                done, why, form_id = _make_page(conn, v, product)
                made += done
                if why:
                    notes.append(why)
        for v in on:
            if v["kind"] == "flow":
                made.append(_make_flow(conn, v, form_id))
        if any(v["kind"] == "launch" for v in on):
            made.append({"kind": "launch", "name": "Get the ads into your ad accounts", "state": "Next",
                         "href": url_for(ENDPOINT["launch"])})
        thread = _thread(conn, d.get("thread")) if d.get("thread") else None
        if thread is None:                              # made without a conversation open: it gets one
            tid = _new_thread(conn, product, said or "A campaign")
            _turn(conn, tid, "member", "said", {"text": said})
        else:
            tid = thread["id"]
        turn = _turn(conn, tid, "desk", "made", {"made": made, "notes": notes, "batch": batch, "credits": spent})
        rid = conn.execute("INSERT INTO desk_runs (workspace_id, user_id, product_id, ask, made_json, credits, created_at, "
                           "thread_id) VALUES (?,?,?,?,?,?,?,?)", (g.ws["id"], g.user["id"], product["id"], said,
                                                                    json.dumps(made), spent, db.now(), tid)).lastrowid
        conn.commit()
    if d.get("turn"):
        _keep_plan(d.get("turn"), q["parts"], ran=True)
    return jsonify({"ok": True, "run": rid, "made": made, "notes": notes, "thread": tid, "turn": turn,
                    "url": url_for("desk.thread", tid=tid) + f"#t{turn}"})


def _now(conn) -> list[dict]:
    """What is waiting for the member, each a link: new leads, ads being made, ads finished this week."""
    import datetime
    out = []
    if plans.has(g.ws, "forms") and "forms.leads" in current_app.view_functions:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        st = pipeline.stages(ws)
        known = [s["key"] for s in st]
        marks = ",".join("?" * len(known))
        n = conn.execute(f"SELECT COUNT(*) FROM contacts WHERE workspace_id=? AND (stage IS NULL OR stage=? OR stage NOT IN ({marks}))",
                         (g.ws["id"], known[0], *known)).fetchone()[0]
        if n:
            out.append({"line": f"{st[0]['name']}: {n}", "href": url_for("forms.leads")})
    busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND status IN ('pending','running')",
                        (g.ws["id"],)).fetchone()[0]
    if busy:
        out.append({"line": f"{_plural(busy, 'ad')} being made", "href": url_for("studio_v2.page")})
    week = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).replace(microsecond=0).isoformat()
    done = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND status='done' AND created_at>=?",
                        (g.ws["id"], week)).fetchone()[0]
    if done:
        out.append({"line": f"{_plural(done, 'ad')} finished this week", "href": url_for("library")
                    if "library" in current_app.view_functions else url_for("studio_v2.page")})
    return out


GUIDE_ASKS = 3            # example asks the guide offers as one-tap starts


def guide(conn, ws, user, product, market, campaigns, shown) -> dict:
    """What the agent says above the box: the next step for this workspace, from where it actually is (Koby, Nov 11
    2026: "our campaignify ai agent that helps users set things up and talks them through and onboards"). Every
    line is worked out from the workspace's own state: no model is called to draw the page, so it costs nothing and
    never says something that isn't so. {"state", "text", "also", "acts": [{"label", "href"} | {"label", "ask"}]};
    an "ask" fills the box below with that sentence.

    In order: no product yet (read my website) -> the brief still to check -> the first ads on us (first_ads.py)
    -> ads being made -> a campaign to pick up -> the first campaign."""
    import first_ads
    import onboarding
    first = ((user["name"] or "").split() or [""])[0] if user is not None else ""
    hi = f"Hi {first}. " if first else ""
    asks = [{"label": e["title"], "ask": e["ask"]} for e in (shown or [])[:GUIDE_ASKS]]
    also = ""
    if market and market.get("reading"):
        also = ("I'm still reading your site for your logo, colours, voice and who you compete with. "
                "You can start now; what I learn shows up beside this.")
    if product is None:
        return {"state": "brand", "also": "", "acts": [{"label": "Read my website", "href": url_for("onboarding.brief")}],
                "text": hi + "I'm your Campaignify agent. Start by pasting your website or product page and I'll learn "
                             "the business: what you sell, how you sound and who buys it. Reading it costs nothing."}
    name = product["name"]
    if onboarding.answers(user).get("brief") == "due":
        return {"state": "brief", "also": also, "acts": [{"label": "Check my brand", "href": url_for("onboarding.brief")}],
                "text": hi + f"I read your page. Check what I understood about {name} before I write anything from "
                             "it: a fix there improves every ad, caption and email."}
    if first_ads.open_for(conn, ws, user):
        room = first_ads.left(conn, ws["id"], user)
        what = " and ".join(x for x in (_plural(room["image"], "image ad") if room["image"] else "",
                                        "a short talking video" if room["talk"] else "") if x)
        are = "is" if (room["image"] == 1 and not room["talk"]) or (not room["image"] and room["talk"]) else "are"
        return {"state": "first", "also": also,
                "acts": [{"label": "Review my first ads", "href": url_for("onboarding.first")}],
                "text": hi + f"Your first {what} {are} on us. I drafted {'it' if are == 'is' else 'them'} for {name} from "
                             "your page. Change any of the words, then make them."}
    import plan_gate
    left = plan_gate.welcome_left(conn, ws)
    if left is not None and not campaigns:      # on the welcome credits, nothing planned yet
        return {"state": "welcome", "also": also,
                "acts": asks + [{"label": "See plans", "href": url_for("plangate.page")}],
                "text": hi + (f"You have {left:,} free credits for {name}'s first image ads. Tell me what you're "
                              "launching in a sentence and I'll plan it, with the price of each ad before anything is "
                              "made. Videos come with a plan." if left else
                              "You've used your free credits. Pick a plan to keep going: Starter and Growth start with "
                              f"a {plan_gate.TRIAL_DAYS}-day free trial.")}
    busy = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND deleted_at IS NULL AND status IN "
                        "('pending','running')", (ws["id"],)).fetchone()[0]
    if busy and campaigns:
        return {"state": "making", "also": also, "acts": [{"label": "Open " + _short(campaigns[0]["title"], 40),
                                                         "href": campaigns[0]["href"]}],
                "text": f"{_plural(busy, 'ad')} {'is' if busy == 1 else 'are'} being made. Open the campaign to watch "
                        "them come in, or tell me what to plan next."}
    if campaigns:
        return {"state": "next", "also": also, "acts": asks,
                "text": hi + f"What's next for {name}? Say it in a sentence below and I'll plan it: the ads, the "
                             "words, a page and the follow-up, each with its price before anything is made."}
    return {"state": "start", "also": also, "acts": asks,
            "text": hi + f"Tell me what you're launching for {name} in a sentence and I'll plan the campaign: the ads, "
                         "the words, a page and the follow-up. You see every price before anything is made."}


NUDGE_KEY = "nudge_ad_accounts"       # kept with the member's answers (users.onboarding_json): "off" once dismissed


def nudge(conn, ws, user, say) -> dict | None:
    """One dismissible line above the box, for the single most useful next connection: the ad accounts, once the
    workspace has a finished ad and nothing is connected. Where launching is on (AD_LAUNCH) it leads to Connections;
    otherwise to the setup hub, when the plan includes it. Never while the agent is still walking the account
    through its first steps, and never again for a member who dismissed it. {"text", "cta", "href"} or None."""
    import adplatforms
    import app as appmod
    import onboarding
    if user is None or (say or {}).get("state") in ("brand", "brief", "first"):
        return None
    if onboarding.answers(user).get(NUDGE_KEY) == "off":
        return None
    if conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? AND status='done' AND deleted_at IS NULL LIMIT 1",
                    (ws["id"],)).fetchone() is None:
        return None
    if adplatforms.enabled(ws, user) and appmod._has_endpoint("adconnect.settings"):
        if adplatforms.has_stored(conn, ws["id"]):
            return None
        return {"text": "Your ads are ready. Connect your ad accounts to launch them from here.",
                "cta": "Connect accounts", "href": url_for("adconnect.settings")}
    if appmod._has_endpoint("adhub.index") and plans.has(ws, "adhub"):
        return {"text": "Your ads are ready. Set up your ad accounts and export each ad in the platform's sizes.",
                "cta": "Set up ad accounts", "href": url_for("adhub.index")}
    return None


@bp.route("/app/desk/nudge/off", methods=["POST"])
@login_required
def nudge_off():
    """Dismiss the desk's nudge, for this member, for good."""
    import onboarding
    with db.connect() as conn:
        onboarding.save(conn, g.user, **{NUDGE_KEY: "off"})
        conn.commit()
    return redirect(url_for("desk.page"))


def _boot(ws, product, conn) -> dict:
    """What the desk's script needs on either page: the product, what can be added, the pickers, the addresses."""
    have = {m["id"] for m in studio_v2.available_modes()}
    cast = studio_v2.roster()
    return {
        "csrf": studio_v2._csrf(), "product": product["id"] if product is not None else None, "balance": db.balance(ws),
        "ask_on": llm.configured(), "starters": starters(conn, product) if product is not None else {},
        "max_count": studio_v2.MAX_COUNT, "max_makes": studio_v2.ASK_MAX_PARTS,
        "add": [{"kind": k, "name": ADD_NAMES[k], "locked": _locked(k)} for k in (*MAKES, *SPINE)
                if (k in have if k in MAKES else _has(k)) and not (k == "presenter" and not cast)],
        "presenters": [{"id": c["id"], "name": c.get("name") or c["id"].title(),
                        "places": [{"id": pl, "name": studio_v2.situation_labels().get(pl, str(pl).replace("_", " "))}
                                   for pl in studio_v2._places(c)]} for c in cast],
        "platforms": [{"id": k, "name": v["short"]} for k, v in platforms.PLATFORMS.items()],
        "flows": flow_templates(),
        "words": {str(dur): catalog.hard_words(dur) for dur in catalog.TOOLS["ugc_video"]["durations"]},
        "claims_check": studio_v2.claims_check.on() and llm.anthropic_ready(),
        "urls": {"plan": url_for("desk.plan"), "quote": url_for("desk.quote"), "run": url_for("desk.run"),
                 "claims": url_for("studio_v2.claims"), "billing": url_for("billing_page")},
    }


def _brand_ctx(conn, ws, product) -> dict:
    """The brand panel: what every part of a campaign is made from."""
    import textlayer
    b = products.get_brand(conn, ws["id"], product["brand_id"]) if product is not None else None
    brief = products.as_brief(b, product, ws)
    keys = b.keys() if b is not None else []
    kit = [c for c in (textlayer.clean_hex(b[k] or "") if k in keys else "" for k in ("kit_primary", "kit_accent")) if c]
    import os

    import brand_identity
    logo = url_for("products.brand_logo", bid=b["id"]) if (b is not None and "kit_logo" in keys and b["kit_logo"]
                                                           and os.path.exists(b["kit_logo"])) else ""
    return {"brief": brief, "kit": kit, "website": brief.get("website") or brief.get("url") or "", "logo": logo,
            "market": brand_identity.panel(conn, ws, b)}


@bp.route("/app/desk")
@login_required
def page():
    """The desk: your campaigns and the examples on top; at the foot, the agent's next step (guide) and the box
    to say what you are launching."""
    if not ui_v2.on():
        return redirect(url_for("studio"))
    g.v2_tab = "desk"
    with db.connect() as conn:
        if request.args.get("done"):                    # an address from before campaigns were conversations
            run_row = conn.execute("SELECT thread_id FROM desk_runs WHERE id=? AND workspace_id=?",
                                   (request.args.get("done", type=int) or 0, g.ws["id"])).fetchone()
            _adopt_runs(conn)
            run_row = conn.execute("SELECT thread_id FROM desk_runs WHERE id=? AND workspace_id=?",
                                   (request.args.get("done", type=int) or 0, g.ws["id"])).fetchone() if run_row else None
            if run_row and run_row["thread_id"]:
                return redirect(url_for("desk.thread", tid=run_row["thread_id"]))
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        product = _product(conn, ws["product_id"])
        campaigns = threads(conn)
        boot = _boot(ws, product, conn)
        brand_ctx = _brand_ctx(conn, ws, product)
        now = _now(conn)
        shown = examples(product)
        say = guide(conn, ws, g.user, product, brand_ctx.get("market"), campaigns, shown)
        tip = nudge(conn, ws, g.user, say)
    import inspiration
    ask = studio_v2._clean(request.args.get("ask"), 600)        # a sentence handed over by the Tools page's examples
    return render_template("v2/desk.html", boot=boot, product=product, examples=shown, campaigns=campaigns,
                           now=now, guide=say, nudge=tip, insp=inspiration.groups(product), ask=ask,
                           has_tools="tools.page" in current_app.view_functions, **brand_ctx)


@bp.route("/app/desk/c/<int:tid>")
@login_required
def thread(tid):
    """One campaign, as the conversation it is: what was said, each plan, what was made, and a box to go on. The
    newest plan that hasn't been made is still open to edit (the script draws it from `boot.plan`, priced now);
    everything else is shown as it was."""
    if not ui_v2.on():
        return redirect(url_for("studio"))
    g.v2_tab = "desk"
    with db.connect() as conn:
        t = _thread(conn, tid)
        if t is None:
            return redirect(url_for("desk.page"))
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        product = _product(conn, t["product_id"])
        turns = _turns(conn, tid)
        live = next((x for x in reversed(turns) if x["kind"] == "plan"), None)
        live = live if (live and not live["body"].get("ran") and product is not None
                        and not any(x["kind"] == "made" and x["id"] > live["id"] for x in turns)) else None
        for x in turns:
            x["live"] = live is not None and x["id"] == live["id"]
            if x["kind"] == "made":
                x["tiles"] = _tiles(conn, x["body"].get("batch"))
        campaigns = threads(conn)
        boot = _boot(ws, product, conn)
        brand_ctx = _brand_ctx(conn, ws, product)
        made = [m for x in turns if x["kind"] == "made" for m in x["body"].get("made") or [] if isinstance(m, dict) and m.get("href")]
    boot["thread"] = tid
    if live is not None:
        plan_now = price(clean_parts(live["body"].get("parts")), product)
        plan_now.update(turn=live["id"], ask=live["body"].get("ask") or "", note=live["body"].get("note") or "",
                        skipped=live["body"].get("skipped") or [], also=live["body"].get("also") or [])
        boot["plan"] = plan_now
    return render_template("v2/desk_thread.html", boot=boot, product=product, t=t, turns=turns, campaigns=campaigns,
                           made=made, busy=any(x.get("tiles", {}).get("busy") for x in turns), **brand_ctx)
