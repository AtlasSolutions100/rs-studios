"""The first ads are on us (Koby, Nov 11 2026): a new workspace's first 2 image ads and its first short talking
video cost it nothing. Offered in the guided first run (onboarding.first) while plans only is on (plan_gate.on())
and the new look is showing (ui_v2.on(), so today an admin's preview).

    left(conn, ws_id)                 how many of each are still on us: {"image": 0..2, "talk": 0..1}
    open_for(conn, ws, user)          is anything still on us for this workspace, right now?
    due(conn, user, ws)               should a new account be walked to the step by itself?
    draft(conn, product)              the two looks, the script and the presenters the step opens with (no AI, free)
    make(conn, ws, user, product, f)  start what was ticked, on us; a conversation on the desk holds it

How "on us" works. Nothing about pricing, queueing or refunds changes. The ads are made through Studio exactly as a
paid set is (studio_v2.generate_parts: the same quote, the same claims check, the same email rule for video), and
the credits they cost are put into the workspace first, as a ledger row of their own ("first_ads"). So the ledger
reads: plus 424 "first_ads", minus 424 for the ads, and the plan's credits are where they were. An ad that fails is
refunded like any other, to the member's balance, so they can make it again.

Once only. Each gifted ad takes a slot in `first_gifts` (workspace, kind, slot: unique). The slots are claimed
before anything is granted, so two requests at the same moment cannot both be given the same ad; a slot whose ad did
not start is given back, and credits granted for it are taken back. One person gets it once: a slot used by the
same owner email in another workspace closes it here too.

Who. A workspace with a plan (a trial counts: plan_gate.exempt), or a site admin looking at the step. Never a
workspace that still has to pick a plan: the gate (plan_gate_routes) sends it to the plan step first.

First means first. The image ads are on us only while the workspace has made no image ad outside the gift, and the
talking video only while it has made no talking video outside it. So a member who skips the step and makes ads in
Studio at the shown price has used "first" for that kind; one who made image ads but never a talking video still
has the video on us. A site admin is let past this rule (still once per slot), so the step can be looked at on an
account that has made ads before.
"""
import json
import logging

from flask import g, url_for

import catalog
import db
import plan_gate
import plans
import ui_v2

log = logging.getLogger("campaignify.first_ads")

IMAGES, TALKS = 2, 1
ALLOW = {"image": IMAGES, "talk": TALKS}
TALK_SECONDS = 8                # the shortest talking video (catalog.TOOLS["ugc_video"]["durations"][0])
REASON = "first_ads"            # the ledger rows: the grant, and anything taken back
TITLE = "Your first campaign"
CAST_SHOWN = 6
KIND_OF_TOOL = {"ad_image": "image", "ugc_video": "talk"}
TOOL_OF_KIND = {v: k for k, v in KIND_OF_TOOL.items()}


def on() -> bool:
    return plan_gate.on() and ui_v2.on()


def words_max() -> int:
    return catalog.max_words(TALK_SECONDS)


def _owner_email(conn, ws, user=None):
    """The email the gift is counted against: the workspace owner's (as plan_gate.start_trial reads it)."""
    row = conn.execute("SELECT * FROM users WHERE workspace_id=? ORDER BY (role='owner') DESC, id LIMIT 1",
                       (ws["id"],)).fetchone()
    return plan_gate._email_norm(row if row is not None else user)


def _admin(user) -> bool:
    return bool(user is not None and plan_gate._key(user, "is_admin"))


def left(conn, ws_id, user=None) -> dict:
    """How many of each are still on us. A kind the workspace has already made outside the gift is closed (it is
    no longer a first), except for a site admin looking at the step."""
    out = dict(ALLOW)
    for r in conn.execute("SELECT kind, COUNT(*) AS n FROM first_gifts WHERE workspace_id=? GROUP BY kind", (ws_id,)):
        if r["kind"] in out:
            out[r["kind"]] = max(0, out[r["kind"]] - r["n"])
    if not _admin(user):
        for kind, tool in TOOL_OF_KIND.items():
            if out[kind] and conn.execute("SELECT 1 FROM jobs j WHERE j.workspace_id=? AND j.tool=? AND NOT EXISTS "
                                          "(SELECT 1 FROM first_gifts f WHERE f.job_id = j.id) LIMIT 1",
                                          (ws_id, tool)).fetchone():
                out[kind] = 0
    return out


