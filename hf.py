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


def estimate(model_id: str, inputs: dict) -> float | None:
    """The vendor's price in USD for these inputs (a free call), or None when
    it can't be had. Never raises."""
    try:
        r = requests.post(f"{BASE}/estimate/{model_id.strip('/')}", json=inputs, headers=_headers(), timeout=15)
        if r.status_code >= 400:
            return None
        return float((r.json() or {}).get("usd"))
    except (requests.RequestException, GenError, ValueError, TypeError):
        return None


def cancel(request_id: str) -> bool:
    r = requests.post(f"{BASE}/requests/{request_id}/cancel", headers=_headers(), timeout=TIMEOUT)
    return r.status_code == 202
