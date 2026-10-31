"""Thin client for the generation API that powers Campaignify.

The vendor is never named in the product UI; this module is the only place
that knows it. API shape (from the vendor's official SDKs):

    POST {BASE}/{model_id}                 JSON body = the model's inputs
         -> {request_id, status, status_url, cancel_url}
    GET  {BASE}/requests/{id}/status        -> {status, images:[{url}], video:{url}}
         status: queued | in_progress | completed | failed | nsfw | canceled
    POST {BASE}/files/generate-upload-url  {content_type}
         -> {public_url, upload_url, upload_headers?}; then PUT the bytes

Auth header: "Key <api_key>:<api_secret>" (one HF_KEY or HF_API_KEY + HF_API_SECRET).
Failed and nsfw results are not charged by the vendor.
"""
import os
import re

import requests

BASE = os.environ.get("HF_BASE_URL", "https://api.higgsfield.ai").rstrip("/")
TIMEOUT = 60

FINAL = {"completed", "failed", "nsfw", "canceled", "cancelled"}


class GenError(RuntimeError):
    pass


def credential() -> str | None:
    """HF_KEY ("key:secret"), or HIGGSFIELD_API_KEY (the deploy secret's name;
    "key:secret" too, with HIGGSFIELD_API_SECRET when the halves are separate),
    or HF_API_KEY + HF_API_SECRET."""
    for name, secret in (("HF_KEY", None), ("HIGGSFIELD_API_KEY", "HIGGSFIELD_API_SECRET"),
                         ("HF_API_KEY", "HF_API_SECRET")):
        key = (os.environ.get(name) or "").strip()
        if not key:
            continue
        sec = (os.environ.get(secret) or "").strip() if secret else ""
        if sec and ":" not in key:
            return f"{key}:{sec}"
        if ":" in key or name == "HIGGSFIELD_API_KEY":
            return key
    return None


def configured() -> bool:
    return bool(credential())


_probe = {"at": 0.0, "result": None}


def probe(max_age: float = 600) -> str:
    """Is the key accepted? One free call (asking for an upload URL costs
    nothing), cached. ok | rejected | out_of_credit | unreachable | unconfigured."""
    import time
    if not configured():
        return "unconfigured"
    if _probe["result"] and time.time() - _probe["at"] < max_age:
        return _probe["result"]
    try:
        r = requests.post(f"{BASE}/files/generate-upload-url", json={"content_type": "image/png"},
                          headers=_headers(), timeout=10)
        res = ("rejected" if r.status_code in (401, 403) else "out_of_credit" if r.status_code == 402
               else "ok" if r.status_code < 400 else f"error {r.status_code}")
    except requests.RequestException:
        res = "unreachable"
    _probe.update(at=time.time(), result=res)
    return res


def _headers():
    cred = credential()
    if not cred:
        raise GenError("The generation service is not configured yet (no API key).")
    return {"Authorization": f"Key {cred}", "Content-Type": "application/json",
            "User-Agent": "campaignify/1.0"}


def _check(r):
    if r.status_code >= 400:
        try:
            j = r.json()
            msg = j.get("detail") or j.get("message") or j.get("error") or r.text
        except ValueError:
            msg = r.text
        if r.status_code == 401:
            msg = "The generation service rejected our API key."
        elif r.status_code == 403:
            # Higgsfield answers 403 both for a bad key and for an API account with no
            # billing credit, so say both and keep the vendor's own words for the log.
            msg = ("The generation service refused the request (API key or API billing credit). "
                   f"Vendor said: {str(msg)[:160]}")
        elif r.status_code == 402:
            msg = "The generation account is out of credit."
        raise GenError(f"{r.status_code}: {str(msg)[:300]}")
    return r


def submit(model_id: str, inputs: dict, webhook_url: str | None = None) -> str:
    url = f"{BASE}/{model_id.strip('/')}"
    params = {"hf_webhook": webhook_url} if webhook_url else None
    r = _check(requests.post(url, json=inputs, params=params, headers=_headers(), timeout=TIMEOUT))
    data = r.json()
    rid = data.get("request_id")
    if not rid:
        raise GenError("No request id in the response.")
    return rid


def status(request_id: str) -> dict:
    r = _check(requests.get(f"{BASE}/requests/{request_id}/status", headers=_headers(), timeout=TIMEOUT))
    return r.json()


def outputs(result: dict) -> list[dict]:
    """[{kind: image|video, url}] from a completed result."""
    out = []
    for im in result.get("images") or []:
        if isinstance(im, dict) and im.get("url"):
            out.append({"kind": "image", "url": im["url"]})
    vid = result.get("video")
    if isinstance(vid, dict) and vid.get("url"):
        out.append({"kind": "video", "url": vid["url"]})
    for v in result.get("videos") or []:
        if isinstance(v, dict) and v.get("url"):
            out.append({"kind": "video", "url": v["url"]})
    return out


