"""Off-site, encrypted backups and the restore drill (backup.py, tools/restore.sh;
roadmap M1.6, REL-1), with rclone replaced by tests/fake_rclone.sh, so no network.

What it pins down:
  * without the six R2 / crypt settings nothing changes: no snapshot, no rclone
    call, one warning in the log, and the System card says backups are off;
  * with them: a consistent rs.db snapshot goes to the bucket through the crypt
    remote only (passwords obscured, never on a command line), media folders and
    the session key are synced, temp files are left out, a file deleted here is
    kept in the dated trash, snapshots and trash older than 30 days are pruned
    (never the newest), alerts.backup_ok is recorded and the System card shows
    the time and size;
  * a failure never raises: the error is stored with secrets redacted, the
    backup_failed alert fires, the last good backup stays on the card;
  * the nightly schedule (due / maybe_run) and the backup_late alert;
  * tools/restore.sh rebuilds a working data folder on a scratch path from the
    newest snapshot and its smoke check passes; it refuses a non-empty folder
    and missing settings.

    python tests/test_backup.py      (tests/test_app.py runs it too)
"""
import gzip
import io
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_backup_")
DATA = os.path.join(TMP, "data")
BUCKET_ROOT = os.path.join(TMP, "r2")
RLOG = os.path.join(TMP, "rclone.log")
FAKE = os.path.join(HERE, "fake_rclone.sh")
os.chmod(FAKE, os.stat(FAKE).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
SECRETS = {"R2_ACCOUNT_ID": "abc123def456", "R2_ACCESS_KEY_ID": "AKIDtest0001", "R2_SECRET_ACCESS_KEY": "sekritR2value",
           "R2_BUCKET": "atlas-backups", "BACKUP_CRYPT_PASSWORD": "correct horse battery", "BACKUP_CRYPT_SALT": "pepper-salt-99"}
os.environ.update(RS_DATA=DATA, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_DISK_MIN_GB="0", RCLONE_BIN=FAKE, FAKE_RCLONE_ROOT=BUCKET_ROOT,
                  FAKE_RCLONE_LOG=RLOG, RESEND_API_KEY="re_test", RS_MAIL_DOMAIN="mail.example.com",
                  OPS_ALERT_EMAIL="ops@example.com")
for k in list(SECRETS) + ["BACKUPS", "BACKUP_MEDIA", "BACKUP_PREFIX", "BACKUP_KEEP_DAYS", "BACKUP_BWLIMIT",
                          "FAKE_RCLONE_FAIL", "FAKE_RCLONE_SAY", "OPS_ALERTS"]:
    os.environ.pop(k, None)

import alerts  # noqa: E402
import app as appmod  # noqa: E402  (creates the schema)
import backup  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


alerts.ASYNC = False
sent = []


class _R:
    status_code = 200

    def json(self):
        return {"id": "em_1"}


def _fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
    sent.append({"url": url, "body": json})
    return _R()


mailer.requests.post = _fake_post
logbuf = io.StringIO()
_h = logging.StreamHandler(logbuf)
_h.setLevel(logging.WARNING)
logging.getLogger("campaignify").addHandler(_h)


def rlog() -> list[str]:
    try:
        with open(RLOG) as f:
            return f.read().splitlines()
    except OSError:
        return []


def clear_rlog():
    if os.path.exists(RLOG):
        os.remove(RLOG)


def bucket(*parts) -> str:
    return os.path.join(BUCKET_ROOT, SECRETS["R2_BUCKET"], "campaignify", *parts)


def set_secrets(on=True):
    for k, v in SECRETS.items():
        if on:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def meta(key):
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def reset_schedule():
    with db.connect() as conn:
        conn.execute("DELETE FROM meta WHERE key IN (?, ?, ?)", (backup.STATUS_META, backup.ATTEMPT_META,
                                                                  alerts.BACKUP_META))
        conn.execute("DELETE FROM meta WHERE key LIKE 'alert:%'")
        conn.commit()


# ---------------------------------------------------------------- data to back up
from werkzeug.security import generate_password_hash  # noqa: E402

now_s = db.now()
with db.connect() as conn:
    ws = conn.execute("INSERT INTO workspaces (name, credits, created_at) VALUES ('Backup Co', 500, ?)", (now_s,)).lastrowid
    conn.execute("INSERT INTO users (email, pw_hash, is_admin, workspace_id, created_at, email_verified_at, role) "
                 "VALUES ('boss@example.com', ?, 1, ?, ?, ?, 'owner')",
                 (generate_password_hash("longpassword1"), ws, now_s, now_s))
    jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, cost_usd, "
                       "created_at, updated_at) VALUES (?, 1, 'image_ad', 'x', '{}', 'done', 0, 0, ?, ?)",
                       (ws, now_s, now_s)).lastrowid
    out_path = os.path.join(DATA, "media", str(ws), str(jid), "ad.png")
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?, 'image', '', ?)", (jid, out_path))
    conn.commit()
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "wb") as f:
    f.write(b"\x89PNG fake ad")
