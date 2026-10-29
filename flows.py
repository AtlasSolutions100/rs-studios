"""Flows: the path after the ad (form -> follow-ups -> actions), as data.

A flow is edited as a *tree*: a trigger and a list of steps, where a condition
step holds a `yes` and a `no` list and both branches carry on with whatever
comes after the condition. The tree is what the editor, the templates and the
AI drafter produce. `compile_tree()` turns it into the node graph the engine
walks (each node points at its `next`, or `yes`/`no`); the two are stored
together in flows.graph_json, so there are no cycles or orphans by
construction, and `check_graph()` re-checks stored graphs anyway.

Sending (email from F1, SMS from F3) isn't connected in F0: those steps
become tasks for the owner with the drafted copy (flows_engine).
"""
import copy
import json
import re
import secrets

import compliance

MAX_NODES = 60
MAX_SENDS = 12
MAX_DEPTH = 3
MAX_WAIT_DAYS = 365
SMS_SEGMENT = 160

TRIGGERS = {
    "form_submitted": "A form is submitted",
    "tag_added": "A tag is added to a contact",
    "contact_added": "A contact is added by hand",
    "community_joined": "Someone joins your community",
    "date": "On a date (everyone with a tag)",
}
STEP_TYPES = {
    "email": {"name": "Send email", "group": "Message"},
    "sms": {"name": "Send text", "group": "Message"},
    "wait": {"name": "Wait", "group": "Timing"},
    "condition": {"name": "If / else", "group": "Logic"},
    "exit_if": {"name": "Exit when", "group": "Logic"},
    "tag": {"name": "Add or remove a tag", "group": "Action"},
    "stage": {"name": "Move to a stage", "group": "Action"},
    "task": {"name": "Task for me", "group": "Action"},
    "notify": {"name": "Notify me", "group": "Action"},
    "webhook": {"name": "Send a webhook", "group": "Action"},
    "community": {"name": "Invite to community", "group": "Action"},
}
TESTS = {
    "has_tag": "Has the tag",
    "field_equals": "A field equals",
    "submitted_form": "Submitted the form",
    "clicked": "Clicked a link in an email",
    "opened": "Opened an email",
    "has_phone": "Has a phone number",
    "has_consent": "Agreed to messages",
    "replied": "Replied to a text",
}
STAGES = ["lead", "engaged", "customer", "repeat", "cold"]
FIELDS = ("name", "email", "phone", "stage", "source")
MERGE_TAGS = ("first_name", "name", "product", "brand")


class FlowError(ValueError):
    pass


def new_id() -> str:
    return "n" + secrets.token_hex(3)


def _ids(steps):
    for s in steps:
        yield s.get("id")
        if s.get("type") == "condition":
            yield from _ids(s.get("yes") or [])
            yield from _ids(s.get("no") or [])


def walk(steps, depth=0):
    """Every step in a step list, depth-first, with its depth."""
    for s in steps:
        yield s, depth
        if s.get("type") == "condition":
            yield from walk(s.get("yes") or [], depth + 1)
            yield from walk(s.get("no") or [], depth + 1)


def _txt(v, n):
    return " ".join(str(v or "").split())[:n] if n < 300 else str(v or "").strip()[:n]


def clean_trigger(t: dict, forms=None) -> dict:
    t = t if isinstance(t, dict) else {}
    kind = t.get("kind") if t.get("kind") in TRIGGERS else ""
    out = {"kind": kind}
    if kind == "form_submitted":
        fid = t.get("form_id")
        out["form_id"] = int(fid) if str(fid or "").isdigit() else None
    if kind in ("tag_added", "date"):
        out["tag"] = clean_tag(t.get("tag"))
    if kind == "date":
        out["at"] = _txt(t.get("at"), 25)
    return out


def clean_tag(v) -> str:
    return re.sub(r"[^a-z0-9 _-]", "", str(v or "").lower()).strip()[:30]


