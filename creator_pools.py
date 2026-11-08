"""Creator Pools: a brand funds a pool that Crowdcut clippers earn from.

A workspace sets up a campaign (brief, allowed content modes, assets or links,
platforms, rate per 1,000 views, caps, minimum views, run length, pool size),
pays once with Stripe Checkout (line 1 the flat fee, line 2 the pool; metadata
app=campaignify kind=creator_pool), and on the paid webhook the pool is pushed
to Crowdcut over the signed partner API. Crowdcut clippers (independent
creators) make and post the videos and submit the posts there. The brand
approves or rejects each post here for brand fit; Crowdcut then checks the
views and pays approved creators by hand. Nothing here pays creators.

Money:
    fee      CREATOR_POOL_FEE_USD (default 99, Koby to confirm), kept.
    pool     at least CREATOR_POOL_MIN_USD (default 200), at most
             CREATOR_POOL_MAX_USD (default 10000). 100% goes to creators;
             what is left when the pool ends is refunded to the brand. The
             refund is a record (creator_pool_refunds) that an admin settles
             from /admin/creator-pools (Refund via Stripe, or Mark refunded),
             never automatically.

Feature flag: CREATOR_POOLS=on shows it to every workspace; anything else
(default off) shows it only to admins and workspaces with an admin member.
Without POOLS_PARTNER_SECRET (shared with Crowdcut) pools can't be paid for.

The partner API (both directions) is signed exactly like Crowdcut's
partner.py: X-Pools-Timestamp + X-Pools-Signature v1=HMAC-SHA256 of
"<ts>\\n<METHOD>\\n<path?query>\\n<sha256 of body>", 5-minute window.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

import db

log = logging.getLogger("campaignify.creator_pools")

MODES = ("assets", "links", "ugc")
MODE_LABELS = {"assets": "Creators cut and post my videos", "links": "Creators clip my livestreams, podcasts or videos",
               "ugc": "Creators film their own video about the product"}
PLATFORMS = {"tiktok": "TikTok", "shorts": "YouTube Shorts", "reels": "Instagram Reels"}
RATE_RANGE = (1.0, 5.0)
DAYS_RANGE = (7, 60)
DEFAULT_MIN_VIEWS = 1000
REFUND_DAYS = 14
ASSET_URL_TTL = 24 * 3600
TOLERANCE = 300
HEADER_TS, HEADER_SIG = "X-Pools-Timestamp", "X-Pools-Signature"
STATUS_TEXT = {"draft": "Not paid yet", "checkout": "Waiting for payment", "paid": "Paid, going live",
               "live": "Live", "paused": "Paused", "ended": "Ended"}


class Refused(ValueError):
    """Why a pool can't be set up or changed (shown as is)."""


# ---------------------------------------------------------------- settings

def _usd(name, default) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def fee_cents() -> int:
    """The flat fee per campaign (CREATOR_POOL_FEE_USD; $99 until Koby confirms)."""
    return int(round(_usd("CREATOR_POOL_FEE_USD", 99) * 100))


def min_pool_cents() -> int:
    return int(round(_usd("CREATOR_POOL_MIN_USD", 200) * 100))


def max_pool_cents() -> int:
    return int(round(_usd("CREATOR_POOL_MAX_USD", 10000) * 100))


def secrets_() -> list[str]:
    return [s.strip() for s in (os.environ.get("POOLS_PARTNER_SECRET") or "").split(",") if s.strip()]


def connected() -> bool:
    return bool(secrets_())


def public() -> bool:
    return (os.environ.get("CREATOR_POOLS") or "off").strip().lower() == "on"


def operator_ws(conn, ws_id) -> bool:
    return bool(conn.execute("SELECT 1 FROM users WHERE workspace_id=? AND is_admin=1 LIMIT 1", (ws_id,)).fetchone())


