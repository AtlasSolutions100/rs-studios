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
"""


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


def adjust_credits(conn, workspace_id: int, delta: int, reason: str, job_id=None) -> bool:
    """Atomically add (delta>0) or spend (delta<0) credits. False if it would go negative."""
    if delta < 0:
        cur = conn.execute("UPDATE workspaces SET credits = credits + ? WHERE id=? AND credits + ? >= 0",
                           (delta, workspace_id, delta))
    else:
        cur = conn.execute("UPDATE workspaces SET credits = credits + ? WHERE id=?", (delta, workspace_id))
    if cur.rowcount != 1:
        return False
    conn.execute("INSERT INTO ledger (workspace_id, delta, reason, job_id, created_at) VALUES (?,?,?,?,?)",
                 (workspace_id, delta, reason, job_id, now()))
    return True


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
