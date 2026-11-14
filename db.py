import os
import sqlite3
import time
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
-- Password reset links and spent nudge login links (login_security.py). Only a SHA-256
-- of each token is kept. kind: reset | magic. expires_at: epoch seconds.
CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id INTEGER NOT NULL, email TEXT NOT NULL,
    expires_at INTEGER NOT NULL, used_at TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_login_tokens_user ON login_tokens(user_id, kind);
-- Account emails (mailer.py, Nov 8 2026): codes, confirm links, resets, nudges, alerts. status:
-- sent | failed | skipped | deferred (quota or rate; payload kept for a transactional one) |
-- retrying | retried (the deferred copy went out later) | dropped. Workspace emails stay in `messages`.
CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, to_addr TEXT NOT NULL, subject TEXT, kind TEXT, status TEXT NOT NULL,
    error TEXT, created_at TEXT NOT NULL, retry_after TEXT, payload TEXT, retried_at TEXT);
CREATE INDEX IF NOT EXISTS idx_email_log_created ON email_log(created_at);
CREATE INDEX IF NOT EXISTS idx_email_log_status ON email_log(status);
CREATE INDEX IF NOT EXISTS idx_email_log_to ON email_log(to_addr, kind);
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
-- task_queue (tasks.py, roadmap M7.2): long AI runs the web hands to the worker process. A worker claims a queued
-- row with one compare-and-set UPDATE (claimed_by, lease_until) and renews the lease while it runs it.
CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                      -- planner | competitors | free_plan
    payload_json TEXT NOT NULL,              -- ids only (campaign, report, credit hold), never a token
    workspace_id INTEGER, ref TEXT,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | interrupted | skipped
    claimed_by TEXT, claimed_at TEXT, lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT);
CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status, id);
-- worker_leases (workerproc.py, M7.2): one worker process runs the loop at a time; a lease the holder stopped
-- renewing lapses after WORKER_LEASE_S and another worker may take it.
CREATE TABLE IF NOT EXISTS worker_leases (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    until TEXT NOT NULL,
    info TEXT);
-- deletion_runs (wsdelete.py, roadmap M7.5): each purge of a workspace whose owner asked for deletion and whose
-- 7-day grace has passed. ids and counts only.
CREATE TABLE IF NOT EXISTS deletion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    ran_at TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    summary TEXT);
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
CREATE TABLE IF NOT EXISTS launch_ticks (       -- launch_kit.py: a plan's own launch items ticked (hub steps stay in adhub_progress)
    workspace_id INTEGER NOT NULL,
    campaign_id INTEGER NOT NULL,
    item TEXT NOT NULL,                       -- c<n> (the plan's checklist item n), disc:<platform>, lib
    done_by INTEGER,
    done_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, campaign_id, item));
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
CREATE TABLE IF NOT EXISTS desk_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER, product_id INTEGER,
    ask TEXT NOT NULL DEFAULT '',                   -- the sentence the campaign came from (desk.py)
    made_json TEXT NOT NULL DEFAULT '[]',           -- what one confirmation made: [{kind, name, href, state}]
    credits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_desk_runs_ws ON desk_runs(workspace_id, id);
CREATE TABLE IF NOT EXISTS desk_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER, product_id INTEGER,
    title TEXT NOT NULL DEFAULT '',                 -- a campaign on the desk: one conversation (desk.py)
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_desk_threads_ws ON desk_threads(workspace_id, archived, updated_at);
CREATE TABLE IF NOT EXISTS desk_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER NOT NULL, workspace_id INTEGER NOT NULL,
    role TEXT NOT NULL,                             -- member | desk
    kind TEXT NOT NULL,                             -- said | plan | made | note
    body_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_desk_turns_thread ON desk_turns(thread_id, id);
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
CREATE TABLE IF NOT EXISTS ad_signup_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL UNIQUE, email_norm TEXT NOT NULL UNIQUE,
    ip_hash TEXT, credits INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ad_grants_ip ON ad_signup_grants(ip_hash, created_at);
-- Card on file (card_on_file.py, RS_CARD_REQUIRED): one row per card saved. `trial` = 1 when that
-- workspace got (or already had) the starter credits; a card or email with a trial row gets none again.
CREATE TABLE IF NOT EXISTS card_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, fingerprint TEXT, email_norm TEXT,
    trial INTEGER NOT NULL DEFAULT 0, credits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_card_trials_fp ON card_trials(fingerprint);
CREATE INDEX IF NOT EXISTS idx_card_trials_email ON card_trials(email_norm);
-- Plan trials (plan_gate.py, PLANS_ONLY): one row per trial subscription. One trial per workspace, per person
-- (email_norm) and per card (fingerprint); a row with credits 0 is a Stripe trial that carried no credits of ours
-- (a second one).
CREATE TABLE IF NOT EXISTS plan_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER, email_norm TEXT,
    plan TEXT NOT NULL, subscription_id TEXT, credits INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
    ends_at TEXT, fingerprint TEXT);
CREATE INDEX IF NOT EXISTS idx_plan_trials_fp ON plan_trials(fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_trials_sub ON plan_trials(subscription_id);
CREATE INDEX IF NOT EXISTS idx_plan_trials_ws ON plan_trials(workspace_id);
CREATE INDEX IF NOT EXISTS idx_plan_trials_email ON plan_trials(email_norm);
-- Clone an ad (adclone.py): a reference ad read into beats, and the remake planned for the member's product.
CREATE TABLE IF NOT EXISTS ad_clones (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER,
    product_id INTEGER, source TEXT, file_path TEXT, media TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
    analysis_json TEXT, remake_json TEXT, frames_json TEXT, hold_id INTEGER, credits INTEGER NOT NULL DEFAULT 0,
    thread_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_ad_clones_ws ON ad_clones(workspace_id, id);
-- The welcome credits (plan_gate.grant_welcome): one row per workspace that got them, once per person (email_norm).
CREATE TABLE IF NOT EXISTS welcome_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL UNIQUE, user_id INTEGER, email_norm TEXT,
    credits INTEGER NOT NULL DEFAULT 0, granted_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_welcome_grants_email ON welcome_grants(email_norm);
-- The first ads on us (first_ads.py): one row per gifted ad. A row is claimed (job_id NULL) before anything is
-- granted, so two requests at once can never both take the same slot; a slot whose ad did not start is given back.
CREATE TABLE IF NOT EXISTS first_gifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, kind TEXT NOT NULL, slot INTEGER NOT NULL,
    job_id INTEGER, credits INTEGER NOT NULL DEFAULT 0, email_norm TEXT, created_at TEXT NOT NULL,
    UNIQUE(workspace_id, kind, slot));
CREATE INDEX IF NOT EXISTS idx_first_gifts_email ON first_gifts(email_norm);
-- Done-for-you requests (tools_page.py): saved before they are emailed, so one is never lost to a mail failure.
CREATE TABLE IF NOT EXISTS service_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER, name TEXT, email TEXT NOT NULL,
    phone TEXT, sells TEXT, wants TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT);