def clean_step(s: dict, depth=0) -> dict:
    """One step with only the fields its type uses (unknown types raise)."""
    typ = s.get("type")
    if typ not in STEP_TYPES:
        raise FlowError(f"Unknown step type: {typ!r}")
    out = {"id": s.get("id") if re.match(r"^[a-z0-9_-]{1,24}$", str(s.get("id") or "")) else new_id(), "type": typ}
    if s.get("hint"):
        out["hint"] = _txt(s.get("hint"), 200)
    if typ == "email":
        out.update(subject=_txt(s.get("subject"), 150), body=_txt(s.get("body"), 5000))
    elif typ == "sms":
        out["body"] = _txt(s.get("body"), 640)
    elif typ == "wait":
        out["days"] = max(0, min(MAX_WAIT_DAYS, int(s.get("days") or 0)))
        out["hours"] = max(0, min(23, int(s.get("hours") or 0)))
        at = str(s.get("at") or "")
        out["at"] = at if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at) else ""
    elif typ in ("condition", "exit_if"):
        out["test"] = clean_test(s.get("test") or {})
        if typ == "condition":
            if depth >= MAX_DEPTH:
                raise FlowError("Conditions can only go three deep.")
            out["yes"] = [clean_step(x, depth + 1) for x in (s.get("yes") or [])]
            out["no"] = [clean_step(x, depth + 1) for x in (s.get("no") or [])]
    elif typ == "tag":
        out.update(op="remove" if s.get("op") == "remove" else "add", tag=clean_tag(s.get("tag")))
    elif typ == "stage":
        out["stage"] = s.get("stage") if s.get("stage") in STAGES else STAGES[0]
    elif typ == "task":
        out.update(title=_txt(s.get("title"), 140), body=_txt(s.get("body"), 2000),
                   due_days=max(0, min(60, int(s.get("due_days") or 0))))
    elif typ == "notify":
        out["message"] = _txt(s.get("message"), 300)
    elif typ == "webhook":
        out["url"] = _txt(s.get("url"), 500)
    return out


def clean_test(t: dict) -> dict:
    kind = t.get("kind") if t.get("kind") in TESTS else "has_tag"
    out = {"kind": kind}
    if kind == "has_tag":
        out["tag"] = clean_tag(t.get("tag"))
    elif kind == "field_equals":
        out["field"] = t.get("field") if t.get("field") in FIELDS else "stage"
        out["value"] = _txt(t.get("value"), 120)
    elif kind == "submitted_form":
        out["form_id"] = int(t["form_id"]) if str(t.get("form_id") or "").isdigit() else None
    elif kind in ("clicked", "opened"):
        out["node"] = str(t.get("node") or "")[:24]
    elif kind == "has_consent":
        out["channel"] = "sms" if t.get("channel") == "sms" else "email"
    return out


def clean_tree(tree: dict) -> dict:
    tree = tree if isinstance(tree, dict) else {}
    steps = [clean_step(s) for s in (tree.get("steps") or []) if isinstance(s, dict)]
    out = {"trigger": clean_trigger(tree.get("trigger") or {}), "steps": steps}
    seen = set()
    for s, _ in walk(out["steps"]):            # ids must be unique (copies get fresh ones)
        while s["id"] in seen:
            s["id"] = new_id()
        seen.add(s["id"])
    return out


def sends(tree) -> list[dict]:
    return [s for s, _ in walk(tree["steps"]) if s["type"] in ("email", "sms")]


