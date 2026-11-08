"""Launch kit on the plan page (roadmap M5.3; UX-4 and UX-10 in the Nov 7 UX audit).

Before this, the launch handoff was split across 3 screens: the plan's "Launch checklist" (ticks in this browser
only), the Library for downloads and the hub for steps. The kit puts it on the plan page, one block per platform in
the plan:

  * the plan's finished ads for that platform, with export links at that platform's sizes (adhub.PLATFORMS exports)
  * the plan's copy for that platform, each field with a Copy button
  * the goal, budget (share of the plan's budget) and audience as plain lines
  * the steps to tick: the plan's checklist items for that platform, the pixel step and the "Create the campaign"
    step from the hub, and disclose-and-label; plus a link to the hub's full setup guide

Ticks are stored on the server. The hub's own steps keep living in adhub_progress (so the hub page, this kit and the
Home step strip agree); the plan's own items go in launch_ticks, per campaign. Both are per workspace, so they
follow the person to any device and are shared with the team, like the hub's.

This is where roadmap M2.3's "Create paused campaign" button lands later (one per platform block).

RS_LAUNCH_KIT=1 turns it on (default off); off, the plan page keeps the old checklist.
"""
import os
import re

from flask import url_for

import adhub
import platforms

# Planner platform -> hub platform (the hub's Google guide is "YouTube & Google Ads").
HUB_KEY = {"google": "youtube"}
NOT_ADS = {"email", "landing"}
GOALS = {"sales": "Sales", "leads": "Leads", "traffic": "Traffic", "awareness": "Awareness",
         "engagement": "Engagement", "installs": "App installs"}
PIXEL_RE = re.compile(r"\b(pixel|tag|conversions?)\b")
CREATE_RE = re.compile(r"\b(create|build|set up|launch)\b.*\bcampaign\b")


def enabled() -> bool:
    return os.environ.get("RS_LAUNCH_KIT", "0").strip().lower() in ("1", "on", "true", "yes")


def hub_key(p: str):
    k = HUB_KEY.get(p, p)
    return k if k in adhub.PLATFORMS else None


def _hub_step(k: str, test) -> int | None:
    for i, (title, _, _) in enumerate(adhub.PLATFORMS[k]["steps"]):
        if test(title.lower()):
            return i
    return None


def create_step(k: str):
    """The hub step that means "the campaign is built in the ads manager" (the Home strip reads the same one)."""
    i = _hub_step(k, lambda t: t.startswith("create the campaign"))
    return i if i is not None else _hub_step(k, lambda t: t.startswith("upload your videos"))


def pixel_step(k: str):
    return _hub_step(k, lambda t: bool(PIXEL_RE.search(t)) and not t.startswith("read"))


def plan_platforms(brief: dict, plan: dict, ads) -> list:
    """The platforms the plan is for, in the brief's order, then any the plan added (budget, ads, copy, checklist)."""
    final = plan.get("final") or {}
    seen = []
    for src in ([brief.get("platforms") or []] + [[b.get("platform") for b in final.get("budget_split") or []]]
                + [[a["platform"] for a in ads]] + [[c.get("platform") for c in plan.get("copy") or []]]
                + [[i.get("platform") for i in final.get("checklist") or []]]):
        for p in src:
            if p in platforms.PLATFORMS and p not in NOT_ADS and p not in seen:
                seen.append(p)
    return seen


def _budget_line(brief: dict, split: dict | None) -> str:
    total = (brief.get("budget") or "").strip()
    if split and split.get("share_percent") is not None:
        line = f"{split['share_percent']}% of {total}" if total else f"{split['share_percent']}% of the budget"
        return line + (f". {split['note'].rstrip('.')}." if split.get("note") else "")
    return total or "Not set in the brief."


def _item_target(title: str, p: str, k: str | None) -> tuple | None:
    """Where a checklist item's link goes: the platform's ads in this kit, its hub step, or its guide."""
    t = title.lower()
    if re.search(r"\b(upload|ads?|creatives?|videos?|images?)\b", t) and not PIXEL_RE.search(t):
        return ("The ads", f"#kit-{p}-ads")
    if re.search(r"\bcopy|headline|caption|text\b", t):
        return ("The copy", f"#kit-{p}-copy")
    if k:
        i = pixel_step(k) if PIXEL_RE.search(t) else None
        return ("Setup guide", url_for("adhub.platform", platform=k) + (f"#s{i}" if i is not None else ""))
    return None