with open(out_path + ".part.mp4", "wb") as f:
    f.write(b"half-written")
os.makedirs(os.path.join(DATA, "uploads", str(ws)), exist_ok=True)
photo = os.path.join(DATA, "uploads", str(ws), "photo.jpg")
with open(photo, "wb") as f:
    f.write(b"jpeg bytes")
os.makedirs(os.path.join(DATA, "presenters"), exist_ok=True)
with open(os.path.join(DATA, "presenters", "p1.jpg"), "wb") as f:
    f.write(b"presenter")

app = appmod.app
_ip = [0]


def client():
    _ip[0] += 1
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.8.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as sess:
        return sess["csrf"]


adm = client()
adm.post("/login", data={"csrf": csrf(adm), "email": "boss@example.com", "password": "longpassword1"})


def card() -> str:
    html = adm.get("/admin").get_data(as_text=True)
    i = html.find('id="backups"')
    return html[i:i + 2500] if i >= 0 else ""


# ---------------------------------------------------------------- 1. not configured
print("== without the R2 settings: nothing changes")
reset_schedule()
backup._warned[0] = False
backup.maybe_run()
backup.maybe_run()
st = backup.run("test")
check(st["ok"] is False and "not set" in (st["error"] or ""), "run() without settings: not ok, says what's missing")
check(not rlog(), "rclone is never called")
check(not os.path.exists(os.path.join(DATA, "backups")), "no local snapshot is made either (exactly as before)")
check(logbuf.getvalue().count("off-site backups are off") == 1, "one warning in the log, not one per pass")
check(meta(backup.STATUS_META) is None and meta(alerts.BACKUP_META) is None,
      "nothing is recorded, so backup_late stays quiet")
c = card()
check("off-site off" in c and "R2_BUCKET" in c and "BACKUP_CRYPT_SALT" in c, "System card: off, lists the missing names")
set_secrets(True)
os.environ.pop("BACKUP_CRYPT_SALT")
check(not backup.configured() and backup.missing() == ["BACKUP_CRYPT_SALT"], "five of six set: still off")
set_secrets(False)

# ---------------------------------------------------------------- 2. a good run
print("== a good run")
set_secrets(True)
clear_rlog()
t0 = datetime(2026, 11, 8, 9, 0, 0, tzinfo=timezone.utc)
# old snapshots and trash already in the bucket: 40 days old goes, 10 days old stays
os.makedirs(bucket("db"), exist_ok=True)
for days in (40, 31, 10):
    name = f"rs-{(t0 - timedelta(days=days)).strftime('%Y%m%dT%H%M%SZ')}.db.gz"
    with open(bucket("db", name), "wb") as f:
        f.write(b"old")
for days in (35, 5):
    d = bucket("media-trash", (t0 - timedelta(days=days)).strftime("%Y-%m-%d"), "uploads")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "x.jpg"), "wb") as f:
        f.write(b"x")
st = backup.run("test", now=t0)
check(st["ok"] is True and st["error"] is None, f"run() ok ({st.get('error')})")
snap = st["snapshot"]
check(snap == "rs-20261108T090000Z.db.gz", "snapshot named by its UTC time")
check(os.path.isfile(bucket("db", snap)), "snapshot uploaded under <bucket>/campaignify/db/")
raw = gzip.decompress(open(bucket("db", snap), "rb").read())
probe = os.path.join(TMP, "probe.db")
with open(probe, "wb") as f:
    f.write(raw)
pc = sqlite3.connect(probe)
check(pc.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
      and pc.execute("SELECT name FROM workspaces WHERE id=?", (ws,)).fetchone()[0] == "Backup Co",
      "the uploaded snapshot is a consistent copy of rs.db")