def problems(tree: dict, forms=None) -> dict:
    """{step id or 'trigger' or 'flow': [messages]}: what stops this flow from being turned on.
    Compliance runs on every message here (and again when it is sent)."""
    out = {}

    def add(k, m):
        out.setdefault(k, []).append(m)
    n = sum(1 for _ in walk(tree["steps"]))
    if n == 0:
        add("flow", "Add at least one step.")
    if n > MAX_NODES:
        add("flow", f"A flow can have at most {MAX_NODES} steps.")
    if len(sends(tree)) > MAX_SENDS:
        add("flow", f"A flow can send at most {MAX_SENDS} messages to one person.")
    t = tree["trigger"]
    if not t.get("kind"):
        add("trigger", "Pick what starts the flow.")
    elif t["kind"] == "form_submitted" and forms is not None and t.get("form_id") not in {f["id"] for f in forms}:
        add("trigger", "Pick one of your forms.")
    elif t["kind"] in ("tag_added", "date") and not t.get("tag"):
        add("trigger", "Name the tag.")
    if t.get("kind") == "date" and not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d", t.get("at") or ""):
        add("trigger", "Pick the date and time.")
    ids = {s["id"] for s, _ in walk(tree["steps"])}
    for s, _ in walk(tree["steps"]):
        sid, typ = s["id"], s["type"]
        if typ == "email":
            if not s["subject"]:
                add(sid, "Write a subject line.")
            if not s["body"]:
                add(sid, "Write the email.")
            if re.match(r"^\s*(re|fwd?):", s["subject"], re.I):
                add(sid, "No fake “Re:” or “Fwd:” subjects.")
            chk = compliance.check(s["subject"], s["body"])
            for m in chk["blocks"]:
                add(sid, m)
        elif typ == "sms":
            if not s["body"]:
                add(sid, "Write the text.")
            for m in compliance.check(s["body"])["blocks"]:
                add(sid, m)
        elif typ == "wait":
            if not s["days"] and not s["hours"] and not s["at"]:
                add(sid, "Set how long to wait.")
        elif typ in ("condition", "exit_if"):
            te = s["test"]
            if te["kind"] == "has_tag" and not te.get("tag"):
                add(sid, "Name the tag.")
            if te["kind"] in ("clicked", "opened") and te.get("node") not in ids:
                add(sid, "Pick the email this depends on.")
            if te["kind"] == "submitted_form" and not te.get("form_id"):
                add(sid, "Pick the form.")
        elif typ == "tag" and not s["tag"]:
            add(sid, "Name the tag.")
        elif typ == "task":
            if not s["title"]:
                add(sid, "Give the task a title.")
        elif typ == "notify" and not s["message"]:
            add(sid, "Write the note.")
        elif typ == "webhook":
            if not s["url"].startswith("https://"):
                add(sid, "Webhooks need an https:// address.")
    return out


# ---------------------------------------------------------------- graph

def compile_tree(tree: dict) -> dict:
    """The engine's graph: {"start": id|None, "nodes": {id: node+next/yes/no}}."""
    nodes = {}

    def seq(steps, after):
        nxt = after
        for s in reversed(steps):
            node = {k: v for k, v in s.items() if k not in ("yes", "no")}
            if s["type"] == "condition":
                node["yes"] = seq(s.get("yes") or [], nxt)
                node["no"] = seq(s.get("no") or [], nxt)
            else:
                node["next"] = nxt
            nodes[s["id"]] = node
            nxt = s["id"]
        return nxt
    start = seq(tree["steps"], None)
    return {"start": start, "nodes": nodes}


def check_graph(graph: dict) -> list[str]:
    """Stored graphs re-checked: every pointer resolves, everything is reachable, no cycles."""
    nodes, errs = graph.get("nodes") or {}, []
    seen, stack = set(), []

    def visit(nid, path):
        if nid is None:
            return
        if nid not in nodes:
            errs.append(f"missing node {nid}")
            return
        if nid in path:
            errs.append(f"cycle at {nid}")
            return
        if len(seen) > MAX_NODES * 4:
            return
        seen.add(nid)
        n = nodes[nid]
        for k in ("next", "yes", "no"):
            if k in n:
                visit(n[k], path | {nid})
    visit(graph.get("start"), frozenset())
    orphans = set(nodes) - seen
    if orphans:
        errs.append(f"unreachable: {', '.join(sorted(orphans))}")
    return errs


def pack(tree: dict) -> str:
    """What flows.graph_json stores: the tree (for the editor) and its compiled graph (for the engine)."""
    g = compile_tree(tree)
    return json.dumps({"tree": tree, "start": g["start"], "nodes": g["nodes"]})


def unpack(raw: str) -> dict:
    d = json.loads(raw or "{}")
    if "tree" not in d:
        d = {"tree": {"trigger": {}, "steps": []}, "start": None, "nodes": {}}
    return d