def build(c, brief: dict, plan: dict, ads, jobs: dict, outs: dict, hub_done: dict, ticks: set) -> dict:
    """Everything the kit shows, from the plan, its ads and the ticks. hub_done: {hub platform: set(step)};
    ticks: the plan's own ticked item keys (launch_ticks)."""
    final = plan.get("final") or {}
    splits = {b.get("platform"): b for b in final.get("budget_split") or []}
    goal = GOALS.get((brief.get("goal") or "sales").lower(), (brief.get("goal") or "Sales").capitalize())
    checklist = list(enumerate(final.get("checklist") or [], start=1))
    blocks, used_hub = [], set()
    for p in plan_platforms(brief, plan, ads):
        k = hub_key(p)
        hp = adhub.PLATFORMS.get(k) if k else None
        made = []
        for a in ads:
            j = jobs.get(a["job_id"]) if a["job_id"] else None
            if a["platform"] != p or not j or j["status"] != "done":
                continue
            for o in outs.get(j["id"]) or []:
                made.append({"id": o["id"], "kind": o["kind"], "angle": a["angle"], "tool": a["tool"],
                             "download": url_for("media", oid=o["id"], download=1),
                             "exports": [(label, f"{w} x {h}", url_for("adhub.export_file", oid=o["id"], platform=k, fmt=code))
                                         for code, label, w, h in (hp["exports"] if hp else [])]})
        waiting = sum(1 for a in ads if a["platform"] == p and a["status"] == "queued"
                      and (jobs.get(a["job_id"]) or {"status": "pending"})["status"] in ("pending", "running"))
        proposed = sum(1 for a in ads if a["platform"] == p and a["status"] == "proposed")
        copy = []
        for cp in plan.get("copy") or []:
            if cp.get("platform") != p:
                continue
            vs = cp.get("variants") or []
            v = next((x for x in vs if x.get("ok")), None) or (vs[0] if vs else None)
            if v:
                copy.append({"kind": platforms.KINDS.get(cp.get("kind"), {}).get("name", "Copy"), "angle": v.get("angle"),
                             "fields": [{"name": f.get("name"), "text": f.get("text")} for f in v.get("fields") or []],
                             "more": max(len(vs) - 1, 0), "ok": bool(v.get("ok"))})
        items = []
        hub_steps = []
        if k and k not in used_hub:
            used_hub.add(k)
            hub_steps = [(idx, rx) for idx, rx in ((pixel_step(k), PIXEL_RE), (create_step(k), CREATE_RE)) if idx is not None]
        for n, i in checklist:
            if i.get("platform") != p:
                continue
            title = i.get("title") or ""
            # The plan's own pixel or "create the campaign" item is the hub's step: one tick, shared with the hub.
            same = next(((idx, rx) for idx, rx in hub_steps if rx.search(title.lower())), None)
            if same:
                hub_steps.remove(same)
                items.append({"key": f"hub:{k}:{same[0]}", "title": title, "detail": i.get("detail") or "", "shared": True,
                              "link": ("Setup guide", url_for("adhub.platform", platform=k) + f"#s{same[0]}"),
                              "done": same[0] in hub_done.get(k, set())})
                continue
            items.append({"key": f"c{n}", "title": title, "detail": i.get("detail") or "",
                          "link": _item_target(title, p, k), "done": f"c{n}" in ticks})
        for idx, _ in hub_steps:
            title, detail, _link = hp["steps"][idx]
            items.append({"key": f"hub:{k}:{idx}", "title": title, "detail": detail, "shared": True,
                          "link": ("Setup guide", url_for("adhub.platform", platform=k) + f"#s{idx}"),
                          "done": idx in hub_done.get(k, set())})
        items.append({"key": f"disc:{p}", "title": "Disclose and label",
                      "detail": "#ad first in captions where it's a paid partnership, and the platform's AI label on "
                                "for AI-made media.", "link": None, "done": f"disc:{p}" in ticks})
        setup_total = len(hp["steps"]) if hp else 0
        blocks.append({
            "key": p, "name": platforms.PLATFORMS[p]["name"], "short": platforms.PLATFORMS[p]["short"], "hub": k,
            "manager": hp["manager"] if hp else None, "guide": url_for("adhub.platform", platform=k) if k else None,
            "setup_done": len(hub_done.get(k, set())) if k else 0, "setup_total": setup_total,
            "goal": goal, "budget": _budget_line(brief, splits.get(p)),
            "audience": (brief.get("audience") or "").strip() or "Not set in the brief. Each angle above names one.",
            "ads": made, "waiting": waiting, "proposed": proposed, "copies": copy, "ticks": items,
            "done": sum(1 for i in items if i["done"]), "total": len(items)})
    general = []
    for n, i in checklist:
        if i.get("platform") not in [b["key"] for b in blocks]:
            general.append({"key": f"c{n}", "title": i.get("title") or "", "detail": i.get("detail") or "",
                            "link": None, "done": f"c{n}" in ticks})
    general.append({"key": "lib", "title": "Download the finished ads",
                    "detail": "Full quality, in each platform's sizes from its block below, or the originals from your library.",
                    "link": ("Library", url_for("library")), "done": "lib" in ticks})
    allitems = general + [i for b in blocks for i in b["ticks"]]
    return {"blocks": blocks, "general": general, "done": sum(1 for i in allitems if i["done"]), "total": len(allitems),
            "keys": {i["key"] for i in allitems}}