def used_elsewhere(conn, ws, user=None) -> bool:
    email = _owner_email(conn, ws, user)
    return bool(email and conn.execute("SELECT 1 FROM first_gifts WHERE email_norm=? AND workspace_id<>? LIMIT 1",
                                       (email, ws["id"])).fetchone())


def open_for(conn, ws, user=None) -> bool:
    """Something is still on us for this workspace, and it may take it now."""
    if ws is None or not on() or plan_gate.needs_plan(conn, ws, user):
        return False
    return any(left(conn, ws["id"], user).values()) and not used_elsewhere(conn, ws, user)


def started(conn, ws_id) -> bool:
    return conn.execute("SELECT 1 FROM first_gifts WHERE workspace_id=? LIMIT 1", (ws_id,)).fetchone() is not None


def due(conn, user, ws) -> bool:
    """A new account is taken to the step by itself: on, open, nothing made yet, and not skipped."""
    import onboarding
    if user is None or ws is None or not open_for(conn, ws, user):
        return False
    if onboarding.answers(user).get("first") == "skip" or started(conn, ws["id"]):
        return False
    return conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? LIMIT 1", (ws["id"],)).fetchone() is None


def thread_of(conn, ws_id):
    """The conversation the first ads live in, once they have been started."""
    row = conn.execute("SELECT t.id FROM desk_threads t JOIN desk_turns x ON x.thread_id = t.id WHERE t.workspace_id=? "
                       "AND t.archived=0 AND x.kind='made' AND x.body_json LIKE '%\"first\": true%' ORDER BY t.id LIMIT 1",
                       (ws_id,)).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------- the drafts

def draft(conn, product) -> dict:
    """What the step opens with, from what the brand step saved: two different looks with their own headline, a
    short script from the product's description, and who can say it. Nothing here is written by AI or costs."""
    import studio_v2
    if product is None:
        return {"looks": [], "script": "", "cast": [], "words": words_max()}
    looks = []
    for t in studio_v2.scene_templates(conn, product["id"], n=IMAGES):
        ex = studio_v2.example_of(t["id"], "")
        looks.append(dict(t, preview=ex.get("preview") or ""))
    cast = [{"id": c["id"], "name": c["name"], "portrait": c["portrait"], "look": c["look"] or c["vibe"]}
            for c in studio_v2.roster()[:CAST_SHOWN]]
    return {"looks": looks, "script": studio_v2.starter_script(product, words_max()), "cast": cast,
            "words": words_max()}


def _parts(conn, product, form, room) -> tuple[list, str]:
    """The Studio parts for what was ticked, within what is still on us. ([(kind, part)], a problem or "")."""
    import studio_v2
    out = []
    looks = studio_v2.scene_templates(conn, product["id"], n=IMAGES)
    for i, t in enumerate(looks[:room.get("image", 0)]):
        if not form.get(f"image{i}"):
            continue
        text = studio_v2._clean(form.get(f"headline{i}"), 120) or t["headline"]
        out.append(("image", studio_v2._scene_part({"product": product["id"]}, t, text=text)))
    if room.get("talk") and form.get("talk"):
        script = studio_v2._clean(form.get("script"), 900)
        if not script:
            return [], "Write what the presenter says, or untick the video."
        if len(script.split()) > words_max():
            return [], (f"The video is {TALK_SECONDS} seconds, which fits about {words_max()} words. "
                        f"Yours has {len(script.split())}. Shorten it, or untick the video.")
        cast = studio_v2.roster()
        who = next((c for c in cast if c["id"] == form.get("presenter")), cast[0] if cast else None)
        if who is None:
            return [], "No presenter is available right now. Untick the video and make it later from Studio."
        out.append(("talk", {"name": f"Presenter video with {who['name']}", "kind": "video",
                             "payload": {"mode": "presenter", "product": product["id"], "text": script,
                                         "presenters": [who["id"]], "duration": TALK_SECONDS}}))
    if not out:
        return [], "Tick at least one ad to make."
    return out, ""


