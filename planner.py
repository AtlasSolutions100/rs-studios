"""The AI campaign planner: a tool-using agent over Campaignify's own tools.

From a brief (offer, audience, goal, budget, platforms) it reads the brand kit
and the template catalog, proposes angles, writes hooks and scripts per angle,
picks formats and templates, proposes ad variants with a credit quote each,
writes platform copy (copywriter.py), and finishes with a budget split and a
launch checklist.

Nothing it proposes spends credits: `propose_ad` only records a proposal. The
member reviews the plan and confirms the ads they want; only then are jobs
queued (campaign_routes.generate). The planning run itself costs what the AI
calls actually cost, capped at `cap_credits()`, charged once when it ends.

`run(cid)` does one planning run; `start(cid)` runs it in a background thread
(inline when RS_WORKER=0, which the tests use).
"""
import json
import os
import threading
import traceback
from datetime import date

import catalog
import compliance
import copywriter
import db
import llm
import platforms
import plans
import products
import seo

MAX_TURNS = 14
TURN_TOKENS = 3000
MAX_ANGLES, MAX_ADS, MAX_COPY = 5, 9, 6
STALE_MINUTES = 12


def cap_credits() -> int:
    """Most a planning run can cost (it stops early when it gets there)."""
    env = os.environ.get("RS_PLANNER_CAP", "").strip()
    return int(env) if env.isdigit() else 150


SYSTEM = """You are Campaignify's campaign strategist. You plan paid and organic social campaigns for small brands and
independent sellers, then line up the ads using Campaignify's own tools. You work through tools only; the member sees
each step as you go and reviews everything before any credits are spent on ads.

How to work (be efficient: about 6-10 tool calls in total, several tools per turn is fine):
1. get_brand_and_catalog once.
2. save_angles: 3 angles (at most 5). An angle is a distinct reason to care, aimed at a specific slice of the audience.
3. save_creative once per angle: 3 hooks for the first 3 seconds, a short 15-30 second script or image concept, and
   on-screen text. Pick the format that fits the angle and the platform.
4. propose_ad: 1-2 ads per angle (at most 9 in total) using Campaignify's tools and templates. Prefer image ads for
   testing many angles cheaply; use video where motion sells the product. Write each brief as a concrete scene that
   keeps the product's label and colors exactly as in the photo. For a talking video (ugc_video), the brief is the
   exact words the presenter says (hook first, then the body, then a call to action), short enough for its length;
   the presenter is an AI and never speaks as a customer reporting results.
5. write_copy for each selected platform (ad copy; add hooks or captions only when they add something).
6. finish: a summary, how to split the budget across platforms and angles for a first test, the KPIs to watch, a
   simple test plan, and a launch checklist (account, pixel/conversion tracking, audiences, creatives, copy, budget,
   disclosure, review). Be concrete and honest; never promise results.

""" + copywriter.RULES + """

Also: never invent statistics, reviews or results. If the budget is small, say so and focus it on one or two
platforms. Keep text tight; the member reads it on a phone."""

