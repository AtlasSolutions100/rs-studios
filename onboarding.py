"""The first minutes of a new account (M14, behind ui_v2.on()).

    /app/welcome            "What do you want to make?"  pick any, or type something else
    /app/welcome/profile    name, company, website (optional)
    then the card step if the workspace needs one (card_routes.py, RS_CARD_REQUIRED)
    /app/welcome/brand      the brand brief: paste a link, see what was read, fix it, pick angles; then Studio

The answers are kept on the member (users.onboarding_json, users.onboarded_at) and used for the first run:
the composer opens on the first thing they said they want that their credits cover (studio_v2.first_mode),
and a website typed here is already in the box on the brand screen and in "Add product from URL".

Only a new account sees the steps by itself: no ad made yet and the steps neither finished nor skipped. Either
page can be opened by hand at any time (an owner previewing the look), and "Skip for now" always leaves.
Nothing here charges, grants or changes credits, plans or anything at Stripe, with one exception that says its price
on the button: "Suggest angles" on the brand brief is the same paid AI call as in Create (creator.angles).

The brand brief (Nov 11 2026) is not a new store of facts. Each card edits the product and brand columns every prompt
already reads through core.brand() (products.as_brief): what you sell (name, description, price, offer), who buys
it (audience), what you can say (approved claims, plus the ones the import suggested), how you sound (the brand's
tone and about). Angles are rows in `angles`, the same ones Create offers.
"""
import json

from flask import Blueprint, g, redirect, render_template, request, url_for

import db
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
START_ENDPOINTS = {"studio", "studio_v2.page", "card.page"}


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
        if request.endpoint != "card.page" and brief_due(conn, g.user, g.ws):
            return redirect(url_for("onboarding.brief"))
    return None


def brief_due(conn, user, ws) -> bool:
    """The brand brief is still waiting: the member answered the first steps, hasn't finished or skipped the brief,
    and has made nothing yet. (It waits behind the card step when there is one.)"""
    if user is None or ws is None or not ui_v2.on() or answers(user).get("brief") != "due":
        return False
    return conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone() is None


def _first_last(name: str) -> tuple[str, str]:
    parts = (name or "").split(None, 1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


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
                return redirect(url_for("studio"))
            save(conn, g.user, goals=clean_goals(request.form.getlist("goals")),
                 other=" ".join((request.form.get("other") or "").split())[:OTHER_MAX])
            conn.commit()
        return redirect(url_for("onboarding.profile"))
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
        f = request.form
        first = " ".join((f.get("first_name") or "").split())[:NAME_MAX]
        last = " ".join((f.get("last_name") or "").split())[:NAME_MAX]
        with db.connect() as conn:
            if not f.get("skip"):
                name = (first + " " + last).strip()[:80]
                if name:
                    conn.execute("UPDATE users SET name=? WHERE id=?", (name, g.user["id"]))
                # one save: answers(user) is read from the request's row, so a second save would undo the first
                save(conn, g.user, company=" ".join((f.get("company") or "").split())[:COMPANY_MAX],
                     website=clean_website(f.get("website")), brief="due")
            finish(conn, g.user)
            conn.commit()
        # the card step, when one is due, catches either page (card_routes.gate); the brief follows it (_start)
        return redirect(url_for("studio" if f.get("skip") else "onboarding.brief"))
    return render_template("v2/onboard_profile.html", first=first, last=last, company=a.get("company") or "",
                           website=a.get("website") or "")


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
    import llm
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
                return redirect(url_for("studio"))
            if do == "import":
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
                elif do == "angles":
                    flash(_suggest_angles(conn, p["id"]), "ok")
            except products.ProductError as e:
                flash(str(e))
            return redirect(here + ("#angles" if do in ("angle", "angles") else ""))
        angles = conn.execute("SELECT * FROM angles WHERE product_id=? AND workspace_id=? ORDER BY id DESC LIMIT 12",
                              (p["id"], ws["id"])).fetchall() if p is not None else []
        photos = conn.execute("SELECT COUNT(*) FROM photos WHERE product_id=?", (p["id"],)).fetchone()[0] if p is not None else 0
    info = products.import_info(p) if p is not None else {}
    source = (info.get("url") if isinstance(info, dict) else "") or (p["url"] if p is not None and "url" in p.keys() else "") or ""
    return render_template("v2/onboard_brief.html", p=p, b=b, angles=angles, photos=photos,
                           claims=products.claims_list(p["claims"]) if p is not None else [],
                           pending=products.pending(p) if p is not None else [],
                           source=source,
                           website=answers(g.user).get("website") or (b["website"] if b is not None else "") or "",
                           angle_price=creator.estimate_angles() if llm.anthropic_ready() else None)

