"""Creative fatigue and "next batch" suggestions (roadmap M4.2, design doc 4.4).

An ad is wearing out when people have seen it too often: they stop clicking while the spend stays the same. From
ad_insights_daily (adinsights.week_over_week: the last 7 complete days against the 7 before, in the ad account's
time zone), an ad is flagged when all of these hold:

    * it delivered in both weeks (a new ad isn't fatigued);
    * the week before had at least MIN_IMPRESSIONS impressions and MIN_LINK_CLICKS link clicks (no flag on noise);
    * its link click rate fell by DROP_PCT % or more;
    * its spend changed by at most STABLE_SPEND_PCT % either way (a click rate that falls because the budget
      doubled into colder audiences is a different problem).

The suggestion uses the winning angle: the launch's ad with the best link click rate this week that isn't wearing
out (at least WIN_MIN_IMPRESSIONS impressions), else the workspace's best. The Create page opens with that angle
and the worn ad's format filled in (create_routes: ?refresh=<launch>:<ad id>). Nothing is made, launched or spent
on its own: the member makes the new ad and launches it like any other. No auto-spend, ever.

Meta launches only, behind AD_LAUNCH like the pages that show it.
"""
import json

import adinsights
import admanage

DROP_PCT = 30.0
STABLE_SPEND_PCT = 25.0
MIN_IMPRESSIONS = 1000
MIN_LINK_CLICKS = 10
WIN_MIN_IMPRESSIONS = 500


def judge(wow: dict) -> bool:
    """Is this ad wearing out? wow is adinsights.week_over_week()."""
    if not wow.get("complete"):
        return False
    last, this = wow["last"], wow["this"]
    if (last["imp"] or 0) < MIN_IMPRESSIONS or (last["link"] or 0) < MIN_LINK_CLICKS:
        return False
    if last["ctr"] is None or this["ctr"] is None or wow.get("ctr_change") is None:
        return False
    if wow.get("spend_change") is None or abs(wow["spend_change"]) > STABLE_SPEND_PCT:
        return False
    return wow["ctr_change"] <= -DROP_PCT


def _spec(L) -> dict:
    try:
        return json.loads(L["spec_json"]) or {}
    except (TypeError, ValueError):
        return {}


def _tz(conn, L):
    c = admanage.connection_of(conn, L)
    return c["timezone"] if c is not None else None


def ads_of(conn, L, s=None) -> list:
    """[{"n", "id", "name", "angle", "kind", "wow", "tired"}] for a launch's ads still on Meta. s: its
    admanage.summary when the caller has it."""
    s = s or admanage.summary(conn, L)
    spec = _spec(L)
    creatives = spec.get("creatives") or []
    tz = _tz(conn, L)
    out = []
    for a in s["objects"]["ads"]:
        row, n = a["row"], a["n"]
        cr = creatives[n - 1] if 0 < n <= len(creatives) else {}
        wow = adinsights.week_over_week(conn, row["external_id"], tz)
        out.append({"n": n, "id": row["external_id"], "name": admanage.ad_name(row, n, s["name"]),
                    "angle": cr.get("name") or admanage.ad_name(row, n, s["name"]), "kind": cr.get("kind") or "image",
                    "headline": cr.get("headline") or "", "primary_text": cr.get("primary_text") or "",
                    "wow": wow, "tired": judge(wow), "status": row["status"]})
    return out


def _winner(ads: list):
    best = None
    for a in ads:
        t = a["wow"]["this"]
        if a["tired"] or (t["imp"] or 0) < WIN_MIN_IMPRESSIONS or t["ctr"] is None:
            continue
        if best is None or t["ctr"] > best["wow"]["this"]["ctr"]:
            best = a
    return best


def for_launch(conn, L, fallback=None, s=None) -> dict:
    """{"ads": [...], "tired": [...], "winner": ad or None} for one launch. fallback: a workspace-wide winner."""
    ads = ads_of(conn, L, s)
    win = _winner(ads) or fallback
    return {"ads": ads, "tired": [a for a in ads if a["tired"]], "winner": win}


