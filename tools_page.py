"""The Tools page of the new look, and the done-for-you request (Koby, Nov 11 2026, after walking through another
ad tool's app: a short menu and one catalog, instead of a long menu of groups).

    /app/tools            everything Campaignify makes and does, as tiles grouped by the job, with a search box
    /app/done-for-you     "we make the ads for you": a short form that reaches the person who handles it

The catalog is the sidebar's own list (app.NAV_GROUPS) regrouped by job, plus the new look's Studio tools. A tile
shows exactly when its menu item would (app._has_endpoint: the page exists and its switch is on), and carries the
same "included in <plan>" mark, so nothing a member can open from the menu today is missing here. With this page in
place the new look's sidebar keeps only the daily places, Tools and Settings; on a tool's own page the sidebar lists
that tool's group, so its neighbours stay one click away (side()).

Done for you (Koby, Nov 11 2026: one button, no form, "contact our team of professionals"). Pressing the button
saves a request (service_requests) with what the workspace already holds (snapshot: who is asking, what they said
at sign-up, the brand, the products, what they have made) and emails it to whoever handles them, with the
member's own address as the reply-to. Who that is, is the setting DONE_FOR_YOU_EMAIL; no owner address lives in
the source (SEC-9, tests/test_liveids.py), so unset, requests go to the site's contact address. The page names no
person, and promises nothing that isn't so: no turnaround, no numbers, no customers. If email isn't connected the
request is still saved and the page gives the site's contact address to write to.
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
            "line": "Short on time? Our team of professionals plans and makes your ads for you.",
            "cta": "Contact our team of professionals", "href": url_for("tools.done_for_you")}]
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
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        import products
        product = products.get_product(conn, ws["id"], ws["product_id"]) if ws["product_id"] else None
    groups = catalog()
    import inspiration
    return render_template("v2/tools.html", groups=groups, cards=cards, count=sum(len(x["tools"]) for x in groups),
                           insp=inspiration.groups(product), has_product=product is not None)


# ---------------------------------------------------------------- done for you

DFY_PER_DAY = 3
PRODUCTS_LISTED = 8
TOOL_WORDS = {"ad_image": "image ads", "ugc_video": "presenter videos", "photo_to_video": "product videos",
              "voiceover_video": "voiceover videos"}


def dfy_email() -> str:
    """Who gets a done-for-you request: DONE_FOR_YOU_EMAIL (an address, or "Name <address>"), else the site's
    contact address. No owner address lives in the source (SEC-9, tests/test_liveids.py)."""
    from email.utils import parseaddr
    addr = parseaddr((os.environ.get("DONE_FOR_YOU_EMAIL") or "").strip())[1]
    if "@" not in addr:
        import public_site
        return public_site.CONTACT
    return addr


def _count_word(tool: str, n: int) -> str:
    words = TOOL_WORDS.get(tool, tool.replace("_", " "))
    return words[:-1] if n == 1 and words.endswith("s") else words


def _one(v, n=400) -> str:
    v = " ".join(str(v or "").split())
    return v if len(v) <= n else v[:n - 1].rstrip() + "\u2026"


def snapshot(conn, ws, user) -> list:
    """What the team is sent with a request, read from the workspace's own rows: [(heading, [(label, value)])].
    Only what the member put into the app, plus counts of what they have made; empty values are left out."""
    import onboarding
    import plan_gate
    import plans
    import products
    out = []
    plan = plans.PLANS.get(plans.plan_code(ws), {}).get("name") or "No plan"
    if plan_gate.in_trial(ws):
        plan += " (in its free trial)"
    owner = conn.execute("SELECT * FROM users WHERE workspace_id=? ORDER BY (role='owner') DESC, id LIMIT 1",
                         (ws["id"],)).fetchone()
    who = [("Name", user["name"] or ""), ("Email", user["email"]),
           ("Workspace", f"{ws['name'] or 'A workspace'} (#{ws['id']})"), ("Plan", plan),
           ("Credits left", f"{db.balance(ws):,}"), ("Workspace since", str(ws["created_at"] or "")[:10])]
    if owner is not None and owner["id"] != user["id"]:
        who.append(("Workspace owner", f"{owner['name'] or ''} <{owner['email']}>".strip()))
    out.append(("Who is asking", who))
    a = onboarding.answers(user)
    goals = dict(onboarding.GOALS)
    said = [("Wants to make", ", ".join(goals[c] for c in a.get("goals") or [] if c in goals)), ("Also said", a.get("other")),
            ("Company", a.get("company")), ("Website", a.get("website"))]
    out.append(("What they said at sign-up", said))
    rows = conn.execute("SELECT * FROM products WHERE workspace_id=? AND archived=0 ORDER BY (id=?) DESC, id LIMIT ?",
                        (ws["id"], ws["product_id"] or 0, PRODUCTS_LISTED)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=? AND archived=0", (ws["id"],)).fetchone()[0]
    first = rows[0] if rows else None
    b = products.get_brand(conn, ws["id"], first["brand_id"] if first is not None else ws["brand_id"]) \
        if (first is not None or ws["brand_id"]) else None
    brief = products.as_brief(b, first, ws)
    out.append(("Brand", [("Name", brief["name"]), ("Website", brief["website"]), ("Voice", brief["tone"]),
                          ("About", _one(brief["about"]))]))
    for i, p in enumerate(rows):
        claims = "; ".join(x.strip() for x in str(p["claims"] or "").splitlines() if x.strip())
        out.append((f"Product {i + 1} of {total}" if total > 1 else "Product",
                    [("Name", p["name"]), ("Price", p["price"]), ("Offer", p["offer"]), ("Page", p["url"]),
                     ("What it is", _one(p["description"])), ("Who buys it", _one(p["audience"], 300)),
                     ("Approved claims", _one(claims, 500))]))
    made = conn.execute("SELECT tool, COUNT(*) AS n FROM jobs WHERE workspace_id=? AND status='done' AND deleted_at IS NULL "
                        "GROUP BY tool ORDER BY n DESC", (ws["id"],)).fetchall()
    last = conn.execute("SELECT MAX(created_at) FROM jobs WHERE workspace_id=?", (ws["id"],)).fetchone()[0]
    camps = [r["title"] for r in conn.execute("SELECT title FROM desk_threads WHERE workspace_id=? AND archived=0 ORDER BY "
                                              "updated_at DESC, id DESC LIMIT 3", (ws["id"],)).fetchall() if r["title"]]
    out.append(("What they have made here", [
        ("Finished ads", ", ".join(f"{r['n']} {_count_word(r['tool'], r['n'])}" for r in made) or "none yet"),
        ("Last ad started", str(last or "")[:10]), ("Recent campaigns", "; ".join(camps))]))
    return [(h, [(k, str(v)) for k, v in pairs if str(v or "").strip()]) for h, pairs in out]


def details_text(snap) -> str:
    return "\n\n".join(h + "\n" + "\n".join(f"  {k}: {v}" for k, v in pairs) for h, pairs in snap if pairs)


def _send(row, ws_name: str, snap) -> bool:
    """Email one saved request to whoever handles them. False when it could not be sent (it stays saved)."""
    import html as htmllib

    import mailer
    import verify
    intro = "A member pressed \"Contact our team of professionals\" in Campaignify. Reply to this email to reach them."
    text = intro + "\n\n" + details_text(snap)
    html = ('<div style="font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#141414;max-width:600px">'
            f"<p>{htmllib.escape(intro)}</p>"
            + "".join(f'<h3 style="font-size:15px;margin:18px 0 6px">{htmllib.escape(h)}</h3>'
                      + "".join(f'<p style="margin:0 0 4px"><b>{htmllib.escape(k)}:</b> {htmllib.escape(v)}</p>' for k, v in pairs)
                      for h, pairs in snap if pairs) + "</div>")
    try:
        mailer.send(frm=verify.from_address(), to=dfy_email(), subject=f"Done for you request: {ws_name}"[:120], html=html,
                    text=text, reply_to=row["email"], tags={"kind": "service_request"},
                    idempotency_key=f"service-request-{row['id']}")
        return True
    except mailer.MailError as e:
        log.warning("done-for-you request %s was saved but not emailed: %s", row["id"], e)
        return False


@bp.route("/app/done-for-you", methods=["GET", "POST"])
@login_required
def done_for_you():
    """One button: "Contact our team of professionals". Pressing it saves a request with what the workspace already
    holds (snapshot) and emails it to whoever handles them, with the member's own address as the reply-to."""
    if not ui_v2.on():
        return redirect(url_for("studio"))
    import public_site
    g.v2_tab = "tools"
    with db.connect() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (g.ws["id"],)).fetchone() or g.ws
        last = conn.execute("SELECT * FROM service_requests WHERE workspace_id=? ORDER BY id DESC LIMIT 1",
                            (ws["id"],)).fetchone()
        if request.method == "POST":
            key = f"dfy:{ws['id']}"
            if db.throttled(key, DFY_PER_DAY, 86400):
                flash("You have already asked a few times today. Our team has your request and will reply by email.")
            else:
                db.hit(key)
                snap = snapshot(conn, ws, g.user)
                rid = conn.execute("INSERT INTO service_requests (workspace_id, user_id, name, email, wants, details, "
                                   "created_at) VALUES (?,?,?,?,?,?,?)",
                                   (ws["id"], g.user["id"], g.user["name"] or "", g.user["email"],
                                    "Asked for our team to get in touch.", details_text(snap), db.now())).lastrowid
                conn.commit()
                row = conn.execute("SELECT * FROM service_requests WHERE id=?", (rid,)).fetchone()
                if _send(row, ws["name"] or "A workspace", snap):
                    conn.execute("UPDATE service_requests SET sent_at=? WHERE id=?", (db.now(), rid))
                    conn.commit()
                return redirect(url_for("tools.done_for_you", sent=rid))
        sent = None
        if request.args.get("sent"):
            sent = conn.execute("SELECT * FROM service_requests WHERE id=? AND workspace_id=?",
                                (request.args.get("sent", type=int) or 0, ws["id"])).fetchone()
    return render_template("v2/done_for_you.html", sent=sent, last=last, contact=public_site.CONTACT,
                           emailed=bool(sent is not None and sent["sent_at"]))
