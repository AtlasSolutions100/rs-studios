"""Runs flows: enrols contacts when a trigger fires and walks each run through
its steps, bounded per tick so generation polling is never starved.

    enrol(conn, ws_id, kind, contact_id, payload)   called inline by the request
                                                   handlers that touch contacts
    tick(now=None)                                 one pass (the worker loop calls it)

A run points at the node it will do next and sleeps until `wake_at` (absolute
UTC). Each pass takes a lease on the runs it works on (`lease_until`), so a
restart or a second worker can't do the same step twice. Every step writes a
flow_events row. A run that raises is marked failed; 20 failures in a row pause
the flow and leave the owner a note.

Messages: email (F1) and SMS (F3) go through SENDERS when a provider is
connected; until then each becomes a task for the owner with the drafted copy.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import db
import flows

log = logging.getLogger("campaignify")
RUN_BATCH = 50
STEPS_PER_RUN = 60
LEASE_MINUTES = 2
FAIL_PAUSE = 20
PUBLIC_URL = os.environ.get("RS_PUBLIC_URL", "https://campaignify.ai").rstrip("/")
# channel -> fn(conn, run, node, contact, now) -> (event kind, detail), or None when this workspace can't
# send on that channel yet (the step becomes a task). F1 adds "email", F3 adds "sms".
SENDERS = {}
AFTER_TICK = []   # fn(now): e.g. send what the steps queued


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _graph(conn, version_id) -> dict:
    v = conn.execute("SELECT graph_json FROM flow_versions WHERE id=?", (version_id,)).fetchone()
    return flows.unpack(v["graph_json"] if v else "{}")


def _event(conn, flow_id, ws_id, run_id, contact_id, node_id, kind, detail="", now=None):
    conn.execute("INSERT INTO flow_events (run_id, flow_id, workspace_id, contact_id, node_id, kind, detail, at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (run_id, flow_id, ws_id, contact_id, node_id, kind, str(detail)[:500],
                                              iso(now or utcnow())))


def _tags(c) -> set:
    return {t for t in (c["tags"] or "").split(",") if t}


def has_consent(c, channel) -> bool:
    if channel == "sms":
        return (c["sms_consent"] or "none") != "none" and not c["sms_stopped_at"]
    return (c["email_consent"] or "none") != "none" and not c["unsubscribed_at"]


def suppressed(conn, ws_id, channel, address) -> bool:
    return bool(address) and conn.execute("SELECT 1 FROM suppressions WHERE workspace_id=? AND channel=? AND address=?",
                                          (ws_id, channel, address.lower())).fetchone() is not None


# ---------------------------------------------------------------- enrolment

def _matches(trigger, kind, payload) -> bool:
    if trigger.get("kind") != kind:
        return False
    if kind == "form_submitted":
        return trigger.get("form_id") in (None, payload.get("form_id"))
    if kind == "tag_added":
        return trigger.get("tag") in (payload.get("tags") or ())
    return True


def _first_send(graph) -> str | None:
    """The channel of the first message on the main path (to check consent before enrolling)."""
    nodes, nid, n = graph.get("nodes") or {}, graph.get("start"), 0
    while nid and n < 200:
        node = nodes.get(nid) or {}
        if node.get("type") in ("email", "sms"):
            return node["type"]
        if node.get("type") == "condition":
            return None   # depends on the person: checked when the message is due
        nid, n = node.get("next"), n + 1
    return None


def enrol(conn, ws_id, kind, contact_id, payload=None, now=None) -> list[int]:
    """Start every live flow in the workspace whose trigger matches. Once per contact per flow. No commit."""
    payload = payload or {}
    now = now or utcnow()
    c = conn.execute("SELECT * FROM contacts WHERE id=? AND workspace_id=?", (contact_id, ws_id)).fetchone()
    if not c:
        return []
    started = []
    for f in conn.execute("SELECT * FROM flows WHERE workspace_id=? AND status='on' AND version_id IS NOT NULL",
                          (ws_id,)).fetchall():
        g = _graph(conn, f["version_id"])
        if not _matches(g["tree"].get("trigger") or {}, kind, payload):
            continue
        if conn.execute("SELECT 1 FROM flow_runs WHERE flow_id=? AND contact_id=?", (f["id"], contact_id)).fetchone():
            continue
        first = _first_send(g)
        if first == "sms" and not has_consent(c, "sms"):
            _event(conn, f["id"], ws_id, None, contact_id, g.get("start"), "skipped",
                   "Not started: the first step is a text and they haven't agreed to texts.", now)
            continue
        rid = conn.execute("INSERT INTO flow_runs (flow_id, version_id, workspace_id, contact_id, status, node_id, wake_at, "
                           "started_at, context_json) VALUES (?,?,?,?,?,?,?,?,?)",
                           (f["id"], f["version_id"], ws_id, contact_id, "waiting", g.get("start"), iso(now), iso(now),
                            json.dumps({"trigger": kind}))).lastrowid
        _event(conn, f["id"], ws_id, rid, contact_id, None, "entered", flows.TRIGGERS.get(kind, kind), now)
        started.append(rid)
    return started


def enrol_tags(conn, ws_id, contact_id, before: set, after: set, now=None) -> list[int]:
    added = sorted(set(after) - set(before))
    return enrol(conn, ws_id, "tag_added", contact_id, {"tags": added}, now) if added else []


def fire_dates(conn, now) -> int:
    """Date triggers: at the set time, enrol everyone with the tag (once)."""
    n = 0
    for f in conn.execute("SELECT * FROM flows WHERE status='on' AND fired_at IS NULL AND version_id IS NOT NULL").fetchall():
        t = _graph(conn, f["version_id"])["tree"].get("trigger") or {}
        if t.get("kind") != "date" or not t.get("at"):
            continue
        try:
            at = _local(t["at"], _ws_tz(conn, f["workspace_id"]))
        except ValueError:
            continue
        if at > now:
            continue
        conn.execute("UPDATE flows SET fired_at=? WHERE id=?", (iso(now), f["id"]))
        for c in conn.execute("SELECT id, tags FROM contacts WHERE workspace_id=?", (f["workspace_id"],)).fetchall():
            if t.get("tag") in _tags(c):
                n += len(enrol(conn, f["workspace_id"], "date", c["id"], {}, now))
    return n


# ---------------------------------------------------------------- time zones

def _ws_tz(conn, ws_id) -> str:
    r = conn.execute("SELECT timezone FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    return (r["timezone"] if r and r["timezone"] else "") or os.environ.get("RS_DEFAULT_TZ", "America/Chicago")


def _zone(name):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _local(naive_iso: str, tzname: str) -> datetime:
    d = datetime.fromisoformat(naive_iso)
    return (d if d.tzinfo else d.replace(tzinfo=_zone(tzname))).astimezone(timezone.utc)


def wake_after(node: dict, now: datetime, tzname: str) -> datetime:
    """When a wait step ends: now + days/hours, then (optionally) the next HH:MM in the contact's zone."""
    t = now + timedelta(days=int(node.get("days") or 0), hours=int(node.get("hours") or 0))
    if node.get("at"):
        hh, mm = (int(x) for x in node["at"].split(":"))
        z = _zone(tzname)
        loc = t.astimezone(z)
        cand = loc.replace(hour=hh, minute=mm, second=0)   # that day at HH:MM ("wait 1 day, then 9:00" = tomorrow 9:00)
        if cand <= now:
            cand += timedelta(days=1)
        t = cand.astimezone(timezone.utc)
    return t