def visible(conn, ws, user) -> bool:
    """Every workspace once CREATOR_POOLS=on (and the link is set up); else
    admins and the operator's own workspaces, for testing."""
    if user is not None and user["is_admin"]:
        return True
    if ws is None:
        return False
    if public() and connected():
        return True
    return operator_ws(conn, ws["id"])


def crowdcut_url() -> str:
    return (os.environ.get("CROWDCUT_URL") or "https://crowdcut.studio").rstrip("/")


def money(c) -> str:
    c = int(c or 0)
    return f"${c // 100:,}" if c % 100 == 0 else f"${c / 100:,.2f}"


def ext_id(pool_id) -> str:
    return f"cfy-{int(pool_id)}"


def pool_id_of(ext) -> int | None:
    m = re.match(r"^cfy-(\d+)$", str(ext or ""))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- signing

def _canonical(ts, method, path, body: bytes) -> bytes:
    return f"{ts}\n{method.upper()}\n{path}\n{hashlib.sha256(body or b'').hexdigest()}".encode()


def sign(secret: str, method: str, path: str, body: bytes = b"", ts: int | None = None) -> dict:
    ts = str(int(ts if ts is not None else time.time()))
    return {HEADER_TS: ts, HEADER_SIG: "v1=" + hmac.new(secret.encode(), _canonical(ts, method, path, body),
                                                         hashlib.sha256).hexdigest()}


def verify(method, path, body: bytes, headers, now: float | None = None) -> bool:
    ts, sig = headers.get(HEADER_TS) or "", (headers.get(HEADER_SIG) or "").strip()
    if not connected() or not ts.isdigit() or not sig.startswith("v1="):
        return False
    if abs((now if now is not None else time.time()) - int(ts)) > TOLERANCE:
        return False
    return any(hmac.compare_digest(hmac.new(s.encode(), _canonical(ts, method, path, body), hashlib.sha256).hexdigest(),
                                   sig[3:]) for s in secrets_())


def verify_request(req) -> bool:
    path = req.full_path.rstrip("?") if req.query_string else req.path
    return verify(req.method, path, req.get_data(cache=True), req.headers)


class CrowdcutError(RuntimeError):
    pass