def upload(data: bytes, content_type: str) -> str:
    """Upload bytes; returns a public URL the models can read."""
    r = _check(requests.post(f"{BASE}/files/generate-upload-url", json={"content_type": content_type},
                             headers=_headers(), timeout=TIMEOUT))
    j = r.json()
    headers = j.get("upload_headers") or {"Content-Type": content_type}
    put = requests.put(j["upload_url"], data=data, headers=headers, timeout=300)
    if put.status_code >= 400:
        raise GenError(f"upload failed: {put.status_code}")
    return j["public_url"]


# ---------------------------------------------------------------- price estimates
#
# POST {BASE}/estimate/{model_id} with the model's inputs is free. It used to answer
# {"usd": 4.622}; it now answers with a pricing *description* as well or instead (plain
# text, a JSON string, or JSON whose fields hold text such as "$4.62" or
# "Estimated cost: 4.6220 USD ($0.4622/s x 10s)"). parse_usd() reads all of these and
# only ever returns US dollars: a bare credit count is never taken as dollars.

_MONEY = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
_AMOUNT_RE = re.compile(r"(?:US\s?\$|\$\s?|USD\s*)" + _MONEY + r"|" + _MONEY + r"\s*(?:USD|usd|dollars?)\b", re.I)
_RATE_RE = re.compile(r"^\s*(?:USD\b\s*)?(?:/|per\b|an?\s+(?:second|sec|image|generation)\b|each\b|x\b|×)", re.I)
_RATE_UNIT_RE = re.compile(r"^\s*(?:USD\b\s*)?(?:/|per\s+|an?\s+)\s*(s|sec|secs|second|seconds|image|images|generation|"
                           r"generations|output|outputs|video|videos)\b", re.I)
_TOTAL_RE = re.compile(r"(total|estimated|estimate|est\.|cost|price|charge|≈|~|=)\W{0,12}$", re.I)
_USD_KEYS = ("usd", "cost_usd", "price_usd", "total_usd", "estimated_usd", "estimate_usd", "usd_total", "amount_usd")
_PLAIN_KEYS = ("total", "estimated_cost", "estimate", "cost", "price", "amount", "total_cost", "estimated_price")
_NEST_KEYS = ("estimate", "pricing", "price", "cost", "data", "result", "quote")
_TEXT_KEYS = ("description", "pricing", "text", "message", "detail", "summary", "estimate", "price", "cost")


def _num(v):
    """A dollar figure from a JSON value: 4.62, "4.6220", "$4.62", "4.62 USD"; else None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v >= 0 else None
    if isinstance(v, str):
        t = v.strip()
        if re.fullmatch(r"\$?\s*" + _MONEY + r"\s*(?:USD|usd)?", t):
            return float(re.sub(r"[^\d.]", "", t))
    return None


def _units(unit: str, inputs: dict | None) -> float | None:
    inputs = inputs or {}
    unit = unit.lower()
    if unit.startswith(("s", "sec", "second")):
        d = inputs.get("duration")
        try:
            return float(d) if d else None
        except (TypeError, ValueError):
            return None
    try:
        return float(inputs.get("batch_size") or 1)
    except (TypeError, ValueError):
        return 1.0


def _pick(found: list, res: str):
    """One amount from several: the one next to our resolution ("720p: $4.62"),
    else None — a guess could refuse a paying member's video."""
    if len(found) == 1:
        return found[0]
    if res:
        # the label before the amount ("720p: $4.62") or after it ("$4.62 at 720p"); if
        # the two readings disagree the text is ambiguous
        picks = {tuple(near[0][:2]) for near in ([f for f in found if res in f[side]] for side in (-1, -2))
                 if len(near) == 1}
        if len(picks) == 1:
            return picks.pop()
    if len({f[0] for f in found}) == 1:
        return found[0]
    return None