# ---------------------------------------------------------------- the walk

def tick(now=None, limit=RUN_BATCH) -> int:
    """Fire date triggers, then advance up to `limit` due runs. Returns how many runs moved."""
    now = now or utcnow()
    moved = 0
    with db.connect() as conn:
        fire_dates(conn, now)
        conn.commit()
        due = conn.execute("SELECT * FROM flow_runs WHERE status='waiting' AND wake_at<=? AND "
                           "(lease_until IS NULL OR lease_until<?) ORDER BY wake_at LIMIT ?",
                           (iso(now), iso(now), limit)).fetchall()
        for run in due:
            got = conn.execute("UPDATE flow_runs SET lease_until=? WHERE id=? AND (lease_until IS NULL OR lease_until<?)",
                               (iso(now + timedelta(minutes=LEASE_MINUTES)), run["id"], iso(now))).rowcount
            conn.commit()
            if not got:
                continue   # someone else has it
            try:
                advance(conn, conn.execute("SELECT * FROM flow_runs WHERE id=?", (run["id"],)).fetchone(), now)
                conn.execute("UPDATE flows SET errors=0 WHERE id=?", (run["flow_id"],))
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                log.exception("flow run %s failed", run["id"])
                _fail(conn, run, str(e), now)
            conn.commit()
            moved += 1
    for fn in AFTER_TICK:
        try:
            fn(now)
        except Exception:  # noqa: BLE001
            log.exception("after-tick step failed")
    return moved


