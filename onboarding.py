"""The first minutes of a new account (M14, behind ui_v2.on()).

    /app/welcome            "What do you want to make?"  pick any, or type something else
    /app/welcome/profile    name, company, website (optional)
    then the card step if the workspace needs one (card_routes.py, RS_CARD_REQUIRED)
    /app/welcome/brand      the brand brief: paste a link, see what was read, fix it, pick angles
    /app/welcome/first      the first ads, on us (first_ads.py): two image ads and a short talking video, drafted
                            from the brief; one button makes them and opens the campaign on the desk

While plans only is on (plan_gate.py) the order is: the two questions, the brand brief, the plan step, the first
ads. The brief comes before the plan so a new account sees its own brand read back before it is asked for a card;
reading a link costs the member nothing, and the one paid thing on the brief (angle ideas) waits for a plan.

A new account that gave us a product link (Koby, Nov 14 2026: the step tracking showed sign-ups stopping at the brief
and at the plan step, with nothing of theirs on screen): the two questions, confirm the email, the free first ads
(first_ads.free_open: FREE_IMAGES image ads from that page, free; the page is read on that step), the first-result
screen (/app/welcome/first/result: the ads as they finish, and for a sign-up from the presenter pages one presenter
still with a line from their product), the plan step, then the brand brief (answers brief="after", read back from the
link and skippable), then the first ads on us that are left (the talking video, with a plan). A sign-up with no link
keeps the order above; one that reads its link on the brief gets the free first ads from there.

The answers are kept on the member (users.onboarding_json, users.onboarded_at) and used for the first run:
the composer opens on the first thing they said they want that their credits cover (studio_v2.first_mode),
and a website typed here is already in the box on the brand screen and in "Add product from URL".

Only a new account sees the steps by itself: no ad made yet and the steps neither finished nor skipped. Either
page can be opened by hand at any time (an owner previewing the look), and "Skip for now" always leaves.
Nothing here charges, grants or changes credits, plans or anything at Stripe, with one exception that says its price
on the button: "Suggest angles" on the brand brief is the same paid AI call as in Create (creator.angles).

A workspace that already has a brand (Nov 11 2026): the brief shows that brand, because every prompt reads it. When
the website the member typed in the questions is not that brand's site, the brief says so instead of leaving them to
wonder why another business is on screen, and offers two plain choices: read the typed site as a new brand of this
workspace (the plan's brand limit applies), or keep the brand shown (other_site, do=other / do=keep).

The brand brief (Nov 11 2026) is not a new store of facts. Each card edits the product and brand columns every prompt
already reads through core.brand() (products.as_brief): what you sell (name, description, price, offer), who buys
it (audience), what you can say (approved claims, plus the ones the import suggested), how you sound (the brand's
tone and about). Angles are rows in `angles`, the same ones Create offers.
"""
import json

from flask import Blueprint, g, redirect, render_template, request, session, url_for

import db
import plans
import ui_v2
from core import login_required

bp = Blueprint("onboarding", __name__)

# (code, label). The codes are stored, so they stay as they are; the labels can change.
GOALS = [
    ("ugc", "UGC presenter videos"),
    ("image", "Static image ads"),
    ("product_video", "Product videos"),
    ("voiceover", "Voiceover ads"),
    ("plan", "Campaign plan and landing pages"),
    ("exploring", "Just exploring"),
]
GOAL_CODES = [c for c, _ in GOALS]
# The composer mode a goal starts in (studio_v2.MODES). "plan" and "exploring" pick no mode.
GOAL_MODE = {"ugc": "presenter", "image": "image", "product_video": "product_video", "voiceover": "voiceover"}
OTHER_MAX, NAME_MAX, COMPANY_MAX, WEBSITE_MAX = 200, 40, 80, 300
# Pages a new account is sent to the steps from (GET only). Everything else is left alone.
START_ENDPOINTS = {"studio", "studio_v2.page", "card.page", "plangate.page"}


def answers(user) -> dict:
    """What this member answered: {"goals": [...], "other", "company", "website"} (empty when never asked)."""
    raw = user["onboarding_json"] if user is not None and "onboarding_json" in user.keys() else None
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        out = {}
    return out if isinstance(out, dict) else {}


def clean_goals(values) -> list:
    """The known goal codes among `values`, in the order they are offered."""
    picked = {str(v) for v in values or []}
    return [c for c in GOAL_CODES if c in picked]


def clean_website(v) -> str:
    v = " ".join(str(v or "").split())[:WEBSITE_MAX]
    if not v or v.lower() in ("http://", "https://"):
        return ""
    return v if v.lower().startswith(("http://", "https://")) else "https://" + v