def workspace(conn, ws_id: int) -> list:
    """[(launch row, for_launch result)] for every Meta launch of the workspace that's on Meta, newest first."""
    Ls = conn.execute("SELECT * FROM launches WHERE workspace_id=? AND platform='meta' AND status='paused' AND "
                      "external_campaign_id IS NOT NULL ORDER BY id DESC", (int(ws_id),)).fetchall()
    res = [(L, for_launch(conn, L)) for L in Ls]
    best = _winner([a for _, r in res for a in r["ads"]])
    for _, r in res:
        if r["winner"] is None:
            r["winner"] = best
    return res


def words(a: dict) -> str:
    """Why an ad is flagged, in plain words."""
    w = a["wow"]
    return (f"Its click rate fell {abs(w['ctr_change']):.0f}% this week ({w['last']['ctr']:.2f}% to {w['this']['ctr']:.2f}%) "
            f"while the spend stayed about the same. People have likely seen it too often.")


def refresh_prefill(conn, ws_id: int, ref: str) -> dict | None:
    """The Create page's start for "Refresh this ad" (?refresh=<launch id>:<ad id>): the worn ad's format and plan
    product, with the winning angle and its headline as the hook. None for anything not this workspace's."""
    try:
        lid_s, ad_id = (ref or "").split(":", 1)
        lid = int(lid_s)
    except ValueError:
        return None
    L = conn.execute("SELECT * FROM launches WHERE id=? AND workspace_id=? AND platform='meta'", (lid, int(ws_id))).fetchone()
    if L is None:
        return None
    r = for_launch(conn, L)
    worn = next((a for a in r["ads"] if a["id"] == ad_id), None)
    if worn is None:
        return None
    if r["winner"] is None:
        r["winner"] = _winner([a for _, x in workspace(conn, ws_id) for a in x["ads"]])
    win = r["winner"] if r["winner"] is not None and r["winner"]["id"] != worn["id"] else None
    plan = conn.execute("SELECT * FROM campaigns WHERE id=? AND workspace_id=?", (L["plan_id"] or 0, int(ws_id))).fetchone()
    pj = {}
    if plan is not None:
        try:
            pj = json.loads(plan["plan_json"] or "{}") or {}
        except ValueError:
            pj = {}
    angle_name = (win or worn)["angle"]
    ang = next((x for x in pj.get("angles") or [] if x.get("name") == angle_name), {}) or {}
    # the worn ad's format: the plan's ad of that angle tells us the tool (image, video, presenter)
    typ = "image" if worn["kind"] == "image" else "ugc"
    if plan is not None:
        import catalog
        ca = conn.execute("SELECT tool, params_json FROM campaign_ads WHERE campaign_id=? AND workspace_id=? AND angle=? "
                          "ORDER BY id LIMIT 1", (plan["id"], int(ws_id), worn["angle"])).fetchone()
        if ca is not None:
            try:
                params = json.loads(ca["params_json"] or "{}") or {}
            except ValueError:
                params = {}
            typ = params.get("ad_type") or catalog.TOOL_AD_TYPE.get(ca["tool"], typ)
    hook = (win or {}).get("headline") or ang.get("hook") or ""
    out = {"type": typ, "product": plan["product_id"] if plan is not None else None, "step": "angle",
           "platforms": ["meta"],
           "angle": {"title": angle_name, "hook": hook, "insight": ang.get("insight") or "",
                     "audience": ang.get("audience") or ""}}
    note = {"worn": worn["name"], "worn_n": worn["n"], "drop": abs(worn["wow"]["ctr_change"] or 0),
            "win": win["name"] if win else None, "win_n": win["n"] if win else None,
            "win_ctr": win["wow"]["this"]["ctr"] if win else None, "launch_id": L["id"], "tired": worn["tired"]}
    return {"prefill": out, "note": note}
