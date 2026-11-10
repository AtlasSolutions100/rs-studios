"""Thin client for the generation API that powers Campaignify.

The vendor is never named in the product UI; this module is the only place
that knows it. API shape (from the vendor's official SDKs):

    POST {BASE}/{model_id}                 JSON body = the model's inputs
         -> {request_id, status, status_url, cancel_url}
    GET  {BASE}/requests/{id}/status        -> {status, images:[{url}], video:{url}}
         status: queued | in_progress | completed | failed | nsfw | canceled
    POST {BASE}/files/generate-upload-url  {content_type}
         -> {public_url, upload_url, upload_headers?}; then PUT the bytes

    POST {BASE}/requests/{id}/cancel        202 canceled | 400 already started | 404 unknown

Models in use (catalog.TOOLS and catalog.INTERNAL_TOOLS hold the ids). Reference-to-video (talking videos) takes
`image_urls`, `video_urls` and `audio_urls`: the worker sends product photos, and behind PRESENTER_STILLS the
presenter's approved still first, behind PRESENTER_VOICE_REF their voice sample as audio_urls[0]. The image model
of the library (marketing-studio/image/sunburst) takes `prompt` and, to edit from references, `image_urls`.

Auth header: "Key <api_key>:<api_secret>" (one HF_KEY or HF_API_KEY + HF_API_SECRET).
Failed and nsfw results are not charged by the vendor.

Submits carry an Idempotency-Key: sending the same key again returns the first
request_id and is not charged twice (vendor docs, concepts/idempotency), so a
submit is retried on 408/425/429/5xx, timeouts and dropped connections with the
same key. HF_RETRY=0 turns the retries off (one try, as before).
"""
import os
import random
import re
import time
import uuid

import requests

BASE = os.environ.get("HF_BASE_URL", "https://api.higgsfield.ai").rstrip("/")
TIMEOUT = 60

FINAL = {"completed", "failed", "nsfw", "canceled", "cancelled"}


class GenError(RuntimeError):
    pass


class Transient(GenError):
    """The vendor was busy or briefly unreachable on every try (429, 5xx, a timeout).
    Nothing is known to have been created: the same submit may be sent again later
    with the same idempotency key."""

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
TRIES = 3
BACKOFF_SECONDS = float(os.environ.get("HF_RETRY_BASE_SECONDS", "1"))
RETRY_AFTER_MAX = 10.0      # a longer Retry-After is left to the worker's own (job-level) backoff
_sleep = time.sleep         # the tests swap this out


def retries_on() -> bool:
    return os.environ.get("HF_RETRY", "1") != "0"


def _backoff(i: int) -> float:
    """Exponential with jitter: about 1, 2, 4 s times 0.5 to 1."""
    return BACKOFF_SECONDS * (2 ** i) * (0.5 + random.random() / 2)


def _retry_after(r) -> float | None:
    try:
        v = float((getattr(r, "headers", None) or {}).get("Retry-After"))
        return max(0.0, v)
    except (TypeError, ValueError):
        return None


def _busy_text(code: int) -> str:
    if code == 429:
        return "429: The generation service is busy right now. Try again in a few minutes."
    return f"{code}: The generation service had a problem on its side. Try again in a few minutes."


def _send(method: str, url: str, tries: int = TRIES, check: bool = True, **kw):
    """One vendor call, tried up to `tries` times on 408/425/429/5xx, timeouts and
    dropped connections, waiting with backoff (or Retry-After, up to RETRY_AFTER_MAX)
    in between. Returns the response (through _check unless check=False); raises
    Transient when every try was a transient failure."""
    last, wait = "The generation service didn't answer.", None
    for i in range(max(1, tries)):
        try:
            r = getattr(requests, method)(url, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            last, wait = f"The generation service didn't answer ({type(e).__name__}). Try again in a few minutes.", None
        else:
            if r.status_code not in RETRY_STATUSES:
                return _check(r) if check else r
            last, wait = _busy_text(r.status_code), _retry_after(r)
        if i + 1 >= tries or (wait is not None and wait > RETRY_AFTER_MAX):
            break
        _sleep(wait if wait is not None else _backoff(i))
    raise Transient(last, retry_after=wait)


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


def new_key() -> str:
    return uuid.uuid4().hex


def submit(model_id: str, inputs: dict, webhook_url: str | None = None, idempotency_key: str | None = None) -> str:
    """Start a generation; returns the vendor's request id. Pass the same
    `idempotency_key` (stored with the job before calling) when a submit is
    resumed, so the vendor hands back the first request instead of a second one.
    Raises Transient (try again later, same key) or GenError (final)."""
    url = f"{BASE}/{model_id.strip('/')}"
    params = {"hf_webhook": webhook_url} if webhook_url else None
    headers = dict(_headers(), **{"Idempotency-Key": idempotency_key or new_key()})
    if retries_on():
        r = _send("post", url, json=inputs, params=params, headers=headers, timeout=TIMEOUT)
    else:
        r = _check(requests.post(url, json=inputs, params=params, headers=headers, timeout=TIMEOUT))
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


def refusal(result) -> str:
    """Why the vendor refused or failed a request, in its own words, when its status reply says so ("" when it
    doesn't). For our own admin pages and logs only: members see our plain message, never the vendor's."""
    if not isinstance(result, dict):
        return ""
    for k in ("error", "detail", "message", "reason", "failure_reason", "nsfw_reason", "status_message", "moderation"):
        v = result.get(k)
        if isinstance(v, dict):
            v = next((v[x] for x in ("message", "detail", "reason", "code", "category") if v.get(x)), None)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v[:6] if isinstance(x, (str, int, float)))
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
            return " ".join(str(v).split())[:300]
    return ""


