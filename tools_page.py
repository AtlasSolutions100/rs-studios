"""The Tools page of the new look, and the done-for-you request (Koby, Nov 11 2026, after walking through another
ad tool's app: a short menu and one catalog, instead of a long menu of groups).

    /app/tools            everything Campaignify makes and does, as tiles grouped by the job, with a search box
    /app/done-for-you     "we make the ads for you": a short form that reaches the person who handles it

The catalog is the sidebar's own list (app.NAV_GROUPS) regrouped by job, plus the new look's Studio tools. A tile
shows exactly when its menu item would (app._has_endpoint: the page exists and its switch is on), and carries the
same "included in <plan>" mark, so nothing a member can open from the menu today is missing here. With this page in
place the new look's sidebar keeps only the daily places, Tools and Settings; on a tool's own page the sidebar lists
that tool's group, so its neighbours stay one click away (side()).

Done for you. A member says what they sell and what they want made; the request is saved first (service_requests)
and then emailed to whoever handles them, with the member's address as the reply-to. Who that is, is the setting
DONE_FOR_YOU_EMAIL: an address, or "Name <address>" so the page can say who replies. No owner address lives in the
source (SEC-9, tests/test_liveids.py): unset, requests go to the site's contact address and the page says "we".
Nothing is promised on the page that isn't so: no turnaround, no numbers, no customers. If email isn't connected
the request is still saved and the page gives the address to write to.
"""
import logging
import os

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

import db
import ui_v2
from core import login_required

log = logging.getLogger("campaignify.tools")
bp = Blueprint("tools", __name__)

# (group, [(endpoint, name, icon, plan feature or None, one line, extra words the search matches)])
CATALOG = [
    ("Make ads", [
        ("studio_v2.page", "Studio", "create", None,
         "Make an image ad, a presenter video, a product video or a voiceover.", "image video ugc presenter voiceover"),
        ("studio_v2.workflow", "Workflow", "flows", None,
         "Describe a campaign, or start from a pack of presets.", "pack preset set"),
        ("create.index", "Advanced", "planner", None,
         "Every setting of an ad, step by step.", "create batch script angle"),
        ("copy.studio", "Copy", "copy", "copy",
         "Ad copy for each platform's fields, and a second pass on copy you already have.", "caption headline text optimizer"),
        ("brand_kit", "Products", "brand", None,
         "Your products, their photos and what you can say about them.", "photo brand kit claims"),
    ]),
    ("Plan", [
        ("campaigns.index", "Planner", "planner", "planner",
         "A campaign plan from a brief: the offer, the audience, the budget and the ads.", "campaign plan budget"),
        ("competitors.index", "Competitors", "competitors", "competitors",
         "Who you are up against: their offers and the angles they use.", "research rivals snapshot"),
        ("seo.index", "SEO & AEO", "seo", "seo",
         "Check a page for search engines and for AI answers.", "search geo google assistant"),
    ]),
    ("Capture leads", [
        ("pages.index", "Pages", "pages", "pages", "A landing page for an offer, built from blocks.", "landing page site"),
        ("forms.index", "Forms", "forms", "forms", "Forms that collect emails and answers.", "form signup lead"),
        ("pages.links", "Links", "links", "links", "Short links that count their clicks.", "smart link tracking utm"),
        ("forms.contacts", "Contacts", "contacts", "forms", "Everyone who filled in a form.", "people list crm"),
    ]),
    ("Follow up", [
        ("flows.index", "Flows", "flows", "flows", "Emails and texts that go out by themselves.", "email sms automation sequence"),
        ("messaging.sending", "Sending", "send", "flows", "The address your messages are sent from.", "domain sender email"),
        ("community.admin", "Community", "community", "community", "A members' space with courses.", "course lesson members"),
    ]),
    ("Launch", [
        ("adhub.index", "Ad accounts", "adhub", "adhub",
         "Set up each ad platform, and export your ads in its sizes.", "meta google tiktok export setup"),
        ("manage.index", "Ad campaigns", "planner", None, "The campaigns you have launched.", "live running budget"),
        ("adconnect.settings", "Connections", "plug", None, "Connect your ad accounts.", "meta google connect pixel"),
        ("cpools.index", "Creator Pools", "creators", None,
         "Fund a pool that creators earn from for clips about your product.", "clippers creators views"),
    ]),
]
def _lit(ep: str) -> tuple:
    """Every endpoint that counts as being 'in' the tool whose tile links to `ep` (the menu's own lists)."""
    import app as appmod
    for _title, items in appmod.NAV_GROUPS:
        for item in items:
            if item[0] == ep:
                return tuple(item[4])
    return {"studio_v2.page": ("studio_v2.page",), "studio_v2.workflow": ("studio_v2.workflow",)}.get(ep, (ep,))