def hub_done(conn, ws_id: int) -> dict:
    out = {}
    for r in conn.execute("SELECT platform, step FROM adhub_progress WHERE workspace_id=?", (ws_id,)):
        out.setdefault(r["platform"], set()).add(r["step"])
    return out


def plan_ticks(conn, ws_id: int, cid: int) -> set:
    return {r["item"] for r in conn.execute("SELECT item FROM launch_ticks WHERE workspace_id=? AND campaign_id=?",
                                            (ws_id, cid))}


def toggle(conn, ws_id: int, user_id: int, cid: int, item: str) -> bool:
    """Flip one tick; True when it is now done. hub:<platform>:<step> goes to the hub's table (shared with the hub
    page), anything else to launch_ticks for this plan. The caller checks the item is in this plan's kit."""
    import db
    m = re.fullmatch(r"hub:([a-z]+):(\d+)", item)
    if m:
        cur = conn.execute("DELETE FROM adhub_progress WHERE workspace_id=? AND platform=? AND step=?",
                           (ws_id, m.group(1), int(m.group(2))))
        if not cur.rowcount:
            conn.execute("INSERT INTO adhub_progress (workspace_id, platform, step, done_by, done_at) VALUES (?,?,?,?,?)",
                         (ws_id, m.group(1), int(m.group(2)), user_id, db.now()))
    else:
        cur = conn.execute("DELETE FROM launch_ticks WHERE workspace_id=? AND campaign_id=? AND item=?", (ws_id, cid, item))
        if not cur.rowcount:
            conn.execute("INSERT INTO launch_ticks (workspace_id, campaign_id, item, done_by, done_at) VALUES (?,?,?,?,?)",
                         (ws_id, cid, item, user_id, db.now()))
    conn.commit()
    return not cur.rowcount


def hub_order(conn, ws_id: int) -> tuple[list, set]:
    """The hub's platform order with the latest finished plan's platforms first, and those platforms."""
    import json
    row = conn.execute("SELECT brief_json, plan_json FROM campaigns WHERE workspace_id=? AND status='ready' "
                       "ORDER BY id DESC LIMIT 1", (ws_id,)).fetchone()
    first = []
    if row:
        for p in plan_platforms(json.loads(row["brief_json"] or "{}"), json.loads(row["plan_json"] or "{}"), []):
            k = hub_key(p)
            if k and k not in first:
                first.append(k)
    return first + [k for k in adhub.ORDER if k not in first], set(first)