CREATE INDEX IF NOT EXISTS idx_service_requests_ws ON service_requests(workspace_id);
-- The free campaign plan of an out-of-credits workspace (plan_paywall.py, RS_PLAN_PAYWALL): 1 per 30 days.
CREATE TABLE IF NOT EXISTS free_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, campaign_id INTEGER, report_id INTEGER,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_free_plans_ws ON free_plans(workspace_id, created_at);
-- "Your free ads are waiting" emails to sign-ups with no job yet (nudges.py): one row per step sent.
-- Step 10 is the one-off "Your ads are ready" email for a member's first batch (ready_email.py).
CREATE TABLE IF NOT EXISTS first_run_nudges (
    user_id INTEGER NOT NULL, step INTEGER NOT NULL, sent_at TEXT NOT NULL, PRIMARY KEY (user_id, step));
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
-- Google Ads conversions of our own site (gads.py): one row per checkout we started, reported once.
CREATE TABLE IF NOT EXISTS ad_conversions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref TEXT NOT NULL, workspace_id INTEGER,
    user_id INTEGER, value_cents INTEGER, currency TEXT, created_at TEXT NOT NULL, reported_at TEXT,
    UNIQUE(kind, ref));
-- Server-side copies of our own conversions (conversions.py: Meta CAPI, TikTok Events API, Reddit CAPI),
-- one row per platform and event id, sent once and retried by the worker.
CREATE TABLE IF NOT EXISTS conversion_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, event TEXT NOT NULL, event_id TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
    created_at TEXT NOT NULL, sent_at TEXT, UNIQUE(platform, event_id));
CREATE INDEX IF NOT EXISTS idx_conversion_queue_status ON conversion_queue(status, created_at);
-- First-party funnel events (site_events.py): append-only, read by HQ. The columns are a contract
-- with HQ's reader; see site_events.py before changing them.
CREATE TABLE IF NOT EXISTS site_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, vid TEXT, name TEXT NOT NULL, path TEXT,
    utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT,
    click_id TEXT, click_type TEXT, referrer_host TEXT, detail TEXT, workspace_id INTEGER);
CREATE INDEX IF NOT EXISTS idx_site_events_ts ON site_events(ts);
CREATE INDEX IF NOT EXISTS idx_site_events_name ON site_events(name, ts);
CREATE INDEX IF NOT EXISTS idx_site_events_vid ON site_events(vid, id);
CREATE INDEX IF NOT EXISTS idx_site_events_ws ON site_events(workspace_id, name);
-- "Import from a link" tries (product_routes.import_url): which store, how it was read
-- (page / jsonld / og / shopify-json / manual) and the outcome (ok / blocked / failed), for the admin system card.
CREATE TABLE IF NOT EXISTS import_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER, domain TEXT NOT NULL, method TEXT NOT NULL,
    status TEXT NOT NULL, http_status INTEGER, detail TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_import_attempts_at ON import_attempts(created_at);
-- Single-use free-month codes (compcodes.py, /admin/codes): one per business, one per workspace.
CREATE TABLE IF NOT EXISTS comp_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, label TEXT, plan TEXT NOT NULL,
    months INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, created_by INTEGER, redeem_by TEXT, batch TEXT,
    redeemed_at TEXT, workspace_id INTEGER, redeemed_by INTEGER, comp_until TEXT,
    revoked INTEGER NOT NULL DEFAULT 0, revoked_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comp_codes_ws ON comp_codes(workspace_id) WHERE workspace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_comp_codes_batch ON comp_codes(batch);
-- Competitor Snapshot runs (competitors.py): one row per run, kept as history; a brand's latest
-- 'ready' row is its current report. status: running | ready | failed.
CREATE TABLE IF NOT EXISTS competitor_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, brand_id INTEGER, user_id INTEGER,
    status TEXT NOT NULL DEFAULT 'running', inputs_json TEXT, report_json TEXT, sources_json TEXT, steps_json TEXT,
    usd_cost REAL NOT NULL DEFAULT 0, credits INTEGER NOT NULL DEFAULT 0, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_competitor_reports_ws ON competitor_reports(workspace_id, brand_id, id);
-- Creator Pools (creator_pools.py): a brand pays a flat fee plus a pool; Crowdcut clippers make and
-- post the videos. status: draft | checkout | paid (paid, not yet on Crowdcut) | live | paused | ended.
CREATE TABLE IF NOT EXISTS creator_pools (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, user_id INTEGER, brand_id INTEGER,
    title TEXT NOT NULL, brand TEXT NOT NULL, product_url TEXT, brief TEXT, talking_points TEXT, dos TEXT, donts TEXT,
    modes TEXT NOT NULL, links TEXT, platforms TEXT NOT NULL, rate_per_1k REAL NOT NULL, per_post_cap_usd REAL NOT NULL,
    per_creator_cap_usd REAL NOT NULL, min_views INTEGER NOT NULL, days INTEGER NOT NULL, pool_cents INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft', stripe_session_id TEXT, stripe_payment_intent TEXT,
    paid_at TEXT, crowdcut_id INTEGER, live_at TEXT, sync_error TEXT, numbers_json TEXT, synced_at TEXT,
    new_posts INTEGER NOT NULL DEFAULT 0, ended_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_creator_pools_ws ON creator_pools(workspace_id, id);
CREATE TABLE IF NOT EXISTS creator_pool_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pool_id INTEGER NOT NULL, workspace_id INTEGER NOT NULL, path TEXT NOT NULL,
    filename TEXT, content_type TEXT, kind TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_creator_pool_assets ON creator_pool_assets(pool_id);
-- Unspent pool money owed back to the brand (the fee is kept). Never automatic: an admin presses
-- Refund (Stripe) or records a refund sent by hand. status: due | refunded
CREATE TABLE IF NOT EXISTS creator_pool_refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pool_id INTEGER NOT NULL UNIQUE, workspace_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'due', method TEXT, stripe_refund_id TEXT,
    reference TEXT, created_at TEXT NOT NULL, updated_at TEXT, done_at TEXT, done_by INTEGER);
