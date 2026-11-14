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

On the welcome credits (plan_gate.grant_welcome, Koby Nov 13 2026) the step is the same walk, but paid: the two image
ads are made with those credits at the shown price (paid()), and there is no talking video (videos come with a plan).
After them the desk opens with the upgrade box once (plan_gate_routes.upgrade_box).

First means first. The image ads are on us only while the workspace has made no image ad outside the gift, and the
talking video only while it has made no talking video outside it. So a member who skips the step and makes ads in
Studio at the shown price has used "first" for that kind; one who made image ads but never a talking video still
has the video on us. A site admin is let past this rule (still once per slot), so the step can be looked at on an
account that has made ads before.

The free first result, before the plan step (Koby, Nov 14 2026). Most sign-ups stopped at the plan step having seen
nothing of theirs. So a new workspace with no plan that gave us a product link (pasted before signing up, typed as its
website, or read on the brand brief: link_of) gets FREE_IMAGES image ads made from that page, free, before it is asked
to pick a plan (free_open). It is the same walk and the same make() as above: the same slots in first_gifts, the same
grant of exactly what the ads cost (3 image ads at today's price, 36 credits at 12 each) and the same take-back of
anything that did not start. What is new: the workspace is marked first (workspaces.first_free_at, and what was granted
in first_free_credits), so it can never be taken twice there; the plan gate is lifted for that one request only
(plan_gate.free_pass), so the granted credits can't be spent on anything else; and the member then sees the ads on
the first-result screen (onboarding.first_result), with the plan step after it. No talking video: videos come with a
plan. A sign-up who came from the presenter pages (came_for_presenters) also sees one presenter still with a line
from their product, labelled as a preview of the video (presenter_preview); nothing is generated for it.
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
FREE_IMAGES = 3                 # the free first result, before the plan step (free_open)
FREE = {"image": FREE_IMAGES, "talk": 0}
# Landing pages whose visitors came for presenter videos (users.first_touch "lp"): they also see a presenter preview
PRESENTER_PAGES = ("/ai-ugc-ad-generator", "/arcads-alternative")
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


def paid(conn, ws, user=None) -> bool:
    """The first ads are made with the welcome credits, not on us: the workspace is on them (no plan yet), and the
    free first result is not open for it."""
    return (ws is not None and plan_gate.on_welcome(conn, ws) and not _admin(user)
            and not free_open(conn, ws, user))


def link_of(conn, ws, user=None) -> str:
    """The product link the free first result is made from: the page the workspace's product was imported from,
    else (no product yet) the link the member gave us, not read yet (onboarding.first_link). "" for none: a product
    added by hand, or no link at all, keeps the walk as it was."""
    import onboarding
    import products
    if ws is None:
        return ""
    p = products.get_product(conn, ws["id"], ws["product_id"]) if plan_gate._key(ws, "product_id") else None
    if p is not None:
        info = products.import_info(p)
        return str((info.get("url") if isinstance(info, dict) else "") or (p["url"] if "url" in p.keys() else "") or "")
    return onboarding.first_link(user)


def free_open(conn, ws, user=None) -> bool:
    """The free first result is still open for this workspace: on, no plan, a product link, not taken here (the mark
    on the workspace, or any first ad already gifted) nor by the same person in another workspace, and no image ad
    made outside it (first means first; a site admin may look)."""
    if ws is None or plans.plan_code(ws) != "free" or not on():
        return False
    row = conn.execute("SELECT first_free_at FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
    if row is None or row["first_free_at"] or started(conn, ws["id"]):
        return False
    if not link_of(conn, ws, user) or used_elsewhere(conn, ws, user):
        return False
    return _admin(user) or conn.execute("SELECT 1 FROM jobs WHERE workspace_id=? AND tool='ad_image' LIMIT 1",
                                        (ws["id"],)).fetchone() is None


def left(conn, ws_id, user=None) -> dict:
    """How many of each are still on us. A kind the workspace has already made outside the gift is closed (it is
    no longer a first), except for a site admin looking at the step. On the welcome credits: no talking video."""
    out = dict(ALLOW)
    ws_row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if free_open(conn, ws_row, user):
        return dict(FREE)
    if paid(conn, ws_row, user):
        out["talk"] = 0
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
    if ws is None or not on():
        return False
    if free_open(conn, ws, user):
        return True
    if plan_gate.needs_plan(conn, ws, user):
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

def draft(conn, product, n=IMAGES) -> dict:
    """What the step opens with, from what the brand step saved: `n` different looks with their own headline, a
    short script from the product's description, and who can say it. Nothing here is written by AI or costs."""
    import studio_v2
    if product is None:
        return {"looks": [], "script": "", "cast": [], "words": words_max()}
    looks = []
    for t in studio_v2.scene_templates(conn, product["id"], n=n):
        ex = studio_v2.example_of(t["id"], "")
        looks.append(dict(t, preview=ex.get("preview") or ""))
    cast = [{"id": c["id"], "name": c["name"], "portrait": c["portrait"], "look": c["look"] or c["vibe"]}
            for c in studio_v2.roster()[:CAST_SHOWN]]
    return {"looks": looks, "script": studio_v2.starter_script(product, words_max()), "cast": cast,
            "words": words_max()}


def _parts(conn, product, form, room, every=False) -> tuple[list, str]:
    """The Studio parts for what was ticked, within what is still on us (`every`: all the looks, as the free first
    result makes them). ([(kind, part)], a problem or "")."""
    import studio_v2
    out = []
    looks = studio_v2.scene_templates(conn, product["id"], n=max(IMAGES, room.get("image", 0)))
    for i, t in enumerate(looks[:room.get("image", 0)]):
        if not (every or form.get(f"image{i}")):
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


def price(conn, product, n=IMAGES) -> dict:
    """What the first ads would cost, for the "on us" line: {"image": each, "talk": credits, "total"} (zeros when it
    can not be priced right now, and the page then leaves the numbers out)."""
    import studio_v2
    d = draft(conn, product, n)
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


def _claim(conn, ws, kinds, email, allow=None) -> list:
    """Take a slot for each ad about to be made (`allow`: how many slots each kind has, ALLOW unless said).
    Returns the row ids, or [] when any of them was already taken (then none is kept)."""
    allow = allow or ALLOW
    ids, now = [], db.now()
    nth = {}
    for kind in kinds:
        got = None
        for slot in range(1, allow.get(kind, 0) + 1):
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
    import contextlib

    import desk
    import studio_v2
    if not open_for(conn, ws, user):
        return {"error": "Your first ads have already been made. Anything more is made from your desk."}
    free_result = free_open(conn, ws, user)
    parts, why = _parts(conn, product, form, left(conn, ws["id"], user), every=free_result)
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
    if paid(conn, ws, user):
        return _make_paid(conn, ws, product, only, total)
    taken = "Your first ads have already been made. Anything more is made from your desk."
    if free_result:        # the mark on the workspace comes first: one request takes the free first result, never two
        if conn.execute("UPDATE workspaces SET first_free_at=? WHERE id=? AND first_free_at IS NULL",
                        (db.now(), ws["id"])).rowcount != 1:
            conn.commit()
            return {"error": taken}
        conn.commit()
    rows = _claim(conn, ws, [k for k, _p in parts], _owner_email(conn, ws, user), FREE if free_result else ALLOW)
    if not rows:
        if free_result:
            conn.execute("UPDATE workspaces SET first_free_at=NULL WHERE id=? AND first_free_credits IS NULL", (ws["id"],))
            conn.commit()
        return {"error": taken}
    bucket = _bucket(ws)
    db.adjust_credits(conn, ws["id"], total, REASON, None, bucket=bucket)
    conn.commit()
    if g.get("ws") is not None and g.ws["id"] == ws["id"]:     # Studio checks the balance on the request's row
        g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone()
    data, status, charged, jobs = {}, 500, 0, []
    try:
        # the free first result is made before a plan: the plan gate steps aside for this request's ads only
        with plan_gate.free_pass(ws["id"]) if free_result else contextlib.nullcontext():
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
        if free_result:    # kept with what it was granted, or given back when no ad started (so it can be tried again)
            if charged:
                conn.execute("UPDATE workspaces SET first_free_credits=? WHERE id=?", (charged, ws["id"]))
            else:
                conn.execute("UPDATE workspaces SET first_free_at=NULL WHERE id=?", (ws["id"],))
        conn.commit()
    if not jobs:
        return {"error": data.get("error") or "The ads could not be started just now. Nothing was used. Try again.",
                "verify": bool(data.get("verify"))}
    images = sum(1 for j in jobs if KIND_OF_TOOL.get(j["tool"]) == "image")
    talks = sum(1 for j in jobs if KIND_OF_TOOL.get(j["tool"]) == "talk")
    names = ([desk._plural(images, "image ad")] if images else []) + (["1 talking video"] if talks else [])
    made = [{"kind": "ads", "name": " and ".join(names), "state": "Being made",
             "href": url_for("studio_v2.page", made=data["batch"])}]
    notes = ["Free: these used none of your credits." if free_result else "On us: these did not use your plan's credits."]
    if data.get("note"):
        notes.append(data["note"])
    tid = desk._new_thread(conn, product, TITLE)
    desk._turn(conn, tid, "member", "said", {"text": "Make my first ads."})
    turn = desk._turn(conn, tid, "desk", "made", {"made": made, "notes": notes, "batch": data["batch"], "credits": 0,
                                                 "first": True})
    conn.execute("INSERT INTO desk_runs (workspace_id, user_id, product_id, ask, made_json, credits, created_at, thread_id) "
                 "VALUES (?,?,?,?,?,?,?,?)", (ws["id"], g.user["id"], product["id"], TITLE, json.dumps(made), 0, db.now(), tid))
    conn.commit()
    return {"ok": True, "thread": tid, "batch": data["batch"], "images": images, "talks": talks, "free": free_result,
            "url": url_for("onboarding.first_result") if free_result else url_for("desk.thread", tid=tid) + f"#t{turn}"}


def free_jobs(conn, ws_id) -> list:
    """The image ads of the free first result (or of the first ads on us), oldest first."""
    return conn.execute("SELECT j.* FROM first_gifts f JOIN jobs j ON j.id = f.job_id WHERE f.workspace_id=? "
                        "AND f.kind='image' AND j.workspace_id=? AND j.deleted_at IS NULL ORDER BY j.id",
                        (ws_id, ws_id)).fetchall()


def came_for_presenters(user) -> bool:
    """The account's first touch landed on a presenter page (PRESENTER_PAGES; users.first_touch, gads.py)."""
    raw = user["first_touch"] if user is not None and "first_touch" in user.keys() else None
    try:
        lp = str((json.loads(raw or "{}") or {}).get("lp") or "")
    except (TypeError, ValueError, AttributeError):
        return False
    return lp.split("?")[0].rstrip("/").lower() in PRESENTER_PAGES


def presenter_preview(product) -> dict | None:
    """One AI presenter's still and a line written from the product, for the first-result screen: a picture of what
    the video would be, not the video (nothing is generated). None without a product or a presenter with a still."""
    import studio_v2
    if product is None:
        return None
    who = next((c for c in studio_v2.roster() if c.get("portrait")), None)
    if who is None:
        return None
    line = studio_v2.starter_script(product, words_max())
    if not line:
        desc = " ".join(str(product["description"] or "").split()).split()
        line = " ".join(desc[:words_max()]) if desc else ""
    return {"id": who["id"], "name": who["name"], "portrait": who["portrait"], "line": line or product["name"],
            "seconds": TALK_SECONDS}


def _make_paid(conn, ws, product, only, total) -> dict:
    """The first image ads on the welcome credits: made through Studio at the shown price, from those credits."""
    import desk
    import studio_v2
    data, status = _json(studio_v2.generate_parts(only, {"total": total, "confirm_many": True}, TITLE,
                                                  {"first": True, "templates": [p.get("template") or
                                                                                p["payload"]["mode"] for p in only]}))
    if status != 200 or not data.get("ok") or not data.get("batch"):
        return {"error": data.get("error") or "The ads could not be started just now. Nothing was used. Try again.",
                "verify": bool(data.get("verify")), "plan_required": bool(data.get("plan_required"))}
    n = conn.execute("SELECT COUNT(*) FROM jobs WHERE batch_id=? AND workspace_id=?", (data["batch"], ws["id"])).fetchone()[0]
    made = [{"kind": "ads", "name": desk._plural(n, "image ad"), "state": "Being made",
             "href": url_for("studio_v2.page", made=data["batch"])}]
    notes = [f"Made with your free credits: {total:,} credits."]
    if data.get("note"):
        notes.append(data["note"])
    tid = desk._new_thread(conn, product, TITLE)
    desk._turn(conn, tid, "member", "said", {"text": "Make my first ads."})
    turn = desk._turn(conn, tid, "desk", "made", {"made": made, "notes": notes, "batch": data["batch"], "credits": total,
                                                 "first": True})
    conn.execute("INSERT INTO desk_runs (workspace_id, user_id, product_id, ask, made_json, credits, created_at, thread_id) "
                 "VALUES (?,?,?,?,?,?,?,?)", (ws["id"], g.user["id"], product["id"], TITLE, json.dumps(made), total, db.now(), tid))
    conn.commit()
    return {"ok": True, "thread": tid, "batch": data["batch"], "images": n, "talks": 0,
            "url": url_for("desk.thread", tid=tid) + f"#t{turn}"}


# ---------------------------------------------------------------- the preview of each look (no AI, free)

PREVIEW_W = 640
ASPECT_H = {"4:5": 1.25, "1:1": 1.0, "9:16": 16 / 9, "16:9": 9 / 16}


def preview_jpeg(conn, ws_id: int, product, template: dict, headline: str) -> bytes | None:
    """What this look's ad will roughly be, from the member's own photo, words and brand kit: the product photo on a
    soft backdrop of itself, the headline and the logo set by textlayer exactly as on the finished ad. The finished
    ad puts the product in the look's scene; this shows the layout, the words and the colours. None without a photo."""
    import io
    import zlib

    from PIL import Image, ImageFilter

    import products
    import textlayer
    if product is None or not product["primary_photo_id"]:
        return None
    ph = conn.execute("SELECT path FROM photos WHERE id=? AND workspace_id=?", (product["primary_photo_id"], ws_id)).fetchone()
    if ph is None:
        return None
    try:
        src = Image.open(ph["path"]).convert("RGB")
    except (OSError, ValueError):
        return None
    aspect = template.get("aspect") or "4:5"
    w = PREVIEW_W
    h = int(round(w * ASPECT_H.get(aspect, 1.25)))
    # the backdrop: the photo itself, filling the frame, blurred and dimmed; the product sits whole on it, low and
    # centred, leaving the top for the words as the finished layout does
    cover = src.copy()
    scale = max(w / cover.width, h / cover.height)
    cover = cover.resize((max(1, int(cover.width * scale)), max(1, int(cover.height * scale))))
    cover = cover.crop(((cover.width - w) // 2, (cover.height - h) // 2, (cover.width - w) // 2 + w, (cover.height - h) // 2 + h))
    b = products.get_brand(conn, ws_id, product["brand_id"]) if product["brand_id"] else None
    kit = textlayer.kit_of(b)
    if zlib.crc32(str(template.get("id") or "").encode()) % 2:
        # every other look sits on the brand's own colour, so two looks don't read as one
        top, bottom = textlayer.hex_rgb(kit["primary"]), textlayer.hex_rgb(kit["accent"])
        cover = Image.new("RGB", (w, h), top)
        band = Image.new("RGB", (w, int(h * 0.42)), bottom)
        cover.paste(Image.blend(band, Image.new("RGB", band.size, top), 0.55), (0, h - band.height))
    else:
        cover = cover.filter(ImageFilter.GaussianBlur(28))
        cover = Image.blend(cover, Image.new("RGB", (w, h), (16, 16, 16)), 0.25)
    fit = src.copy()
    fit.thumbnail((int(w * 0.78), int(h * 0.62)))
    cover.paste(fit, ((w - fit.width) // 2, h - fit.height - int(h * 0.1)))
    layout = template.get("layout") or "headline"
    try:
        out, _rep = textlayer.render(cover, layout, {"headline": headline or template.get("headline") or product["name"]},
                                     kit, aspect, fit=False, people=[])
    except Exception as e:  # noqa: BLE001  (a preview is a nicety: the step still works without it)
        log.warning("first ads preview failed: %s", str(e)[:160])
        out = cover
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=82)
    return buf.getvalue()
