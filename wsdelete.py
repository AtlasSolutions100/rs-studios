"""Workspace deletion with a 7-day grace (roadmap M7.5; SEC-7, SEC-12; Crowdcut's retention.py workspace rule).

Only the workspace owner asks for it (Team > Delete this workspace). Nothing is removed then: request() sets
delete_after = now + GRACE_DAYS (7) and every page shows a banner with the date and a Cancel button (cancel(), the
owner, until then). There is no age-based deletion of anyone's data here: only a deletion an owner asked for, after
its grace. (storage.py's RS_STORAGE_PURGE_DELETED is a separate, existing opt-in for deleted ads' files.)

A live Stripe plan blocks it: the owner cancels the plan on the Billing page first (or it is already set to end at
the period's end). This module never cancels, changes or refunds a plan, and never touches Stripe.

Once the grace has passed, the worker (tick(), hourly) purges the workspace:
  * work stops first: queued generation jobs are failed (no charge was taken that isn't refunded by the usual
    path), queued launches and changes stop; a job still running at the vendor waits for the next run;
  * vendor copies, where the API allows: each finished Higgsfield request is deleted at the vendor
    (DELETE /requests/{id}: its input and output files, prompt and parameters; hf.delete_request). A request the
    vendor still has queued or in progress (409) or can't delete right now is tried again next run, up to
    VENDOR_TRIES runs. Photos and reference videos uploaded to the vendor's file store have no delete call; the
    vendor removes uploads within 7 days (docs.higgsfield.ai, data retention, read 2026-11-09);
  * connected ad accounts: Campaignify's access is revoked at the platform where it has a revoke call and this
    login isn't used by another workspace (as Disconnect does), and the tokens are deleted. Campaigns already made
    on the member's own ad accounts stay there: they are the member's;
  * files on disk: the workspace's media, uploads, Creator Pool assets, exports and any stored path under the data
    folder;
  * rows: everything the workspace made (products, photos, ads, plans, copy, pages, forms, contacts and their
    events, messages, flows and their runs, links, communities, launches, insights, connections, pixels, MCP keys)
    is deleted. Kept, as records, with no content: the ledger, purchases, plan invoices, refunds and disputes,
    credit holds, comp codes, paid Creator Pools (amounts only; the brief is cleared), the one-trial-per-card and
    free-plan abuse records, and generation jobs (credits and vendor cost; brief and settings cleared);
  * people: every login of the workspace is anonymized (email deleted-<id>@deleted.invalid, no name, no password,
    no Google link) and its login links, verification codes and nudge rows go; the email log's copies to those
    addresses are cleared;
  * the workspace row stays (billing history points at it), renamed "Deleted workspace #<id>", with deleted_at.
Never a workspace an operator (is_admin) belongs to. Each run is logged (ids and counts only) in deletion_runs.

forget_contact() is the single-contact delete (SEC-12): the contact's messages lose the address and body, its flow
runs their context, its tasks go, along with its events and form answers.
"""
import json
import logging
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("campaignify.wsdelete")

GRACE_DAYS = 7
VENDOR_TRIES = 6                     # runs (hours) a vendor copy is retried before it's recorded as not deleted
LIVE_PLAN = ("active", "past_due", "trialing", "paused", "pending", "incomplete", "unpaid")
TICK_SECONDS = 3600
_last = [0.0]

# Kept as records (rows stay, content columns cleared); everything else with a workspace_id is deleted.
KEEP = {"ledger", "purchases", "plan_invoices", "credit_reversals", "credit_holds", "comp_codes", "creator_pools",
        "creator_pool_refunds", "card_trials", "free_plans", "free_previews", "ad_signup_grants", "jobs", "users",
        "workspaces"}
SCRUB = {"jobs": {"brief": "", "params_json": "{}"},
         "creator_pools": {"brief": None, "talking_points": None, "dos": None, "donts": None, "links": None,
                           "product_url": None}}