# ---------------------------------------------------------------- timeline

def wait_minutes(s) -> int:
    return int(s.get("days") or 0) * 1440 + int(s.get("hours") or 0) * 60


def describe(s: dict, forms_by_id=None, steps_by_id=None) -> str:
    """One line for a step (timeline, cards, activity)."""
    t = s["type"]
    if t == "email":
        return f"Email: {s.get('subject') or '(no subject)'}"
    if t == "sms":
        return f"Text: {(s.get('body') or '')[:60]}"
    if t == "wait":
        bits = []
        if s.get("days"):
            bits.append(f"{s['days']} day{'s' if s['days'] != 1 else ''}")
        if s.get("hours"):
            bits.append(f"{s['hours']} hour{'s' if s['hours'] != 1 else ''}")
        return "Wait " + (" ".join(bits) or "") + (f", then until {s['at']}" if s.get("at") else "")
    if t in ("condition", "exit_if"):
        return ("If " if t == "condition" else "Exit when ") + describe_test(s["test"], forms_by_id, steps_by_id)
    if t == "tag":
        return f"{'Add' if s['op'] == 'add' else 'Remove'} tag “{s.get('tag')}”"
    if t == "stage":
        return f"Move to stage: {s.get('stage')}"
    if t == "task":
        return f"Task: {s.get('title') or ''}"
    if t == "notify":
        return f"Notify me: {s.get('message') or ''}"
    if t == "webhook":
        return "Webhook to " + re.sub(r"^https://", "", s.get("url") or "")[:40]
    if t == "community":
        return "Invite to your community"
    return t


def describe_test(te: dict, forms_by_id=None, steps_by_id=None) -> str:
    k = te.get("kind")
    if k == "has_tag":
        return f"they have the tag “{te.get('tag')}”"
    if k == "field_equals":
        return f"{te.get('field')} is “{te.get('value')}”"
    if k == "submitted_form":
        name = (forms_by_id or {}).get(te.get("form_id"), "a form")
        return f"they filled in {name}"
    if k in ("clicked", "opened"):
        ref = (steps_by_id or {}).get(te.get("node"))
        what = f"“{ref.get('subject')}”" if ref else "an email"
        return f"they {'clicked a link in' if k == 'clicked' else 'opened'} {what}"
    if k == "has_phone":
        return "they gave a phone number"
    if k == "has_consent":
        return f"they agreed to {'texts' if te.get('channel') == 'sms' else 'emails'}"
    if k == "replied":
        return "they replied to a text"
    return k or ""


def timeline(tree: dict, forms_by_id=None) -> list[dict]:
    """Day-by-day rows from the waits: [{day, at, label, branch, approx, type}]."""
    steps_by_id = {s["id"]: s for s, _ in walk(tree["steps"])}
    rows = []

    def run(steps, t, branch, approx):
        for s in steps:
            if s["type"] == "wait":
                t += wait_minutes(s)
                if s.get("at"):
                    approx = True
                continue
            if s["type"] == "condition":
                label = describe_test(s["test"], forms_by_id, steps_by_id)
                ty = run(s.get("yes") or [], t, branch + [f"if {label}"], approx)
                tn = run(s.get("no") or [], t, branch + [f"if not: {label}"], approx)
                approx = approx or ty != tn
                t = max(ty, tn)
                continue
            rows.append({"minutes": t, "day": t // 1440, "hour": (t % 1440) // 60, "label": describe(s, forms_by_id, steps_by_id),
                         "branch": " · ".join(branch), "approx": approx, "type": s["type"], "id": s["id"],
                         "hint": s.get("hint") or ""})
        return t
    run(tree["steps"], 0, [], False)
    return rows


# ---------------------------------------------------------------- messages