CREATE TABLE IF NOT EXISTS credit_holds (           -- credits.py: an AI run's credits taken before it runs
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,                 -- the most the run can cost (its quote or cap)
    plan_part INTEGER NOT NULL DEFAULT 0,    -- how much of it came from the plan bucket
    label TEXT, ref TEXT,                    -- ledger wording; the run it pays for ("competitor:12")
    status TEXT NOT NULL DEFAULT 'held',     -- held | settled | released
    charged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_holds_status ON credit_holds(status, created_at);
CREATE INDEX IF NOT EXISTS idx_holds_ref ON credit_holds(ref);
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY, type TEXT, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credit_reversals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,                    -- NULL while unmatched (the payment isn't credited yet)
    kind TEXT NOT NULL,                      -- refund | dispute
    stripe_id TEXT NOT NULL,                 -- the charge (refund) or dispute id
    payment_intent TEXT,
    source TEXT,                             -- purchase | plan_invoice
    source_id TEXT,                          -- purchases.id or plan_invoices.invoice_id
    amount_cents INTEGER NOT NULL DEFAULT 0, -- refunded or disputed so far
    credits INTEGER NOT NULL DEFAULT 0,      -- credits voided by this reversal
    status TEXT NOT NULL,                    -- unmatched | applied | voided | won | lost | cleared | restored
    reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (kind, stripe_id));
CREATE INDEX IF NOT EXISTS idx_reversals_pi ON credit_reversals(payment_intent);
CREATE INDEX IF NOT EXISTS idx_reversals_ws ON credit_reversals(workspace_id, status);
-- Ad platforms (adplatforms/, roadmap M2.1): a workspace's own connected ad accounts. Tokens are
-- kept only as token boxes (secretsbox.py, AES-GCM sealed to "<table>:<workspace_id>").
CREATE TABLE IF NOT EXISTS ad_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    platform TEXT NOT NULL,                  -- meta | google | tiktok | snapchat
    external_account_id TEXT NOT NULL,       -- Meta: the ad account id without act_; "pending:<hex>" until picked
    account_name TEXT, currency TEXT, timezone TEXT,
    business_id TEXT,
    page_id TEXT, page_name TEXT,
    instagram_actor_id TEXT, instagram_username TEXT,   -- the Page's Instagram account (instagram_user_id on creatives)
    external_user_id TEXT,                   -- the platform user who granted access (revoke only when nobody else uses it)
    token_enc BLOB, token_expires_at TEXT, refresh_token_enc BLOB,
    scopes TEXT,                             -- granted, space separated
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | active | expired | revoked | error
    last_error TEXT,
    pending_json TEXT,                       -- what can still be picked (ids and names only, never a token)
    created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, platform, external_account_id));
CREATE INDEX IF NOT EXISTS idx_adconn_user ON ad_connections(platform, external_user_id);
-- Meta's deauthorize and data deletion callbacks (adconnect_routes.py, M-3): one row per request, so the
-- status page can answer "done" for a confirmation code. The Meta user id is kept only as a hash.
CREATE TABLE IF NOT EXISTS meta_data_requests (
    code TEXT PRIMARY KEY, kind TEXT NOT NULL, user_hash TEXT NOT NULL, connections INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, created_at TEXT NOT NULL, done_at TEXT);
CREATE TABLE IF NOT EXISTS pixels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    connection_id INTEGER,                   -- the ad_connections row it was picked from
    external_pixel_id TEXT NOT NULL, name TEXT,
    capi_token_enc BLOB,                     -- token box, from M4 (conversions)
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, platform, external_pixel_id));
-- adhttp.py: per (platform, ad account) point budget, and per-object edit locks ("edit:<id>").
CREATE TABLE IF NOT EXISTS api_budget (
    platform TEXT NOT NULL, account TEXT NOT NULL,
    points REAL NOT NULL DEFAULT 0, at REAL NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, account));
-- Launches (launches.py, roadmap M2.3): a plan made into a PAUSED campaign on the member's own ad account.
-- approval_cards: what the member saw and approved ("what Campaignify will create"), with the spec it came from.
CREATE TABLE IF NOT EXISTS approval_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    launch_id INTEGER,                       -- set once approved and the launch exists
    kind TEXT NOT NULL,                      -- launch | budget_change | status_change (admanage.py, M2.4) | pause_all
    campaign_id INTEGER, connection_id INTEGER,
    spec_json TEXT, spec_hash TEXT,          -- the CampaignSpec the card was made from (adspec.to_dict)
    before_json TEXT, after_json TEXT,       -- after_json: the card itself (lines, objects, issues)
    created_by INTEGER, created_at TEXT NOT NULL,
    decided_by INTEGER, decision TEXT, decided_at TEXT,   -- decision: approved | rejected
    applied_at TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS idx_cards_ws ON approval_cards(workspace_id, campaign_id, id);
CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    plan_id INTEGER,                         -- campaigns.id (the planner's plan)
    platform TEXT NOT NULL, connection_id INTEGER NOT NULL,
    card_id INTEGER,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,                    -- queued | launching | paused | failed | rolled_back
    external_campaign_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,    -- sha256(workspace, plan, spec hash): one launch per approved spec
    error TEXT,                              -- plain words for the member
    created_by INTEGER, approved_by INTEGER, approved_at TEXT,
    next_at TEXT, lease_at TEXT,             -- the worker's schedule (rate limits, video processing) and claim
    runs INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT);
