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
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT, price TEXT, offer TEXT,
    claims TEXT,                     -- approved claims, one per line (the only ones AI may use)
    claims_pending TEXT,             -- JSON list of suggested claims waiting for approval
    audience TEXT, url TEXT, tags TEXT,
    primary_photo_id INTEGER,
    import_json TEXT,                -- last "import from URL": source and candidate images
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_products_ws ON products(workspace_id, brand_id, id);
CREATE TABLE IF NOT EXISTS angles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    hook TEXT, insight TEXT, audience TEXT,
    source TEXT NOT NULL DEFAULT 'user',      -- user | ai | planner
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_angles_p ON angles(product_id, id);
CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    angle_id INTEGER,
    ad_type TEXT,
    title TEXT, hook TEXT, body TEXT NOT NULL, on_screen TEXT,
    source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_scripts_p ON scripts(product_id, id);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    user_id INTEGER,
    product_id INTEGER,
    ad_type TEXT NOT NULL,
    title TEXT,
    config_json TEXT,
    credits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_batches_ws ON batches(workspace_id, id);
CREATE TABLE IF NOT EXISTS assistant_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',     -- messages, cards, setup answers
    credits INTEGER NOT NULL DEFAULT 0,       -- charged (beyond the free allowance)
    free_credits INTEGER NOT NULL DEFAULT 0,  -- covered by the free allowance
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',      -- open | closed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_asst_u ON assistant_threads(user_id, id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT);
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
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',          -- draft | on | paused | archived
    graph_json TEXT NOT NULL DEFAULT '{}',          -- the editing copy (tree + compiled nodes)
    settings_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 0, version_id INTEGER, dirty INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0, fired_at TEXT, created_by INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, turned_on_at TEXT);
CREATE INDEX IF NOT EXISTS idx_flows_ws ON flows(workspace_id, status);
CREATE TABLE IF NOT EXISTS flow_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id INTEGER NOT NULL, version INTEGER NOT NULL,
    graph_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id INTEGER NOT NULL, version_id INTEGER NOT NULL,
    workspace_id INTEGER NOT NULL, contact_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',         -- waiting | done | exited | failed
    node_id TEXT, wake_at TEXT, lease_until TEXT, started_at TEXT NOT NULL, ended_at TEXT, exit_reason TEXT,
    context_json TEXT NOT NULL DEFAULT '{}', UNIQUE(flow_id, contact_id));