TOOLS = [
    {"name": "get_brand_and_catalog",
     "description": "The brand kit (product, approved claims, audience), product photos, and Campaignify's ad tools and "
                    "templates with their credit prices.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "save_angles",
     "description": "Record the campaign's angles.",
     "input_schema": {"type": "object", "properties": {"angles": {"type": "array", "items": {
         "type": "object", "properties": {
             "name": {"type": "string", "description": "Short name, 2-5 words"},
             "insight": {"type": "string", "description": "The reason to care, one sentence"},
             "audience": {"type": "string", "description": "Who exactly this angle is for"},
             "why": {"type": "string", "description": "Why it should work, one sentence"}},
         "required": ["name", "insight", "audience"]}}}, "required": ["angles"]}},
    {"name": "save_creative",
     "description": "Record the hooks, script and on-screen text for one angle.",
     "input_schema": {"type": "object", "properties": {
         "angle": {"type": "string"},
         "format": {"type": "string", "description": "e.g. 'UGC talking video', 'product image ad', 'how-to clip'"},
         "hooks": {"type": "array", "items": {"type": "string"}},
         "script": {"type": "string", "description": "Beat-by-beat script or image concept"},
         "on_screen_text": {"type": "array", "items": {"type": "string"}}},
         "required": ["angle", "hooks", "script"]}},
    {"name": "propose_ad",
     "description": "Propose one ad to generate with a Campaignify tool. This does not spend credits; it returns the "
                    "price and the member decides later.",
     "input_schema": {"type": "object", "properties": {
         "angle": {"type": "string"},
         "platform": {"type": "string", "enum": platforms.SOCIAL + ["google"]},
         "tool": {"type": "string", "enum": list(catalog.TOOLS)},
         "template_id": {"type": "string", "description": "Optional template id from the catalog"},
         "brief": {"type": "string", "description": "What the ad shows, as a concrete scene"},
         "aspect": {"type": "string", "enum": catalog.TOOLS["ad_image"]["aspects"]},
         "duration": {"type": "integer", "enum": sorted(set(catalog.TOOLS["photo_to_video"]["durations"]
                                                            + catalog.TOOLS["ugc_video"]["durations"]))},
         "presenter": {"type": "string", "enum": [p["id"] for p in catalog.PRESENTERS],
                       "description": "Talking videos only"},
         "versions": {"type": "integer", "description": "Image ads only: 1-4 versions"}},
         "required": ["angle", "platform", "tool", "brief"]}},
    {"name": "write_copy",
     "description": "Write platform copy for the campaign (runs Campaignify's copy studio).",
     "input_schema": {"type": "object", "properties": {
         "platform": {"type": "string", "enum": platforms.ORDER},
         "kind": {"type": "string", "enum": list(platforms.KINDS)},
         "angle": {"type": "string", "description": "Which angle to write for, or 'all'"},
         "variants": {"type": "integer", "description": "1-3"}},
         "required": ["platform", "kind"]}},
    {"name": "check_compliance",
     "description": "Check a piece of text against the compliance rules.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "finish",
     "description": "Finish the plan.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "3-5 sentences: the strategy in plain words"},
         "budget_split": {"type": "array", "items": {"type": "object", "properties": {
             "platform": {"type": "string"}, "share_percent": {"type": "integer"}, "note": {"type": "string"}},
             "required": ["platform", "share_percent"]}},
         "kpis": {"type": "array", "items": {"type": "string"}},
         "test_plan": {"type": "string"},
         "checklist": {"type": "array", "items": {"type": "object", "properties": {
             "title": {"type": "string"}, "detail": {"type": "string"},
             "platform": {"type": "string", "description": "Platform key, or 'all'"}},
             "required": ["title"]}}},
         "required": ["summary", "checklist"]}},
]


# Added for workspaces whose plan includes the SEO optimizer.
SEO_TOOL = {"name": "audit_page",
            "description": "Audit the campaign's landing page for search, answer engines and AI assistants (free). "
                           "Use it once when the brief has a link, and turn the top issues into checklist items.",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}


class Plan:
    """What the agent has produced so far (saved to campaigns.plan_json)."""

    def __init__(self, data=None):
        data = data or {}
        self.angles = data.get("angles") or []
        self.creative = data.get("creative") or []
        self.copy = data.get("copy") or []
        self.final = data.get("final") or None

    def to_json(self):
        return {"angles": self.angles, "creative": self.creative, "copy": self.copy, "final": self.final}


def _step(steps, kind, title, detail=""):
    steps.append({"at": db.now(), "kind": kind, "title": title[:160], "detail": (detail or "")[:600]})


def brief_prompt(c, brand, brief, notes_from_before=None):
    plats = ", ".join(platforms.PLATFORMS[p]["name"] for p in brief.get("platforms") or [] if p in platforms.PLATFORMS)
    lines = [f"Today is {date.today().isoformat()}.",
             "Plan a campaign for this brief:",
             copywriter.brief_text(brand, brief),
             f"Platforms: {plats or 'pick the best one or two'}",
             f"Budget: {brief.get('budget') or 'not given (assume a small first test)'}"]
    if brief.get("dates"):
        lines.append(f"Timing: {brief['dates']}")
    if notes_from_before:
        lines.append("This is a revision. The member's notes on the previous plan: " + notes_from_before)
    return "\n".join(lines)


def _photos(ws_id, product_id=None):
    """The campaign product's photos (primary first), else the workspace's."""
    with db.connect() as conn:
        rows = products.photos_for(conn, ws_id, product_id) if product_id else []
        if not rows:
            rows = conn.execute("SELECT id, filename FROM photos WHERE workspace_id=? ORDER BY id DESC LIMIT 12",
                                (ws_id,)).fetchall()
        return [{"id": r["id"], "filename": r["filename"]} for r in rows[:12]]