CREATE INDEX IF NOT EXISTS idx_launches_ws ON launches(workspace_id, plan_id, id);
CREATE INDEX IF NOT EXISTS idx_launches_status ON launches(status);
-- launch_steps: the saga's ledger, one row per platform call that makes an object, in order.
CREATE TABLE IF NOT EXISTS launch_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    launch_id INTEGER NOT NULL,
    step_no INTEGER NOT NULL,
    step_key TEXT NOT NULL,                  -- upload:1, creative:1, campaign, adset, ad:1 (in the external name)
    kind TEXT NOT NULL,                      -- upload | creative | campaign | adset | ad
    name TEXT,                               -- the object's name without the cfy tag (what the card shows)
    request_json TEXT,                       -- the call's fields; "$key" points at an earlier step's result
    external_id TEXT, result_json TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | compensated | kept
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,                              -- scrubbed platform error (never a token)
    started_at TEXT, finished_at TEXT,
    UNIQUE (launch_id, step_no));
-- ad_objects: every object a launch made on a platform, with its external id.
CREATE TABLE IF NOT EXISTS ad_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    launch_id INTEGER NOT NULL,
    workspace_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,                      -- video | creative | campaign | adset | ad
    external_id TEXT NOT NULL,
    parent_external_id TEXT,
    name TEXT,
    status TEXT,                             -- PAUSED | DELETED (removed by compensation) | ...
    created_at TEXT NOT NULL,
    UNIQUE (platform, external_id));
CREATE INDEX IF NOT EXISTS idx_adobj_launch ON ad_objects(launch_id);
-- ad_insights_daily (adinsights.py, roadmap M2.5): what Meta reports per ad per day, in the ad account's currency and
-- time zone. Pulled every few hours for today and the 14 days before it (two full weeks for the creative fatigue check, M4.2; late
-- purchases settle inside it). Money in minor units (adspec.minor_factor); rates are computed from the sums.
CREATE TABLE IF NOT EXISTS ad_insights_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    connection_id INTEGER, launch_id INTEGER,
    platform TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'ad',        -- ad (campaign and ad set figures are sums of their ads)
    external_object_id TEXT NOT NULL,        -- the ad id
    campaign_external_id TEXT, adset_external_id TEXT,
    day TEXT NOT NULL,                       -- YYYY-MM-DD in the ad account's time zone (Meta's date_start)
    currency TEXT,
    spend_minor INTEGER NOT NULL DEFAULT 0,
    impressions INTEGER NOT NULL DEFAULT 0, reach INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0,       -- all clicks (Meta's clicks)
    link_clicks INTEGER NOT NULL DEFAULT 0,  -- Meta's inline_link_clicks (Ads Manager "Link clicks")
    ctr REAL,                                -- link click-through rate in %, link_clicks / impressions * 100
    ctr_all REAL,                            -- Meta's ctr (all clicks) in %
    cpc_minor INTEGER,                       -- spend per link click
    results INTEGER NOT NULL DEFAULT 0, result_kind TEXT,   -- purchase | lead | link_click | reach | <event>
    cost_per_result_minor INTEGER,
    leads INTEGER NOT NULL DEFAULT 0, purchases INTEGER NOT NULL DEFAULT 0, purchase_value_minor INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT,                           -- Meta's row as it came (ids and numbers only)
    fetched_at TEXT NOT NULL,
    UNIQUE (platform, external_object_id, day));
CREATE INDEX IF NOT EXISTS idx_insights_launch ON ad_insights_daily(launch_id, day);
CREATE INDEX IF NOT EXISTS idx_insights_ws ON ad_insights_daily(workspace_id, day);
-- lead_sources (adleads.py, roadmap M4.1): which ad brought each form lead. One row per sent form that carried ad
-- tags or a click id; the ad and its launch when they match this workspace's launched objects.
CREATE TABLE IF NOT EXISTS lead_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    contact_id INTEGER, submission_id INTEGER UNIQUE, form_id INTEGER, page_slug TEXT,
    platform TEXT,                           -- meta | google | tiktok | snapchat (NULL: tags without an ad platform)
    launch_id INTEGER,                       -- the Campaignify launch of the ad, when matched
    campaign_external_id TEXT, ad_external_id TEXT,
    matched TEXT NOT NULL DEFAULT 'none',    -- ad | campaign | platform | none
    utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT,
    click_type TEXT, click_id TEXT,          -- gclid | fbclid | ttclid (only when the page or form measures visitors)
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_lead_sources_launch ON lead_sources(workspace_id, launch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lead_sources_contact ON lead_sources(workspace_id, contact_id);
-- weekly_reports (weekly_report.py, roadmap M4.4): one row per workspace, person and report week (the Monday it was
-- due), written before the send so nobody is mailed twice. status: sending | sent | failed | nothing (no numbers).
CREATE TABLE IF NOT EXISTS weekly_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL, user_id INTEGER NOT NULL, week TEXT NOT NULL,
    status TEXT NOT NULL, subject TEXT, error TEXT,
    created_at TEXT NOT NULL, sent_at TEXT,
    UNIQUE (workspace_id, user_id, week));
-- Page views by date (results.py, pages_routes.public). pages.views is a running count with no dates; this keeps one
-- row per page per day, in the workspace's own day, so a date range can count views. Counted from the release that
-- added it (Nov 12 2026) on: there is no history before that. No visitor data: a number per page per day.
CREATE TABLE IF NOT EXISTS page_views_daily (
    page_id INTEGER NOT NULL,
    workspace_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    views INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (page_id, day));
CREATE INDEX IF NOT EXISTS idx_page_views_daily_ws ON page_views_daily(workspace_id, day);

