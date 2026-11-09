"""Ad platforms: connecting a workspace's own ad accounts, and (from M2.3) launching paused
campaigns on them. Design: scratchpad designs/ad-platform-connect-and-launch.md.

Everything here sits behind AD_LAUNCH (default off):
    AD_LAUNCH=off      no page, no route, no nav item (404)
    AD_LAUNCH=beta     only workspaces listed in AD_LAUNCH_WORKSPACES (comma-separated ids) and admins
    AD_LAUNCH=on       every workspace

One adapter per platform implements AdPlatform (adplatforms/meta.py first, adplatforms/google.py behind
its own GOOGLE_ADS flag on top of AD_LAUNCH, adplatforms/snapchat.py behind SNAPCHAT_ADS, adplatforms/tiktok.py behind TIKTOK_ADS). Tokens are kept
only as token boxes (secretsbox.py, sealed to the workspace) and opened only inside an
adapter call; they are never logged, flashed or put in a page.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

import db
import secretsbox

PLATFORMS = ("meta", "google", "tiktok", "snapchat")
STATUSES = ("pending", "active", "expired", "revoked", "error")
PENDING_TTL_S = 3600          # an unpicked connection (token fetched, no account chosen) is dropped after this
SWEEP_SECONDS = 60
_last_sweep = [0.0]
log = logging.getLogger("campaignify.adconnect")


def mode() -> str:
    v = (os.environ.get("AD_LAUNCH") or "off").strip().lower()
    if v in ("on", "1", "true", "yes"):
        return "on"
    return "beta" if v == "beta" else "off"


def beta_workspaces() -> set:
    return {int(x) for x in (os.environ.get("AD_LAUNCH_WORKSPACES") or "").replace(" ", "").split(",") if x.isdigit()}


def enabled(ws, user=None) -> bool:
    """May this workspace see ad connections at all (the AD_LAUNCH flag)?"""
    m = mode()
    if m == "off" or ws is None:
        return False
    if m == "on":
        return True
    return int(ws["id"]) in beta_workspaces() or bool(user is not None and user["is_admin"])


class AdPlatform(Protocol):
    """One adapter per platform (design doc section 3). M2.1 implements the connect half for
    Meta; the launch, manage and report half (validate .. pull_insights) arrives in M2.3 to M2.5."""
    key: str
    name: str

    def configured(self) -> bool: ...
    def oauth_start(self, state: str, code_challenge: str) -> str: ...
    def oauth_finish(self, code: str, code_verifier: str) -> dict: ...      # token box fields + what can be picked
    def list_accounts(self, token: str) -> list: ...
    def list_pages(self, token: str) -> list: ...
    def list_pixels(self, token: str, account_id: str) -> list: ...
    def revoke(self, token: str) -> bool: ...
    def validate(self, spec, conn) -> list: ...
    def plan_steps(self, spec, conn) -> list: ...
    def execute_step(self, step, conn): ...
    def compensate(self, step, conn): ...
    def set_status(self, obj, conn, status: str): ...
    def update_budget(self, obj, conn, daily_minor: int): ...
    def pull_insights(self, conn, objects, day_range) -> list: ...
    def upload_asset(self, conn, asset): ...


def adapter(platform: str):
    if platform == "meta":
        from adplatforms import meta
        return meta.ADAPTER
    if platform == "google":
        from adplatforms import google
        return google.ADAPTER
    if platform == "snapchat":
        from adplatforms import snapchat
        return snapchat.ADAPTER
    if platform == "tiktok":
        from adplatforms import tiktok
        return tiktok.ADAPTER
    raise KeyError(platform)


# ---------------------------------------------------------------- connections (ad_connections rows)

def seal_token(workspace_id: int, token: str) -> str:
    return secretsbox.seal(token, secretsbox.aad_for("ad_connections", workspace_id))


def open_token(row) -> str:
    """The token of a connection row, for an adapter call only. Never log or show it."""
    if not row["token_enc"]:
        raise secretsbox.BoxError("no token")
    return secretsbox.open_box(row["token_enc"], secretsbox.aad_for("ad_connections", row["workspace_id"]))


def get(conn, workspace_id: int, conn_id: int):
    """A connection of this workspace, or None (callers answer 404: another workspace's id looks missing)."""
    return conn.execute("SELECT * FROM ad_connections WHERE id=? AND workspace_id=?",
                        (int(conn_id), int(workspace_id))).fetchone()


def listed(conn, workspace_id: int):
    """The workspace's connections the Settings page shows (not pending or disconnected)."""
    expire_due(conn, workspace_id)
    return conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND status IN ('active','expired','error') "
                        "ORDER BY platform, account_name, id", (int(workspace_id),)).fetchall()


def expire_due(conn, workspace_id: int):
    """Tokens past their expiry read as expired; unpicked connections older than an hour go (sweep_pending)."""
    now = db.now()
    conn.execute("UPDATE ad_connections SET status='expired', last_error='The access expired.', updated_at=? "
                 "WHERE workspace_id=? AND status='active' AND token_expires_at IS NOT NULL AND token_expires_at < ?",
                 (now, int(workspace_id), now))
    conn.commit()
    sweep_pending(conn, workspace_id)


