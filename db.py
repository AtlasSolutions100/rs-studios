import os
import sqlite3
from datetime import datetime, timezone

DATA = os.environ.get("RS_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.environ.get("RS_DB", os.path.join(DATA, "rs.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 0,
    brand_name TEXT, product_name TEXT, claims TEXT, audience TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    workspace_id INTEGER,
    note TEXT,
    used_by INTEGER,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    path TEXT NOT NULL, filename TEXT, content_type TEXT,
    remote_url TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    path TEXT NOT NULL, content_type TEXT,
    seconds REAL,
    remote_url TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    tool TEXT NOT NULL,
    template_id TEXT,
    brief TEXT,
    params_json TEXT,
    request_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    credits INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_ws ON jobs(workspace_id, id);
CREATE TABLE IF NOT EXISTS outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    url TEXT,
    path TEXT);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT,
    job_id INTEGER,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    user_id INTEGER,
    pack TEXT NOT NULL,
    credits INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd',
    stripe_session_id TEXT UNIQUE,
    stripe_payment_intent TEXT,
    status TEXT NOT NULL DEFAULT 'open',      -- open | paid | failed
    created_at TEXT NOT NULL,
    paid_at TEXT);
CREATE INDEX IF NOT EXISTS idx_purchases_ws ON purchases(workspace_id, id);
CREATE TABLE IF NOT EXISTS throttle (
    key TEXT NOT NULL,
    at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_throttle ON throttle(key, at);
CREATE TABLE IF NOT EXISTS plan_invoices (
    invoice_id TEXT PRIMARY KEY,
    workspace_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 0,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    billing_reason TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    brand_name TEXT, product_name TEXT, claims TEXT, audience TEXT,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_brands_ws ON brands(workspace_id, id);
CREATE TABLE IF NOT EXISTS copy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    user_id INTEGER,
    brand_id INTEGER,
    kind TEXT NOT NULL,              -- ad | hooks | captions | headlines | email | landing | optimize
    platform TEXT,
    input_json TEXT,
    output_json TEXT,
    credits INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_copy_ws ON copy_runs(workspace_id, id);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    user_id INTEGER,
    brand_id INTEGER,
    name TEXT NOT NULL,
    brief_json TEXT,
    status TEXT NOT NULL DEFAULT 'planning',   -- planning | ready | failed
    plan_json TEXT,
    steps_json TEXT,
    credits INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_campaigns_ws ON campaigns(workspace_id, id);
CREATE TABLE IF NOT EXISTS campaign_ads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    workspace_id INTEGER NOT NULL,
    angle TEXT, platform TEXT,
    tool TEXT NOT NULL, template_id TEXT,
    brief TEXT, params_json TEXT,
    credits INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'proposed',   -- proposed | queued
    job_id INTEGER,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cads_c ON campaign_ads(campaign_id, id);
CREATE TABLE IF NOT EXISTS adhub_progress (
    workspace_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    step INTEGER NOT NULL,
    done_by INTEGER,
    done_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, platform, step));
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'draft',     -- draft | published
    theme TEXT NOT NULL DEFAULT 'light',
    accent TEXT NOT NULL DEFAULT '#BDFA25',
    blocks_json TEXT NOT NULL DEFAULT '[]',
    views INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT);
CREATE INDEX IF NOT EXISTS idx_pages_ws ON pages(workspace_id, id);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    code TEXT NOT NULL UNIQUE,
    title TEXT,
    target_url TEXT NOT NULL,
    utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
    passthrough INTEGER NOT NULL DEFAULT 1,
    clicks INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_links_ws ON links(workspace_id, id);
CREATE TABLE IF NOT EXISTS link_clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    referrer TEXT,
    country TEXT,
    device TEXT,
    utm_source TEXT, utm_medium TEXT, utm_campaign TEXT);