def call(method: str, path: str, payload: dict | None = None, tries: int = 3, timeout: int = 15) -> dict:
    """A signed request to Crowdcut, retried on network errors and 5xx (every
    write there is idempotent)."""
    if not connected():
        raise CrowdcutError("Creator Pools isn't connected yet.")
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    last = None
    for i in range(max(1, tries)):
        headers = {"Content-Type": "application/json", **sign(secrets_()[0], method, path, body)}
        try:
            r = requests.request(method, crowdcut_url() + path, data=body or None, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last = f"Crowdcut unreachable: {e.__class__.__name__}"
        else:
            if r.status_code < 500:
                try:
                    data = r.json()
                except ValueError:
                    data = {}
                if r.status_code >= 400:
                    raise CrowdcutError(str(data.get("error") or f"Crowdcut answered {r.status_code}"))
                return data
            last = f"Crowdcut answered {r.status_code}"
        if i + 1 < tries:
            time.sleep(0.5 * (2 ** i))
    raise CrowdcutError(last or "Crowdcut didn't answer")


# ---------------------------------------------------------------- the form

def _lines(v, n=8, size=200) -> list[str]:
    out = [re.sub(r"\s+", " ", x).strip()[:size] for x in str(v or "").replace("\r", "").split("\n")]
    return [x for x in out if x][:n]


def _url(v, required=False, what="link") -> str:
    v = re.sub(r"\s+", "", str(v or ""))[:500]
    if not v:
        if required:
            raise Refused(f"Add the {what}.")
        return ""
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    p = urlparse(v)
    if p.scheme not in ("http", "https") or "." not in (p.hostname or ""):
        raise Refused(f"That {what} doesn't look like a web address.")
    return v


def _num(v, what, lo, hi, kind=float):
    try:
        x = kind(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        raise Refused(f"{what} must be a number.")
    if not lo <= x <= hi:
        raise Refused(f"{what} must be between {lo:,} and {hi:,}." if kind is int else
                      f"{what} must be between ${lo:,.2f} and ${hi:,.2f}.")
    return x


# Things a brief may not ask for (the creator terms forbid them too).
BANNED_ASKS = [
    (r"\b(fake|made[- ]up|invented)\s+(review|testimonial)s?", "fake reviews or testimonials"),
    (r"\b(no|without|skip|remove|hide|don'?t (use|add|include))\s+(the\s+)?(#?ad|disclosure|paid partnership|sponsored)\b",
     "removing the #ad disclosure"),
    (r"\bpretend\b", "pretending (to be a customer, or to have used it)"),
    (r"\b(cure[sd]?|treat(s|ed)?|heal(s|ed)?|lose \d+ ?(lb|lbs|pounds|kg)|weight[- ]loss)\b", "health claims"),
    (r"\b(get rich|passive income|make \$?\d|earn \$?\d|financial freedom)\b", "money or income claims"),
]


def check_brief(text: str) -> None:
    """The brief, do's, don'ts and talking points can't ask for fake reviews,
    drop the disclosure, or make health or money claims. Raises Refused."""
    t = (text or "").lower()
    for pat, what in BANNED_ASKS:
        if re.search(pat, t):
            raise Refused(f"The brief can't ask creators for {what}. Creator Pools posts are honest, disclosed ads. "
                          "Edit the brief and try again.")


def clean(form, brand_default: str = "") -> dict:
    """The campaign from the form. Raises Refused in words."""
    title = re.sub(r"\s+", " ", form.get("title") or "").strip()[:120]
    brand = re.sub(r"\s+", " ", form.get("brand") or brand_default or "").strip()[:80]
    if not title:
        raise Refused("Give the campaign a title.")
    if not brand:
        raise Refused("Add the brand name creators should say.")
    product_url = _url(form.get("product_url"), required=True, what="product link")
    brief = re.sub(r"[ \t]+", " ", (form.get("brief") or "").replace("\r", "")).strip()[:1500]
    if len(brief) < 20:
        raise Refused("Write a short brief (what the video should show), at least a sentence.")
    tps, dos, donts = _lines(form.get("talking_points")), _lines(form.get("dos")), _lines(form.get("donts"))
    check_brief("\n".join([brief, *tps, *dos]))     # don'ts list what to avoid, so they may name it
    getl = form.getlist if hasattr(form, "getlist") else (lambda k: form.get(k) or [])
    modes = [m for m in MODES if m in getl("modes")]
    if not modes:
        raise Refused("Pick at least one way creators can take part.")
    links = [_url(x, what="link") for x in _lines(form.get("links"), n=10, size=500)]
    links = [x for x in links if x]
    if "links" in modes and not links:
        raise Refused("Add at least one link to your livestreams, podcasts or videos, or untick that option.")
    plats = [p for p in PLATFORMS if p in getl("platforms")]
    if not plats:
        raise Refused("Pick at least one platform.")
    rate = round(_num(form.get("rate_per_1k"), "The rate per 1,000 views", *RATE_RANGE), 2)
    pool = int(round(_num(form.get("pool_usd"), "The pool", min_pool_cents() / 100, max_pool_cents() / 100) * 100))
    post_cap = round(_num(form.get("per_post_cap_usd"), "The most one post can earn", 1, pool / 100), 2)
    creator_cap = round(_num(form.get("per_creator_cap_usd"), "The most one creator can earn", post_cap, pool / 100), 2)
    mv = _num(form.get("min_views") or DEFAULT_MIN_VIEWS, "Minimum views", 100, 1_000_000, int)
    days = _num(form.get("days") or 30, "The run length in days", *DAYS_RANGE, int)
    return {"title": title, "brand": brand, "product_url": product_url, "brief": brief,
            "talking_points": tps, "dos": dos, "donts": donts, "modes": modes, "links": links if "links" in modes else [],
            "platforms": plats, "rate_per_1k": rate, "pool_cents": pool, "per_post_cap_usd": post_cap,
            "per_creator_cap_usd": creator_cap, "min_views": mv, "days": days}


def create_draft(conn, ws_id: int, user_id: int, brand_id, data: dict, assets: list) -> int:
    """A draft pool with its uploaded files (assets: [(path, filename, ctype)])."""
    vids = [a for a in assets if a[2].startswith("video/")]
    if "assets" in data["modes"] and not vids:
        raise Refused("Upload at least one product video for creators to cut, or untick that option.")
    now = db.now()
    pid = conn.execute(
        "INSERT INTO creator_pools (workspace_id, user_id, brand_id, title, brand, product_url, brief, talking_points, "
        "dos, donts, modes, links, platforms, rate_per_1k, per_post_cap_usd, per_creator_cap_usd, min_views, days, "
        "pool_cents, fee_cents, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ws_id, user_id, brand_id, data["title"], data["brand"], data["product_url"], data["brief"],
         json.dumps(data["talking_points"]), json.dumps(data["dos"]), json.dumps(data["donts"]),
         ",".join(data["modes"]), json.dumps(data["links"]), ",".join(data["platforms"]), data["rate_per_1k"],
         data["per_post_cap_usd"], data["per_creator_cap_usd"], data["min_views"], data["days"], data["pool_cents"],
         fee_cents(), "draft", now, now)).lastrowid
    for path, name, ctype in assets:
        conn.execute("INSERT INTO creator_pool_assets (pool_id, workspace_id, path, filename, content_type, kind, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (pid, ws_id, path, (name or "")[:120], ctype,
                                                "video" if ctype.startswith("video/") else "image", now))
    conn.commit()
    return pid