-- Conversion tracking on members' own pages and forms (conversions.py, site_events.py; roadmap M3.4).
-- page_events: the first-party collector. One row per page view sent server side and per form lead, with the
-- UUID the browser pixels fire with. No IP address, no raw email or phone: hashes_json holds SHA-256 values only.
CREATE TABLE IF NOT EXISTS page_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    page_id INTEGER, form_id INTEGER, submission_id INTEGER,
    name TEXT NOT NULL,                      -- PageView | Lead
    event_id TEXT NOT NULL UNIQUE,           -- a UUID (version 4)
    path TEXT,                               -- no query string
    click_type TEXT, click_id TEXT,          -- gclid | fbclid | ttclid of the visit, when it had one
    hashes_json TEXT,                        -- Lead: {"google": {"email": sha256, ...}} for the Google tag's user_data
    browser_at TEXT,                         -- when the thank-you page fired the browser copy (once)
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_page_events_ws ON page_events(workspace_id, name, created_at);
-- ws_conversion_queue: the server-side copies, one row per destination and event id, sent once, retried by the
-- worker. dest_id: pixels.id (meta, tiktok) or conversion_actions.id (google). payload: hashed identifiers only.
CREATE TABLE IF NOT EXISTS ws_conversion_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    platform TEXT NOT NULL,                  -- meta | tiktok | google
    dest_id INTEGER NOT NULL,
    event TEXT NOT NULL, event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | sending | sent | failed | dropped
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
    created_at TEXT NOT NULL, sent_at TEXT,
    UNIQUE (platform, dest_id, event_id));
CREATE INDEX IF NOT EXISTS idx_wsconv_status ON ws_conversion_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_wsconv_dest ON ws_conversion_queue(workspace_id, platform, dest_id, id);
-- conversion_actions: Google Ads conversion actions on a connected customer (filled by M3.5's
-- ConversionActionService sync). kind lead (tag on pages), lead_server (Data Manager uploads), purchase.
CREATE TABLE IF NOT EXISTS conversion_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    connection_id INTEGER NOT NULL,
    customer_id TEXT NOT NULL,
    kind TEXT NOT NULL,                      -- lead | lead_server | purchase
    external_id TEXT NOT NULL, resource_name TEXT, name TEXT,
    status TEXT,                             -- ENABLED | REMOVED | HIDDEN
    tag_id TEXT,                             -- AW-123456789 (tag-based kinds)
    label TEXT,                              -- the conversion label: send_to is AW-123456789/<label>
    created_by_us INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (connection_id, kind));
-- conversion_setups: what M3.5's sync read from a Google connection's customer (conversion_actions.py): the tag id,
-- a manager that owns its conversions (cross-account tracking), the customer data terms and enhanced conversions for
-- leads as Google reports them, other primary actions the account already had, Google's purchase snippet to copy.
CREATE TABLE IF NOT EXISTS conversion_setups (
    connection_id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL,
    customer_id TEXT, tag_id TEXT, managed_by TEXT,
    accepted_terms INTEGER, ec_leads INTEGER,  -- 1 / 0, NULL when Google didn't say
    other_actions INTEGER NOT NULL DEFAULT 0,
    purchase_snippet TEXT,
    status TEXT,                             -- ready | partial | none | managed_elsewhere | error
    error TEXT, checked_at TEXT);
