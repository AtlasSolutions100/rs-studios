"""The lead pipeline: every contact stands at one stage, the workspace names the stages, and a contact page
shows everything that has happened and lets the member write to the person (Leads, /app/leads; Koby, Nov 11 2026).

Stages. A contact's stage is `contacts.stage`, the column flows already move ("Move to a stage"). The keys flows
know stay the keys here, so a flow step and the board always mean the same stage; what a workspace changes is the
NAME of each one, and it can add up to MAX_EXTRA stages of its own in the middle of the funnel:

    lead        New lead        (where every contact starts; an empty stage reads as this)
    engaged     Contacted
    qualified   Qualified
    x1..x4      the workspace's own, in the order it added them (workspaces.pipeline_json)
    customer    Won
    repeat      Delivered
    cold        Lost            (kept in its own list, off the board)

History. Every move writes a contact_events row (kind "stage"), so the contact's timeline says who moved them
where and when, beside the forms they filled in, the links they clicked and every email and text a flow sent
them (the messages table, which is what a flow's email and SMS steps write).

Writing to a lead. A message written on the contact page is a messages row with no flow. The same senders send
it (messaging.send_queued, sms.send_queued), so consent, unsubscribes, suppressions, the daily caps, the content
rules and the credit price are exactly a flow message's: one credit an email, the usual per-segment price a text.
"""
import json
import re
import secrets

import compliance
import db

BUILT_IN = [("lead", "New lead"), ("engaged", "Contacted"), ("qualified", "Qualified"), ("customer", "Won"),
            ("repeat", "Delivered")]
LOST = ("cold", "Lost")
EXTRA_AFTER = "qualified"        # a workspace's own stages sit after this one, before Won
MAX_EXTRA = 4
NAME_MAX = 24
BOARD_LIMIT = 60                 # cards shown per stage; the count is always the real one


