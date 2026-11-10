"""The first minutes of a new account (M14, behind ui_v2.on()).

    /app/welcome            "What do you want to make?"  pick any, or type something else
    /app/welcome/profile    name, company, website (optional)
    then the card step if the workspace needs one (card_routes.py, RS_CARD_REQUIRED), then Studio

The answers are kept on the member (users.onboarding_json, users.onboarded_at) and used for the first run:
the composer opens on the first thing they said they want that their credits cover (studio_v2.first_mode),
and a website typed here is already in the box on the brand screen and in "Add product from URL".

Only a new account sees the steps by itself: no ad made yet and the steps neither finished nor skipped. Either
page can be opened by hand at any time (an owner previewing the look), and "Skip for now" always leaves.
Nothing here charges, grants or changes credits, plans or anything at Stripe.
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
    return None


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
                save(conn, g.user, company=" ".join((f.get("company") or "").split())[:COMPANY_MAX],
                     website=clean_website(f.get("website")))
            finish(conn, g.user)
            conn.commit()
        return redirect(url_for("studio"))      # the card step, when one is due, catches this (card_routes.gate)
    return render_template("v2/onboard_profile.html", first=first, last=last, company=a.get("company") or "",
                           website=a.get("website") or "")