def _refs():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, title, seconds FROM refs WHERE active=1 ORDER BY id DESC LIMIT 6")]


def catalog_view(brand, photos, refs):
    tools = {k: {"name": t["name"], "what": t["blurb"], "kind": t["kind"]} for k, t in catalog.TOOLS.items()}
    tools["ad_image"]["credits"] = {f"{n} version{'s' if n > 1 else ''}": catalog.price_credits("ad_image", batch=n)
                                    for n in (1, 2, 4)}
    tools["ad_image"]["aspects"] = catalog.TOOLS["ad_image"]["aspects"]
    tools["photo_to_video"]["credits"] = {f"{d}s": catalog.price_credits("photo_to_video", duration=d) for d in (5, 10)}
    if catalog.capabilities()["talking"]:
        tools["ugc_video"]["credits"] = {f"{d}s": catalog.price_credits("ugc_video", duration=d)
                                         for d in catalog.TOOLS["ugc_video"]["durations"]}
        tools["ugc_video"]["max_spoken_words"] = {f"{d}s": catalog.max_words(d) for d in catalog.TOOLS["ugc_video"]["durations"]}
        tools["ugc_video"]["presenters"] = [{"id": p["id"], "vibe": p["vibe"], "look": p["look"]} for p in catalog.PRESENTERS]
    else:
        tools.pop("ugc_video", None)
    tools["motion_copy"]["credits"] = {r["title"]: catalog.price_credits("motion_copy", ref_seconds=r["seconds"] or 15)
                                       for r in refs} or "no reference videos available; don't use this tool"
    tpls = [{"id": t["id"], "name": t["name"], "what": t["line"], "tool": t["tool"],
             "fits": "any product" if t.get("fits") == "any" else f"{t.get('fits')} products only"}
            for t in catalog.TEMPLATES]
    return {"brand": brand, "product_photos": len(photos),
            "photo_note": "Photo-to-video and motion copy need a product photo." if not photos else
            "The member's product photos are attached to each ad automatically.",
            "tools": tools, "templates": tpls}


def propose(conn, cid, ws_id, inp, photos, refs):
    """Validate one proposed ad and store it. Returns what the agent sees."""
    tool = inp.get("tool")
    if tool not in catalog.TOOLS:
        return {"error": "unknown tool"}
    if tool == "motion_copy" and not refs:
        return {"error": "no reference videos are available; use another tool"}
    if tool == "ugc_video" and not catalog.capabilities()["talking"]:
        return {"error": "talking videos aren't available; use another tool"}
    brief = str(inp.get("brief") or "").strip()[:2000]
    chk = compliance.check(brief)
    if not chk["ok"]:
        return {"error": "brief breaks the rules: " + " ".join(chk["blocks"]) + " Rewrite it."}
    tpl = catalog.TEMPLATE_BY_ID.get(inp.get("template_id") or "")
    if tpl and tpl["tool"] != tool:
        tpl = None
    params = {"photo_ids": [p["id"] for p in photos][: catalog.TOOLS[tool]["max_photos"]]}
    if tool == "ad_image":
        batch = max(1, min(4, int(inp.get("versions") or 1)))
        aspect = inp.get("aspect") if inp.get("aspect") in catalog.TOOLS["ad_image"]["aspects"] else \
            (tpl or {}).get("aspect") or "9:16"
        params.update(batch=batch, aspect=aspect, audience="", niche="")
        credits, usd = catalog.price_credits(tool, batch=batch), catalog.cost_usd(tool, batch=batch)
    elif tool == "ugc_video":
        durs = catalog.TOOLS["ugc_video"]["durations"]
        dur = int(inp.get("duration") or 10) if int(inp.get("duration") or 10) in durs else 10
        if len(brief.split()) > catalog.max_words(dur) * 1.2:
            return {"error": f"that script is {len(brief.split())} words; a {dur}s video fits about "
                             f"{catalog.max_words(dur)}. Shorten it or use a longer duration."}
        if "AI presenters can't speak as customers or claim results." in chk["warnings"]:
            return {"error": "the presenter can't speak as a customer or claim results. Rewrite it."}
        pr = inp.get("presenter") if inp.get("presenter") in catalog.PRESENTER_BY_ID else catalog.PRESENTERS[0]["id"]
        aspect = inp.get("aspect") if inp.get("aspect") in catalog.TOOLS["ugc_video"]["aspects"] else "9:16"
        params.update(duration=dur, aspect=aspect, ad_type="ugc", presenter=pr,
                      voice={"maya": "warm_f", "priya": "warm_f", "elena": "warm_f", "sam": "upbeat_f",
                             "lena": "upbeat_f", "marcus": "calm_m"}.get(pr, "chatty_m"))
        credits, usd = catalog.price_credits(tool, duration=dur), catalog.cost_usd(tool, duration=dur)
    elif tool == "photo_to_video":
        dur = 10 if int(inp.get("duration") or 5) == 10 else 5
        params.update(duration=dur, audio=False)
        credits, usd = catalog.price_credits(tool, duration=dur), catalog.cost_usd(tool, duration=dur)
    else:
        ref = refs[0]
        params.update(ref_id=ref["id"])
        credits = catalog.price_credits(tool, ref_seconds=ref["seconds"] or 15)
        usd = catalog.cost_usd(tool, ref_seconds=ref["seconds"] or 15)
    needs_photo = tool != "ad_image" and not params["photo_ids"]
    platform = inp.get("platform") if inp.get("platform") in platforms.PLATFORMS else "meta"
    aid = conn.execute("INSERT INTO campaign_ads (campaign_id, workspace_id, angle, platform, tool, template_id, brief, "
                       "params_json, credits, cost_usd, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, ws_id, str(inp.get("angle") or "")[:120], platform, tool, tpl["id"] if tpl else None, brief,
                        json.dumps(params), credits, round(usd, 4), "proposed", db.now())).lastrowid
    conn.commit()
    out = {"ok": True, "ad_id": aid, "credits": credits}
    if needs_photo:
        out["note"] = "No product photo yet: the member must add one before this ad can be generated."
    if chk["warnings"]:
        out["warnings"] = chk["warnings"]
    return out