def _pending_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=PENDING_TTL_S)).replace(microsecond=0).isoformat()


def pending_expired(row) -> bool:
    """A connect that was never finished (no account picked) and is past PENDING_TTL_S."""
    return row is not None and row["status"] == "pending" and row["created_at"] < _pending_cutoff()


def has_stored(conn, workspace_id: int) -> bool:
    """The workspace still holds connections (with or without a token): Disconnect stays reachable whatever
    AD_LAUNCH says, so tokens can always be removed (M-3)."""
    return conn.execute("SELECT 1 FROM ad_connections WHERE workspace_id=? AND status IN ('active','expired','error') "
                        "LIMIT 1", (int(workspace_id),)).fetchone() is not None


def _others_hold(conn, row) -> bool:
    """Another connection still uses this platform user's grant (so it must not be revoked at Meta)."""
    return bool(row["external_user_id"]) and conn.execute(
        "SELECT 1 FROM ad_connections WHERE platform=? AND external_user_id=? AND id!=? AND token_enc IS NOT NULL "
        "AND status IN ('active','expired','error') LIMIT 1",
        (row["platform"], row["external_user_id"], row["id"])).fetchone() is not None


def sweep_pending(conn, workspace_id: int = None) -> int:
    """Drop connects that never finished (L-13): pending rows older than PENDING_TTL_S lose their token and go.
    The grant is also revoked at the platform when that person has no other connection here (best effort).
    Runs from the worker every minute (maybe_sweep) and when Settings lists connections. Returns rows dropped."""
    sql = "SELECT * FROM ad_connections WHERE status='pending' AND created_at < ?"
    args = [_pending_cutoff()]
    if workspace_id is not None:
        sql += " AND workspace_id=?"
        args.append(int(workspace_id))
    n = 0
    for row in conn.execute(sql, args).fetchall():
        if row["token_enc"] and not _others_hold(conn, row):
            try:
                ad = adapter(row["platform"])
                if ad.configured():
                    ad.revoke(open_token(row))
            except Exception as e:  # noqa: BLE001  best effort: the token is deleted here either way
                log.warning("pending connect %s: revoke failed: %s", row["id"], type(e).__name__)
        conn.execute("DELETE FROM ad_connections WHERE id=? AND status='pending'", (row["id"],))
        n += 1
    conn.commit()
    if n:
        log.info("dropped %s unfinished ad account connect(s)", n)
    return n


def maybe_sweep():
    """The worker loop's call: at most once a minute, never raises, whatever AD_LAUNCH says."""
    if time.time() - _last_sweep[0] < SWEEP_SECONDS:
        return
    _last_sweep[0] = time.time()
    try:
        with db.connect() as conn:
            sweep_pending(conn)
    except Exception:  # noqa: BLE001
        log.exception("pending ad connection sweep failed")


def platform_on(platform: str, ws, user=None) -> bool:
    """May this workspace connect and launch on `platform`? Meta: AD_LAUNCH. Google: AD_LAUNCH and GOOGLE_ADS.
    Snapchat: AD_LAUNCH and SNAPCHAT_ADS. TikTok: AD_LAUNCH and TIKTOK_ADS."""
    if platform == "google":
        from adplatforms import google
        return google.enabled(ws, user)
    if platform == "snapchat":
        from adplatforms import snapchat
        return snapchat.enabled(ws, user)
    if platform == "tiktok":
        from adplatforms import tiktok
        return tiktok.enabled(ws, user)
    return platform == "meta" and enabled(ws, user)


def forget_platform_user(conn, platform: str, external_user_id: str, delete: bool = False) -> int:
    """Meta told us a person removed the app (deauthorize) or asked for their data to be deleted: drop every
    token their grant gave us, in every workspace. delete=True also removes the connection rows and their
    pixels (what we read from Meta: account, Page and pixel names); otherwise the rows stay as "revoked" (with no
    token) as a record, and Settings no longer lists them. Commits. Returns how many connections were affected."""
    rows = conn.execute("SELECT id, workspace_id FROM ad_connections WHERE platform=? AND external_user_id=?",
                        (platform, str(external_user_id))).fetchall()
    for r in rows:
        conn.execute("DELETE FROM pixels WHERE connection_id=? AND workspace_id=?", (r["id"], r["workspace_id"]))
        if delete:
            conn.execute("DELETE FROM ad_connections WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE ad_connections SET status='revoked', token_enc=NULL, refresh_token_enc=NULL, "
                         "pending_json=NULL, last_error='Removed on Meta.', updated_at=? WHERE id=?", (db.now(), r["id"]))
    conn.commit()
    return len(rows)


def mark(conn, conn_id: int, status: str, error: str = None):
    assert status in STATUSES
    conn.execute("UPDATE ad_connections SET status=?, last_error=?, updated_at=? WHERE id=?",
                 (status, (error or None) and str(error)[:300], db.now(), int(conn_id)))
    conn.commit()
