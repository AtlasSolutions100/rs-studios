"""Forms: field definitions, show-if logic, validation, spam guards and
outgoing webhooks.

Public pages run no scripts, so show-if logic is done in CSS (`:has()` on the
controlling field) and enforced again on the server: a hidden field is never
required and its value is dropped. Submissions carry a signed timestamp (too
fast or too old is refused), a honeypot field, size limits and per-IP rate
limits. Webhooks go only to public https addresses (fetcher's checks), signed
with HMAC-SHA256.
"""
import hashlib
import hmac
import json
import re
import threading
import time

import urllib3

import fetcher

FIELD_TYPES = {"text": "Short text", "email": "Email", "phone": "Phone", "textarea": "Long text",
               "select": "Dropdown", "checkbox": "Checkbox"}
MAX_FIELDS = 15
EMAIL_RX = re.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
PHONE_RX = re.compile(r"^[+0-9() .-]{6,24}$")
MIN_SECONDS, MAX_AGE = 2, 86400


def key_for(label: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "field").lower()).strip("_")[:24] or "field"
    k, n = base, 2
    while k in taken:
        k = f"{base}_{n}"
        n += 1
    return k


def clean_fields(raw: list[dict]) -> list[dict]:
    """Validate the editor's field list."""
    out, taken = [], set()
    for f in raw[:MAX_FIELDS]:
        label = str(f.get("label") or "").strip()[:120]
        ftype = f.get("type") if f.get("type") in FIELD_TYPES else "text"
        if not label:
            continue
        key = f.get("key") if re.match(r"^[a-z0-9_]{1,30}$", str(f.get("key") or "")) and f.get("key") not in taken \
            else key_for(label, taken)
        taken.add(key)
        opts = [o.strip()[:80] for o in str(f.get("options") or "").splitlines() if o.strip()][:20] \
            if ftype == "select" else []
        out.append({"key": key, "label": label, "type": ftype, "required": bool(f.get("required")), "options": opts,
                    "show_if": None})
        cond_field, cond_val = str(f.get("show_if_field") or ""), str(f.get("show_if_value") or "").strip()[:80]
        if cond_field:
            out[-1]["show_if"] = {"field": cond_field, "equals": cond_val}
    keys = {f["key"]: f for f in out}
    for i, f in enumerate(out):
        c = f["show_if"]
        # Only earlier dropdowns or checkboxes can control a field.
        ctrl = keys.get(c["field"]) if c else None
        if not c or not ctrl or out.index(ctrl) >= i or ctrl["type"] not in ("select", "checkbox") or \
                (ctrl["type"] == "select" and c["equals"] not in ctrl["options"]):
            f["show_if"] = None
        elif ctrl["type"] == "checkbox":
            f["show_if"]["equals"] = "yes"
    return out


def visible(field: dict, values: dict) -> bool:
    c = field.get("show_if")
    if not c:
        return True
    return values.get(c["field"]) == c["equals"]


def validate(fields: list[dict], form) -> tuple[dict, list[str]]:
    """(clean values, errors) from submitted form data."""
    values, errors = {}, []
    for f in fields:
        if f["type"] == "checkbox":
            values[f["key"]] = "yes" if form.get(f"f_{f['key']}") in ("yes", "on", "1") else ""
        else:
            values[f["key"]] = str(form.get(f"f_{f['key']}") or "").strip()[:4000 if f["type"] == "textarea" else 300]
            if f["type"] == "email":
                values[f["key"]] = values[f["key"]].lower()
    for f in fields:
        v = values[f["key"]]
        if not visible(f, values):
            values[f["key"]] = ""
            continue
        if f["required"] and not v:
            errors.append(f"{f['label']} is required.")
        elif v and f["type"] == "email" and not EMAIL_RX.match(v):
            errors.append(f"{f['label']} doesn't look like an email address.")
        elif v and f["type"] == "phone" and not PHONE_RX.match(v):
            errors.append(f"{f['label']} doesn't look like a phone number.")
        elif v and f["type"] == "select" and v not in f["options"]:
            errors.append(f"Pick one of the options for {f['label']}.")
    return {k: v for k, v in values.items() if v}, errors


def logic_css(form_id: str, fields: list[dict]) -> str:
    """CSS that shows conditional fields when their condition is met."""
    rules = []
    for f in fields:
        c = f.get("show_if")
        if not c:
            continue
        sel = f"#{form_id} .fld-{f['key']}"
        rules.append(f"{sel}{{display:none}}")
        ctrl = f"[name=f_{c['field']}]"
        if c["equals"] == "yes":
            cond = f"{ctrl}:checked"
        else:
            val = c["equals"].replace("\\", "\\\\").replace('"', '\\"')
            cond = f'{ctrl} option[value="{val}"]:checked'
        rules.append(f"#{form_id}:has({cond}) .fld-{f['key']}{{display:block}}")
    return "\n".join(rules)


# ---------------------------------------------------------------- spam guard

def stamp(secret: str, code: str, now: float | None = None) -> str:
    t = str(int(now if now is not None else time.time()))
    mac = hmac.new(secret.encode(), f"{code}.{t}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{t}.{mac}"


def stamp_ok(secret: str, code: str, value: str, now: float | None = None) -> bool:
    try:
        t, mac = (value or "").split(".", 1)
        ti = int(t)
    except ValueError:
        return False
    good = hmac.new(secret.encode(), f"{code}.{t}".encode(), hashlib.sha256).hexdigest()[:24]
    age = (now if now is not None else time.time()) - ti
    return hmac.compare_digest(good, mac) and MIN_SECONDS <= age <= MAX_AGE


# ---------------------------------------------------------------- webhooks

def webhook_url_ok(url: str) -> str | None:
    """None when the URL is acceptable, else the reason."""
    try:
        scheme, host, port, _, _ = fetcher.check_url(url)
        if scheme != "https":
            return "Webhooks must use https."
        fetcher.resolve(host, port)
    except fetcher.FetchError as e:
        return str(e)
    return None


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(url: str, secret: str, payload: dict) -> str:
    """POST the JSON payload to a public https URL (IP checked and pinned,
    no redirects). Returns a short status string."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        scheme, host, port, path, _ = fetcher.check_url(url)
        if scheme != "https":
            return "refused: not https"
        ip = fetcher.resolve(host, port)
        pool = fetcher._open(scheme, host, port, ip)
        r = pool.urlopen("POST", path, body=body, redirect=False, retries=False,
                         headers={"Host": host, "Content-Type": "application/json", "User-Agent": fetcher.UA,
                                  "X-Campaignify-Event": payload.get("event", "form.submitted"),
                                  "X-Campaignify-Signature": sign(secret, body)})
        status = r.status
        r.release_conn()
        pool.close()
        return f"{status}"
    except fetcher.FetchError as e:
        return f"refused: {e}"[:120]
    except (urllib3.exceptions.HTTPError, OSError) as e:
        return f"failed: {e.__class__.__name__}"


def send_async(url, secret, payload, done=None, sync=False):
    def run():
        st = post_webhook(url, secret, payload)
        if done:
            done(st)
    if sync:
        run()
    else:
        threading.Thread(target=run, name="webhook", daemon=True).start()