pc.close()
check(os.path.isfile(os.path.join(DATA, "backups", snap)), "a local copy is kept in data/backups")
check(open(bucket("media", "uploads", str(ws), "photo.jpg"), "rb").read() == b"jpeg bytes"
      and os.path.isfile(bucket("media", "media", str(ws), str(jid), "ad.png"))
      and os.path.isfile(bucket("media", "presenters", "p1.jpg")), "uploads, media and presenters synced")
check(not os.path.exists(bucket("media", "media", str(ws), str(jid), "ad.png.part.mp4")), "temp files are left out")
check(os.path.isfile(bucket("keys", "secret_key")), "the session key is backed up")
names = sorted(os.listdir(bucket("db")))
check(len(names) == 2 and snap in names
      and f"rs-{(t0 - timedelta(days=40)).strftime('%Y%m%d')}T090000Z.db.gz" not in names
      and f"rs-{(t0 - timedelta(days=31)).strftime('%Y%m%d')}T090000Z.db.gz" not in names
      and f"rs-{(t0 - timedelta(days=10)).strftime('%Y%m%d')}T090000Z.db.gz" in names,
      "snapshots over 30 days old pruned, newer ones kept")
check(not os.path.exists(bucket("media-trash", (t0 - timedelta(days=35)).strftime("%Y-%m-%d")))
      and os.path.exists(bucket("media-trash", (t0 - timedelta(days=5)).strftime("%Y-%m-%d"))),
      "trash folders over 30 days old purged")
check(st["remote_snapshots"] == 2 and st["pruned"] == 3, "counts: 2 snapshots left, 3 items pruned")
lines = rlog()
uses = [ln for ln in lines if not ln.startswith(("via ", "obscure"))]
check(all("cfybr2:" not in ln for ln in uses), "no call names the raw R2 remote: everything goes through crypt")
check(any(ln.startswith("via crypt cfybackup -> cfybr2:atlas-backups/campaignify") for ln in lines),
      "the crypt remote sits on <bucket>/campaignify")
check(not any(SECRETS["BACKUP_CRYPT_PASSWORD"] in ln or SECRETS["R2_SECRET_ACCESS_KEY"] in ln for ln in lines),
      "no secret on any rclone command line")
env = backup.rclone_env()
check(env["RCLONE_CONFIG_CFYBACKUP_PASSWORD"].startswith("obscured-")
      and env["RCLONE_CONFIG_CFYBACKUP_PASSWORD"] != SECRETS["BACKUP_CRYPT_PASSWORD"]
      and env["RCLONE_CONFIG_CFYBACKUP_PASSWORD2"].startswith("obscured-"), "crypt password and salt obscured")
check("RCLONE_CONFIG_CFYBACKUP_PASSWORD" not in os.environ, "the rclone config never lands in os.environ")
check(meta(alerts.BACKUP_META) is not None, "alerts.backup_ok recorded (backup_late resets)")
s = backup.status()
check(s["good"]["snapshot"] == snap and s["good"]["db_bytes"] > 0 and s["good"]["media_files"] >= 3
      and s["late"] is False, "status: last good backup with sizes, not late")
c = card()
check(">ok<" in c and "Last good" in c and "database" in c and "media" in c and "snapshots in R2" in c,
      "System card shows the last good backup, its size and the snapshot count")

# ---------------------------------------------------------------- 3. deletes go to the trash
print("== a deleted file is kept in the trash")
os.remove(photo)
t1 = t0 + timedelta(days=1)
st = backup.run("test", now=t1)
check(st["ok"], "second run ok")
check(not os.path.exists(bucket("media", "uploads", str(ws), "photo.jpg")), "gone from the mirror")
check(open(bucket("media-trash", t1.strftime("%Y-%m-%d"), "uploads", str(ws), "photo.jpg"), "rb").read() == b"jpeg bytes",
      "kept in media-trash/<date>/")
with open(photo, "wb") as f:
    f.write(b"jpeg bytes")

# ---------------------------------------------------------------- 4. BACKUP_MEDIA=off and BACKUP_PREFIX
print("== switches")
os.environ["BACKUP_MEDIA"] = "off"
clear_rlog()
st = backup.run("test", now=t1 + timedelta(hours=1))
check(st["ok"] and st["media_bytes"] is None and not any(ln.startswith("sync") for ln in rlog()),
      "BACKUP_MEDIA=off: database only")