def catalog() -> list[dict]:
    """The groups to show now: [{"title", "tools": [{"ep", "name", "icon", "line", "words", "href", "lock"}]}]. A
    tool is listed when its page exists and its switch is on; `lock` is the plan that includes it when the
    workspace's plan does not."""
    import app as appmod
    import plans
    out = []
    for title, items in CATALOG:
        tools = []
        for ep, name, icon, feat, line, words in items:
            if not appmod._has_endpoint(ep):
                continue
            lock = ""
            if feat and g.get("ws") is not None and not plans.has(g.ws, feat):
                lock = plans.required(feat)["name"]
            tools.append({"ep": ep, "name": name, "icon": icon, "line": line, "words": words, "href": url_for(ep),
                          "lock": lock, "lit": _lit(ep)})
        if tools:
            out.append({"title": title, "tools": tools})
    return out


def side() -> dict | None:
    """For the sidebar: the group of the tool the current page belongs to ({"title", "tools"}), or None on a page
    that is not a tool (the desk, Library, Settings and so on)."""
    ep = request.endpoint or ""
    if not ep or ep == "tools.page":
        return None
    if "tools_side" not in g:
        g.tools_side = None
        for grp in catalog():
            if any(ep in t["lit"] for t in grp["tools"]):
                g.tools_side = grp
                break
    return g.tools_side


def featured(conn) -> list[dict]:
    """The row on top of the page: what is worth knowing about now. Each one is real and open to this workspace."""
    out = [{"key": "dfy", "title": "We make the ads for you",
            "line": "Short on time? Our team at Rector & Stull plans and makes your ads. Tell us what you need.",
            "cta": "Tell us what you need", "href": url_for("tools.done_for_you")}]
    try:
        import mcpserver
        if mcpserver.enabled():
            out.append({"key": "mcp", "title": "Make ads from Claude or ChatGPT",
                        "line": "Connect Campaignify to the assistant you already use and ask it for ads there.",
                        "cta": "See how to connect", "href": "/mcp"})
    except Exception as e:  # noqa: BLE001  (a featured card is never a reason for the page to fail)
        log.warning("the MCP card was left out: %s", str(e)[:120])
    try:
        import first_ads
        if first_ads.open_for(conn, g.ws, g.user):
            out.append({"key": "first", "title": "Your first ads are on us",
                        "line": "2 image ads and a short talking video, drafted from your page.",
                        "cta": "Review my first ads", "href": url_for("onboarding.first")})
    except Exception as e:  # noqa: BLE001
        log.warning("the first-ads card was left out: %s", str(e)[:120])
    return out


@bp.route("/app/tools")
@login_required
def page():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    g.v2_tab = "tools"
    with db.connect() as conn:
        cards = featured(conn)
    groups = catalog()
    return render_template("v2/tools.html", groups=groups, cards=cards, count=sum(len(x["tools"]) for x in groups))


# ---------------------------------------------------------------- done for you

DFY_PER_DAY = 3
WHAT_MAX, SELL_MAX, PHONE_MAX = 2000, 300, 40


def dfy_contact() -> tuple[str, str]:
    """(name or "", address) of who handles done-for-you requests: DONE_FOR_YOU_EMAIL, else the site's contact."""
    from email.utils import parseaddr
    name, addr = parseaddr((os.environ.get("DONE_FOR_YOU_EMAIL") or "").strip())
    if "@" not in addr:
        import public_site
        return "", public_site.CONTACT
    return " ".join(name.split())[:40], addr