def get(conn, ws_id, pid):
    return conn.execute("SELECT * FROM creator_pools WHERE id=? AND workspace_id=?", (pid, ws_id)).fetchone()


def assets(conn, pid) -> list:
    return conn.execute("SELECT * FROM creator_pool_assets WHERE pool_id=? ORDER BY id", (pid,)).fetchall()


def _j(v, default):
    try:
        x = json.loads(v or "")
    except (TypeError, ValueError):
        return default
    return x if isinstance(x, type(default)) else default


def view(conn, p) -> dict:
    """A pool as the pages show it."""
    n = _j(p["numbers_json"], {})
    d = dict(p)
    d.update(talking_points=_j(p["talking_points"], []), dos=_j(p["dos"], []), donts=_j(p["donts"], []),
             links=_j(p["links"], []), modes=(p["modes"] or "").split(","), platforms=(p["platforms"] or "").split(","),
             status_text=STATUS_TEXT.get(p["status"], p["status"]), pool_text=money(p["pool_cents"]),
             fee_text=money(p["fee_cents"]), total_text=money(p["pool_cents"] + p["fee_cents"]),
             rate_text=f"{money(int(round(p['rate_per_1k'] * 100)))} per 1,000 views",
             numbers=n, assets=[dict(a) for a in assets(conn, p["id"])])
    spent = int(n.get("spent_cents") or 0)
    d["spent_text"], d["left_text"] = money(spent), money(max(0, p["pool_cents"] - spent))
    r = conn.execute("SELECT * FROM creator_pool_refunds WHERE pool_id=?", (p["id"],)).fetchone()
    d["refund"] = dict(r) if r else None
    return d


# ---------------------------------------------------------------- paying

