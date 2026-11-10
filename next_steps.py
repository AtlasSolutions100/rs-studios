"""The next-step thread on Home (roadmap M5.1; UX-1, UX-2, COPY-2 in the Nov 7 UX audit).

A made account sees one strip, "Your campaign": Ads · Plan · Page · Launch, with the step it is on highlighted and
one primary button for it. After the 2 free ads the next step is "Plan the campaign for {product}" (on Free that
opens the upgrade wall, whose copy is written for that moment). Each step is read from what the workspace has:

  * Ads: an ad finished or rendering (jobs and outputs)
  * Plan: a campaign plan that finished (campaigns.status = 'ready')
  * Page: a published landing page (pages.status = 'published')
  * Launch: the hub's "Create the campaign" step ticked for any platform (adhub_progress, the ticks the hub and
    the plan page share). Nothing in the app launches a campaign yet (roadmap M2.3), so the tick is the signal.

RS_NEXT_STEPS=1 turns it on (default off). Off, Home, the upgrade wall and the first-ads card are as before.
"""
import json
import os

from flask import g, url_for

import db
import plans

STEPS = ("ads", "plan", "page", "launch")


def enabled() -> bool:
    return os.environ.get("RS_NEXT_STEPS", "0").strip().lower() in ("1", "on", "true", "yes")


def _launch_steps() -> dict:
    """platform -> the hub step index that means "the campaign is built in the ads manager"."""
    import adhub
    out = {}
    for k, p in adhub.PLATFORMS.items():
        titles = [s[0].lower() for s in p["steps"]]
        i = next((n for n, t in enumerate(titles) if t.startswith("create the campaign")), None)
        if i is None:   # YouTube & Google: the videos go up in Google Ads instead
            i = next((n for n, t in enumerate(titles) if t.startswith("upload your videos")), None)
        if i is not None:
            out[k] = i
    return out


def state(conn, ws, product_name: str = "") -> dict:
    """The strip for this workspace: each step's done flag and line, the current step and its one button."""
    wid = ws["id"]
    outputs = conn.execute("SELECT COUNT(*) FROM outputs o JOIN jobs j ON j.id=o.job_id WHERE j.workspace_id=? "
                           "AND j.deleted_at IS NULL", (wid,)).fetchone()[0]
    rendering = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND status IN ('pending','running') "
                             "AND deleted_at IS NULL", (wid,)).fetchone()[0]
    plan = conn.execute("SELECT id, name FROM campaigns WHERE workspace_id=? AND status='ready' ORDER BY id DESC LIMIT 1",
                        (wid,)).fetchone()
    page = conn.execute("SELECT id, title, slug FROM pages WHERE workspace_id=? AND status='published' ORDER BY id DESC "
                        "LIMIT 1", (wid,)).fetchone()
    ticks = conn.execute("SELECT platform, step FROM adhub_progress WHERE workspace_id=?", (wid,)).fetchall()
    launch_at = _launch_steps()
    launched = [t["platform"] for t in ticks if launch_at.get(t["platform"]) == t["step"]]
    product = (product_name or "").strip() or "your product"

    steps = [
        {"key": "ads", "name": "Ads", "done": bool(outputs or rendering),
         "line": (f"{outputs:,} made" if outputs else "") + (" · " if outputs and rendering else "")
         + (f"{rendering:,} rendering" if rendering else "") or "None yet",
         "action": ("Make an ad", url_for("create.index"))},
        {"key": "plan", "name": "Plan", "done": bool(plan),
         "line": plan["name"] if plan else "Budget, platforms and a 2-week test",
         "href": url_for("campaigns.show", cid=plan["id"]) if plan else None,
         "action": (f"Plan the campaign for {product}", url_for("campaigns.index"))},
        {"key": "page", "name": "Page", "done": bool(page),
         "line": f"/p/{page['slug']}" if page else "Where the ads send people",
         "href": url_for("pages.edit", pid=page["id"]) if page else None,
         "action": ("Build the landing page", url_for("pages.index"))},
        {"key": "launch", "name": "Launch", "done": bool(launched),
         "line": "Built in the ads manager" if launched else "By hand in each ads manager, step by step",
         "action": ("Open the launch checklist",
                    url_for("campaigns.show", cid=plan["id"]) + "#launch" if plan else url_for("adhub.index"))},
    ]
    cur = next((s for s in steps if not s["done"]), None)
    if cur is None:
        primary = ("Plan the next campaign", url_for("campaigns.index"))
    else:
        primary = cur["action"]
    return {"steps": steps, "current": cur["key"] if cur else None, "primary": primary,
            "done": sum(1 for s in steps if s["done"]), "locked": not plans.has(ws, "planner")}