def price(conn, product) -> dict:
    """What the first ads would cost, for the "on us" line: {"image": each, "talk": credits, "total"} (zeros when it
    can not be priced right now, and the page then leaves the numbers out)."""
    import studio_v2
    d = draft(conn, product)
    out = {"image": 0, "talk": 0, "total": 0}
    try:
        if d["looks"]:
            q = studio_v2.quote_parts([studio_v2._scene_part({"product": product["id"]}, d["looks"][0])])
            out["image"] = 0 if q.get("error") else int(q["total"])
        if d["cast"]:
            q = studio_v2.quote_parts([{"name": "Presenter video", "kind": "video",
                                        "payload": {"mode": "presenter", "product": product["id"],
                                                    "text": d["script"] or "A short line about the product.",
                                                    "presenters": [d["cast"][0]["id"]], "duration": TALK_SECONDS}}])
            out["talk"] = 0 if q.get("error") else int(q["total"])
    except Exception as e:  # noqa: BLE001  (a price is a nicety here; the step still opens)
        log.warning("first ads could not be priced for the page: %s", str(e)[:160])
    out["total"] = out["image"] * len(d["looks"]) + out["talk"]
    return out


# ---------------------------------------------------------------- making them

def _json(out):
    resp, status = out if isinstance(out, tuple) else (out, getattr(out, "status_code", 200))
    return (resp.get_json(silent=True) or {}), status


def _claims(parts) -> str:
    """The same free claims check Studio runs before a set is made. "" when it is clean (or off)."""
    import claims_check
    import create_routes
    import llm
    import studio_v2
    if not (claims_check.on() and llm.anthropic_ready()):
        return ""
    for _kind, part in parts:
        data, status = _json(create_routes.claims_for(studio_v2.config_for(part["payload"])))
        if status != 200:
            return data.get("error") or "The words could not be checked just now. Try again in a minute."
        if data.get("status") == "flagged":
            first = (data.get("flags") or [{}])[0]
            said = first.get("sentence") or ""
            return ("Something here says more than your page backs up" + (f': "{said}"' if said else "")
                    + ". Edit it, then make them.")
    return ""


def _claim(conn, ws, kinds, email) -> list:
    """Take a slot for each ad about to be made. Returns the row ids, or [] when any of them was already taken (then
    none is kept)."""
    ids, now = [], db.now()
    nth = {}
    for kind in kinds:
        got = None
        for slot in range(1, ALLOW[kind] + 1):
            if slot <= nth.get(kind, 0):
                continue
            cur = conn.execute("INSERT OR IGNORE INTO first_gifts (workspace_id, kind, slot, email_norm, created_at) "
                               "VALUES (?,?,?,?,?)", (ws["id"], kind, slot, email, now))
            if cur.rowcount:
                got, nth[kind] = cur.lastrowid, slot
                break
        if got is None:
            if ids:
                conn.execute(f"DELETE FROM first_gifts WHERE id IN ({','.join('?' * len(ids))}) AND job_id IS NULL", ids)
            conn.commit()
            return []
        ids.append(got)
    conn.commit()
    return ids


def _bucket(ws) -> str:
    return "plan" if plans.plan_code(ws) != "free" else "pack"