-- Quality wave 2. asset_qa (image_qa.py): one row per picture the automatic check looked at (or skipped), kept
-- for the acceptance numbers. asset_fixes (free_fix.py): the one free fix of a delivered picture.
-- claims_checks (claims_check.py): the last claims check of a script, so Generate doesn't pay for it twice.
CREATE TABLE IF NOT EXISTS asset_qa (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, job_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1, idx INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL DEFAULT 'image',
    ad_type TEXT, preset TEXT,
    verdict TEXT NOT NULL,                   -- pass | fail | skipped
    reasons_json TEXT, checks_json TEXT, model TEXT, usd REAL NOT NULL DEFAULT 0,
    url TEXT, path TEXT, kept INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_asset_qa_job ON asset_qa(job_id, attempt);
CREATE INDEX IF NOT EXISTS idx_asset_qa_at ON asset_qa(created_at);
CREATE TABLE IF NOT EXISTS asset_fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, job_id INTEGER NOT NULL,
    output_id INTEGER NOT NULL, fix_job_id INTEGER, user_id INTEGER, note TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'used',     -- used | released (the fix failed on our side: it can be asked for again)
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_asset_fixes_ws ON asset_fixes(workspace_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_asset_fixes_one ON asset_fixes(output_id) WHERE status='used';
CREATE TABLE IF NOT EXISTS claims_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
    text_hash TEXT NOT NULL, status TEXT NOT NULL,   -- ok | flagged | skipped
    result_json TEXT, usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_claims_checks ON claims_checks(workspace_id, product_id, text_hash);
-- m15, keyframe_video.py: what a keyframe-first video is made of. One row per vendor request: the keyframe still,
-- each beat, each generated cutaway; a second try of one is a new row (attempt 2). The job itself stays 'running'.
CREATE TABLE IF NOT EXISTS video_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, job_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                      -- keyframe | beat | broll
    idx INTEGER NOT NULL DEFAULT 0,          -- which beat (0-based); 0 for the keyframe
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | made (downloaded, not yet checked) | done | failed | skipped
    model TEXT, inputs_json TEXT, submit_key TEXT, request_id TEXT, tries INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0,
    url TEXT, path TEXT, remote_url TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_video_parts_job ON video_parts(job_id, kind, idx, attempt);
CREATE INDEX IF NOT EXISTS idx_video_parts_status ON video_parts(status);
-- m38, recordings.py: a product's screen recording (the member's own capture of their app or site), played as it is
-- in the screen beats of a video preset. One per product; the file is in the workspace's uploads folder.
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
    path TEXT NOT NULL, filename TEXT, content_type TEXT,
    seconds REAL NOT NULL DEFAULT 0, width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_recordings_product ON recordings(workspace_id, product_id);
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
    ("ledger", "hold_id", "INTEGER"),          # credit_holds row a reserve/settle/release row belongs to (credits.py)
    # brands & products (products_v1)
    ("workspaces", "product_id", "INTEGER"),
    ("brands", "product_id", "INTEGER"),
    ("brands", "about", "TEXT"),
    ("brands", "website", "TEXT"),
    ("brands", "tone", "TEXT"),
    ("photos", "product_id", "INTEGER"),
    ("photos", "brand_id", "INTEGER"),         # brand assets (brand_assets.py): the brand a picture belongs to
    ("photos", "kind", "TEXT"),                # product, packaging, lifestyle, logo, place, person, other
    ("photos", "label", "TEXT"),
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
    ("jobs", "vendor_deleted_at", "TEXT"),          # wsdelete.py (M7.5): the vendor's copy was deleted (or why not)
    ("workspaces", "delete_requested_at", "TEXT"),  # wsdelete.py (M7.5): the owner asked to delete the workspace
    ("workspaces", "delete_after", "TEXT"),         # ... and it is purged after this (7 days of grace)
    ("workspaces", "delete_requested_by", "INTEGER"),
    ("workspaces", "deleted_at", "TEXT"),
    # vendor submits (worker.py): the idempotency key, stored before the call, and the worker's claim on it
    ("jobs", "submit_key", "TEXT"),
    ("jobs", "claimed_at", "TEXT"),
    # IA cleanup (phase 4): what kind of product, so templates fit
    ("products", "category", "TEXT"),
    # flows (phase 6): contact stage and consent, workspace sending details
    ("contacts", "stage", "TEXT"),
    # the lead pipeline (pipeline.py): the workspace's own names for its stages, and the stages it added
    ("workspaces", "pipeline_json", "TEXT"),
    ("workspaces", "stripe_old_json", "TEXT"),   # ids from another Stripe account, set aside (stripe_setup.forget)
    ("desk_runs", "thread_id", "INTEGER"),          # the conversation a desk run belongs to (desk_threads)
    ("workspaces", "trial_open", "INTEGER NOT NULL DEFAULT 0"),   # a plan trial began and nothing is paid yet (plan_gate.py)
    ("workspaces", "trial_ends_at", "TEXT"),                       # when that trial ends (the first charge)
    ("plan_trials", "reminded_at", "TEXT"),                        # when the trial's reminder email was claimed
    ("service_requests", "details", "TEXT"),                       # what the workspace held when it asked (tools_page.snapshot)
    ("brands", "rivals_json", "TEXT"),              # competitors found at import: [{name, website, why, sources}]
    ("brands", "enrich_json", "TEXT"),              # the last reading of the brand's site (brand_identity.py)
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
    ("users", "verify_code_hash", "TEXT"),      # HMAC of the 6-digit code (verify.py), never the code itself
    ("users", "verify_code_exp", "INTEGER"),    # epoch seconds
    ("users", "verify_code_tries", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "pending_email", "TEXT"),         # the address an unconfirmed member asked to change to (verify.change_address)
    # private share links for a finished ad (share_routes.py)
    ("outputs", "share_token", "TEXT"),
    ("outputs", "shared_at", "TEXT"),
    ("users", "first_touch", "TEXT"),            # JSON: UTM tags / click ids of the visit that brought them
    ("ad_conversions", "ctx_json", "TEXT"),      # browser context for the server-side purchase (conversions.py)
    ("users", "auth_provider", "TEXT"),          # "google" for accounts made with Continue with Google (google_auth.py)
    # login hardening (login_security.py): sessions carry this number; a password reset bumps it,
    # which ends every other session (N8). A form sign-up waits for its emailed link before
    # its first login (signup_pending=1 until the email is confirmed, SEC-8).
    ("users", "session_version", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "signup_pending", "INTEGER NOT NULL DEFAULT 0"),
    # free-month codes (compcodes.py): the plan, when it ends, when the next month's credits land
    ("workspaces", "comp_plan", "TEXT"),
    ("workspaces", "comp_until", "TEXT"),
    ("workspaces", "comp_next_grant", "TEXT"),
    # refunds and disputes (billing_holds.py): downloads only until cleared
    ("workspaces", "billing_hold", "INTEGER NOT NULL DEFAULT 0"),
    ("workspaces", "billing_hold_reason", "TEXT"),
    ("workspaces", "billing_hold_at", "TEXT"),
    # card on file at sign-up (card_on_file.py): the saved card, and the starter credits waiting for it
    ("workspaces", "card_saved_at", "TEXT"),
    ("workspaces", "card_customer_id", "TEXT"),
    ("workspaces", "card_pm_id", "TEXT"),
    ("workspaces", "card_label", "TEXT"),
    ("workspaces", "card_pending_credits", "INTEGER NOT NULL DEFAULT 0"),
    ("workspaces", "card_pending_reason", "TEXT"),
    # L-3: the normalized email (preview.norm_email), indexed, so one-trial-per-person is a lookup, not a scan of
    # every Gmail user; filled at sign-up and backfilled in init(). L-2: the member whose card a card_trials row is.
    ("users", "email_norm", "TEXT"),
    ("card_trials", "user_id", "INTEGER"),
    ("campaigns", "free_plan", "INTEGER NOT NULL DEFAULT 0"),   # made free while out of credits (plan_paywall.py)
    ("plan_invoices", "payment_intent", "TEXT"),
    # plans switch on only when paid (BILL-3); a paid period is kept to its end (plans.GRACE_STATUSES)
    ("workspaces", "plan_paid_through", "TEXT"),
    ("workspaces", "plan_pending", "TEXT"),
    ("workspaces", "plan_pending_at", "TEXT"),
    # parked reversals (billing_holds.retry_parked, N1): whose charge it was, retries, the last error
    ("credit_reversals", "customer", "TEXT"),
    ("credit_reversals", "ours", "INTEGER NOT NULL DEFAULT 0"),
    ("credit_reversals", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("credit_reversals", "last_error", "TEXT"),
    ("credit_reversals", "full", "INTEGER NOT NULL DEFAULT 1"),   # 0: a partial refund (voids its share, no hold)
    # a run still in progress keeps its credit hold (credits.keepalive, N13)
    ("credit_holds", "alive_at", "TEXT"),
    # the free plan is counted per person (plan_paywall.py, M-1): the card and the email, and failed tries (L-7)
    ("free_plans", "fingerprint", "TEXT"),
    ("free_plans", "email_norm", "TEXT"),
    ("free_plans", "status", "TEXT"),          # running | ready | failed (NULL: made before this column)
    # Manage a launched campaign (admanage.py, M2.4): a status_change / budget_change card is applied after approval,
    # by the request and then the worker when Meta says wait (the 30 s edit slot, the rate budget).
    ("approval_cards", "apply_state", "TEXT"),        # queued | applying | waiting | done | failed (change cards only)
    ("approval_cards", "next_at", "TEXT"),            # when a waiting change may try again
    ("approval_cards", "lease_at", "TEXT"),           # a run applying it holds it until this is LEASE old
    ("approval_cards", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    # what Meta last said about a launched object (read when a card is drafted, approved or applied)
    ("ad_objects", "effective_status", "TEXT"),       # Meta's effective_status (ACTIVE, CAMPAIGN_PAUSED, DISAPPROVED...)
    ("ad_objects", "daily_budget_minor", "INTEGER"),  # campaigns: the daily budget in the account's minor units
    ("ad_objects", "synced_at", "TEXT"),
    # Insights pulls per connection (adinsights.py, M2.5)
    ("ad_connections", "insights_at", "TEXT"),        # the last pull that worked
    ("ad_connections", "insights_next_at", "TEXT"),   # when the next pull is due (a rate limit pushes it out)
    ("ad_connections", "insights_error", "TEXT"),     # the last pull's problem, in plain words
    ("users", "weekly_report_off", "TEXT"),           # when they stopped the weekly report email (weekly_report.py, M4.4)
    # M14 (ui_v2.py, onboarding.py): an admin's own "new look" preview, and what a new account answered at sign-up
    ("users", "ui_v2", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "onboarding_json", "TEXT"),               # {"goals": [...], "other", "company", "website"}
    ("users", "onboarded_at", "TEXT"),                  # finished or skipped the onboarding steps
    # conversion tracking on members' pages and forms (conversions.py, M3.4): the owner's measurement setting
    # ('us': visitors in the US who send no GPC / Do Not Track; 'off': never), and per pixel the Test Events code
    ("pages", "measurement", "TEXT NOT NULL DEFAULT 'us'"),
    ("forms", "measurement", "TEXT NOT NULL DEFAULT 'us'"),
    ("pixels", "test_code", "TEXT"),
    # quality wave 2: the brand kit a static's text layer is drawn with (textlayer.py), and a workspace's own
    # monthly cap on free fixes (free_fix.py; NULL = the FREE_FIX_MONTHLY_CAP setting)
    ("brands", "kit_primary", "TEXT"),
    ("brands", "kit_accent", "TEXT"),
    ("brands", "kit_font", "TEXT"),
    ("brands", "kit_logo", "TEXT"),
    ("workspaces", "free_fix_cap", "INTEGER"),
    ("asset_fixes", "kind", "TEXT NOT NULL DEFAULT 'image'"),   # m15: 'video' = one beat of a keyframe-first video
    ("asset_fixes", "beat", "INTEGER"),                         # which beat (0-based) a video fix re-renders
    # how the brand name is said aloud ("Cam-pain-ih-fy"): sent to the video model in place of the written name,
    # so presenters don't mispronounce it (motion_edit.spoken); captions keep the written name
    ("brands", "say_as", "TEXT"),
]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def lease(conn, name: str, holder: str, ttl_s: float = 90.0, info: str | None = None) -> bool:
    """Take or renew the lease `name` for `holder` until ttl_s from now (Crowdcut's db.lease). True when `holder` has
    it: it was free, expired, or already ours. One atomic upsert, so two processes never both get True."""
    from datetime import timedelta
    ts = datetime.now(timezone.utc)
    until = (ts + timedelta(seconds=ttl_s)).isoformat()
    cur = conn.execute(
        "INSERT INTO worker_leases (name, holder, until, info) VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
        "holder=excluded.holder, until=excluded.until, info=excluded.info WHERE worker_leases.holder=excluded.holder "
        "OR worker_leases.until < ?", (name, holder, until, info, ts.isoformat()))
    conn.commit()
    return cur.rowcount == 1


def connect():
    """A connection to the app database. It times its write lock waits for the admin Database load panel
    (dbstats.py, roadmap M7.3; DB_STATS=off gives the plain sqlite3 connection)."""
    import dbstats
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    t0 = time.perf_counter()
    conn = sqlite3.connect(DB_PATH, timeout=15, factory=dbstats.factory())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    dbstats.note_connect(time.perf_counter() - t0)
    return conn


def norm_email(email: str) -> str:
    """Case, +tags and Gmail dots folded (preview.norm_email calls this): one person, one key."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def _backfill_email_norm(conn):
    """users.email_norm for rows written before the column (or by an older image during a deploy). No commit."""
    rows = conn.execute("SELECT id, email FROM users WHERE email_norm IS NULL").fetchall()
    for r in rows:
        conn.execute("UPDATE users SET email_norm=? WHERE id=?", (norm_email(r["email"]), r["id"]))


def init():
    """Create and upgrade the schema in place. Runs on every boot, and the deploy hook's
    side container runs it against the live database before the switch (N3): a rollback
    puts back the old image, never the old schema. So migrations are additive only: new
    tables, new columns (COLUMNS) and backfills of new columns. Never drop, rename or
    retype a column, or drop a table; old code must keep working on the new schema.
    tests/test_migrations.py fails on a migration that would."""
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
                if (table, col) == ("workspaces", "plan_paid_through"):
                    # Until now each paid invoice set plan_period_end; that's what was paid for.
                    conn.execute("UPDATE workspaces SET plan_paid_through=plan_period_end "
                                 "WHERE plan_status IN ('active','trialing','past_due')")
                if (table, col) == ("users", "email_verified_at"):
                    # Everyone who signed up before verification existed counts as verified.
                    conn.execute("UPDATE users SET email_verified_at=COALESCE(created_at, ?)", (now(),))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_sub ON workspaces(stripe_subscription_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_p ON photos(product_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_p ON jobs(workspace_id, product_id, id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outputs_share ON outputs(share_token)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_free_plans_fp ON free_plans(fingerprint, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_free_plans_email ON free_plans(email_norm, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email_norm ON users(email_norm)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_card_trials_user ON card_trials(user_id)")
        _backfill_email_norm(conn)
        conn.commit()
        first_owner = (os.environ.get("RS_FIRST_OWNER_EMAIL") or "").strip().lower()
        if first_owner and not conn.execute("SELECT 1 FROM meta WHERE key='owner_admin_v1'").fetchone():
            # The first owner signed up through the normal form (user 1, workspace 1)
            # instead of an owner setup link, so the account never got the operator flag.
            # One time, pinned to that exact row. Production ran this already (meta
            # owner_admin_v1); the email is a setting, never in the source (SEC-9, M7.4).
            conn.execute("UPDATE users SET is_admin=1 WHERE id=1 AND workspace_id=1 AND lower(email)=?",
                         (first_owner,))
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


def adjust_credits(conn, workspace_id: int, delta: int, reason: str, job_id=None, bucket: str = "pack",
                   hold_ok: bool = False) -> bool:
    """Atomically add (delta>0) or spend (delta<0) credits. False if it would go negative.

    Spending takes plan credits first (they expire at renewal), then pack
    credits. Adding goes to the pack bucket unless bucket="plan". The ledger's
    plan_delta records the plan-bucket part of each row, so refunds go back
    where they came from. A workspace on a billing hold (a refund or dispute,
    billing_holds.py) can't spend at all unless hold_ok (an admin's adjustment)."""
    if delta < 0:
        row = conn.execute("SELECT credits, plan_credits FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if not row:
            return False
        need = -delta
        from_plan = min(max(row["plan_credits"], 0), need)
        from_pack = need - from_plan
        # Pack credits below zero (a reversed payment, billing_holds.py) block only
        # spends that need pack credits; plan credits can still be spent (N4).
        cur = conn.execute("UPDATE workspaces SET plan_credits = plan_credits - ?, credits = credits - ? "
                           "WHERE id=? AND plan_credits >= ? AND (? = 0 OR credits >= ?)"
                           + ("" if hold_ok else " AND COALESCE(billing_hold, 0) = 0"),
                           (from_plan, from_pack, workspace_id, from_plan, from_pack, from_pack))
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


def void_credits(conn, workspace_id: int, amount: int, reason: str, prefer: str = "pack") -> int:
    """Take back credits a reversed payment bought (billing_holds.py), even when
    they are already spent: what isn't there leaves the pack bucket negative, so
    nothing more can be spent until it's paid back. `prefer` is the bucket the
    payment filled (pack for a credit pack, plan for a plan invoice); that one
    is emptied first, then the other. Returns the credits voided."""
    amount = int(amount)
    if amount <= 0:
        return 0
    row = conn.execute("SELECT credits, plan_credits FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    if not row:
        return 0
    plan_left, pack_left = max(int(row["plan_credits"] or 0), 0), max(int(row["credits"] or 0), 0)
    if prefer == "plan":
        from_plan = min(plan_left, amount)
        from_pack = amount - from_plan
    else:
        from_pack_have = min(pack_left, amount)
        from_plan = min(plan_left, amount - from_pack_have)
        from_pack = amount - from_plan
    conn.execute("UPDATE workspaces SET plan_credits = plan_credits - ?, credits = credits - ? WHERE id=?",
                 (from_plan, from_pack, workspace_id))
    conn.execute("INSERT INTO ledger (workspace_id, delta, reason, created_at, plan_delta) VALUES (?,?,?,?,?)",
                 (workspace_id, -amount, reason, now(), -from_plan))
    return amount


def balance(ws) -> int:
    """Everything a workspace can spend right now (plan + pack credits). A
    bucket below zero (credits voided after they were spent, billing_holds.py)
    counts as 0 here: it can't be spent, and it doesn't block the other bucket
    (adjust_credits). What it owes is owed(ws)."""
    keys = ws.keys()
    plan = int((ws["plan_credits"] if "plan_credits" in keys else 0) or 0)
    return max(0, int(ws["credits"] or 0)) + max(0, plan)


def owed(ws) -> int:
    """Credits a workspace owes: voided after they were spent (the pack bucket
    below zero). The next pack it buys pays them back first."""
    keys = ws.keys()
    plan = int((ws["plan_credits"] if "plan_credits" in keys else 0) or 0)
    return max(0, -int(ws["credits"] or 0)) + max(0, -plan)


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


def refund_part(conn, job, amount: int, reason: str) -> int:
    """Give back part of a job's credits (some of its pictures failed the automatic check, image_qa.py) and lower
    the job's own figure by it. Pack credits go back first (they were spent last), the rest to the plan bucket
    while a plan is on."""
    amount = max(0, min(int(amount or 0), int(job["credits"] or 0)))
    if not amount:
        return 0
    spent_plan = -(conn.execute("SELECT COALESCE(SUM(plan_delta),0) FROM ledger WHERE job_id=? AND delta<0",
                                (job["id"],)).fetchone()[0] or 0)
    spent_plan = max(0, min(spent_plan, job["credits"]))
    to_pack = min(amount, job["credits"] - spent_plan)
    to_plan = amount - to_pack
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (job["workspace_id"],)).fetchone()
    import plans
    if to_plan and plans.plan_code(ws) == "free":
        to_pack, to_plan = amount, 0
    if to_plan:
        adjust_credits(conn, job["workspace_id"], to_plan, reason, job["id"], bucket="plan")
    if to_pack:
        adjust_credits(conn, job["workspace_id"], to_pack, reason, job["id"])
    conn.execute("UPDATE jobs SET credits = credits - ? WHERE id=?", (amount, job["id"]))
    return amount


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


def unhit(key: str):
    """Take back the newest attempt for `key` (an email that never went out shouldn't count against a resend limit)."""
    with connect() as conn:
        conn.execute("DELETE FROM throttle WHERE rowid=(SELECT rowid FROM throttle WHERE key=? ORDER BY at DESC LIMIT 1)",
                     (key,))
        conn.commit()


def hit(key: str):
    """Record one attempt for `key` (and forget anything older than a day)."""
    import time
    t = time.time()
    with connect() as conn:
        conn.execute("INSERT INTO throttle (key, at) VALUES (?,?)", (key, t))
        conn.execute("DELETE FROM throttle WHERE at<?", (t - 86400,))
        conn.commit()