def checkout(conn, p, base: str, email: str, create_session) -> str:
    """Start Stripe Checkout for a draft pool: the fee and the pool as two
    lines. Returns the Checkout URL. create_session is billing's function."""
    if p["status"] not in ("draft", "checkout"):
        raise Refused("This pool is already paid for.")
    if not connected():
        raise Refused("Creator Pools isn't connected to Crowdcut yet, so it can't be paid for. Nothing was charged.")
    meta = {"app": "campaignify", "kind": "creator_pool", "pool_id": p["id"], "workspace_id": p["workspace_id"]}
    sess = create_session(
        lines=[{"name": "Creator Pools campaign fee", "description": f"{p['title']} ({p['brand']})",
                "cents": p["fee_cents"]},
               {"name": "Creator pool", "description": "Paid to creators for approved posts. Unspent money is refunded.",
                "cents": p["pool_cents"]}],
        success_url=f"{base}/app/creator-pools/{p['id']}?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/app/creator-pools/{p['id']}?checkout=cancel",
        email=email, metadata=meta, client_reference_id=f"cfy-pool-{p['id']}",
        description=f"Campaignify Creator Pool: {p['title']}", idempotency_key=f"campaignify-pool-{p['id']}-{p['updated_at']}")
    conn.execute("UPDATE creator_pools SET status='checkout', stripe_session_id=?, updated_at=? WHERE id=?",
                 (sess["id"], db.now(), p["id"]))
    conn.commit()
    return sess["url"]


def paid(conn, sess: dict) -> dict:
    """checkout.session.completed for kind=creator_pool: mark it paid (once)
    and push it to Crowdcut. Amount and currency must match."""
    meta = sess.get("metadata") or {}
    if not str(meta.get("pool_id", "")).isdigit():
        return {"ignored": "no pool id"}
    if sess.get("payment_status") not in ("paid", "no_payment_required"):
        return {"pending": "payment not settled"}
    p = conn.execute("SELECT * FROM creator_pools WHERE id=?", (int(meta["pool_id"]),)).fetchone()
    if not p:
        return {"ignored": "unknown pool"}
    if str(p["workspace_id"]) != str(meta.get("workspace_id")) or sess.get("amount_total") != p["pool_cents"] + p["fee_cents"] \
            or (sess.get("currency") or "").lower() != "usd":
        log.error("creator pool %s: session %s does not match", p["id"], sess.get("id"))
        return {"error": "mismatch"}
    cur = conn.execute("UPDATE creator_pools SET status='paid', paid_at=?, stripe_session_id=?, stripe_payment_intent=?, "
                       "updated_at=? WHERE id=? AND status IN ('draft','checkout')",
                       (db.now(), sess.get("id"), sess.get("payment_intent"), db.now(), p["id"]))
    conn.commit()
    if cur.rowcount != 1:
        return {"ok": True, "duplicate": True}
    push(conn, p["id"])
    return {"ok": True, "pool": p["id"]}


def payload(conn, p) -> dict:
    d = view(conn, p)
    return {"title": p["title"], "brand": p["brand"], "product_url": p["product_url"], "brief": p["brief"],
            "talking_points": d["talking_points"], "dos": d["dos"], "donts": d["donts"], "modes": d["modes"],
            "links": d["links"], "assets": [{"id": a["id"], "name": a["filename"] or f"File {a['id']}", "kind": a["kind"]}
                                            for a in d["assets"]],
            "platforms": d["platforms"], "rate_per_1k": p["rate_per_1k"], "per_post_cap_usd": p["per_post_cap_usd"],
            "per_creator_cap_usd": p["per_creator_cap_usd"], "min_views": p["min_views"], "days": p["days"],
            "pool_usd": p["pool_cents"] / 100}


def push(conn, pid) -> bool:
    """Create (or update) the pool on Crowdcut. A paid pool that couldn't be
    pushed stays 'paid' with sync_error; the page and the admin retry it."""
    p = conn.execute("SELECT * FROM creator_pools WHERE id=?", (pid,)).fetchone()
    if not p or p["status"] not in ("paid", "live", "paused"):
        return False
    try:
        r = call("PUT", f"/api/partner/v1/creator-pools/{ext_id(pid)}", payload(conn, p))
    except CrowdcutError as e:
        conn.execute("UPDATE creator_pools SET sync_error=?, updated_at=? WHERE id=?", (str(e)[:300], db.now(), pid))
        conn.commit()
        log.warning("creator pool %s not pushed: %s", pid, e)
        return False
    n = r.get("pool") or {}
    conn.execute("UPDATE creator_pools SET status=CASE WHEN status='paid' THEN 'live' ELSE status END, crowdcut_id=?, "
                 "live_at=COALESCE(live_at, ?), sync_error=NULL, numbers_json=?, synced_at=?, updated_at=? WHERE id=?",
                 (n.get("crowdcut_id"), db.now(), json.dumps(n), db.now(), db.now(), pid))
    conn.commit()
    return True