def make(conn, ws, user, product, form) -> dict:
    """Start the ticked first ads, on us. {"ok", "url"} or {"error"}; nothing is granted or kept unless ads started."""
    import desk
    import studio_v2
    if not open_for(conn, ws, user):
        return {"error": "Your first ads have already been made. Anything more is made from your desk."}
    parts, why = _parts(conn, product, form, left(conn, ws["id"], user))
    if why:
        return {"error": why}
    only = [p for _k, p in parts]
    q = studio_v2.quote_parts(only)
    if q.get("error"):
        return {"error": q["error"]}
    why = _claims(parts)
    if why:
        return {"error": why}
    total = int(q["total"])
    rows = _claim(conn, ws, [k for k, _p in parts], _owner_email(conn, ws, user))
    if not rows:
        return {"error": "Your first ads have already been made. Anything more is made from your desk."}
    bucket = _bucket(ws)
    db.adjust_credits(conn, ws["id"], total, REASON, None, bucket=bucket)
    conn.commit()
    data, status, charged, jobs = {}, 500, 0, []
    try:
        data, status = _json(studio_v2.generate_parts(only, {"total": total, "confirm_many": True}, TITLE,
                                                      {"first": True, "templates": [p.get("template") or
                                                                                    p["payload"]["mode"] for p in only]}))
        if status == 200 and data.get("ok") and data.get("batch"):
            jobs = conn.execute("SELECT id, tool, credits FROM jobs WHERE batch_id=? AND workspace_id=? ORDER BY id",
                                (data["batch"], ws["id"])).fetchall()
    finally:
        # match each started ad to a claimed slot of its kind; a slot whose ad did not start is given back, and
        # what was granted for it is taken back (never below what the ads that did start cost)
        free = {}
        for rid in rows:
            r = conn.execute("SELECT id, kind FROM first_gifts WHERE id=?", (rid,)).fetchone()
            if r is not None:
                free.setdefault(r["kind"], []).append(r["id"])
        for j in jobs:
            kind = KIND_OF_TOOL.get(j["tool"])
            if kind and free.get(kind):
                conn.execute("UPDATE first_gifts SET job_id=?, credits=? WHERE id=?", (j["id"], j["credits"], free[kind].pop(0)))
                charged += j["credits"]
        spare = [rid for ids in free.values() for rid in ids]
        if spare:
            conn.execute(f"DELETE FROM first_gifts WHERE id IN ({','.join('?' * len(spare))}) AND job_id IS NULL", spare)
        back = total - charged
        if back > 0 and not db.adjust_credits(conn, ws["id"], -back, REASON, None, hold_ok=True):
            log.error("first ads: could not take back %s unused credits from workspace %s", back, ws["id"])
        conn.commit()
    if not jobs:
        return {"error": data.get("error") or "The ads could not be started just now. Nothing was used. Try again.",
                "verify": bool(data.get("verify"))}
    images = sum(1 for j in jobs if KIND_OF_TOOL.get(j["tool"]) == "image")
    talks = sum(1 for j in jobs if KIND_OF_TOOL.get(j["tool"]) == "talk")
    names = ([desk._plural(images, "image ad")] if images else []) + (["1 talking video"] if talks else [])
    made = [{"kind": "ads", "name": " and ".join(names), "state": "Being made",
             "href": url_for("studio_v2.page", made=data["batch"])}]
    notes = ["On us: these did not use your plan's credits."]
    if data.get("note"):
        notes.append(data["note"])
    tid = desk._new_thread(conn, product, TITLE)
    desk._turn(conn, tid, "member", "said", {"text": "Make my first ads."})
    turn = desk._turn(conn, tid, "desk", "made", {"made": made, "notes": notes, "batch": data["batch"], "credits": 0,
                                                 "first": True})
    conn.execute("INSERT INTO desk_runs (workspace_id, user_id, product_id, ask, made_json, credits, created_at, thread_id) "
                 "VALUES (?,?,?,?,?,?,?,?)", (ws["id"], g.user["id"], product["id"], TITLE, json.dumps(made), 0, db.now(), tid))
    conn.commit()
    return {"ok": True, "thread": tid, "batch": data["batch"], "images": images, "talks": talks,
            "url": url_for("desk.thread", tid=tid) + f"#t{turn}"}
