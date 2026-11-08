"""Smoke check of a restored Campaignify data folder (tools/restore.sh runs it).

    python tools/restore_smoke.py DATA_DIR [--media]

1. PRAGMA integrity_check is "ok" and the core tables are there; prints row counts.
2. The app boots on that folder in a child process with a clean environment
   (no API keys, no worker, no plan sync, no nudges, no alerts, so nothing is
   sent anywhere) and answers GET /health and GET /login with 200.
3. With --media: of the newest 200 finished ads with a local file, how many
   files are present (reported; deleted ads and expired exports may be gone).

Exit 0 when 1 and 2 pass.
"""
import json
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = ("workspaces", "users", "jobs", "outputs", "meta")

BOOT = r"""
import json, sys
import app
c = app.app.test_client()
out = {}
for path in ("/health", "/login"):
    r = c.get(path)
    out[path] = r.status_code
    if path == "/health":
        out["health"] = r.get_json(silent=True)
print("SMOKE " + json.dumps(out))
"""


def check_db(data: str) -> dict:
    path = os.path.join(data, "rs.db")
    if not os.path.isfile(path):
        raise SystemExit(f"FAIL: {path} not found")
    conn = sqlite3.connect(path)
    try:
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise SystemExit(f"FAIL: {path} is not a readable database: {e}") from None
        if ok != "ok":
            raise SystemExit(f"FAIL: integrity_check: {ok}")
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        lost = [t for t in TABLES if t not in have]
        if lost:
            raise SystemExit(f"FAIL: tables missing: {', '.join(lost)}")
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    finally:
        conn.close()


def boot(data: str) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "PYTHONPATH": ROOT, "RS_DATA": data, "RS_WORKER": "0", "RS_PLAN_SYNC": "0", "RS_NUDGE_LOOP": "0",
           "OPS_ALERTS": "0", "BACKUPS": "off", "RS_HEALTH_WORKER_MAX_AGE": "31536000", "RS_DISK_MIN_GB": "0"}
    for k in ("TMPDIR", "LANG"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    p = subprocess.run([sys.executable, "-c", BOOT], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=300)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("SMOKE ")), None)
    if p.returncode != 0 or not line:
        raise SystemExit("FAIL: the app didn't boot on the restored data:\n" + (p.stderr or p.stdout)[-2000:])
    return json.loads(line[6:])


def check_media(data: str) -> tuple[int, int]:
    conn = sqlite3.connect(os.path.join(data, "rs.db"))
    try:
        rows = conn.execute("SELECT path FROM outputs WHERE path IS NOT NULL AND path != '' "
                            "ORDER BY id DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    have = 0
    for (p,) in rows:
        rel = p.split("/data/", 1)[1] if "/data/" in p else p.lstrip("/")
        if os.path.exists(os.path.join(data, rel)):
            have += 1
    return have, len(rows)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    media = "--media" in argv
    args = [a for a in argv if a != "--media"]
    if len(args) != 1:
        print(__doc__)
        return 2
    data = os.path.abspath(args[0])
    counts = check_db(data)
    print("  database ok: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    res = boot(data)
    if res.get("/health") != 200 or res.get("/login") != 200:
        raise SystemExit(f"FAIL: the app answered {res}")
    print(f"  app boots: /health 200 ({json.dumps((res.get('health') or {}).get('checks'))}), /login 200")
    if media:
        have, n = check_media(data)
        print(f"  media: {have} of the newest {n} ad files present")
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