def render(text: str, contact: dict, brand: dict, links: dict | None = None) -> str:
    """Merge tags: {{first_name}} (falls back to "there"), {{name}}, {{product}}, {{brand}}, {{link:page-slug}}."""
    first = (str(contact.get("name") or "").split() or ["there"])[0]
    vals = {"first_name": first, "name": contact.get("name") or "there", "product": brand.get("product") or "",
            "brand": brand.get("name") or ""}
    out = re.sub(r"\{\{\s*(first_name|name|product|brand)\s*\}\}", lambda m: vals[m.group(1)], text or "")
    return re.sub(r"\{\{\s*link:([a-z0-9-]{1,60})\s*\}\}", lambda m: (links or {}).get(m.group(1), f"/p/{m.group(1)}"), out)


def sms_segments(body: str) -> tuple[int, str]:
    """(segments, encoding): GSM-7 fits 160 (153 per part when split); anything else is UCS-2 (70/67)."""
    gsm = re.compile(r"^[A-Za-z0-9 \r\n@£$¥èéùìòÇØøÅå_ÆæßÉ!\"#%&'()*+,\-./:;<=>?¡ÄÖÑÜ§¿äöñüà^{}\\\[~\]|€]*$")
    body = body or ""
    if gsm.match(body):
        n = len(body) + sum(body.count(c) for c in "^{}\\[~]|€")
        return (1 if n <= 160 else -(-n // 153)), "GSM-7"
    n = len(body.encode("utf-16-le")) // 2
    return (1 if n <= 70 else -(-n // 67)), "UCS-2"


# ---------------------------------------------------------------- templates
# Campaignify's own starter flows: plain, specific, one call to action each,
# with an explicit end. Every message is checked against the compliance rules
# (tests/test_app.py). {{link:your-page}} stays a placeholder until the member
# picks a page.

def _e(subject, body, hint=""):
    return {"type": "email", "subject": subject, "body": body, "hint": hint}


def _w(days=0, hours=0, at=""):
    return {"type": "wait", "days": days, "hours": hours, "at": at}


TEMPLATES = [
    {"id": "lead_magnet", "name": "Lead magnet → nurture", "channels": ["email"],
     "line": "Deliver what they signed up for, then six short emails over two weeks.",
     "tree": {"trigger": {"kind": "form_submitted"}, "steps": [
         _e("Here's what you asked for", "Hi {{first_name}},\n\nThanks for signing up. Here's the guide you asked for: "
            "{{link:your-page}}\n\nOne thing to try first: pick the single tip that fits your week and start there.\n\n"
            "Talk soon,\n{{brand}}", "Day 0: deliver what was promised and give one next step."),
         _w(2),
         _e("The part most people skip", "Hi {{first_name}},\n\nA quick follow-up on the guide. The step people skip "
            "most is the simplest one: doing it at the same time every day. Set a reminder for tomorrow and see how it "
            "feels.\n\nReply and tell me how it goes.\n{{brand}}", "Day 2: go one level deeper on the topic."),
         _w(2),
         _e("Why this is harder than it looks", "Hi {{first_name}},\n\nIf it hasn't stuck yet, you're not alone. Most "
            "routines fail for one reason: they need too many decisions. Fewer choices, same time, same place.\n\n"
            "Tomorrow I'll show you how {{product}} fits that.\n{{brand}}", "Day 4: name the problem plainly."),
         _w(3),
         _e("How {{product}} fits in", "Hi {{first_name}},\n\n{{product}} is built to take a decision out of your day. "
            "Here's how people use it: {{link:your-page}}\n\nIf you have a question, just reply.\n{{brand}}",
            "Day 7: show how the product solves it."),
         {"type": "condition", "test": {"kind": "clicked", "node": ""},
          "yes": [{"type": "tag", "op": "add", "tag": "warm"}],
          "no": [_w(3), _e("A quick look at how it works", "Hi {{first_name}},\n\nHere's a two-minute look at how "
                           "{{product}} works, start to finish: {{link:your-page}}\n\n{{brand}}",
                           "Day 10: a how-it-works email for people who didn't click.")]},
         _w(4),
         _e("Ready when you are", "Hi {{first_name}},\n\nIf {{product}} sounds right for you, this is the place to get "
            "it: {{link:your-page}}\n\nThat's the last email in this series. Thanks for reading.\n{{brand}}",
            "Day 14: the offer, one clear link, and a clean ending."),
     ]}},
    {"id": "welcome", "name": "New customer welcome", "channels": ["email", "sms"],
     "line": "Welcome, a how-to, a check-in and a reorder reminder.",
     "tree": {"trigger": {"kind": "tag_added", "tag": "customer"}, "steps": [
         _e("Welcome, {{first_name}}", "Hi {{first_name}},\n\nThanks for your order. One tip to get the most from "
            "{{product}} on day one: keep it where you'll see it.\n\nAny questions, just reply.\n{{brand}}",
            "Day 0: welcome and one first-use tip."),
         _w(1),
         {"type": "condition", "test": {"kind": "has_consent", "channel": "sms"},
          "yes": [{"type": "sms", "body": "{{brand}}: your {{product}} should be with you soon. Questions? Reply here."}],
          "no": [_e("Has it arrived?", "Hi {{first_name}},\n\nJust checking your {{product}} arrived. Here's a short "
                    "how-to if you'd like one: {{link:your-page}}\n\n{{brand}}")]},
         _w(4),
         _e("How's it going?", "Hi {{first_name}},\n\nYou've had {{product}} for a few days. How is it going? If you "
            "have a minute, a short review helps other people decide: {{link:your-page}}\n\n{{brand}}",
            "Day 5: check in and ask for a review."),
         _w(9),
         _e("Running low?", "Hi {{first_name}},\n\nIf you're getting near the end of your {{product}}, you can reorder "
            "here: {{link:your-page}}\n\n{{brand}}", "Day 14: a reorder reminder."),
     ]}},
    {"id": "abandoned", "name": "Didn't finish the form", "channels": ["email"],
     "line": "For people who clicked through but didn't sign up: three reminders.",
     "tree": {"trigger": {"kind": "tag_added", "tag": "visited"}, "steps": [
         _w(1),
         {"type": "exit_if", "test": {"kind": "has_tag", "tag": "signed-up"}},
         _e("You were almost there", "Hi {{first_name}},\n\nYou started signing up for {{product}} and didn't get to "
            "the end. Here's the link if you'd like to finish: {{link:your-page}}\n\n{{brand}}",
            "Day 1: one reminder of what they'd get."),
         _w(2),
         {"type": "exit_if", "test": {"kind": "has_tag", "tag": "signed-up"}},
         _e("A question people ask", "Hi {{first_name}},\n\nThe question we hear most before people sign up is how "
            "much time it takes. The honest answer is on this page: {{link:your-page}}\n\n{{brand}}",
            "Day 3: answer the top question."),
         _w(4),
         {"type": "exit_if", "test": {"kind": "has_tag", "tag": "signed-up"}},
         _e("Last note from us", "Hi {{first_name}},\n\nThis is the last reminder. If now isn't the time, no problem. "
            "The link stays here: {{link:your-page}}\n\n{{brand}}", "Day 7: a last, low-pressure note."),
     ]}},
    {"id": "event", "name": "Launch or live event", "channels": ["email", "sms"],
     "line": "Announce, remind the day before, text two hours before, recap after.",
     "tree": {"trigger": {"kind": "date", "tag": "event"}, "steps": [
         _e("You're invited: {{product}} live", "Hi {{first_name}},\n\nWe're going live next week to show {{product}} "
            "and answer questions. Save your spot: {{link:your-page}}\n\n{{brand}}", "One week before: announce."),
         _w(6),
         _e("Tomorrow: {{product}} live", "Hi {{first_name}},\n\nA reminder that we're live tomorrow. Here's the link: "
            "{{link:your-page}}\n\n{{brand}}", "The day before: remind."),
         _w(0, 22),
         {"type": "condition", "test": {"kind": "has_consent", "channel": "sms"},
          "yes": [{"type": "sms", "body": "{{brand}}: we're live in 2 hours. Link: {{link:your-page}}"}], "no": []},
         _w(1),
         _e("The replay, and what's next", "Hi {{first_name}},\n\nThanks for being part of it. Here's the replay and a "
            "short summary: {{link:your-page}}\n\n{{brand}}", "The day after: replay and next step."),
     ]}},
    {"id": "reengage", "name": "Re-engage quiet contacts", "channels": ["email"],
     "line": "An honest check-in, what's new, and a one-click stay or leave.",
     "tree": {"trigger": {"kind": "date", "tag": "quiet"}, "steps": [
         _e("Still interested?", "Hi {{first_name}},\n\nIt's been a while. Is {{product}} still something you're "
            "thinking about? Either answer is fine; just reply.\n\n{{brand}}", "Day 0: an honest check-in."),
         _w(3),
         _e("What's new since you last looked", "Hi {{first_name}},\n\nA few things changed since you last heard from "
            "us. The short version is here: {{link:your-page}}\n\n{{brand}}", "Day 3: what's new."),
         _w(7),
         _e("Should we stop emailing you?", "Hi {{first_name}},\n\nWe don't want to crowd your inbox. If you'd like to "
            "keep hearing from us, click here: {{link:your-page}}. If not, there's an unsubscribe link below and we'll "
            "stop.\n\n{{brand}}", "Day 10: let them choose."),
         _w(4),
         {"type": "condition", "test": {"kind": "clicked", "node": ""},
          "yes": [{"type": "tag", "op": "add", "tag": "engaged"}],
          "no": [{"type": "tag", "op": "add", "tag": "cold"}, {"type": "stage", "stage": "cold"}]},
     ]}},
    {"id": "sms_welcome", "name": "Text welcome", "channels": ["sms"],
     "line": "For people who agreed to texts: a confirmation, one reminder, a final nudge.",
     "tree": {"trigger": {"kind": "form_submitted"}, "steps": [
         {"type": "sms", "body": "{{brand}}: thanks for signing up. Here's your link: {{link:your-page}}",
          "hint": "Immediately: confirm (the opt-out line is added automatically)."},
         _w(1),
         {"type": "sms", "body": "{{brand}}: a quick reminder, your link is here: {{link:your-page}}"},
         _w(6),
         {"type": "exit_if", "test": {"kind": "has_tag", "tag": "customer"}},
         {"type": "sms", "body": "{{brand}}: last note from us about {{product}}: {{link:your-page}}"},
     ]}},
]
TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}


def from_template(tid: str) -> dict:
    """A fresh copy of a template's tree with new ids, "clicked" tests pointing at the email just before them."""
    t = TEMPLATE_BY_ID[tid]
    tree = clean_tree(copy.deepcopy(t["tree"]))

    def fix(steps, last_email):
        for s in steps:
            if s["type"] == "email":
                last_email = s["id"]
            if s["type"] in ("condition", "exit_if") and s["test"]["kind"] in ("clicked", "opened") and not s["test"].get("node"):
                s["test"]["node"] = last_email or ""
            if s["type"] == "condition":
                fix(s["yes"], last_email)
                fix(s["no"], last_email)
        return last_email
    fix(tree["steps"], None)
    return tree


# ---------------------------------------------------------------- AI draft

DRAFT_STEP = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["email", "sms", "wait", "tag", "stage", "task", "notify", "exit_if"]},
        "subject": {"type": "string"}, "body": {"type": "string"},
        "days": {"type": "integer"}, "hours": {"type": "integer"},
        "tag": {"type": "string"}, "op": {"type": "string", "enum": ["add", "remove"]},
        "stage": {"type": "string", "enum": STAGES}, "title": {"type": "string"}, "message": {"type": "string"},
        "hint": {"type": "string", "description": "One line: why this step and this timing."},
    },
    "required": ["type"],
}
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "trigger": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(TRIGGERS)}, "tag": {"type": "string"}}, "required": ["kind"]},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": dict(DRAFT_STEP["properties"], type={"type": "string", "enum": [
                "email", "sms", "wait", "tag", "stage", "task", "notify", "exit_if", "condition"]},
                test_kind={"type": "string", "enum": ["has_tag", "clicked", "opened", "has_phone", "has_consent"]},
                test_tag={"type": "string"},
                yes={"type": "array", "items": DRAFT_STEP}, no={"type": "array", "items": DRAFT_STEP}),
            "required": ["type"]}},
    },
    "required": ["name", "trigger", "steps"],
}
DRAFT_TOKENS = (2500, 2500)