def dispatch(name, inp, ctx):
    """Run one tool call. ctx: cid, ws_id, brand, brief, plan, steps, usage, photos, refs, counts."""
    plan, steps = ctx["plan"], ctx["steps"]
    if name == "get_brand_and_catalog":
        _step(steps, "read", "Read your brand kit and the template catalog",
              f"{len(ctx['photos'])} product photo(s), {len(catalog.TEMPLATES)} templates")
        return catalog_view(ctx["brand"], ctx["photos"], ctx["refs"])
    if name == "save_angles":
        angles = [{k: str(a.get(k) or "")[:300] for k in ("name", "insight", "audience", "why")}
                  for a in (inp.get("angles") or []) if isinstance(a, dict)][:MAX_ANGLES]
        plan.angles = angles
        _step(steps, "angles", f"Proposed {len(angles)} angle{'s' if len(angles) != 1 else ''}",
              " · ".join(a["name"] for a in angles))
        return {"ok": True, "saved": len(angles)}
    if name == "save_creative":
        cr = {"angle": str(inp.get("angle") or "")[:120], "format": str(inp.get("format") or "")[:80],
              "hooks": [str(h)[:200] for h in (inp.get("hooks") or [])][:5],
              "script": str(inp.get("script") or "")[:2500],
              "on_screen_text": [str(t)[:120] for t in (inp.get("on_screen_text") or [])][:6]}
        chk = compliance.check(" ".join(cr["hooks"]), cr["script"], " ".join(cr["on_screen_text"]))
        if not chk["ok"]:
            return {"error": "this breaks the rules: " + " ".join(chk["blocks"]) + " Rewrite and save again."}
        plan.creative = [c for c in plan.creative if c["angle"] != cr["angle"]] + [cr]
        _step(steps, "creative", f"Wrote hooks and a script for “{cr['angle']}”", cr["hooks"][0] if cr["hooks"] else "")
        return {"ok": True}
    if name == "propose_ad":
        if ctx["counts"]["ads"] >= MAX_ADS:
            return {"error": f"at most {MAX_ADS} ads per plan"}
        with db.connect() as conn:
            out = propose(conn, ctx["cid"], ctx["ws_id"], inp, ctx["photos"], ctx["refs"])
        if out.get("ok"):
            ctx["counts"]["ads"] += 1
            t = catalog.TOOLS[inp["tool"]]["name"].lower()
            _step(steps, "ad", f"Lined up {'an' if t[0] in 'aeiou' else 'a'} {t} for “{str(inp.get('angle') or '')[:60]}”",
                  f"{platforms.PLATFORMS.get(inp.get('platform'), {}).get('short', '')} · {out['credits']} credits if you "
                  f"generate it")
        return out
    if name == "write_copy":
        if ctx["counts"]["copy"] >= MAX_COPY:
            return {"error": f"at most {MAX_COPY} copy runs per plan"}
        platform = inp.get("platform") if inp.get("platform") in platforms.PLATFORMS else "meta"
        kind = inp.get("kind") if inp.get("kind") in platforms.KINDS else "ad"
        n = max(1, min(3, int(inp.get("variants") or 3)))
        angle = str(inp.get("angle") or "all")[:120]
        brief = dict(ctx["brief"])
        if angle and angle != "all":
            a = next((x for x in plan.angles if x["name"] == angle), None)
            brief["notes"] = ((brief.get("notes") or "") + f"\nAngle: {angle}" +
                              (f" ({a['insight']}; for {a['audience']})" if a else "")).strip()
        try:
            variants = copywriter.write(ctx["brand"], kind, platform, brief, n, ctx["usage"])
        except llm.LLMError as e:
            return {"error": f"copy writer failed: {e}"}
        ctx["counts"]["copy"] += 1
        plan.copy.append({"platform": platform, "kind": kind, "angle": angle, "variants": variants})
        _step(steps, "copy", f"Wrote {platforms.KINDS[kind]['name'].lower()} for {platforms.PLATFORMS[platform]['name']}",
              f"{len(variants)} variant(s)" + (f" for “{angle}”" if angle != "all" else ""))
        return {"ok": True, "variants": [{"angle": v["angle"], "first_field": (v["fields"][0]["text"] if v["fields"] else "")[:140],
                                          "passes_checks": v["ok"]} for v in variants]}
    if name == "audit_page" and ctx.get("seo"):
        try:
            page, report, _ = seo.run_audit(str(inp.get("url") or "")[:500], None)
        except seo.fetcher.FetchError as e:
            return {"error": f"couldn't read the page: {e}"}
        top = [f"{lvl}: {t}" for grp in ("seo", "aeo", "geo") for lvl, t, _ in report[grp] if lvl in ("fail", "warn")]
        _step(steps, "seo", f"Audited the landing page: {report['score']}/100", "; ".join(top[:3]))
        return {"score": report["score"], "title": page["title"], "issues": top[:10]}
    if name == "check_compliance":
        return compliance.check(str(inp.get("text") or ""))
    if name == "finish":
        plan.final = {"summary": str(inp.get("summary") or "")[:2000],
                      "budget_split": [{"platform": str(b.get("platform") or "")[:40],
                                        "share_percent": max(0, min(100, int(b.get("share_percent") or 0))),
                                        "note": str(b.get("note") or "")[:300]}
                                       for b in (inp.get("budget_split") or []) if isinstance(b, dict)][:10],
                      "kpis": [str(k)[:160] for k in (inp.get("kpis") or [])][:8],
                      "test_plan": str(inp.get("test_plan") or "")[:1500],
                      "checklist": [{"title": str(i.get("title") or "")[:160], "detail": str(i.get("detail") or "")[:500],
                                     "platform": str(i.get("platform") or "all")[:20]}
                                    for i in (inp.get("checklist") or []) if isinstance(i, dict)][:24]}
        _step(steps, "done", "Finished the plan", f"{len(plan.final['checklist'])} launch steps")
        return {"ok": True}
    return {"error": f"unknown tool {name}"}