os.environ.pop("BACKUP_MEDIA")
os.environ["BACKUP_PREFIX"] = "cfy-test"
check(backup.rclone_env()["RCLONE_CONFIG_CFYBACKUP_REMOTE"] == "cfybr2:atlas-backups/cfy-test", "BACKUP_PREFIX moves the folder")
os.environ.pop("BACKUP_PREFIX")
os.environ["BACKUP_BWLIMIT"] = "8M"
clear_rlog()
backup.rclone(backup.rclone_env(), "lsf", "--files-only", backup.remote("db"))
check(any("lsf" in ln for ln in rlog()), "BACKUP_BWLIMIT passes --bwlimit (the fake accepts it)")
os.environ.pop("BACKUP_BWLIMIT")

# ---------------------------------------------------------------- 5. failures
print("== failures never raise")
good_before = backup.status()["good"]
sent.clear()
os.environ["FAKE_RCLONE_FAIL"] = "copyto"
os.environ["FAKE_RCLONE_SAY"] = "denied for key " + SECRETS["R2_SECRET_ACCESS_KEY"]
st = backup.run("test", now=t1 + timedelta(hours=2))
check(st["ok"] is False and st["error"].startswith("database upload:"), "upload failure: not ok, says the step")
check(SECRETS["R2_SECRET_ACCESS_KEY"] not in st["error"] and "***" in st["error"], "the error has the secret redacted")
check(any("Backup failed" in (m["body"] or {}).get("subject", "") for m in sent), "backup_failed alert emailed")
check(backup.status()["good"] == good_before, "the last good backup stays on the card")
c = card()
check("failed" in c and "database upload" in c and SECRETS["R2_SECRET_ACCESS_KEY"] not in c,
      "System card shows the failed try, redacted")
os.environ["FAKE_RCLONE_FAIL"] = "obscure"
st = backup.run("test", now=t1 + timedelta(hours=3))
check(not st["ok"] and st["error"].startswith("rclone setup:"), "obscure failing: not ok, no raise")
os.environ.pop("FAKE_RCLONE_FAIL")
os.environ.pop("FAKE_RCLONE_SAY")
os.environ["RCLONE_BIN"] = os.path.join(TMP, "no-such-rclone")
st = backup.run("test", now=t1 + timedelta(hours=4))
check(not st["ok"] and "isn't installed" in st["error"], "rclone missing: not ok, says so")
os.environ["RCLONE_BIN"] = FAKE

# ---------------------------------------------------------------- 6. schedule and the late alert
print("== schedule and backup_late")
reset_schedule()
check(backup.due(), "never backed up: due")
st = backup.run("test")
check(st["ok"], f"good run now ({st.get('error')})")
n = datetime.now(timezone.utc)
check(not backup.due(n), "right after a good run: not due")
check(backup.due(n + timedelta(hours=25)), "25 hours later: due")
with db.connect() as conn:
    conn.execute("UPDATE meta SET value=? WHERE key=?",
                 (str((n + timedelta(hours=25) - timedelta(minutes=10)).timestamp()), backup.ATTEMPT_META))
    conn.commit()
check(not backup.due(n + timedelta(hours=25)), "a try in the last hour holds the next one back")
nightly = n.replace(hour=int(os.environ.get("BACKUP_HOUR_UTC", 8)), minute=30)
if nightly <= n:
    nightly += timedelta(days=1)
with db.connect() as conn:
    stored = json.loads(meta(backup.STATUS_META))
    stored["good"]["finished_at"] = (nightly - timedelta(hours=20)).replace(microsecond=0).isoformat()
    conn.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(stored), backup.STATUS_META))
    conn.execute("UPDATE meta SET value='0' WHERE key=?", (backup.ATTEMPT_META,))
    conn.commit()
check(backup.due(nightly), "after the nightly hour on a new day: due")
check(not backup.due(nightly.replace(hour=max(0, nightly.hour - 1))) or nightly.hour == 0,
      "before the nightly hour (20 h after the last): not due")
reset_schedule()
clear_rlog()
backup.maybe_run()
for _ in range(100):
    if not backup._running.locked():
        break
    time.sleep(0.05)
check(backup.status()["good"] is not None and any(ln.startswith("copyto") for ln in rlog()),
      "maybe_run() runs it in its own thread when due")