WS_CLEAR = {"brand_name": None, "product_name": None, "claims": None, "audience": None, "setup_json": None,
            "sender_name": None, "reply_to": None, "postal_address": None, "flows_json": None, "card_label": None,
            "brand_id": None, "product_id": None}


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _cols(conn, table) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _has(conn, table) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS deletion_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_id INTEGER NOT NULL,
        ran_at TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        summary TEXT)""")


# ---------------------------------------------------------------- the owner's request

def live_plan(ws) -> bool:
    """A Stripe subscription that will charge again: the owner cancels it first (we never do)."""
    ws = dict(ws)
    if not ws.get("stripe_subscription_id"):
        return False
    if (ws.get("plan_status") or "") not in LIVE_PLAN:
        return False
    return not ws.get("plan_cancel_at_period_end")


def operator_ws(conn, ws_id: int) -> bool:
    return bool(conn.execute("SELECT 1 FROM users WHERE workspace_id=? AND is_admin=1", (int(ws_id),)).fetchone())


def status(conn, ws_id: int) -> dict:
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (int(ws_id),)).fetchone()
    w = dict(ws) if ws else {}
    blocked = None
    if operator_ws(conn, ws_id):
        blocked = "operator"
    elif ws is not None and live_plan(ws):
        blocked = "billing"
    return {"requested_at": w.get("delete_requested_at"), "delete_after": w.get("delete_after"),
            "deleted_at": w.get("deleted_at"), "blocked": blocked, "grace_days": GRACE_DAYS,
            "scheduled": bool(w.get("delete_after") and not w.get("deleted_at"))}


def request(conn, ws_id: int, user_id: int, now: datetime | None = None) -> dict:
    """Schedule the deletion GRACE_DAYS from now (the caller checked the owner). Nothing is removed yet."""
    now = now or datetime.now(timezone.utc)
    st = status(conn, ws_id)
    if st["blocked"] or st["scheduled"] or st["deleted_at"]:
        return st
    after = _iso(now + timedelta(days=GRACE_DAYS))
    conn.execute("UPDATE workspaces SET delete_requested_at=?, delete_after=?, delete_requested_by=? WHERE id=?",
                 (_iso(now), after, int(user_id), int(ws_id)))
    conn.commit()
    log.warning("workspace %s deletion requested by user %s, runs after %s", ws_id, user_id, after)
    return status(conn, ws_id)


def cancel(conn, ws_id: int, user_id: int) -> dict:
    st = status(conn, ws_id)
    if st["scheduled"]:
        conn.execute("UPDATE workspaces SET delete_requested_at=NULL, delete_after=NULL, delete_requested_by=NULL "
                     "WHERE id=? AND deleted_at IS NULL", (int(ws_id),))
        conn.commit()
        log.warning("workspace %s deletion canceled by user %s", ws_id, user_id)
    return status(conn, ws_id)


# ---------------------------------------------------------------- one contact (SEC-12)

def forget_contact(conn, ws_id: int, cid: int):
    """Everything that still names this contact, in the caller's transaction (no commit): its events, its form
    answers (kept as empty rows: the counts stay), the address and body of its messages, its flow runs' context,
    its tasks. Unsubscribes (suppressions) stay, so a re-import is never emailed against their wish."""
    ws_id, cid = int(ws_id), int(cid)
    conn.execute("DELETE FROM contact_events WHERE contact_id=? AND workspace_id=?", (cid, ws_id))
    conn.execute("UPDATE form_submissions SET contact_id=NULL, data_json='{}', referrer=NULL WHERE contact_id=? AND "
                 "workspace_id=?", (cid, ws_id))
    conn.execute("UPDATE messages SET to_addr=NULL, subject=NULL, body_text=NULL, body_html=NULL, contact_id=NULL "
                 "WHERE contact_id=? AND workspace_id=?", (cid, ws_id))
    conn.execute("UPDATE flow_runs SET context_json='{}' WHERE contact_id=? AND workspace_id=?", (cid, ws_id))
    if _has(conn, "flow_runs_archive"):
        conn.execute("UPDATE flow_runs_archive SET context_json='{}' WHERE contact_id=? AND workspace_id=?", (cid, ws_id))
    conn.execute("DELETE FROM tasks WHERE contact_id=? AND workspace_id=?", (cid, ws_id))
    if _has(conn, "flow_events"):
        conn.execute("UPDATE flow_events SET detail=NULL WHERE contact_id=? AND workspace_id=?", (cid, ws_id))


# ---------------------------------------------------------------- the purge

def _under_data(path) -> bool:
    if not path:
        return False
    try:
        return os.path.realpath(path).startswith(os.path.realpath(db.DATA) + os.sep)
    except (TypeError, ValueError):
        return False


def _remove_file(path) -> int:
    if not _under_data(path):
        return 0
    n = 0
    for p in (path, path + ".jpg"):
        try:
            n += os.path.getsize(p)
            os.remove(p)
        except OSError:
            pass
    return n


def _dir_bytes(path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _ws_dirs(ws_id: int) -> list:
    return [os.path.join(db.DATA, "media", str(ws_id)), os.path.join(db.DATA, "uploads", str(ws_id)),
            os.path.join(db.DATA, "uploads", "creator_pools", str(ws_id))]


def _vendor_delete(conn, ws_id: int, tries: int) -> dict:
    """Delete each finished generation request at the vendor. Returns counts; "left" > 0 means try again."""
    import hf
    out = {"deleted": 0, "gone": 0, "left": 0, "failed": 0}
    if not hf.configured():
        return out
    cols = _cols(conn, "jobs")
    if "vendor_deleted_at" not in cols:
        return out
    rows = conn.execute("SELECT id, request_id FROM jobs WHERE workspace_id=? AND request_id IS NOT NULL AND "
                        "vendor_deleted_at IS NULL ORDER BY id LIMIT 500", (ws_id,)).fetchall()
    for r in rows:
        res = hf.delete_request(r["request_id"])
        if res in (hf.DELETED, hf.GONE):
            conn.execute("UPDATE jobs SET vendor_deleted_at=? WHERE id=?", (db.now(), r["id"]))
            out["deleted" if res == hf.DELETED else "gone"] += 1
        elif tries >= VENDOR_TRIES:
            conn.execute("UPDATE jobs SET vendor_deleted_at=? WHERE id=?", ("not deleted: " + res[:40], r["id"]))
            out["failed"] += 1
        else:
            out["left"] += 1
    if _has(conn, "video_parts"):
        # A keyframe-first video (keyframe_video.py) is several vendor requests: its keyframe, each beat, each
        # cutaway. Each is deleted there too. A part whose request is dealt with loses its request id; purge()
        # only drops a part's row once it has (so one the vendor is still busy with is tried again next run).
        for r in conn.execute("SELECT id, request_id FROM video_parts WHERE workspace_id=? AND request_id IS NOT NULL "
                              "ORDER BY id LIMIT 500", (ws_id,)).fetchall():
            res = hf.delete_request(r["request_id"])
            if res in (hf.DELETED, hf.GONE):
                conn.execute("UPDATE video_parts SET request_id=NULL WHERE id=?", (r["id"],))
                out["deleted" if res == hf.DELETED else "gone"] += 1
            elif tries >= VENDOR_TRIES:
                conn.execute("UPDATE video_parts SET request_id=NULL, error=? WHERE id=?",
                             ("vendor copy not deleted: " + res[:40], r["id"]))
                out["failed"] += 1
            else:
                out["left"] += 1
    conn.commit()
    return out


def _revoke_connections(conn, ws_id: int) -> int:
    import adhttp
    import adplatforms
    import secretsbox
    n = 0
    for row in conn.execute("SELECT * FROM ad_connections WHERE workspace_id=? AND token_enc IS NOT NULL",
                            (ws_id,)).fetchall():
        shared = row["external_user_id"] and conn.execute(
            "SELECT 1 FROM ad_connections WHERE platform=? AND external_user_id=? AND workspace_id!=? AND "
            "token_enc IS NOT NULL", (row["platform"], row["external_user_id"], ws_id)).fetchone()
        try:
            ad = adplatforms.adapter(row["platform"])
            if not shared and ad.configured() and ad.revoke(adplatforms.open_token(row)):
                n += 1
        except (adhttp.AdHttpError, secretsbox.BoxError, NotImplementedError, KeyError) as e:
            log.warning("workspace %s: %s revoke on delete: %s", ws_id, row["platform"], type(e).__name__)
        except Exception:  # noqa: BLE001  never let one platform stop the deletion
            log.exception("workspace %s: revoke failed", ws_id)
    return n


def _child_deletes(conn, ws_id: int) -> dict:
    """Rows keyed to the workspace through a parent (no workspace_id of their own). Files first."""
    freed = 0
    n = {}
    jobs = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE workspace_id=?", (ws_id,))]
    for i in range(0, len(jobs), 500):
        part = jobs[i:i + 500]
        marks = ",".join("?" * len(part))
        for o in conn.execute(f"SELECT path FROM outputs WHERE job_id IN ({marks})", part).fetchall():
            freed += _remove_file(o["path"])
        n["outputs"] = n.get("outputs", 0) + conn.execute(f"DELETE FROM outputs WHERE job_id IN ({marks})", part).rowcount
    for table in ("photos", "creator_pool_assets", "recordings"):
        if _has(conn, table):
            for r in conn.execute(f"SELECT path FROM {table} WHERE workspace_id=?", (ws_id,)).fetchall():
                freed += _remove_file(r["path"])
    pairs = [("launch_steps", "launch_id", "launches"), ("flow_versions", "flow_id", "flows"),
             ("link_clicks", "link_id", "links"), ("message_links", "message_id", "messages")]
    for child, key, parent in pairs:
        if _has(conn, child) and _has(conn, parent):
            n[child] = conn.execute(f"DELETE FROM {child} WHERE {key} IN (SELECT id FROM {parent} WHERE workspace_id=?)",
                                    (ws_id,)).rowcount
    if _has(conn, "c_progress") and "member_id" in _cols(conn, "c_progress"):
        n["c_progress"] = conn.execute("DELETE FROM c_progress WHERE member_id IN (SELECT id FROM c_members WHERE "
                                       "workspace_id=?)", (ws_id,)).rowcount
    return {"rows": n, "freed": freed}


def _people(conn, ws_id: int) -> int:
    users = conn.execute("SELECT * FROM users WHERE workspace_id=? AND is_admin=0", (ws_id,)).fetchall()
    ucols = _cols(conn, "users")
    for u in users:
        old = u["email"]
        fake = f"deleted-{u['id']}@deleted.invalid"
        sets = {"email": fake, "name": None, "pw_hash": "!" + secrets.token_hex(16)}
        for col, val in (("email_norm", fake), ("auth_provider", None), ("verify_nonce", None), ("verify_code_hash", None),
                         ("first_touch", None), ("signup_pending", 0), ("weekly_report_off", db.now())):
            if col in ucols:
                sets[col] = val
        if "session_version" in ucols:
            conn.execute("UPDATE users SET session_version=COALESCE(session_version,0)+1 WHERE id=?", (u["id"],))
        conn.execute(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), u["id"]))
        for table in ("login_tokens", "first_run_nudges", "mcp_tokens", "mcp_grants", "mcp_codes"):
            if _has(conn, table) and "user_id" in _cols(conn, table):
                conn.execute(f"DELETE FROM {table} WHERE user_id=?", (u["id"],))
        if _has(conn, "email_log"):
            conn.execute("UPDATE email_log SET to_addr=?, payload=NULL WHERE lower(to_addr)=lower(?)", (fake, old))
        if _has(conn, "outreach_log") and "to_addr" in _cols(conn, "outreach_log"):
            conn.execute("UPDATE outreach_log SET to_addr=? WHERE lower(to_addr)=lower(?)", (fake, old))
    return len(users)


def purge(conn, ws, tries: int = 1) -> dict:
    """Remove one workspace's data after its grace. 'done' is False while something still has to wait (a job running
    at the vendor, vendor copies to retry): the next run finishes it."""
    ws_id = int(ws["id"])
    res = {"workspace_id": ws_id}
    if operator_ws(conn, ws_id):
        return dict(res, done=False, blocked="operator")
    if live_plan(ws):
        return dict(res, done=False, blocked="billing")
    # work stops first
    import worker
    for j in conn.execute("SELECT * FROM jobs WHERE workspace_id=? AND status='pending'", (ws_id,)).fetchall():
        worker._fail(conn, j, "The workspace was deleted.")
    conn.execute("UPDATE launches SET status='failed', error='The workspace was deleted.' WHERE workspace_id=? AND "
                 "status IN ('queued','launching')", (ws_id,))
    conn.commit()
    running = conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id=? AND status='running'", (ws_id,)).fetchone()[0]
    vendor = _vendor_delete(conn, ws_id, tries)
    res.update(vendor=vendor, running_jobs=running)
    res["revoked"] = _revoke_connections(conn, ws_id)
    ch = _child_deletes(conn, ws_id)
    freed = ch["freed"]
    for d in _ws_dirs(ws_id):
        if os.path.isdir(d):
            freed += _dir_bytes(d)
            shutil.rmtree(d, ignore_errors=True)
    deleted = dict(ch["rows"])
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        if t in KEEP or t in ("deletion_runs",) or "workspace_id" not in _cols(conn, t):
            continue
        if t == "video_parts":
            # a part whose vendor copy still has to be deleted keeps its row (and nothing else) until it is
            conn.execute("UPDATE video_parts SET inputs_json=NULL, url=NULL, path=NULL, remote_url=NULL WHERE workspace_id=?",
                         (ws_id,))
            n = conn.execute("DELETE FROM video_parts WHERE workspace_id=? AND request_id IS NULL", (ws_id,)).rowcount
            if n:
                deleted[t] = n
            continue
        n = conn.execute(f"DELETE FROM {t} WHERE workspace_id=?", (ws_id,)).rowcount
        if n:
            deleted[t] = n
    for t, sets in SCRUB.items():
        if _has(conn, t):
            have = _cols(conn, t)
            s = {k: v for k, v in sets.items() if k in have}
            if s:
                conn.execute(f"UPDATE {t} SET {', '.join(k + '=?' for k in s)} WHERE workspace_id=?", (*s.values(), ws_id))
    if "deleted_at" in _cols(conn, "jobs"):
        conn.execute("UPDATE jobs SET deleted_at=COALESCE(deleted_at, ?) WHERE workspace_id=?", (db.now(), ws_id))
    people = _people(conn, ws_id)
    done = not running and not vendor["left"]
    wcols = _cols(conn, "workspaces")
    clear = {k: v for k, v in WS_CLEAR.items() if k in wcols}
    clear["name"] = f"Deleted workspace #{ws_id}"
    if done:
        clear["deleted_at"] = db.now()
    conn.execute(f"UPDATE workspaces SET {', '.join(k + '=?' for k in clear)} WHERE id=?", (*clear.values(), ws_id))
    res.update(done=done, freed_bytes=freed, rows=deleted, people=people)
    ensure_table(conn)
    conn.execute("INSERT INTO deletion_runs (workspace_id, ran_at, done, summary) VALUES (?,?,?,?)",
                 (ws_id, db.now(), int(done), json.dumps(res, default=str)[:4000]))
    conn.commit()
    log.warning("workspace %s %s: %s rows, %s bytes, %s people, vendor %s", ws_id, "deleted" if done else "partly deleted",
                sum(deleted.values()), freed, people, vendor)
    return res


def due(conn, now: datetime | None = None) -> list:
    if "delete_after" not in _cols(conn, "workspaces"):
        return []
    return conn.execute("SELECT * FROM workspaces WHERE delete_after IS NOT NULL AND delete_after <= ? AND deleted_at IS "
                        "NULL ORDER BY id", (_iso(now or datetime.now(timezone.utc)),)).fetchall()


def tick(now: datetime | None = None) -> list:
    """Purge every workspace whose grace has passed. Returns the results."""
    out = []
    with db.connect() as conn:
        ensure_table(conn)
        for ws in due(conn, now):
            tries = conn.execute("SELECT COUNT(*) FROM deletion_runs WHERE workspace_id=?", (ws["id"],)).fetchone()[0] + 1
            try:
                out.append(purge(conn, ws, tries))
            except Exception as e:  # noqa: BLE001  one workspace never stops the rest
                conn.rollback()
                log.exception("workspace %s deletion failed", ws["id"])
                out.append({"workspace_id": ws["id"], "done": False, "error": type(e).__name__})
    return out


def maybe_tick():
    """The worker loop's call: at most hourly, never raises."""
    import time
    if time.time() - _last[0] < TICK_SECONDS:
        return
    _last[0] = time.time()
    try:
        tick()
    except Exception:  # noqa: BLE001
        log.exception("workspace deletion tick failed")


def pending(conn) -> list:
    """For the admin page: scheduled and recent deletions (ids and dates only)."""
    ensure_table(conn)
    if "delete_after" not in _cols(conn, "workspaces"):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT id, delete_requested_at, delete_after, deleted_at FROM workspaces WHERE delete_after IS NOT NULL "
        "ORDER BY delete_after DESC LIMIT 20")]