def draft_system(rules: str) -> str:
    return ("You design short, respectful follow-up sequences for small brands: what happens after someone fills in a "
            "form or becomes a customer. You output a flow as data.\n\n"
            "How to build it:\n"
            "- Start with what the person asked for or just did. One goal per message, one link or action per message.\n"
            "- Emails: a plain subject line (no fake 'Re:'), 50 to 125 words, short paragraphs, a friendly sign-off "
            "with {{brand}}. Use {{first_name}} for the greeting (it falls back to 'there'). Put links as "
            "{{link:your-page}}.\n"
            "- Timing: day 0 right away, then gaps of 1 to 4 days early on and longer later. Waits are whole days or "
            "hours. At most 12 messages. End the sequence cleanly; never loop.\n"
            "- Texts only if the user asked for texts. Texts are one line under 160 characters, start with {{brand}}:, "
            "and never include offers or urgency. The opt-out line is added automatically; don't write it.\n"
            "- For 'if they clicked' use a condition step with test_kind 'clicked' right after a wait that follows the "
            "email it depends on; put the follow-up steps in yes and no.\n"
            "- Give every step a one-line hint that explains why it's there and why at that time.\n\n" + rules)


def without(tree: dict, typ: str) -> dict:
    def keep(steps):
        out = []
        for s in steps:
            if s["type"] == typ:
                continue
            if s["type"] == "condition":
                s["yes"], s["no"] = keep(s["yes"]), keep(s["no"])
            out.append(s)
        return out
    tree["steps"] = keep(tree["steps"])
    return tree