def _fail(conn, run, msg, now):
    conn.execute("UPDATE flow_runs SET status='failed', exit_reason=?, ended_at=?, lease_until=NULL WHERE id=?",
                 (msg[:300], iso(now), run["id"]))
    _event(conn, run["flow_id"], run["workspace_id"], run["id"], run["contact_id"], run["node_id"], "error", msg, now)
    conn.execute("UPDATE flows SET errors=errors+1 WHERE id=?", (run["flow_id"],))
    errs = conn.execute("SELECT errors FROM flows WHERE id=?", (run["flow_id"],)).fetchone()
    if errs and errs[0] >= FAIL_PAUSE:
        f = conn.execute("SELECT * FROM flows WHERE id=?", (run["flow_id"],)).fetchone()
        if f["status"] == "on":
            conn.execute("UPDATE flows SET status='paused' WHERE id=?", (f["id"],))
            _task(conn, f["workspace_id"], None, None, f["id"], None, "notify",
                  f"“{f['name']}” was paused after {FAIL_PAUSE} errors in a row",
                  f"The last error: {msg[:300]}\n\nOpen the flow's Activity tab to see what happened, then turn it on "
                  "again.", now)


def _task(conn, ws_id, contact_id, run_id, flow_id, node_id, kind, title, body, now, due=None):
    return conn.execute("INSERT INTO tasks (workspace_id, contact_id, run_id, flow_id, node_id, kind, title, body, due_at, "
                        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ws_id, contact_id, run_id, flow_id, node_id, kind, title[:200], (body or "")[:6000],
                         iso(due or now), iso(now))).lastrowid


def _brand(conn, ws_id) -> dict:
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    b = conn.execute("SELECT brand_name FROM brands WHERE id=?", (ws["brand_id"] or 0,)).fetchone() if ws else None
    p = conn.execute("SELECT name FROM products WHERE id=?", (ws["product_id"] or 0,)).fetchone() if ws else None
    return {"name": (b["brand_name"] if b else "") or (ws["sender_name"] if ws and ws["sender_name"] else "") or "",
            "product": p["name"] if p else ""}


def _links(conn, ws_id) -> dict:
    return {r["slug"]: f"{PUBLIC_URL}/p/{r['slug']}" for r in
            conn.execute("SELECT slug FROM pages WHERE workspace_id=?", (ws_id,)).fetchall()}


def test(conn, run, node_test: dict, c) -> bool:
    k = node_test.get("kind")
    if k == "has_tag":
        return node_test.get("tag") in _tags(c)
    if k == "field_equals":
        f = node_test.get("field")
        return f in c.keys() and str(c[f] or "").strip().lower() == str(node_test.get("value") or "").strip().lower()
    if k == "submitted_form":
        return conn.execute("SELECT 1 FROM form_submissions WHERE contact_id=? AND form_id=?",
                            (c["id"], node_test.get("form_id"))).fetchone() is not None
    if k in ("clicked", "opened"):
        hook = TESTS_HOOKS.get(k)
        return bool(hook and hook(conn, run, node_test.get("node")))
    if k == "has_phone":
        return bool(c["phone_e164"] or c["phone"])
    if k == "has_consent":
        return has_consent(c, node_test.get("channel") or "email")
    return False


# kind -> fn(conn, run, node_id) -> bool. F1 adds "clicked"/"opened" (from message tracking).
TESTS_HOOKS = {}