def parse_text_usd(text: str, inputs: dict | None = None) -> float | None:
    """Dollars from a pricing description, or None. A total ("Total: $4.62",
    "= $4.62") wins; then a single flat amount; a per-second or per-image rate
    alone is multiplied out by the inputs (duration, batch_size). Several amounts
    (a price list by resolution) count only when one sits next to our resolution."""
    if not text:
        return None
    res = str((inputs or {}).get("resolution") or "").lower()
    flats, totals, rates = [], [], []
    ms = list(_AMOUNT_RE.finditer(text))
    for i, m in enumerate(ms):
        raw = m.group(1) or m.group(2)
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        after, before = text[m.end():m.end() + 24], text[max(0, m.start() - 24):m.start()]
        # this amount's own labels: back to the previous amount, on to the next one or a separator
        lab_b = text[ms[i - 1].end() if i else 0:m.start()].lower()
        lab_b = re.split(r"[,;\n]", lab_b)[-1]
        lab_a = re.split(r"[,;\n(]", text[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(text)].lower())[0]
        ctx = (lab_a, lab_b)
        unit = _RATE_UNIT_RE.match(after)
        if unit:
            rates.append((val, unit.group(1), *ctx))
        elif _RATE_RE.match(after) and not re.match(r"^\s*(?:USD\b\s*)?=", after):
            # "$0.46 x 10s": a factor in a sum; the result after "=" is what counts
            rates.append((val, "s" if re.search(r"\bs(?:ec|econds?)?\b", after) else "generation", *ctx))
        elif _TOTAL_RE.search(before) and re.search(r"(total|=|≈)", before, re.I):
            totals.append(val)
        else:
            flats.append((val, *ctx))
    if totals:
        return totals[-1]
    if flats:
        f = _pick(flats, res)
        return f[0] if f else None
    r = _pick(rates, res)
    if r:
        n = _units(r[1], inputs)
        if n:
            return round(r[0] * n, 6)
    return None


def parse_usd(body, inputs: dict | None = None) -> tuple[float | None, str]:
    """(USD or None, how): read the estimate endpoint's reply, whatever shape it
    takes. `body` is the decoded JSON (dict, list, str, number) or the raw text."""
    if isinstance(body, (int, float)) and not isinstance(body, bool):
        return (float(body), "number") if body >= 0 else (None, "negative number")
    if isinstance(body, str):
        t = body.strip()
        if t[:1] in "{[\"":
            try:
                import json
                return parse_usd(json.loads(t), inputs)
            except ValueError:
                pass
        v = _num(t)
        if v is not None:
            return v, "number"
        v = parse_text_usd(t, inputs)
        return (v, "text") if v is not None else (None, "no single clear dollar amount in the text")
    if isinstance(body, list):
        for item in body[:5]:
            v, how = parse_usd(item, inputs)
            if v is not None:
                return v, how
        return None, "no dollar amount in the list"
    if not isinstance(body, dict):
        return None, "empty reply"
    cur = str(body.get("currency") or "usd").strip().lower()
    for k in _USD_KEYS:
        v = _num(body.get(k))
        if v is not None:
            return v, f"field {k}"
    if cur in ("usd", "$", "us$"):
        for k in _PLAIN_KEYS:
            v = _num(body.get(k))
            if v is not None:
                return v, f"field {k}"
        q, up = _num(body.get("quantity")), _num(body.get("unit_price"))
        if q is not None and up is not None:
            return round(q * up, 6), "unit_price x quantity"
    for k in _NEST_KEYS:
        if isinstance(body.get(k), (dict, list)):
            v, how = parse_usd(body[k], inputs)
            if v is not None:
                return v, f"{k}.{how}"
    for k in _TEXT_KEYS:
        if isinstance(body.get(k), str):
            v = parse_text_usd(body[k], inputs)
            if v is not None:
                return v, f"text in {k}"
    return None, "no dollar amount in the reply"


# What the last estimate call got back (the admin health card shows it).
last_estimate = {"at": None, "ok": None, "why": None, "sample": None, "how": None}


def estimate_detail(model_id: str, inputs: dict) -> tuple[float | None, str | None]:
    """(USD, None) or (None, why). Never raises; the admin price check shows the why."""
    import time
    usd, why, sample, how = None, None, None, None
    try:
        r = requests.post(f"{BASE}/estimate/{model_id.strip('/')}", json=inputs, headers=_headers(), timeout=15)
        sample = (r.text or "")[:300]
        if r.status_code >= 400:
            why = f"{r.status_code}: {sample[:200]}"
        else:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            usd, how = parse_usd(body, inputs)
            if usd is None:
                why = f"{how}; reply: {sample[:200]}"
    except (requests.RequestException, GenError) as e:
        why = str(e)[:200]
    except Exception as e:  # noqa: BLE001 — an estimate never breaks generation
        why = f"unreadable reply ({type(e).__name__}): {(sample or '')[:160]}"
    last_estimate.update(at=time.time(), ok=usd is not None, why=why, sample=sample, how=how, model=model_id)
    return usd, why


def estimate(model_id: str, inputs: dict) -> float | None:
    """The vendor's price in USD for these inputs (a free call), or None when
    it can't be had or read. Never raises."""
    return estimate_detail(model_id, inputs)[0]


def cancel(request_id: str) -> bool:
    r = requests.post(f"{BASE}/requests/{request_id}/cancel", headers=_headers(), timeout=TIMEOUT)
    return r.status_code == 202