CREATE INDEX IF NOT EXISTS idx_runs_wake ON flow_runs(status, wake_at);
CREATE TABLE IF NOT EXISTS flow_runs_archive (
    id INTEGER PRIMARY KEY, flow_id INTEGER NOT NULL, version_id INTEGER, workspace_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL, status TEXT, node_id TEXT, started_at TEXT, ended_at TEXT, exit_reason TEXT,
    context_json TEXT, archived_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, flow_id INTEGER NOT NULL, workspace_id INTEGER NOT NULL,
    contact_id INTEGER, node_id TEXT, kind TEXT NOT NULL, detail TEXT, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_fev_flow ON flow_events(flow_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, contact_id INTEGER, run_id INTEGER,
    flow_id INTEGER, node_id TEXT, kind TEXT NOT NULL DEFAULT 'task',   -- task | notify | email | sms | community
    title TEXT NOT NULL, body TEXT, due_at TEXT, done_at TEXT, done_by INTEGER, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tasks_ws ON tasks(workspace_id, done_at);
CREATE TABLE IF NOT EXISTS suppressions (
    workspace_id INTEGER NOT NULL, channel TEXT NOT NULL, address TEXT NOT NULL, reason TEXT NOT NULL,
    detail TEXT, at TEXT NOT NULL, PRIMARY KEY (workspace_id, channel, address));
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, contact_id INTEGER, run_id INTEGER, flow_id INTEGER,
    node_id TEXT, channel TEXT NOT NULL, to_addr TEXT, from_addr TEXT, reply_to TEXT, subject TEXT, body_text TEXT,
    body_html TEXT, status TEXT NOT NULL DEFAULT 'queued', provider_id TEXT, token TEXT UNIQUE, segments INTEGER,
    credits INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, sent_at TEXT, delivered_at TEXT,
    opened_at TEXT, clicked_at TEXT, UNIQUE(run_id, node_id));
CREATE INDEX IF NOT EXISTS idx_msg_status ON messages(status, id);
CREATE INDEX IF NOT EXISTS idx_msg_ws ON messages(workspace_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_msg_provider ON messages(provider_id);
CREATE TABLE IF NOT EXISTS message_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, token TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
    clicks INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sender_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, domain TEXT NOT NULL, local_part TEXT NOT NULL DEFAULT 'hello',
    resend_domain_id TEXT, status TEXT NOT NULL DEFAULT 'pending', dns_json TEXT, created_at TEXT NOT NULL, verified_at TEXT,
    checked_at TEXT);
CREATE TABLE IF NOT EXISTS sms_profiles (
    workspace_id INTEGER PRIMARY KEY, path TEXT NOT NULL DEFAULT 'tollfree', status TEXT NOT NULL DEFAULT 'none',
    fields_json TEXT, customer_profile_sid TEXT, brand_sid TEXT, brand_type TEXT, campaign_sid TEXT,
    messaging_service_sid TEXT, phone_sid TEXT, phone_number TEXT, verification_sid TEXT, notes TEXT, fees_json TEXT,
    number_fee_month TEXT, checked_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS webhook_events (
    provider TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT, at TEXT NOT NULL, PRIMARY KEY (provider, event_id));
CREATE TABLE IF NOT EXISTS job_reviews (
    job_id INTEGER NOT NULL, user_id INTEGER NOT NULL, workspace_id INTEGER NOT NULL, verdict TEXT NOT NULL,
    note TEXT, at TEXT NOT NULL, PRIMARY KEY (job_id, user_id));
CREATE TABLE IF NOT EXISTS free_previews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL UNIQUE, email_norm TEXT NOT NULL UNIQUE,
    ip_hash TEXT, job_id INTEGER, tries INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS c_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, title TEXT NOT NULL, starts_at TEXT NOT NULL,
    tz TEXT, duration_min INTEGER NOT NULL DEFAULT 60, link TEXT, about TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outreach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, to_addr TEXT NOT NULL, brand TEXT, subject TEXT NOT NULL, body_text TEXT,
    follow_up INTEGER NOT NULL DEFAULT 0, is_test INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'sending',
    provider_id TEXT, error TEXT, user_id INTEGER, created_at TEXT NOT NULL, sent_at TEXT);
CREATE INDEX IF NOT EXISTS idx_outreach_to ON outreach_log(to_addr, created_at);
CREATE TABLE IF NOT EXISTS outreach_suppressions (
    entry TEXT PRIMARY KEY, note TEXT, user_id INTEGER, created_at TEXT NOT NULL);
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
    # brands & products (products_v1)
    ("workspaces", "product_id", "INTEGER"),
    ("brands", "product_id", "INTEGER"),
    ("brands", "about", "TEXT"),
    ("brands", "website", "TEXT"),
    ("brands", "tone", "TEXT"),
    ("photos", "product_id", "INTEGER"),
    ("jobs", "brand_id", "INTEGER"),
    ("jobs", "product_id", "INTEGER"),
    ("jobs", "batch_id", "INTEGER"),
    ("copy_runs", "product_id", "INTEGER"),
    ("campaigns", "product_id", "INTEGER"),
    # setup assistant
    ("workspaces", "assistant_free_used", "INTEGER NOT NULL DEFAULT 0"),
    ("workspaces", "setup_json", "TEXT"),
    # results loop (phase 3)
    ("jobs", "finished_at", "TEXT"),
    ("jobs", "parent_job_id", "INTEGER"),
    ("jobs", "deleted_at", "TEXT"),
    # IA cleanup (phase 4): what kind of product, so templates fit
    ("products", "category", "TEXT"),
    # flows (phase 6): contact stage and consent, workspace sending details
    ("contacts", "stage", "TEXT"),
    ("contacts", "phone_e164", "TEXT"),
    ("contacts", "timezone", "TEXT"),
    ("contacts", "email_consent", "TEXT NOT NULL DEFAULT 'none'"),
    ("contacts", "email_consent_at", "TEXT"),
    ("contacts", "email_consent_detail", "TEXT"),
    ("contacts", "sms_consent", "TEXT NOT NULL DEFAULT 'none'"),
    ("contacts", "sms_consent_at", "TEXT"),
    ("contacts", "sms_consent_detail", "TEXT"),
    ("contacts", "unsubscribed_at", "TEXT"),
    ("contacts", "sms_stopped_at", "TEXT"),
    ("workspaces", "timezone", "TEXT"),
    ("workspaces", "sender_name", "TEXT"),
    ("workspaces", "reply_to", "TEXT"),
    ("workspaces", "postal_address", "TEXT"),
    ("workspaces", "flows_json", "TEXT"),
    # email sending (phase 7)
    ("workspaces", "messaging_accepted_at", "TEXT"),
    ("workspaces", "messaging_accepted_by", "INTEGER"),
    ("workspaces", "consent_manual", "INTEGER NOT NULL DEFAULT 0"),
    ("forms", "consent_email", "INTEGER NOT NULL DEFAULT 0"),
    ("forms", "consent_sms", "INTEGER NOT NULL DEFAULT 0"),
    ("forms", "consent_text", "TEXT"),
    ("forms", "consent_sms_text", "TEXT"),
    # texts (phase 9)
    ("messages", "scheduled_at", "TEXT"),
    # email verification (verify.py): accounts made before it are grandfathered in init()
    ("users", "email_verified_at", "TEXT"),
    ("users", "verify_nonce", "TEXT"),
    # private share links for a finished ad (share_routes.py)
    ("outputs", "share_token", "TEXT"),
    ("outputs", "shared_at", "TEXT"),
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
        _backup_before_schema_change(conn)
        conn.executescript(SCHEMA)
        for table, col, ddl in COLUMNS:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                if (table, col) == ("users", "role"):
                    # The first person in each existing workspace owns it.
                    conn.execute("UPDATE users SET role='owner' WHERE id IN "
                                 "(SELECT MIN(id) FROM users GROUP BY workspace_id)")
                if (table, col) == ("users", "email_verified_at"):
                    # Everyone who signed up before verification existed counts as verified.
                    conn.execute("UPDATE users SET email_verified_at=COALESCE(created_at, ?)", (now(),))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_sub ON workspaces(stripe_subscription_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_p ON photos(product_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_p ON jobs(workspace_id, product_id, id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outputs_share ON outputs(share_token)")
        conn.commit()
        if not conn.execute("SELECT 1 FROM meta WHERE key='owner_admin_v1'").fetchone():
            # Koby signed up through the normal form (user 1, workspace 1, the Scale
            # subscription's metadata) instead of his owner setup link, so his account
            # never got the operator flag. One time, pinned to that exact row.
            conn.execute("UPDATE users SET is_admin=1 WHERE id=1 AND workspace_id=1 "
                         "AND lower(email)='koby@rectorandstull.com'")
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('owner_admin_v1', ?)", (now(),))
            conn.commit()
        if not conn.execute("SELECT 1 FROM meta WHERE key='products_v1'").fetchone():
            _backup(conn, "pre-products")
            try:
                migrate_products(conn)
            except Exception:  # noqa: BLE001 — never keep the app from starting; it retries next start
                conn.rollback()
                import logging
                logging.getLogger("campaignify").exception("products migration failed; rolled back")


def _backup_before_schema_change(conn):
    """New tables and columns are additive, but copy the database first anyway
    (once per change) so a bad release can always be rolled back."""
    import re as _re
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "workspaces" not in have:
        return
    missing = [t for t in _re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA) if t not in have]
    for table, col, _ in COLUMNS:
        if table in have and col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            missing.append(f"{table}.{col}")
    if missing:
        _backup(conn, "pre-schema")


def _backup(conn, tag: str):
    """A consistent copy of the database next to it before a data migration
    (only when there is data to protect)."""
    try:
        if not conn.execute("SELECT 1 FROM workspaces LIMIT 1").fetchone():
            return
        dest = f"{DB_PATH}.{tag}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        with sqlite3.connect(dest) as out:
            conn.backup(out)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("campaignify").exception("database backup before %s failed", tag)


def migrate_products(conn):
    """One-time move from one brand kit per brand (brands.product_name/claims/
    audience, workspace-wide photos) to brands with products. Keeps every
    existing value: each brand's kit becomes that brand's first product, the
    workspace's photos go to its active brand's product, and work already made
    is tagged with the brand and product when the workspace had only one brand.
    The old columns stay and keep mirroring the active product (core.brand())."""
    t = now()
    for ws in conn.execute("SELECT * FROM workspaces").fetchall():
        bid = ws["brand_id"]
        if not bid or not conn.execute("SELECT 1 FROM brands WHERE id=? AND workspace_id=?", (bid, ws["id"])).fetchone():
            bid = conn.execute("INSERT INTO brands (workspace_id, brand_name, product_name, claims, audience, created_at) "
                               "VALUES (?,?,?,?,?,?)", (ws["id"], ws["brand_name"], ws["product_name"], ws["claims"],
                                                         ws["audience"], t)).lastrowid
            conn.execute("UPDATE workspaces SET brand_id=? WHERE id=?", (bid, ws["id"]))
        brands = conn.execute("SELECT * FROM brands WHERE workspace_id=? ORDER BY id", (ws["id"],)).fetchall()
        for b in brands:
            if b["product_id"] or conn.execute("SELECT 1 FROM products WHERE brand_id=?", (b["id"],)).fetchone():
                continue
            name = (b["product_name"] or "").strip()
            if not name:
                continue
            pid = conn.execute("INSERT INTO products (workspace_id, brand_id, name, claims, audience, created_at, updated_at) "
                               "VALUES (?,?,?,?,?,?,?)", (ws["id"], b["id"], name[:80], b["claims"], b["audience"], t, t)
                               ).lastrowid
            conn.execute("UPDATE brands SET product_id=? WHERE id=?", (pid, b["id"]))
        active = conn.execute("SELECT product_id FROM brands WHERE id=?", (bid,)).fetchone()["product_id"]
        if active:
            conn.execute("UPDATE workspaces SET product_id=? WHERE id=?", (active, ws["id"]))
            conn.execute("UPDATE photos SET product_id=? WHERE workspace_id=? AND product_id IS NULL", (active, ws["id"]))
            newest = conn.execute("SELECT MAX(id) FROM photos WHERE product_id=?", (active,)).fetchone()[0]
            if newest:
                conn.execute("UPDATE products SET primary_photo_id=? WHERE id=? AND primary_photo_id IS NULL", (newest, active))
        for b in brands:
            pid = conn.execute("SELECT product_id FROM brands WHERE id=?", (b["id"],)).fetchone()["product_id"]
            if pid:
                conn.execute("UPDATE copy_runs SET product_id=? WHERE brand_id=? AND product_id IS NULL", (pid, b["id"]))
                conn.execute("UPDATE campaigns SET product_id=? WHERE brand_id=? AND product_id IS NULL", (pid, b["id"]))
        if len(brands) == 1:
            conn.execute("UPDATE jobs SET brand_id=?, product_id=? WHERE workspace_id=? AND brand_id IS NULL",
                         (bid, active, ws["id"]))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('products_v1', ?)", (t,))
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
