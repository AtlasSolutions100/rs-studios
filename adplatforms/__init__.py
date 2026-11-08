"""Ad platforms: connecting a workspace's own ad accounts, and (from M2.3) launching paused
campaigns on them. Design: scratchpad designs/ad-platform-connect-and-launch.md.

Everything here sits behind AD_LAUNCH (default off):
    AD_LAUNCH=off      no page, no route, no nav item (404)
    AD_LAUNCH=beta     only workspaces listed in AD_LAUNCH_WORKSPACES (comma-separated ids) and admins
    AD_LAUNCH=on       every workspace

One adapter per platform implements AdPlatform (adplatforms/meta.py first). Tokens are kept
only as token boxes (secretsbox.py, sealed to the workspace) and opened only inside an
adapter call; they are never logged, flashed or put in a page.
"""
import os
from typing import Protocol

import db
import secretsbox

PLATFORMS = ("meta", "google", "tiktok", "snapchat")
STATUSES = ("pending", "active", "expired", "revoked", "error")
PENDING_TTL_S = 3600          # an unpicked connection (token fetched, no account chosen) is dropped after this


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
    """Tokens past their expiry read as expired; unpicked connections older than an hour go."""
    now = db.now()
    conn.execute("UPDATE ad_connections SET status='expired', last_error='The access expired.', updated_at=? "
                 "WHERE workspace_id=? AND status='active' AND token_expires_at IS NOT NULL AND token_expires_at < ?",
                 (now, int(workspace_id), now))
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=PENDING_TTL_S)).replace(microsecond=0).isoformat()
    conn.execute("DELETE FROM ad_connections WHERE workspace_id=? AND status='pending' AND created_at < ?",
                 (int(workspace_id), cutoff))
    conn.commit()


def mark(conn, conn_id: int, status: str, error: str = None):
    assert status in STATUSES
    conn.execute("UPDATE ad_connections SET status=?, last_error=?, updated_at=? WHERE id=?",
                 (status, (error or None) and str(error)[:300], db.now(), int(conn_id)))
    conn.commit()