CREATE INDEX IF NOT EXISTS idx_clicks_link ON link_clicks(link_id, at);
CREATE TABLE IF NOT EXISTS forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '[]',
    button_text TEXT,
    success_message TEXT,
    redirect_url TEXT,
    webhook_url TEXT,
    webhook_secret TEXT,
    tags TEXT,
    submissions INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_forms_ws ON forms(workspace_id, id);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    name TEXT, phone TEXT,
    tags TEXT NOT NULL DEFAULT '',
    notes TEXT,
    source TEXT,
    submissions INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (workspace_id, email));
CREATE TABLE IF NOT EXISTS form_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL,
    workspace_id INTEGER NOT NULL,
    contact_id INTEGER,
    data_json TEXT NOT NULL,
    page_slug TEXT, link_code TEXT, referrer TEXT,
    utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
    webhook_status TEXT,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_subs_form ON form_submissions(form_id, id);
CREATE TABLE IF NOT EXISTS contact_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- form | page | link
    ref TEXT, detail TEXT,
    at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cev_c ON contact_events(contact_id, id);
CREATE TABLE IF NOT EXISTS communities (
    workspace_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    about TEXT,
    join_code TEXT NOT NULL,
    open_join INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS c_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    name TEXT,
    pw_hash TEXT,
    role TEXT NOT NULL DEFAULT 'member',      -- owner | admin | member
    status TEXT NOT NULL DEFAULT 'invited',   -- invited | active | removed
    invite_code TEXT UNIQUE,
    user_id INTEGER,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    UNIQUE (workspace_id, email));
CREATE TABLE IF NOT EXISTS c_spaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL,
    name TEXT NOT NULL, about TEXT, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS c_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, space_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT, pinned INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cposts ON c_posts(space_id, id);
CREATE TABLE IF NOT EXISTS c_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ccomments ON c_comments(post_id, id);
CREATE TABLE IF NOT EXISTS c_courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, title TEXT NOT NULL, about TEXT,
    published INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS c_modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, course_id INTEGER NOT NULL,
    title TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS c_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, module_id INTEGER NOT NULL,
    title TEXT NOT NULL, body TEXT, media_id INTEGER, embed_url TEXT, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS c_progress (
    member_id INTEGER NOT NULL, lesson_id INTEGER NOT NULL, done_at TEXT NOT NULL, PRIMARY KEY (member_id, lesson_id));
CREATE TABLE IF NOT EXISTS c_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, title TEXT NOT NULL, starts_at TEXT NOT NULL,
    tz TEXT, duration_min INTEGER NOT NULL DEFAULT 60, link TEXT, about TEXT, created_at TEXT NOT NULL);