def pasted_link() -> str:
    """The product link a visitor pasted before signing up (home page or /shopify box, kept by signup), or ""."""
    return clean_website(session.get("try_url") or "")


def first_link(user) -> str:
    """The product link a new account gave us: the website typed on the second question, else the link pasted
    before signing up (first_ads.link_of: the free first result is made from it). "" for none."""
    return clean_website(answers(user).get("website")) or pasted_link()


def save(conn, user, **fields) -> dict:
    """Merge `fields` into the member's answers (no commit)."""
    out = dict(answers(user), **fields)
    conn.execute("UPDATE users SET onboarding_json=? WHERE id=?", (json.dumps(out), user["id"]))
    return out


def finish(conn, user):
    conn.execute("UPDATE users SET onboarded_at=COALESCE(onboarded_at, ?) WHERE id=?", (db.now(), user["id"]))


def due(conn, user, ws) -> bool:
    """Should this member be walked through the steps now? New look on, not done yet, nothing made yet."""
    if user is None or ws is None or not ui_v2.on():
        return False
    if "onboarded_at" not in user.keys() or user["onboarded_at"]:
        return False
    return conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone() is None


@bp.before_app_request
def _start():
    """A new account's first page is the first step (before Home, Studio or the card step)."""
    if request.method != "GET" or request.endpoint not in START_ENDPOINTS:
        return None
    if g.get("user") is None or g.get("ws") is None or g.get("viewing") is not None:
        return None
    if request.args.get("canceled") or request.args.get("declined"):     # back from Stripe's card page
        return None
    with db.connect() as conn:
        if due(conn, g.user, g.ws):
            return redirect(url_for("onboarding.intent"))
        # not from the card page: the brief sits behind the card step, so sending the card page there would loop
        if request.endpoint not in ("card.page", "plangate.page"):
            if brief_due(conn, g.user, g.ws):
                return redirect(url_for("onboarding.brief"))
            import first_ads
            if first_ads.due(conn, g.user, g.ws):        # a plan is picked and nothing is made: the first ads
                return redirect(url_for("onboarding.first"))
    return None