def advance(conn, run, now):
    """Walk one run until it has to wait, ends or exits."""
    g = _graph(conn, run["version_id"])
    nodes = g.get("nodes") or {}
    ctx = json.loads(run["context_json"] or "{}")
    nid = run["node_id"]
    ws_id, cid, fid = run["workspace_id"], run["contact_id"], run["flow_id"]
    for _ in range(STEPS_PER_RUN):
        if not nid:
            conn.execute("UPDATE flow_runs SET status='done', node_id=NULL, ended_at=?, lease_until=NULL, context_json=? "
                         "WHERE id=?", (iso(now), json.dumps(ctx), run["id"]))
            _event(conn, fid, ws_id, run["id"], cid, None, "exited", "Finished the flow", now)
            return
        node = nodes.get(nid)
        if node is None:
            raise RuntimeError(f"step {nid} is missing from this version")
        c = conn.execute("SELECT * FROM contacts WHERE id=? AND workspace_id=?", (cid, ws_id)).fetchone()
        if c is None:
            conn.execute("UPDATE flow_runs SET status='exited', exit_reason='contact deleted', ended_at=?, lease_until=NULL "
                         "WHERE id=?", (iso(now), run["id"]))
            return
        typ = node["type"]
        nxt = node.get("next")
        if typ == "wait":
            if ctx.get("waited") != nid:
                wake = wake_after(node, now, c["timezone"] or _ws_tz(conn, ws_id))
                ctx["waited"] = nid
                conn.execute("UPDATE flow_runs SET node_id=?, wake_at=?, lease_until=NULL, context_json=? WHERE id=?",
                             (nid, iso(wake), json.dumps(ctx), run["id"]))
                _event(conn, fid, ws_id, run["id"], cid, nid, "waited", f"until {iso(wake)}", now)
                return
            ctx.pop("waited", None)
        elif typ == "condition":
            ok = test(conn, run, node["test"], c)
            nxt = node.get("yes") if ok else node.get("no")
            _event(conn, fid, ws_id, run["id"], cid, nid, "branch", "yes" if ok else "no", now)
        elif typ == "exit_if":
            if test(conn, run, node["test"], c):
                conn.execute("UPDATE flow_runs SET status='exited', exit_reason='goal reached', node_id=?, ended_at=?, "
                             "lease_until=NULL WHERE id=?", (nid, iso(now), run["id"]))
                _event(conn, fid, ws_id, run["id"], cid, nid, "exited", "Reached the goal: " +
                       flows.describe_test(node["test"]), now)
                return
        elif typ in ("email", "sms"):
            _message(conn, run, node, c, now)
        elif typ == "tag":
            tags = _tags(c)
            new = (tags | {node["tag"]}) if node["op"] == "add" else (tags - {node["tag"]})
            conn.execute("UPDATE contacts SET tags=? WHERE id=?", (",".join(sorted(new)), cid))
            _event(conn, fid, ws_id, run["id"], cid, nid, "action", flows.describe(node), now)
            enrol_tags(conn, ws_id, cid, tags, new, now)
        elif typ == "stage":
            conn.execute("UPDATE contacts SET stage=? WHERE id=?", (node["stage"], cid))
            _event(conn, fid, ws_id, run["id"], cid, nid, "action", flows.describe(node), now)
        elif typ == "task":
            body = flows.render(node.get("body") or "", dict(c), _brand(conn, ws_id), _links(conn, ws_id))
            _task(conn, ws_id, cid, run["id"], fid, nid, "task", flows.render(node["title"], dict(c), _brand(conn, ws_id)),
                  body, now, now + timedelta(days=int(node.get("due_days") or 0)))
            _event(conn, fid, ws_id, run["id"], cid, nid, "task", node["title"], now)
        elif typ == "notify":
            msg = flows.render(node["message"], dict(c), _brand(conn, ws_id))
            _task(conn, ws_id, cid, run["id"], fid, nid, "notify", msg, f"{c['name'] or c['email']} reached this step.", now)
            _event(conn, fid, ws_id, run["id"], cid, nid, "action", "Notified you", now)
        elif typ == "webhook":
            import forms as forms_mod
            f = conn.execute("SELECT * FROM flows WHERE id=?", (fid,)).fetchone()
            secret = json.loads(f["settings_json"] or "{}").get("webhook_secret") or ""
            payload = {"event": "flow.step", "flow": {"id": fid, "name": f["name"]}, "step": nid,
                       "contact": {"id": cid, "email": c["email"], "name": c["name"], "tags": sorted(_tags(c)),
                                   "stage": c["stage"]}, "at": iso(now)}
            forms_mod.send_async(node["url"], secret, payload, sync=os.environ.get("RS_WORKER", "1") != "1")
            _event(conn, fid, ws_id, run["id"], cid, nid, "action", flows.describe(node), now)
        elif typ == "community":
            com = conn.execute("SELECT * FROM communities WHERE workspace_id=?", (ws_id,)).fetchone()
            if com and com["open_join"]:
                link = f"{PUBLIC_URL}/c/{com['slug']}/join/{com['join_code']}"
                _task(conn, ws_id, cid, run["id"], fid, nid, "community", f"Invite {c['name'] or c['email']} to "
                      f"{com['name']}", f"Send them this join link: {link}", now)
                _event(conn, fid, ws_id, run["id"], cid, nid, "task", "Community invite to send", now)
            else:
                _event(conn, fid, ws_id, run["id"], cid, nid, "skipped", "No community with an open join link", now)
        nid = nxt
        conn.execute("UPDATE flow_runs SET node_id=?, context_json=? WHERE id=?", (nid, json.dumps(ctx), run["id"]))
    # A very long chain without a wait: carry on next tick.
    conn.execute("UPDATE flow_runs SET node_id=?, wake_at=?, lease_until=NULL, context_json=? WHERE id=?",
                 (nid, iso(now), json.dumps(ctx), run["id"]))


