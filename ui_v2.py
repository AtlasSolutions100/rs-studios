"""The calmer, neutral look and the Studio composer (M14), behind a switch.

    UI_V2=1          everyone gets it (a repo variable, sent by deploy.yml's sync list). Default off.
    preview          a site admin (the owners) turns it on for themself only:
                       * Settings: the account menu's "New look" switch (users.ui_v2), or
                       * any page with ?ui=v2, which sets a signed cookie that sticks for 180 days in that
                         browser (it keeps working on the public pages after logging out, so the landing page
                         and sign-up can be looked at too). ?ui=v1 removes the cookie and the setting.

Off, nothing changes: the same templates, the same stylesheet, the same routes. On, base.html adds
data-ui="v2" and static/ui_v2.css (tokens and component overrides, so every page picks the look up), Home becomes
the Studio composer (studio_v2.py), new accounts get the onboarding steps (onboarding.py), and the card step
gets the timeline (web/v2/card.html). The old Create flow stays at /app/create as "Advanced". M15 carries the look
to every page (the public site, the standalone pages, error pages) and adds Studio's Workflow tab.

The cookie is signed with the app's secret, so a cookie value typed by hand turns nothing on; only a logged-in
admin can get one set.
"""
import os

from flask import Blueprint, abort, current_app, g, redirect, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer

import db
from core import login_required

bp = Blueprint("ui_v2", __name__)

COOKIE = "cfy_ui"
COOKIE_DAYS = 180
PARAM = "ui"
_ON = ("v2", "2", "new", "on")
_OFF = ("v1", "1", "old", "off")


def enabled() -> bool:
    """UI_V2: 1 / on gives the new look to everyone (default off)."""
    return (os.environ.get("UI_V2") or "").strip().lower() in ("1", "on", "true", "yes")


def can_preview(user) -> bool:
    """Who may turn the preview on for themself: site admins (the owners, owners.py)."""
    return user is not None and bool(user["is_admin"])


def _signer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.secret_key, salt="cfy-ui-preview")


def cookie_value() -> str:
    return _signer().dumps("v2")


def cookie_ok(raw) -> bool:
    """A preview cookie we signed (anything else is ignored)."""
    if not raw:
        return False
    try:
        return _signer().loads(raw) == "v2"
    except BadSignature:
        return False


def _user_pref(user) -> bool:
    return user is not None and "ui_v2" in user.keys() and bool(user["ui_v2"])


def on() -> bool:
    """Is the new look on for this request? Worked out once per request."""
    if "ui_v2_on" not in g:
        user = g.get("user")
        want = (request.args.get(PARAM) or "").strip().lower()
        if enabled():
            g.ui_v2_on = True
        elif want in _OFF:
            g.ui_v2_on = False
        elif want in _ON and can_preview(user):
            g.ui_v2_on = True
        elif user is not None:
            # logged in: only an admin's own setting or signed cookie counts
            g.ui_v2_on = can_preview(user) and (_user_pref(user) or cookie_ok(request.cookies.get(COOKIE)))
        else:
            g.ui_v2_on = cookie_ok(request.cookies.get(COOKIE))
    return g.ui_v2_on


@bp.after_app_request
def _sticky(resp):
    """?ui=v2 from an admin sets the signed cookie; ?ui=v1 from anyone removes it."""
    want = (request.args.get(PARAM) or "").strip().lower()
    if not want:
        return resp
    if want in _ON and can_preview(g.get("user")):
        resp.set_cookie(COOKIE, cookie_value(), max_age=COOKIE_DAYS * 86400, httponly=True, samesite="Lax",
                        secure=request.is_secure)
    elif want in _OFF:
        if request.cookies.get(COOKIE):
            resp.delete_cookie(COOKIE)
        user = g.get("user")
        if _user_pref(user):
            with db.connect() as conn:
                conn.execute("UPDATE users SET ui_v2=0 WHERE id=?", (user["id"],))
                conn.commit()
    return resp


@bp.route("/app/settings/look", methods=["POST"])
@login_required
def set_look():
    """The account menu's "New look" switch: an admin's own setting, kept on the account."""
    if not can_preview(g.user):
        abort(403)
    want = request.form.get("look") == "v2"
    with db.connect() as conn:
        conn.execute("UPDATE users SET ui_v2=? WHERE id=?", (1 if want else 0, g.user["id"]))
        conn.commit()
    resp = redirect(url_for("studio"))
    if not want and request.cookies.get(COOKIE):
        resp.delete_cookie(COOKIE)
    return resp


def surface() -> str:
    """"app" (dark by default) or "public" (light by default: the site, log in, sign up, onboarding)."""
    ep = request.endpoint or ""
    if g.get("user") is None or ep.startswith("onboarding.") or ep == "card.page":
        return "public"
    return "app" if request.path.startswith(("/app", "/admin")) else "public"


@bp.app_context_processor
def _ctx():
    # Static files render nothing. A page with no endpoint is an error page (404, 500): it takes the look too, and if
    # working that out fails there (the error came before the member was known) it falls back to the old one.
    if request.endpoint == "static":
        return {"ui_v2": False}
    try:
        is_on, where = on(), surface()
    except Exception:  # noqa: BLE001
        return {"ui_v2": False}
    import public_site
    return {"ui_v2": is_on, "ui_v2_surface": where, "ui_v2_can_preview": can_preview(g.get("user")),
            "ui_v2_global": enabled(), "ui_v2_pref": _user_pref(g.get("user")),
            # the home page's gallery of real outputs and its neutral tiles (public_site.showcase, M15)
            "showcase": public_site.showcase, "showcase_shapes": public_site.SHOWCASE_SHAPES}