def upload(data: bytes, content_type: str) -> str:
    """Upload bytes; returns a public URL the models can read."""
    url, body = f"{BASE}/files/generate-upload-url", {"content_type": content_type}
    if retries_on():
        r = _send("post", url, json=body, headers=_headers(), timeout=TIMEOUT)
    else:
        r = _check(requests.post(url, json=body, headers=_headers(), timeout=TIMEOUT))
    j = r.json()
    headers = j.get("upload_headers") or {"Content-Type": content_type}
    put = requests.put(j["upload_url"], data=data, headers=headers, timeout=300)
    if put.status_code >= 400:
        raise GenError(f"upload failed: {put.status_code}")
    return j["public_url"]


# ---------------------------------------------------------------- the vendor's own presets (read only, free)
#
# The vendor can run some of ITS Marketing Studio presets by id through this API: POST {BASE}/marketing-studio/image
# with enhance_prompt=true, a preset_id and one product image (10% dearer than a plain request). The ids are per
# account and change, and its docs say to read them from the catalog below at run time and never to hard-code them
# (open.higgsfield.ai, "Marketing Studio Image" API reference, read 2026-11-09). Nothing in Campaignify sends a
# preset_id yet: presets.Preset.vendor_preset is the slot for one, and this call is how an admin sees what the
# account has (the library admin page).

def vendor_presets(size: int = 50, cursor: str | None = None) -> dict:
    """One page of the vendor's preset catalog for our account: {"items": [{id, type, name}], "cursor", "total"}.
    A free GET. Raises GenError when the vendor refuses or can't be reached."""
    params = {"size": max(1, min(100, int(size or 50)))}
    if cursor:
        params["cursor"] = cursor
    try:
        r = _check(requests.get(f"{BASE}/marketing-studio/image/presets", params=params, headers=_headers(),
                                timeout=20))
        j = r.json()
    except requests.RequestException as e:
        raise GenError(f"The generation service didn't answer ({type(e).__name__}).") from e
    except ValueError as e:
        raise GenError("The preset catalog couldn't be read.") from e
    items = [{"id": str(i.get("id") or ""), "type": str(i.get("type") or ""), "name": str(i.get("name") or "")}
             for i in (j.get("items") or []) if isinstance(i, dict) and i.get("id")]
    return {"items": items, "cursor": j.get("cursor"), "total": j.get("total")}


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


CANCELED, STARTED, GONE = "canceled", "started", "gone"
DELETED, BUSY = "deleted", "busy"


def delete_request(request_id: str) -> str:
    """Delete a finished request's data at the vendor: its input and output files, prompt and parameters, right away
    (DELETE /requests/{id}; docs.higgsfield.ai "Delete request data", read 2026-11-09; billing records stay there).
    Returns "deleted" (200), "gone" (404: no such request, or already expired), "busy" (409: still queued or in
    progress) or "error: ..." (anything else, after the usual retries). Idempotent. Never raises (workspace
    deletion, wsdelete.py, M7.5)."""
    try:
        r = _send("delete", f"{BASE}/requests/{request_id}", tries=TRIES if retries_on() else 1, check=False,
                  headers=_headers(), timeout=TIMEOUT)
    except GenError as e:
        return f"error: {str(e)[:120]}"
    except requests.RequestException as e:
        return f"error: {type(e).__name__}"
    if r.status_code == 200:
        return DELETED
    if r.status_code == 404:
        return GONE
    if r.status_code == 409:
        return BUSY
    return f"error: {r.status_code}"


def cancel(request_id: str) -> str:
    """Ask the vendor to drop a request. The vendor only cancels one that hasn't
    started. Returns "canceled" (202: dropped, never billed), "started" (400: it's
    being made and will finish and bill), "gone" (404: the vendor has no such
    request) or "error: ..." (anything else, after the usual retries). Never raises."""
    try:
        r = _send("post", f"{BASE}/requests/{request_id}/cancel", tries=TRIES if retries_on() else 1, check=False,
                  headers=_headers(), timeout=TIMEOUT)
    except GenError as e:
        return f"error: {str(e)[:120]}"
    except requests.RequestException as e:
        return f"error: {type(e).__name__}"
    if r.status_code == 202:
        return CANCELED
    if r.status_code == 400:
        return STARTED
    if r.status_code == 404:
        return GONE
    return f"error: {r.status_code}"