def _conf(ws) -> dict:
    try:
        d = json.loads(ws["pipeline_json"] or "{}") if "pipeline_json" in ws.keys() else {}
    except (TypeError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def _name(v, fallback: str) -> str:
    return " ".join(str(v or "").split())[:NAME_MAX] or fallback


def stages(ws) -> list[dict]:
    """The workspace's stages in order, the lost one last: [{key, name, lost, own}]."""
    conf = _conf(ws)
    names = conf.get("names") if isinstance(conf.get("names"), dict) else {}
    extra = [e for e in (conf.get("extra") or []) if isinstance(e, dict) and re.fullmatch(r"x[1-9]", str(e.get("key") or ""))]
    out = []
    for key, default in BUILT_IN:
        out.append({"key": key, "name": _name(names.get(key), default), "lost": False, "own": False})
        if key == EXTRA_AFTER:
            out += [{"key": e["key"], "name": _name(e.get("name"), "Stage"), "lost": False, "own": True} for e in extra[:MAX_EXTRA]]
    out.append({"key": LOST[0], "name": _name(names.get(LOST[0]), LOST[1]), "lost": True, "own": False})
    return out


def key_of(ws, raw) -> str:
    """The stage a stored value stands for: itself when the workspace has it, else the first stage."""
    known = {s["key"] for s in stages(ws)}
    return raw if raw in known else BUILT_IN[0][0]


def name_of(ws, raw) -> str:
    key = key_of(ws, raw)
    return next(s["name"] for s in stages(ws) if s["key"] == key)


def save_stages(conn, ws, form) -> str | None:
    """Rename stages, add the workspace's own (a filled `new` field) and remove one of its own (`remove`: its leads
    go back to the stage before it). Returns a sentence when something was refused."""
    conf = _conf(ws)
    names = dict(conf.get("names") or {}) if isinstance(conf.get("names"), dict) else {}
    extra = [dict(e) for e in (conf.get("extra") or []) if isinstance(e, dict)]
    for s in stages(ws):
        v = form.get(f"name_{s['key']}")
        if v is None:
            continue
        v = _name(v, "")
        if not v:
            return "A stage needs a name."
        if s["own"]:
            for e in extra:
                if e.get("key") == s["key"]:
                    e["name"] = v
        else:
            names[s["key"]] = v
    gone = form.get("remove") or ""
    if any(e.get("key") == gone for e in extra):
        extra = [e for e in extra if e.get("key") != gone]
        conn.execute("UPDATE contacts SET stage=? WHERE workspace_id=? AND stage=?", (EXTRA_AFTER, ws["id"], gone))
    new = _name(form.get("new"), "")
    if new:
        if len(extra) >= MAX_EXTRA:
            return f"A pipeline holds up to {MAX_EXTRA} stages of your own. Remove one first."
        used = {e.get("key") for e in extra}
        extra.append({"key": next(f"x{i}" for i in range(1, 10) if f"x{i}" not in used), "name": new})
    conn.execute("UPDATE workspaces SET pipeline_json=? WHERE id=?", (json.dumps({"names": names, "extra": extra}), ws["id"]))
    return None


def move(conn, ws, contact, key, by="") -> bool:
    """Move a contact to a stage and write it to their timeline. False when nothing changed."""
    known = {s["key"]: s["name"] for s in stages(ws)}
    was = key_of(ws, contact["stage"])
    if key not in known or key == was:
        return False
    conn.execute("UPDATE contacts SET stage=? WHERE id=? AND workspace_id=?", (key, contact["id"], ws["id"]))
    conn.execute("INSERT INTO contact_events (workspace_id, contact_id, kind, ref, detail, at) VALUES (?,?,?,?,?,?)",
                 (ws["id"], contact["id"], "stage", key,
                  f"{known[was]} to {known[key]}" + (f", by {by}" if by else ""), db.now()))
    return True


def _last_touch(conn, ws_id, ids) -> dict:
    """{contact id: (words, when)}: the latest thing that happened with each contact."""
    out = {}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    words = {"form": "Filled in a form", "page": "Visited a page", "link": "Came through a link", "stage": "Moved",
             "msg_open": "Opened an email", "msg_click": "Clicked a link in an email", "sms_reply": "Replied by text",
             "ad": "Came from an ad"}
    for r in conn.execute(f"SELECT contact_id, kind, detail, MAX(id), at FROM contact_events WHERE workspace_id=? AND "
                          f"contact_id IN ({marks}) GROUP BY contact_id", (ws_id, *ids)):
        out[r[0]] = (words.get(r[1], "Something happened") + (f": {r[2]}" if r[1] == "stage" and r[2] else ""), r[4])
    for r in conn.execute(f"SELECT contact_id, channel, status, MAX(id), COALESCE(sent_at, created_at) FROM messages WHERE "
                          f"workspace_id=? AND contact_id IN ({marks}) GROUP BY contact_id", (ws_id, *ids)):
        what = ("Email" if r[1] == "email" else "Text") + {"sent": " sent", "queued": " waiting to send"}.get(r[2], " not sent")
        if r[0] not in out or (r[4] or "") > (out[r[0]][1] or ""):
            out[r[0]] = (what, r[4])
    return out


def board(conn, ws) -> dict:
    """{"columns": [{key, name, count, cards}], "lost": {key, name, count}, "total"}: the board, newest lead first."""
    st = stages(ws)
    first = st[0]["key"]
    known = {s["key"] for s in st}
    rows = conn.execute("SELECT id, email, name, stage, source, last_seen FROM contacts WHERE workspace_id=? "
                        "ORDER BY last_seen DESC", (ws["id"],)).fetchall()
    by = {s["key"]: [] for s in st}
    for r in rows:
        by[r["stage"] if r["stage"] in known else first].append(r)
    shown = [r["id"] for s in st if not s["lost"] for r in by[s["key"]][:BOARD_LIMIT]]
    touch = _last_touch(conn, ws["id"], shown)
    cols = [{"key": s["key"], "name": s["name"], "count": len(by[s["key"]]),
             "cards": [{"id": r["id"], "name": r["name"] or r["email"], "source": r["source"] or "",
                        "last": touch.get(r["id"], ("", ""))[0], "at": touch.get(r["id"], ("", r["last_seen"]))[1]}
                       for r in by[s["key"]][:BOARD_LIMIT]]} for s in st if not s["lost"]]
    lost = st[-1]
    return {"columns": cols, "lost": {"key": lost["key"], "name": lost["name"], "count": len(by[lost["key"]])},
            "total": len(rows)}


def timeline(conn, ws, cid, limit=120) -> list[dict]:
    """Everything that has happened with one contact, newest first: [{kind, what, detail, at}]. Events (forms,
    pages, links, ads, opens, clicks, replies, stage moves) and every email and text sent or waiting."""
    label = {"form": "Filled in", "page": "On page", "link": "Came through smart link", "ad": "Came from",
             "msg_click": "Clicked a link in", "msg_open": "Opened", "sms_reply": "Replied by text:", "stage": "Moved from"}
    out = [{"kind": e["kind"], "what": label.get(e["kind"], e["kind"]), "detail": e["detail"] or "", "at": e["at"]}
           for e in conn.execute("SELECT * FROM contact_events WHERE contact_id=? AND workspace_id=? ORDER BY id DESC LIMIT ?",
                                 (cid, ws["id"], limit))]
    state = {"sent": "sent", "queued": "waiting to send", "failed": "not sent", "skipped": "not sent", "suppressed": "not sent"}
    for m in conn.execute("SELECT m.*, f.name fname FROM messages m LEFT JOIN flows f ON f.id=m.flow_id WHERE m.contact_id=? "
                          "AND m.workspace_id=? ORDER BY m.id DESC LIMIT ?", (cid, ws["id"], limit)):
        what = ("Email " if m["channel"] == "email" else "Text ") + state.get(m["status"], m["status"])
        words = m["subject"] if m["channel"] == "email" else (m["body_text"] or "")[:80]
        who = f"by the follow-up “{m['fname']}”" if m["fname"] else "written by you"
        why = f" ({m['error']})" if m["error"] and m["status"] != "sent" else ""
        out.append({"kind": "message", "what": what, "detail": f"{words or ''}, {who}{why}".strip(", "),
                    "at": m["sent_at"] or m["created_at"]})
    out.sort(key=lambda e: e["at"] or "", reverse=True)
    return out[:limit]


def can_write(conn, ws, contact, channel) -> list[str]:
    """What stops a message to this contact on this channel ([] = it can go): the workspace's sending setup, then
    the contact's consent, unsubscribe and number."""
    import flows_engine
    import messaging
    import sms
    if channel == "sms":
        out = list(sms.sms_ready(ws))
        if contact["sms_stopped_at"] or flows_engine.suppressed(conn, ws["id"], "sms", contact["phone_e164"] or ""):
            out.append("They replied STOP, so texts to them are off.")
        elif (contact["sms_consent"] or "none") == "none" or not contact["phone_e164"]:
            out.append("There is no consent for texts, or no mobile number, on record for them.")
        return out
    out = list(messaging.email_ready(ws))
    if contact["unsubscribed_at"] or flows_engine.suppressed(conn, ws["id"], "email", contact["email"]):
        out.append("They unsubscribed, or their address bounced.")
    elif not messaging.consent_ok(ws, contact):
        out.append("There is no email consent on record for them.")
    return out


def write(conn, ws, contact, channel, subject, body) -> tuple[bool, str]:
    """Queue one email or text to a contact, written by the member. (queued?, a sentence for the member)."""
    channel = "sms" if channel == "sms" else "email"
    subject = " ".join(str(subject or "").split())[:200]
    body = str(body or "").strip()[:640 if channel == "sms" else 20000]
    if not body or (channel == "email" and not subject):
        return False, "Write the message first." if body or channel == "sms" else "Give the email a subject and a message."
    stops = can_write(conn, ws, contact, channel)
    if stops:
        return False, stops[0]
    verdict = compliance.check(subject, body)       # the member's own words: an offer they state here is theirs
    if not verdict["ok"]:
        return False, "That message breaks a content rule: " + verdict["blocks"][0]
    conn.execute("INSERT INTO messages (workspace_id, contact_id, channel, to_addr, subject, body_text, status, token, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (ws["id"], contact["id"], channel, contact["phone_e164"] if channel == "sms" else contact["email"],
                  subject if channel == "email" else None, body, "queued", secrets.token_urlsafe(18), db.now()))
    return True, ("Your email is on its way." if channel == "email" else "Your text is on its way.")