def from_draft(d: dict) -> dict:
    """The AI's draft as a clean tree (conditions get their tests; 'clicked' points at the email before it)."""
    def conv(s):
        s = dict(s)
        if s.get("type") in ("condition", "exit_if"):
            s["test"] = {"kind": s.get("test_kind") or "has_tag", "tag": s.get("test_tag") or s.get("tag") or "",
                         "channel": "sms" if s.get("test_kind") == "has_consent" else "email"}
        if s.get("type") == "condition":
            s["yes"] = [conv(x) for x in s.get("yes") or [] if isinstance(x, dict)]
            s["no"] = [conv(x) for x in s.get("no") or [] if isinstance(x, dict)]
        return s
    raw = {"trigger": d.get("trigger") or {}, "steps": [conv(s) for s in d.get("steps") or [] if isinstance(s, dict)]}
    good = []
    for s in raw["steps"]:
        try:
            clean_step(s)
            good.append(s)
        except FlowError:
            continue
    tree = clean_tree({"trigger": raw["trigger"], "steps": good})

    def fix(steps, last_email):
        for s in steps:
            if s["type"] == "email":
                last_email = s["id"]
            if s["type"] in ("condition", "exit_if") and s["test"]["kind"] in ("clicked", "opened"):
                s["test"]["node"] = last_email or ""
            if s["type"] == "condition":
                fix(s["yes"], last_email)
                fix(s["no"], last_email)
        return last_email
    fix(tree["steps"], None)
    # Drop messages that break the rules rather than ship them.
    def keep(steps):
        out = []
        for s in steps:
            if s["type"] in ("email", "sms") and not compliance.check(s.get("subject") or "", s.get("body") or "")["ok"]:
                continue
            if s["type"] == "condition":
                s["yes"], s["no"] = keep(s["yes"]), keep(s["no"])
            out.append(s)
        return out
    tree["steps"] = keep(tree["steps"])
    # At most MAX_SENDS messages.
    n = 0

    def cap(steps):
        nonlocal n
        out = []
        for s in steps:
            if s["type"] in ("email", "sms"):
                n += 1
                if n > MAX_SENDS:
                    continue
            if s["type"] == "condition":
                s["yes"], s["no"] = cap(s["yes"]), cap(s["no"])
            out.append(s)
        return out
    tree["steps"] = cap(tree["steps"])
    return tree