def made_counts(made) -> dict:
    """UX-2: count ads, not jobs. The 2 free ads are two single-ad jobs (two templates); a job made with
    versions > 1 (params batch) still counts once per version."""
    if not made:
        return {"ready": 0, "planned": 0}
    ready = sum(len(made["outs"].get(j["id"]) or []) for j in made["jobs"] if j["status"] == "done")
    planned = 0
    for j in made["jobs"]:
        try:
            n = int((json.loads(j["params_json"] or "{}") or {}).get("batch") or 1)
        except (TypeError, ValueError):
            n = 1
        planned += max(n, len(made["outs"].get(j["id"]) or []))
    return {"ready": ready, "planned": planned}


# The upgrade wall (web/upgrade.html), replacement copy from the UX audit (03b section 5). The planner wall is written
# out; other features keep their own name and blurb as the first paragraph and share the plan line and buttons.
STARTER_EXTRAS = "the copy studio, landing pages, smart links, flows and the ad-account guide"


def wall(feature: dict, need: dict) -> dict:
    from core import brand
    import planner
    on_free = plans.plan_code(g.ws) == "free" if g.get("ws") is not None else True
    if feature is plans.FEATURES["planner"] or feature.get("name") == plans.FEATURES["planner"]["name"]:
        product = ((brand() or {}).get("product") or "").strip() if g.get("ws") is not None else ""
        h1 = f"Plan the campaign for {product}" if product else "Plan the whole campaign"
        lede = ("The planner reads your product and approved claims, proposes the platforms, a budget split and a "
                "2-week test, writes 3 angles with hooks and scripts, lines up the ads with a credit price on each, "
                "writes the copy for every platform and hands you the launch checklist. "
                f"Planning costs up to {planner.cap_credits():,} credits; you pay what it uses.")
    else:
        h1, lede = feature["name"], feature["blurb"]
    if need["code"] == "starter":
        extras = STARTER_EXTRAS
    else:
        names = [f["name"] for f in plans.FEATURES.values() if f["plan"] == need["code"] and f.get("shipped")]
        below = plans.PLANS[plans.ORDER[max(plans.rank(need["code"]) - 1, 0)]]["name"]
        extras = f"everything in {below}" + (", and " + ", ".join(n[0].lower() + n[1:] for n in names)
                                                         if names else "")
    carry = "Your free credits, product and ads carry over." if on_free else "Your credits, products and ads carry over."
    return {"eyebrow": f"Comes with {need['name']}, ${need['usd']} a month", "h1": h1, "lede": lede,
            "plan_line": f"{need['name']} is ${need['usd']} a month with {need['credits']:,} credits, plus {extras}. {carry}",
            "cta": f"Get {need['name']}, ${need['usd']} a month", "small": "Cancel any month. No trial that bills you."}


def _home():
    """Template helper: the strip for the signed-in workspace (None when off)."""
    if not enabled() or g.get("ws") is None:
        return None
    from core import brand
    with db.connect() as conn:
        return state(conn, g.ws, (brand() or {}).get("product") or "")


def init(app):
    app.jinja_env.globals.update(next_steps_on=enabled, next_steps_home=_home, next_steps_counts=made_counts,
                                 next_steps_wall=wall)