# ---------------------------------------------------------------- running

def _save_numbers(conn, p, n: dict) -> None:
    st = {"active": "live", "paused": "paused", "ended": "ended"}.get(n.get("status"), p["status"])
    conn.execute("UPDATE creator_pools SET numbers_json=?, synced_at=?, status=?, ended_at=CASE WHEN ?='ended' "
                 "THEN COALESCE(ended_at, ?) ELSE ended_at END, updated_at=? WHERE id=?",
                 (json.dumps(n), db.now(), st, st, db.now(), db.now(), p["id"]))
    conn.commit()
    if st == "ended":
        record_refund(conn, conn.execute("SELECT * FROM creator_pools WHERE id=?", (p["id"],)).fetchone())


def submissions(conn, p) -> list:
    """The pool's posts from Crowdcut (and its live numbers)."""
    r = call("GET", f"/api/partner/v1/creator-pools/{ext_id(p['id'])}/submissions")
    _save_numbers(conn, p, r.get("pool") or {})
    conn.execute("UPDATE creator_pools SET new_posts=0 WHERE id=?", (p["id"],))
    conn.commit()
    return r.get("submissions") or []


def refresh(conn, p) -> dict:
    r = call("GET", f"/api/partner/v1/creator-pools/{ext_id(p['id'])}")
    _save_numbers(conn, p, r.get("pool") or {})
    return r.get("pool") or {}


def review(conn, p, sid: int, decision: str, reason: str = "") -> dict:
    if p["status"] not in ("live", "paused", "ended"):
        raise Refused("This pool isn't live.")
    if decision not in ("approve", "reject"):
        raise Refused("Approve or reject.")
    reason = re.sub(r"\s+", " ", reason or "").strip()[:300]
    if decision == "reject" and not reason:
        raise Refused("Give the creator a reason.")
    try:
        return call("POST", f"/api/partner/v1/creator-pools/{ext_id(p['id'])}/submissions/{int(sid)}/review",
                    {"decision": decision, "reason": reason}).get("submission") or {}
    except CrowdcutError as e:
        raise Refused(str(e))


def set_status(conn, p, status: str) -> dict:
    if status not in ("active", "paused", "ended"):
        raise Refused("Unknown action.")
    if p["status"] not in ("live", "paused"):
        raise Refused("This pool isn't running.")
    try:
        n = call("POST", f"/api/partner/v1/creator-pools/{ext_id(p['id'])}/status", {"status": status}).get("pool") or {}
    except CrowdcutError as e:
        raise Refused(str(e))
    _save_numbers(conn, p, n)
    return n


# ---------------------------------------------------------------- refunds (never automatic)

def unspent_cents(p) -> int:
    n = _j(p["numbers_json"], {})
    if "remaining_cents" in n:
        return max(0, int(n["remaining_cents"]))
    return p["pool_cents"]


def pending_posts(p) -> int:
    posts = (_j(p["numbers_json"], {}).get("posts") or {})
    return int(posts.get("brand_pending") or 0) + int(posts.get("audit_pending") or 0)