clear_rlog()
backup.maybe_run()
time.sleep(0.2)
check(not any(ln.startswith("copyto") for ln in rlog()), "and not again right after")
os.environ["BACKUPS"] = "off"
reset_schedule()
clear_rlog()
backup.maybe_run()
time.sleep(0.2)
check(not rlog(), "BACKUPS=off: the nightly run doesn't start")
os.environ.pop("BACKUPS")
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (
        alerts.BACKUP_META, (datetime.now(timezone.utc) - timedelta(hours=30)).replace(microsecond=0).isoformat()))
    conn.execute("DELETE FROM meta WHERE key LIKE 'alert:%'")
    conn.commit()
check("backup_late" in alerts.sweep(probe=False), "a last good backup 30 hours old raises backup_late")

# ---------------------------------------------------------------- 7. the CLI
r = subprocess.run([sys.executable, os.path.join(ROOT, "backup.py"), "--status"], capture_output=True, text=True,
                   env=dict(os.environ), cwd=ROOT, timeout=120)
check(r.returncode == 0 and json.loads(r.stdout)["configured"] is True, "python backup.py --status prints JSON")

# ---------------------------------------------------------------- 8. the restore drill
print("== tools/restore.sh")
reset_schedule()
st = backup.run("test")
check(st["ok"], "fresh backup for the drill")
restore = os.path.join(ROOT, "tools", "restore.sh")
os.chmod(restore, os.stat(restore).st_mode | stat.S_IXUSR)
renv = {k: v for k, v in os.environ.items() if k not in SECRETS and not k.startswith("RS_")}
renv.update(SECRETS, RCLONE_BIN=FAKE, PYTHON=sys.executable)
scratch = os.path.join(TMP, "drill")
r = subprocess.run(["bash", restore, "--media", scratch], capture_output=True, text=True, env=renv, timeout=600)
check(r.returncode == 0 and "SMOKE OK" in r.stdout and "Restore drill passed" in r.stdout,
      "restore.sh --media: rebuilt and the smoke check passed\n" + r.stdout[-800:] + r.stderr[-800:])
check(f"Restoring {max(os.listdir(bucket('db')))} " in r.stdout, "it restored the newest snapshot")
rc = sqlite3.connect(os.path.join(scratch, "data", "rs.db"))
check(rc.execute("SELECT COUNT(*) FROM users WHERE email='boss@example.com'").fetchone()[0] == 1, "restored users are there")
rc.close()
check(open(os.path.join(scratch, "data", "uploads", str(ws), "photo.jpg"), "rb").read() == b"jpeg bytes"
      and os.path.isfile(os.path.join(scratch, "data", "secret_key")), "media and the session key restored")
check("1 of the newest 1 ad files present" in r.stdout, "the smoke check counts the ad files")
r = subprocess.run(["bash", restore, scratch], capture_output=True, text=True, env=renv, timeout=120)
check(r.returncode == 2 and "not empty" in r.stderr, "refuses a scratch folder that isn't empty")
bare = {k: v for k, v in renv.items() if k not in SECRETS}
r = subprocess.run(["bash", restore, os.path.join(TMP, "drill2")], capture_output=True, text=True, env=bare, timeout=120)
check(r.returncode == 2 and "Not set:" in r.stderr and "R2_BUCKET" in r.stderr, "refuses without the settings")
sfile = os.path.join(TMP, "settings.txt")
with open(sfile, "w") as f:
    f.write("HF_KEY=x\n" + "".join(f"{k}={v}\n" for k, v in SECRETS.items()))
r = subprocess.run(["bash", restore, "--settings", sfile, "--snapshot", st["snapshot"], os.path.join(TMP, "drill3")],
                   capture_output=True, text=True, env=bare, timeout=600)
check(r.returncode == 0 and "SMOKE OK" in r.stdout, "--settings FILE and --snapshot NAME work\n" + r.stderr[-500:])
bad = os.path.join(TMP, "drill4", "data")
os.makedirs(bad)
with open(os.path.join(bad, "rs.db"), "wb") as f:
    f.write(b"not a database at all" * 100)
r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "restore_smoke.py"), bad], capture_output=True,
                   text=True, timeout=120)
check(r.returncode != 0 and "FAIL" in (r.stdout + r.stderr), "the smoke check fails a broken database")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\ntest_backup: {passed} checks passed")