def brief_due(conn, user, ws) -> bool:
    """The brand brief is still waiting: the member answered the first steps, hasn't finished or skipped the brief,
    and has made nothing yet. (It waits behind the card step when there is one.) After the free first ads
    (brief="after"): once they are made or skipped, and once the plan step is behind them."""
    if user is None or ws is None or not ui_v2.on():
        return False
    a = answers(user)
    if a.get("brief") == "after":
        import plan_gate
        row = conn.execute("SELECT first_free_at FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
        return bool((row is not None and row["first_free_at"]) or a.get("first") == "skip") \
            and not plan_gate.needs_plan(conn, ws, user)
    if a.get("brief") != "due":
        return False
    return conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone() is None


def _first_last(name: str) -> tuple[str, str]:
    parts = (name or "").split(None, 1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def step(name: str, action: str) -> None:
    """The sign-up funnel: this step seen, done or skipped (site_events.on_onboard), never for an admin viewing."""
    if g.get("ws") is None or g.get("viewing") is not None:
        return
    import site_events
    with db.connect() as conn:
        site_events.on_onboard(conn, g.ws["id"], name, action, request)


@bp.route("/app/welcome", methods=["GET", "POST"])
@login_required
def intent():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    a = answers(g.user)
    if request.method == "POST":
        with db.connect() as conn:
            if request.form.get("skip"):
                finish(conn, g.user)
                conn.commit()
                step("intent", "skipped")
                return redirect(url_for("studio"))
            save(conn, g.user, goals=clean_goals(request.form.getlist("goals")),
                 other=" ".join((request.form.get("other") or "").split())[:OTHER_MAX])
            conn.commit()
        step("intent", "done")
        return redirect(url_for("onboarding.profile"))
    step("intent", "seen")
    return render_template("v2/onboard_intent.html", goals=GOALS, picked=set(a.get("goals") or []),
                           other=a.get("other") or "")


@bp.route("/app/welcome/profile", methods=["GET", "POST"])
@login_required
def profile():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    a = answers(g.user)
    first, last = _first_last(g.user["name"] or "")
    if request.method == "POST":
        import first_ads
        f = request.form
        first = " ".join((f.get("first_name") or "").split())[:NAME_MAX]
        last = " ".join((f.get("last_name") or "").split())[:NAME_MAX]
        free = False
        with db.connect() as conn:
            if not f.get("skip"):
                name = (first + " " + last).strip()[:80]
                if name:
                    conn.execute("UPDATE users SET name=? WHERE id=?", (name, g.user["id"]))
                # one save: answers(user) is read from the request's row, so a second save would undo the first
                save(conn, g.user, company=" ".join((f.get("company") or "").split())[:COMPANY_MAX],
                     website=clean_website(f.get("website")), brief="due")
                # a link given here or pasted before signing up: the free first ads come next, the brief after them
                u = conn.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
                ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
                free = first_ads.free_open(conn, ws, u)
                if free:
                    save(conn, u, brief="after")
            finish(conn, g.user)
            conn.commit()
        step("profile", "skipped" if f.get("skip") else "done")
        # the card step, when one is due, catches either page (card_routes.gate); the brief follows it (_start).
        # The confirm step catches the free first ads (they are made, so the address is proven first).
        return redirect(url_for("studio" if f.get("skip") else "onboarding.first" if free else "onboarding.brief"))
    step("profile", "seen")
    return render_template("v2/onboard_profile.html", first=first, last=last, company=a.get("company") or "",
                           website=a.get("website") or pasted_link())


# ---------------------------------------------------------------- the brand brief
ANGLE_TYPE = "ugc"          # the ad type angle ideas are written for here (Create asks per type; this is its default)


def _import_link():
    """Read the pasted link with the first-run importer (app.start_import: free, no AI read, throttled). Returns
    (ok, words for the member)."""
    from flask import current_app
    out = current_app.view_functions["start_import"]()
    resp, status = (out[0], out[1]) if isinstance(out, tuple) else (out, getattr(out, "status_code", 200))
    data = resp.get_json(silent=True) or {}
    if status == 200 and data.get("ok"):
        return True, "Here is what I read. Fix anything I got wrong."
    return False, data.get("error") or "That page couldn't be read. Try another link, or skip and add it by hand."


def _host(url) -> str:
    """The site of an address, lower case and without www: "rooflongevity.example" for
    "https://www.RoofLongevity.example/roofs". "" for nothing usable."""
    from urllib.parse import urlsplit
    u = str(url or "").strip()
    if not u:
        return ""
    try:
        host = (urlsplit(u if "://" in u else "https://" + u).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def other_site(user, p, b) -> str:
    """The site the member typed in the questions when it is not the site of the brand on screen: the workspace was
    set up for another business before they answered. "" when they typed none, when it is this brand's site, or
    when they already chose here (read a link in this step, or kept the brand shown)."""
    import products
    a = answers(user)
    typed = _host(a.get("website"))
    if not typed or p is None or a.get("site_kept") == typed:
        return ""
    info = products.import_info(p)
    have = {_host(info.get("url") if isinstance(info, dict) else ""), _host(p["url"] if "url" in p.keys() else ""),
            _host(b["website"] if b is not None else "")}
    return "" if typed in have else typed


def _brand_name_for(user, host: str) -> str:
    """What to call the new brand until its page is read: the company they typed, else the site's own name."""
    company = " ".join(str(answers(user).get("company") or "").split())
    return (company or host.split(".")[0].replace("-", " ").replace("_", " ").title() or "New brand")[:80]


def _suggest_angles(conn, pid) -> str:
    """The paid step: the same call, hold and settle as Create's angle ideas. Returns the words for the member."""
    import core
    import create_routes
    import creator
    import llm
    if not llm.anthropic_ready():
        return "AI writing isn't switched on yet. Add your own angle below; nothing was charged."
    quote = creator.estimate_angles()
    hold = core.reserve_ai(quote, "AI angle ideas")
    if hold is None:
        return f"That needs about {quote} credits and you have {create_routes._bal():,}. Add your own angle below."
    usage = llm.Usage()
    have = [r["title"] for r in conn.execute("SELECT title FROM angles WHERE product_id=?", (pid,))]
    try:
        out, err = creator.angles(core.brand(pid), ANGLE_TYPE, 5, have[:20], usage), None
    except llm.LLMError as e:
        out, err = [], f"The writer didn't finish ({e}). Try again."
    saved = sum(1 for a in out if create_routes.save_angle(conn, pid, a, "ai"))
    taken = create_routes._settle(hold, usage)
    conn.commit()
    if err:
        return err + (f" {taken} credits were charged." if taken else " Nothing was charged.")
    return f"{saved} angle{'' if saved == 1 else 's'} added for {taken} credit{'' if taken == 1 else 's'}."


@bp.route("/app/welcome/brand", methods=["GET", "POST"])
@login_required
def brief():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import creator
    import first_ads
    import llm
    import plan_gate
    import products
    from flask import flash
    here = url_for("onboarding.brief")
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        p = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
        b = products.get_brand(conn, ws["id"], p["brand_id"] if p is not None else ws["brand_id"]) \
            if (p is not None or ws["brand_id"]) else None
        if request.method == "POST":
            do, f = request.form.get("do") or "", request.form
            if do in ("done", "skip"):
                save(conn, g.user, brief="done")
                finish(conn, g.user)
                conn.commit()
                step("brief", "done" if do == "done" else "skipped")
                # the first ads come next (the gate sends a workspace with no plan to the plan step on the way)
                return redirect(url_for("onboarding.first" if first_ads.on() and p is not None else "studio"))
            if do == "import":
                ok, words = _import_link()
                if ok:      # what is on screen now is what they just asked to be read
                    save(conn, g.user, site_kept=_host(answers(g.user).get("website")))
                    conn.commit()
                flash(words, "ok") if ok else flash(words)
                return redirect(here)
            other = other_site(g.user, p, b)
            if do == "keep" and other:
                save(conn, g.user, site_kept=other)
                conn.commit()
                return redirect(here)
            if do == "other" and other and _host(f.get("url")) == other:
                try:
                    bid = products.create_brand(conn, ws, _brand_name_for(g.user, other),
                                                website=answers(g.user).get("website") or "")
                    products.activate(conn, ws, brand_id=bid)
                except products.ProductError as e:
                    flash(f"{e} To keep working here, choose to keep {(b['brand_name'] if b is not None else '') or p['name']}.")
                    return redirect(here)
                save(conn, g.user, site_kept=other)
                conn.commit()
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()   # the new brand is active
                ok, words = _import_link()
                flash(words, "ok") if ok else flash(words)
                return redirect(here)
            if p is None:
                return redirect(here)
            try:
                if do == "product":
                    products.update_product(conn, ws["id"], p["id"], f)
                    flash("Saved. Everything written for you starts from this.", "ok")
                elif do == "brand" and b is not None:
                    products.update_brand(conn, ws["id"], b["id"], {k: f.get(k) for k in ("tone", "about") if k in f})
                    flash("Saved.", "ok")
                elif do == "claims":
                    took = products.approve_claims(conn, ws["id"], p["id"], [int(x) for x in f.getlist("pick") if x.isdigit()])
                    if took:
                        flash(f"{len(took)} added to what you can say.", "ok")
                    else:
                        flash("Tick the ones that are true, then add them.")
                elif do == "angle":
                    import create_routes
                    aid = create_routes.save_angle(conn, p["id"], {"title": f.get("title"), "hook": f.get("hook")}, "user")
                    flash("Angle added.", "ok") if aid else flash("Not added: give it a name, and keep to what you can truthfully say.")
                elif do == "angles" and not plan_gate.needs_plan(conn, ws, g.user):
                    flash(_suggest_angles(conn, p["id"]), "ok")
            except products.ProductError as e:
                flash(str(e))
            return redirect(here + ("#angles" if do in ("angle", "angles") else ""))
        angles = conn.execute("SELECT * FROM angles WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 12",
                              (p["id"], ws["id"])).fetchall() if p is not None else []
        photos = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (p["id"],)).fetchone()[0] if p is not None else 0
        no_plan = plan_gate.needs_plan(conn, ws, g.user)          # the paid step (angle ideas) waits for a plan
        free = first_ads.free_open(conn, ws, g.user)              # the free first ads, before the plan step
        gift = first_ads.on() and (no_plan or first_ads.open_for(conn, ws, g.user))
    info = products.import_info(p) if p is not None else {}
    source = (info.get("url") if isinstance(info, dict) else "") or (p["url"] if p is not None and "url" in p.keys() else "") or ""
    step("brief", "seen")
    return render_template("v2/onboard_brief.html", p=p, b=b, angles=angles, photos=photos,
                           claims=products.claims_list(p["claims"]) if p is not None else [],
                           pending=products.pending(p) if p is not None else [],
                           source=source,
                           website=answers(g.user).get("website") or (b["website"] if b is not None else "") or
                           pasted_link(),
                           angle_price=creator.estimate_angles() if (llm.anthropic_ready() and not no_plan) else None,
                           gift=gift, no_plan=no_plan, other=other_site(g.user, p, b), free=free,
                           free_images=first_ads.FREE_IMAGES)


# ---------------------------------------------------------------- the first ads, on us

@bp.route("/app/welcome/first/preview/<tid>.jpg")
@login_required
def first_preview(tid):
    """The look's preview on the member's own photo and words (first_ads.preview_jpeg); ?h= is the headline."""
    import first_ads
    import products
    from flask import Response, abort
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        p = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
        n = max(first_ads.IMAGES, first_ads.FREE_IMAGES)
        look = next((t for t in first_ads.draft(conn, p, n)["looks"] if t["id"] == tid), None) if p is not None else None
        data = first_ads.preview_jpeg(conn, ws["id"], p, look, (request.args.get("h") or "")[:120]) if look else None
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@bp.route("/app/welcome/first", methods=["GET", "POST"])
@login_required
def first():
    """The first ads, on us (first_ads.py). GET shows the drafts; POST makes what is ticked and opens the campaign.
    Skipping leaves them on us: the desk keeps offering them until they are made.

    Before a plan, the free first result (first_ads.free_open): the link is read here when it hasn't been yet
    (do=import, the brief's free importer), the looks are made together, and the first-result screen follows. A
    workspace with no plan that it isn't open for goes to the plan step, as the gate used to send it."""
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import first_ads
    import plan_gate
    import products
    from flask import flash
    if not first_ads.on():
        return redirect(url_for("studio"))
    kept = {}
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        p = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
        free = first_ads.free_open(conn, ws, g.user)
        name = "free" if free else "first"
        if not free and plan_gate.needs_plan(conn, ws, g.user):
            return redirect(url_for("plangate.page"))
        if request.method == "POST":
            do = request.form.get("do")
            if do == "skip":
                save(conn, g.user, first="skip")
                conn.commit()
                step(name, "skipped")
                return redirect(url_for("studio"))
            if do == "import" and free and p is None:
                ok, words = _import_link()
                if ok:      # the brief later shows this page's read-back as it is
                    save(conn, g.user, site_kept=_host(answers(g.user).get("website")))
                    conn.commit()
                    return redirect(url_for("onboarding.first"))
                flash(words)
                kept = request.form
            elif p is None:
                return redirect(url_for("onboarding.brief"))
            else:
                out = first_ads.make(conn, ws, g.user, p, request.form)
                if out.get("ok"):
                    conn.commit()
                    step(name, "done")
                    return redirect(out["url"])
                flash(out.get("error") or "The ads could not be started. Nothing was used.")
                kept = request.form
        room = first_ads.left(conn, ws["id"], g.user)
        is_open = first_ads.open_for(conn, ws, g.user)
        n = first_ads.FREE_IMAGES if free else first_ads.IMAGES
        d = first_ads.draft(conn, p, n)
        cost = first_ads.price(conn, p, n) if (p is not None and is_open) else {"image": 0, "talk": 0, "total": 0}
        tid = first_ads.thread_of(conn, ws["id"])
        paid = first_ads.paid(conn, ws, g.user)
        if paid or free:        # on the welcome credits, or free before a plan: the image ads only, at their price
            cost = dict(cost, talk=0, total=cost["image"] * min(len(d["looks"]), room["image"]))
        balance = int(ws["credits"] or 0)
        made = bool(first_ads.free_jobs(conn, ws["id"])) and plans.plan_code(ws) == "free"
        link = first_ads.link_of(conn, ws, g.user) if free else ""
    step(name, "seen")
    return render_template("v2/onboard_first.html", p=p, d=d, room=room, is_open=is_open, cost=cost, kept=kept,
                           paid=paid, balance=balance, free=free, link=link, result=made,
                           thread=url_for("onboarding.first_result") if made else
                           url_for("desk.thread", tid=tid) if tid else "",
                           seconds=first_ads.TALK_SECONDS, images=first_ads.IMAGES)


@bp.route("/app/welcome/first/result")
@login_required
def first_result():
    """The first-result screen: the free first ads as they finish (the live cards, as in Studio), then the plan step.
    A sign-up from the presenter pages also sees one presenter still with a line from their product, labelled as a
    preview of the video, with a way to make the real one once a plan is picked."""
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import first_ads
    import jobs_routes
    import plan_gate
    import products
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        jobs = first_ads.free_jobs(conn, ws["id"])
        if not jobs:
            return redirect(url_for("onboarding.first"))
        p = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
        cards = [jobs_routes.card_html(conn, j, "batch") for j in jobs]
        no_plan = plans.plan_code(ws) == "free"
        needs = plan_gate.needs_plan(conn, ws, g.user)
        tid = first_ads.thread_of(conn, ws["id"])
    presenter = first_ads.presenter_preview(p) if first_ads.came_for_presenters(g.user) else None
    return render_template("v2/onboard_result.html", p=p, cards=cards, count=len(jobs), presenter=presenter,
                           no_plan=no_plan, needs_plan=needs,
                           next_url=url_for("plangate.page") if no_plan else url_for("studio"),
                           video_url=url_for("plangate.page") if no_plan else url_for("onboarding.first"),
                           thread=url_for("desk.thread", tid=tid) if tid and not needs else "")