def _message(conn, run, node, c, now):
    """An email or text step: send it when the channel is connected, else hand it to the owner as a task."""
    ws_id, fid, nid = run["workspace_id"], run["flow_id"], node["id"]
    channel = node["type"]
    sender = SENDERS.get(channel)
    res = sender(conn, run, node, c, now) if sender else None
    if res:
        _event(conn, fid, ws_id, run["id"], c["id"], nid, res[0], res[1], now)
        return
    brand = _brand(conn, ws_id)
    links = _links(conn, ws_id)
    who = c["name"] or c["email"]
    if channel == "email":
        subject = flows.render(node.get("subject") or "", dict(c), brand, links)
        body = flows.render(node.get("body") or "", dict(c), brand, links)
        title = f"Email {who}: {subject}"
        text = f"To: {c['email']}\nSubject: {subject}\n\n{body}"
    else:
        body = flows.render(node.get("body") or "", dict(c), brand, links)
        title = f"Text {who}"
        text = f"To: {c['phone_e164'] or c['phone'] or '(no phone on file)'}\n\n{body}"
    _task(conn, ws_id, c["id"], run["id"], fid, nid, channel, title, text, now)
    _event(conn, fid, ws_id, run["id"], c["id"], nid, "task",
           f"{'Email' if channel == 'email' else 'Text'} sending isn't connected: left you a task to send it", now)


# ---------------------------------------------------------------- turning on

def publish(conn, flow) -> int:
    """Snapshot the editing copy as a new version; new runs use it, existing runs keep theirs."""
    v = (flow["version"] or 0) + 1
    vid = conn.execute("INSERT INTO flow_versions (flow_id, version, graph_json, created_at) VALUES (?,?,?,?)",
                       (flow["id"], v, flow["graph_json"], db.now())).lastrowid
    conn.execute("UPDATE flows SET version=?, version_id=?, dirty=0 WHERE id=?", (v, vid, flow["id"]))
    return vid


def exit_run(conn, run_id, ws_id, reason, now=None):
    now = now or utcnow()
    r = conn.execute("SELECT * FROM flow_runs WHERE id=? AND workspace_id=? AND status='waiting'", (run_id, ws_id)).fetchone()
    if not r:
        return False
    conn.execute("UPDATE flow_runs SET status='exited', exit_reason=?, ended_at=?, lease_until=NULL WHERE id=?",
                 (reason[:200], iso(now), run_id))
    _event(conn, r["flow_id"], ws_id, run_id, r["contact_id"], r["node_id"], "exited", reason, now)
    return True