def _with_cache(messages):
    """Mark the end of the conversation so the next turn reads it from the prompt cache."""
    if not messages:
        return messages
    msgs = [dict(m) for m in messages]
    last = msgs[-1]
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    content = [dict(b) for b in content]
    content[-1]["cache_control"] = {"type": "ephemeral"}
    last["content"] = content
    return msgs


def _save(cid, plan, steps, status=None, **kw):
    sets = {"plan_json": json.dumps(plan.to_json()), "steps_json": json.dumps(steps), "updated_at": db.now()}
    if status:
        sets["status"] = status
    sets.update(kw)
    with db.connect() as conn:
        conn.execute(f"UPDATE campaigns SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cid))
        conn.commit()


def run(cid: int, notes: str | None = None):
    """One planning run for campaign `cid`. Charges the workspace once at the end."""
    with db.connect() as conn:
        c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        b = conn.execute("SELECT * FROM brands WHERE id=?", (c["brand_id"],)).fetchone() if c["brand_id"] else None
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (c["workspace_id"],)).fetchone()
        p = products.get_product(conn, c["workspace_id"], c["product_id"]) if c["product_id"] else None
    if p is not None:
        brand = products.as_brief(b, p, ws)
        brand["name"] = brand["name"] if b is not None else ""
    else:
        src = b or ws
        brand = {"name": src["brand_name"] or "", "product": src["product_name"] or "", "claims": src["claims"] or "",
                 "audience": src["audience"] or ""}
    brief = json.loads(c["brief_json"] or "{}")
    previous = Plan(json.loads(c["plan_json"] or "{}")) if notes else None
    plan, steps = Plan(), json.loads(c["steps_json"] or "[]") if notes else []
    if notes:
        _step(steps, "revise", "Revising the plan with your notes", notes[:300])
    usage = llm.Usage()
    has_seo = plans.has(ws, "seo")
    ctx = {"cid": cid, "ws_id": c["workspace_id"], "brand": brand, "brief": brief, "plan": plan, "steps": steps,
           "usage": usage, "photos": _photos(c["workspace_id"], c["product_id"]), "refs": _refs(), "counts": {"ads": 0, "copy": 0},
           "seo": has_seo}
    tools = TOOLS[:-1] + [SEO_TOOL, TOOLS[-1]] if has_seo else TOOLS
    prompt = brief_prompt(c, brand, brief, notes)
    if previous and previous.final:
        prompt += "\n\nThe previous plan's summary: " + previous.final.get("summary", "")
    messages = [{"role": "user", "content": prompt}]
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    cap = cap_credits()
    error = None
    try:
        for turn in range(MAX_TURNS):
            if usage.credits >= cap:
                _step(steps, "note", "Stopped at the planning budget", f"{cap} credits")
                break
            resp = llm.chat(system=system, messages=_with_cache(messages), tools=tools, max_tokens=TURN_TOKENS,
                            usage=usage, temperature=0.6)
            content = resp.get("content") or []
            messages.append({"role": "assistant", "content": content})
            for blk in content:
                if blk.get("type") == "text" and blk.get("text", "").strip():
                    _step(steps, "note", blk["text"].strip().split("\n")[0][:160], blk["text"].strip()[:600])
            uses = [blk for blk in content if blk.get("type") == "tool_use"]
            if not uses:
                break
            results = []
            for tu in uses:
                try:
                    out = dispatch(tu.get("name"), tu.get("input") or {}, ctx)
                except Exception as e:  # noqa: BLE001 — a bad tool call shouldn't end the run
                    out = {"error": f"{e.__class__.__name__}: {e}"[:300]}
                results.append({"type": "tool_result", "tool_use_id": tu.get("id"),
                                "content": json.dumps(out)[:12000], **({"is_error": True} if "error" in out else {})})
            messages.append({"role": "user", "content": results})
            _save(cid, plan, steps)
            if plan.final:
                break
    except llm.LLMError as e:
        error = str(e)
        _step(steps, "error", "The planner stopped early", error)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        error = f"{e.__class__.__name__}"
        _step(steps, "error", "The planner stopped early", error)
    credits = min(usage.credits, cap)
    taken = 0
    if credits:
        with db.connect() as conn:
            have = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (c["workspace_id"],)).fetchone())
            taken = min(credits, have)
            if taken:
                db.adjust_credits(conn, c["workspace_id"], -taken, "AI campaign planning")
            conn.commit()
    produced = bool(plan.angles or plan.final)
    status = "ready" if produced else "failed"
    if produced and not plan.final:
        _step(steps, "note", "The plan is partial", "Revise it to finish the rest.")
    with db.connect() as conn:
        prev = conn.execute("SELECT credits, cost_usd FROM campaigns WHERE id=?", (cid,)).fetchone()
    _save(cid, plan, steps, status=status, error=error if not produced else None,
          credits=(prev["credits"] or 0) + taken, cost_usd=round((prev["cost_usd"] or 0) + usage.usd, 5))
    return status


def start(cid: int, notes: str | None = None):
    if os.environ.get("RS_WORKER", "1") != "1":
        return run(cid, notes)
    threading.Thread(target=_safe_run, args=(cid, notes), name=f"planner-{cid}", daemon=True).start()
    return "planning"


def _safe_run(cid, notes):
    try:
        run(cid, notes)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        with db.connect() as conn:
            conn.execute("UPDATE campaigns SET status='failed', error='planner crashed', updated_at=? WHERE id=?",
                         (db.now(), cid))
            conn.commit()
