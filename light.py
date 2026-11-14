"""Light model work: short, structured jobs (the questions the agent asks before it plans) that don't need Claude.

Where a job runs, in order:
  1. The Mac worker (deploy/mac-light/worker.py): a local model on the Mac mini, free to run. It holds a long poll
     open on /_light/next (Bearer LIGHT_WORKER_TOKEN), takes the oldest queued job, and posts the answer to
     /_light/done/<id>. Each poll records when it was last seen, so the web knows whether it is there.
  2. Groq (GROQ_API_KEY), when the Mac hasn't been seen lately or doesn't answer in time.
  3. Nothing: ask_json returns (None, ""), and the caller uses its own rules.
Claude is never called from here, so this step costs no Claude API at all.

The queue is one table (light_jobs). A job nobody takes within STALE seconds is marked expired so a worker that
comes back later doesn't answer a question no one is waiting for."""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, request

import db
import llm

log = logging.getLogger("campaignify")
bp = Blueprint("light", __name__)

SEEN_KEY = "light_worker_seen"
ALIVE_S = 40          # a worker seen this recently is there (its long poll is 20s)
LONG_POLL_S = 20      # how long /_light/next waits for a job before answering 204
STALE_S = 30          # a queued job older than this is not handed out
MAX_RESULT = 20000    # characters of an answer kept


def ensure_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS light_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER, "
                 "kind TEXT NOT NULL, prompt_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', "
                 "result_json TEXT, error TEXT, taken_by TEXT, created_at TEXT NOT NULL, taken_at TEXT, done_at TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_light_jobs_status ON light_jobs(status, id)")


def _token() -> str:
    return (os.environ.get("LIGHT_WORKER_TOKEN") or "").strip()


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def worker() -> dict | None:
    """The Mac worker as last seen ({"at", "model", "by"}), or None when it hasn't been seen within ALIVE_S."""
    if not _token():
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (SEEN_KEY,)).fetchone()
    try:
        seen = json.loads(row["value"]) if row else None
    except (TypeError, ValueError):
        return None
    if not seen or str(seen.get("at") or "") < _ago(ALIVE_S):
        return None
    return seen


def status() -> str:
    """One line for the admin page."""
    w = worker()
    if w:
        return f"Mac worker on ({w.get('model') or 'local model'})"
    return "Mac worker off" + (", Groq fallback on" if os.environ.get("GROQ_API_KEY") else ", rules only")


def _groq_json(system: str, prompt: str, schema: dict, max_tokens: int) -> dict:
    mid = llm.model("groq")
    body = {"model": mid, "max_tokens": max_tokens, "temperature": 0.4, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system + "\n\nReply with one JSON object that matches this JSON "
                          "Schema, and nothing else:\n" + json.dumps(schema)},
                         {"role": "user", "content": prompt}]}
    if "gpt-oss" in mid:
        body["reasoning_effort"] = "low"
    resp = llm._groq(body)  # noqa: SLF001 (the one Groq call, shared with llm.structured)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return llm.parse_json(text)


def ask_json(kind: str, system: str, prompt: str, schema: dict, ws_id: int | None = None, wait: float = 20.0,
             max_tokens: int = 900) -> tuple[dict | None, str]:
    """One JSON answer from a light model, as (answer, where it came from: "mac", "groq" or ""). Never raises."""
    if worker():
        job = None
        try:
            with db.connect() as conn:
                ensure_table(conn)
                job = conn.execute("INSERT INTO light_jobs (workspace_id, kind, prompt_json, created_at) VALUES "
                                   "(?, ?, ?, ?)", (ws_id, kind, json.dumps({"system": system, "prompt": prompt,
                                                                             "schema": schema,
                                                                             "max_tokens": max_tokens}),
                                                    db.now())).lastrowid
                conn.commit()
            end = time.monotonic() + wait
            while time.monotonic() < end:
                time.sleep(0.25)
                with db.connect() as conn:
                    row = conn.execute("SELECT status, result_json FROM light_jobs WHERE id=?", (job,)).fetchone()
                if row and row["status"] == "done":
                    return json.loads(row["result_json"] or "{}"), "mac"
                if row and row["status"] == "failed":
                    break
        except Exception:  # noqa: BLE001
            log.exception("light job failed")
        if job:
            with db.connect() as conn:
                conn.execute("UPDATE light_jobs SET status='expired' WHERE id=? AND status IN ('queued', 'taken')",
                             (job,))
                conn.commit()
    if os.environ.get("GROQ_API_KEY"):
        try:
            return _groq_json(system, prompt, schema, max_tokens), "groq"
        except Exception:  # noqa: BLE001
            log.warning("light groq call failed", exc_info=True)
    return None, ""


# ---------------------------------------------------------------- the worker's two calls

def _authed() -> None:
    want = _token()
    sent = request.headers.get("Authorization", "")
    if not want:
        abort(404)
    if not sent.startswith("Bearer ") or not hmac.compare_digest(sent[7:].strip(), want):
        abort(401)


def _seen(d: dict) -> None:
    seen = {"at": db.now(), "model": str(d.get("model") or "")[:60], "by": str(d.get("by") or "")[:60]}
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (SEEN_KEY, json.dumps(seen)))
        conn.commit()


@bp.route("/_light/next", methods=["POST"])
def next_job():
    """The oldest queued job, taken (one compare-and-set UPDATE), or 204 after LONG_POLL_S with none."""
    _authed()
    d = request.get_json(silent=True) or {}
    _seen(d)
    wait = max(0.0, min(float(d.get("wait") or LONG_POLL_S), LONG_POLL_S))
    end = time.monotonic() + wait
    while True:
        with db.connect() as conn:
            ensure_table(conn)
            conn.execute("UPDATE light_jobs SET status='expired' WHERE status='queued' AND created_at < ?",
                         (_ago(STALE_S),))
            row = conn.execute("SELECT id FROM light_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            got = None
            if row:
                cur = conn.execute("UPDATE light_jobs SET status='taken', taken_at=?, taken_by=? WHERE id=? AND "
                                   "status='queued'", (db.now(), str(d.get("by") or "")[:60], row["id"]))
                if cur.rowcount:
                    got = conn.execute("SELECT id, kind, prompt_json FROM light_jobs WHERE id=?",
                                       (row["id"],)).fetchone()
            conn.commit()
        if got:
            return jsonify({"id": got["id"], "kind": got["kind"], **json.loads(got["prompt_json"])})
        if time.monotonic() >= end:
            return "", 204
        time.sleep(0.25)


@bp.route("/_light/done/<int:job_id>", methods=["POST"])
def job_done(job_id):
    _authed()
    d = request.get_json(silent=True) or {}
    result, error = d.get("result"), str(d.get("error") or "")[:300]
    ok = isinstance(result, dict) and not error
    text = json.dumps(result) if ok else None
    if text and len(text) > MAX_RESULT:
        ok, text, error = False, None, "too long"
    with db.connect() as conn:
        ensure_table(conn)
        cur = conn.execute("UPDATE light_jobs SET status=?, result_json=?, error=?, done_at=? WHERE id=? AND "
                           "status='taken'", ("done" if ok else "failed", text, error or None, db.now(), job_id))
        conn.commit()
    return jsonify({"ok": bool(cur.rowcount)})