def record_refund(conn, p) -> None:
    """An ended pool owes the brand what's left of the pool (the fee is kept).
    The amount follows Crowdcut's numbers until an admin settles it."""
    amt = unspent_cents(p)
    r = conn.execute("SELECT * FROM creator_pool_refunds WHERE pool_id=?", (p["id"],)).fetchone()
    if r is None:
        conn.execute("INSERT INTO creator_pool_refunds (pool_id, workspace_id, amount_cents, status, created_at, updated_at) "
                     "VALUES (?,?,?,'due',?,?)", (p["id"], p["workspace_id"], amt, db.now(), db.now()))
    elif r["status"] == "due" and r["amount_cents"] != amt:
        conn.execute("UPDATE creator_pool_refunds SET amount_cents=?, updated_at=? WHERE id=?", (amt, db.now(), r["id"]))
    conn.commit()


def settle_refund(conn, pool_id: int, admin_id: int, method: str, reference: str = "", stripe_refund=None) -> dict:
    """An admin settles a due refund: method 'stripe' calls stripe_refund
    (billing.create_refund) for the amount on the pool's payment; 'hand'
    records one Koby sent another way. Refused while posts still wait."""
    p = conn.execute("SELECT * FROM creator_pools WHERE id=?", (pool_id,)).fetchone()
    r = conn.execute("SELECT * FROM creator_pool_refunds WHERE pool_id=?", (pool_id,)).fetchone()
    if not p or not r:
        raise Refused("No refund is recorded for that pool.")
    if r["status"] != "due":
        raise Refused("That refund was already settled.")
    if pending_posts(p):
        raise Refused(f"{pending_posts(p)} posts are still waiting for the brand or the view check. Settle the refund "
                      "once they're done (press Sync first).")
    amt = r["amount_cents"]
    ref = re.sub(r"\s+", " ", reference or "").strip()[:120]
    rid = None
    if method == "stripe":
        if amt <= 0:
            raise Refused("Nothing is left to refund.")
        if not p["stripe_payment_intent"]:
            raise Refused("This pool's Stripe payment isn't on record. Refund it by hand in Stripe and mark it here.")
        res = stripe_refund(p["stripe_payment_intent"], amt, f"campaignify-pool-refund-{pool_id}",
                            {"app": "campaignify", "kind": "creator_pool_refund", "pool_id": pool_id})
        rid = res.get("id")
    elif method != "hand":
        raise Refused("Unknown refund method.")
    conn.execute("UPDATE creator_pool_refunds SET status='refunded', method=?, stripe_refund_id=?, reference=?, done_at=?, "
                 "done_by=?, updated_at=? WHERE id=?", (method, rid, ref or None, db.now(), admin_id, db.now(), r["id"]))
    conn.commit()
    return dict(conn.execute("SELECT * FROM creator_pool_refunds WHERE id=?", (r["id"],)).fetchone())


# ---------------------------------------------------------------- assets for Crowdcut (signed links)

def asset_token(asset_id: int, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ASSET_URL_TTL)
    msg = f"asset|{int(asset_id)}|{exp}".encode()
    mac = hmac.new(secrets_()[0].encode(), msg, hashlib.sha256).digest()[:18]
    return f"{int(asset_id)}.{exp}.{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"


def asset_from_token(tok: str, now: float | None = None):
    try:
        aid, exp, mac = str(tok).split(".")
        aid, exp = int(aid), int(exp)
    except ValueError:
        return None
    if exp < (now if now is not None else time.time()) or not connected():
        return None
    for s in secrets_():
        want = base64.urlsafe_b64encode(hmac.new(s.encode(), f"asset|{aid}|{exp}".encode(),
                                                 hashlib.sha256).digest()[:18]).decode().rstrip("=")
        if hmac.compare_digest(want, mac):
            return aid
    return None


def ends_on(p) -> str:
    n = _j(p["numbers_json"], {})
    if n.get("ends_at"):
        return n["ends_at"][:10]
    if p["paid_at"]:
        try:
            return (datetime.fromisoformat(p["paid_at"]) + timedelta(days=p["days"])).date().isoformat()
        except ValueError:
            pass
    return (datetime.now(timezone.utc) + timedelta(days=p["days"])).date().isoformat()