"""

# Columns added after the first release: (table, column, definition).
# init() adds any that are missing, so old databases upgrade in place.
COLUMNS = [
    ("workspaces", "plan", "TEXT NOT NULL DEFAULT 'free'"),
    ("workspaces", "plan_status", "TEXT"),
    ("workspaces", "plan_override", "TEXT"),
    ("workspaces", "plan_credits", "INTEGER NOT NULL DEFAULT 0"),
    ("workspaces", "plan_granted", "INTEGER NOT NULL DEFAULT 0"),
    ("workspaces", "plan_period_end", "TEXT"),
    ("workspaces", "plan_cancel_at_period_end", "INTEGER NOT NULL DEFAULT 0"),
    ("workspaces", "stripe_customer_id", "TEXT"),
    ("workspaces", "stripe_subscription_id", "TEXT"),
    ("workspaces", "brand_id", "INTEGER"),
    ("users", "role", "TEXT NOT NULL DEFAULT 'member'"),
    ("invites", "created_by", "INTEGER"),
    ("jobs", "priority", "INTEGER NOT NULL DEFAULT 0"),
    ("ledger", "plan_delta", "INTEGER NOT NULL DEFAULT 0"),
]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, col, ddl in COLUMNS:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                if (table, col) == ("users", "role"):
                    # The first person in each existing workspace owns it.
                    conn.execute("UPDATE users SET role='owner' WHERE id IN "
                                 "(SELECT MIN(id) FROM users GROUP BY workspace_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_sub ON workspaces(stripe_subscription_id)")
        conn.commit()


def adjust_credits(conn, workspace_id: int, delta: int, reason: str, job_id=None, bucket: str = "pack") -> bool:
    """Atomically add (delta>0) or spend (delta<0) credits. False if it would go negative.

    Spending takes plan credits first (they expire at renewal), then pack
    credits. Adding goes to the pack bucket unless bucket="plan". The ledger's
    plan_delta records the plan-bucket part of each row, so refunds go back
    where they came from."""
    if delta < 0:
        row = conn.execute("SELECT credits, plan_credits FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if not row:
            return False
        need = -delta
        from_plan = min(max(row["plan_credits"], 0), need)
        from_pack = need - from_plan
        cur = conn.execute("UPDATE workspaces SET plan_credits = plan_credits - ?, credits = credits - ? "
                           "WHERE id=? AND plan_credits >= ? AND credits >= ?",
                           (from_plan, from_pack, workspace_id, from_plan, from_pack))
        plan_delta = -from_plan
    elif bucket == "plan":
        cur = conn.execute("UPDATE workspaces SET plan_credits = plan_credits + ? WHERE id=?", (delta, workspace_id))
        plan_delta = delta
    else:
        cur = conn.execute("UPDATE workspaces SET credits = credits + ? WHERE id=?", (delta, workspace_id))
        plan_delta = 0
    if cur.rowcount != 1:
        return False
    conn.execute("INSERT INTO ledger (workspace_id, delta, reason, job_id, created_at, plan_delta) VALUES (?,?,?,?,?,?)",
                 (workspace_id, delta, reason, job_id, now(), plan_delta))
    return True


def balance(ws) -> int:
    """Everything a workspace can spend right now (plan + pack credits)."""
    keys = ws.keys()
    return int(ws["credits"] or 0) + int((ws["plan_credits"] if "plan_credits" in keys else 0) or 0)


def refund_job(conn, job, reason: str) -> int:
    """Give a failed job's credits back to the buckets they came from. Plan
    credits go back to the plan bucket only while a plan is still on;
    otherwise everything returns as pack credits."""
    if not job["credits"]:
        return 0
    spent_plan = -(conn.execute("SELECT COALESCE(SUM(plan_delta),0) FROM ledger WHERE job_id=? AND delta<0",
                                (job["id"],)).fetchone()[0] or 0)
    spent_plan = max(0, min(spent_plan, job["credits"]))
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (job["workspace_id"],)).fetchone()
    import plans  # local import: plans has no db dependency, this keeps db importable on its own
    if spent_plan and plans.plan_code(ws) != "free":
        adjust_credits(conn, job["workspace_id"], spent_plan, reason, job["id"], bucket="plan")
        rest = job["credits"] - spent_plan
    else:
        rest = job["credits"]
    if rest:
        adjust_credits(conn, job["workspace_id"], rest, reason, job["id"])
    return job["credits"]


def set_plan_credits(conn, workspace_id: int, amount: int, reason: str):
    """Replace the plan bucket (renewal): what's left expires, the new grant lands."""
    ws = conn.execute("SELECT plan_credits FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    left = int(ws["plan_credits"] or 0) if ws else 0
    if left > 0:
        conn.execute("UPDATE workspaces SET plan_credits = 0 WHERE id=?", (workspace_id,))
        conn.execute("INSERT INTO ledger (workspace_id, delta, reason, created_at, plan_delta) VALUES (?,?,?,?,?)",
                     (workspace_id, -left, "unused plan credits expired", now(), -left))
    if amount > 0:
        adjust_credits(conn, workspace_id, amount, reason, bucket="plan")


def throttled(key: str, limit: int, window_s: float) -> bool:
    """True when `key` has been hit `limit` times in the last `window_s` seconds."""
    import time
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM throttle WHERE key=? AND at>?", (key, time.time() - window_s)).fetchone()[0]
    return n >= limit


def hit(key: str):
    """Record one attempt for `key` (and forget anything older than a day)."""
    import time
    t = time.time()
    with connect() as conn:
        conn.execute("INSERT INTO throttle (key, at) VALUES (?,?)", (key, t))
        conn.execute("DELETE FROM throttle WHERE at<?", (t - 86400,))
        conn.commit()