def dfy_email() -> str:
    return dfy_contact()[1]


def _clean(v, n) -> str:
    return " ".join(str(v or "").split())[:n]


def _send(row, ws_name: str) -> bool:
    """Email one saved request to whoever handles them. False when it could not be sent (it stays saved)."""
    import html as htmllib

    import mailer
    import verify
    lines = [("From", f"{row['name'] or 'A member'} <{row['email']}>"), ("Workspace", f"{ws_name} (#{row['workspace_id']})"),
             ("Phone", row["phone"] or "not given"), ("What they sell", row["sells"] or "not said"),
             ("What they want", row["wants"])]
    text = "A done-for-you request from Campaignify.\n\n" + "\n".join(f"{k}: {v}" for k, v in lines) \
        + "\n\nReply to this email to reach them."
    html = ('<div style="font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#141414;max-width:560px">'
            "<p>A done-for-you request from Campaignify.</p>"
            + "".join(f"<p><b>{htmllib.escape(k)}</b><br>{htmllib.escape(str(v))}</p>" for k, v in lines)
            + "<p>Reply to this email to reach them.</p></div>")
    try:
        mailer.send(frm=verify.from_address(), to=dfy_email(), subject=f"Done for you: {ws_name}"[:120], html=html,
                    text=text, reply_to=row["email"], tags={"kind": "service_request"},
                    idempotency_key=f"service-request-{row['id']}")
        return True
    except mailer.MailError as e:
        log.warning("done-for-you request %s was saved but not emailed: %s", row["id"], e)
        return False


@bp.route("/app/done-for-you", methods=["GET", "POST"])
@login_required
def done_for_you():
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import products
    g.v2_tab = "tools"
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        p = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
        last = conn.execute("SELECT * FROM service_requests WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                            (ws["id"],)).fetchone()
        kept = {"sells": (p["name"] if p is not None else ""), "wants": "", "email": g.user["email"], "phone": ""}
        if request.method == "POST":
            f = request.form
            kept = {"sells": _clean(f.get("sells"), SELL_MAX), "wants": " ".join(str(f.get("wants") or "").split())[:WHAT_MAX],
                    "email": _clean(f.get("email"), 200), "phone": _clean(f.get("phone"), PHONE_MAX)}
            key = f"dfy:{ws['id']}"
            if not kept["wants"]:
                flash("Say what you want made, in a sentence or two.")
            elif "@" not in kept["email"] or "." not in kept["email"].split("@")[-1]:
                flash("Give an email address we can reply to.")
            elif db.throttled(key, DFY_PER_DAY, 86400):
                flash("You have already sent a few requests today. We have them, and will reply by email.")
            else:
                db.hit(key)
                rid = conn.execute("INSERT INTO service_requests (workspace_id, user_id, name, email, phone, sells, wants, "
                                   "created_at) VALUES (?,?,?,?,?,?,?,?)",
                                   (ws["id"], g.user["id"], g.user["name"] or "", kept["email"], kept["phone"],
                                    kept["sells"], kept["wants"], db.now())).lastrowid
                conn.commit()
                row = conn.execute("SELECT * FROM service_requests WHERE id=?", (rid,)).fetchone()
                if _send(row, ws["name"] or "A workspace"):
                    conn.execute("UPDATE service_requests SET sent_at=? WHERE id=?", (db.now(), rid))
                    conn.commit()
                return redirect(url_for("tools.done_for_you", sent=rid))
        sent = None
        if request.args.get("sent"):
            sent = conn.execute("SELECT * FROM service_requests WHERE id=? AND workspace_id=?",
                                (request.args.get("sent", type=int) or 0, ws["id"])).fetchone()
    who, contact = dfy_contact()
    return render_template("v2/done_for_you.html", kept=kept, sent=sent, last=last, contact=contact, who=who,
                           emailed=bool(sent is not None and sent["sent_at"]))
