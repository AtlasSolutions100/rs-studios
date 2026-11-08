"""Nightly off-site, encrypted backups (roadmap M1.6, REL-1).

Once a night (after BACKUP_HOUR_UTC, default 8 = 2 or 3 am Central) the worker
loop starts run() in its own thread, so jobs keep moving while it works:

1. A consistent snapshot of rs.db with SQLite's online backup API (what
   `sqlite3 rs.db ".backup FILE"` does), gzipped to data/backups/rs-<utc>.db.gz.
   The newest BACKUP_KEEP_LOCAL (3) stay on this server.
2. The snapshot goes to Cloudflare R2 through rclone's crypt remote: contents
   and names are encrypted here, before they leave the server. Snapshots older
   than BACKUP_KEEP_DAYS (30) are deleted from the bucket.
3. The media folders (data/uploads, data/media, data/presenters) and the
   session key are synced to the bucket. A file deleted here is moved to a
   dated trash folder in the bucket and kept BACKUP_KEEP_DAYS, so a mistaken
   delete can be undone.
4. A good run calls alerts.backup_ok, which resets the backup_late alert
   (OPS_BACKUP_MAX_HOURS, 26). A failed run raises the backup_failed alert.

Settings (GitHub secrets, synced by deploy.yml): R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_BUCKET, BACKUP_CRYPT_PASSWORD, BACKUP_CRYPT_SALT.
Without all six nothing changes from before (no nightly job, nothing uploaded);
the app logs one warning and the admin System card says backups are off.
Optional: BACKUP_PREFIX (folder in the bucket, default "campaignify"),
BACKUP_MEDIA=off (database only), BACKUP_BWLIMIT (rclone --bwlimit, e.g. 20M),
BACKUPS=off (stop the nightly run).

    python backup.py             # back up now (in the app container)
    python backup.py --status    # what the System card shows, as JSON

Restore: tools/restore.sh rebuilds the app's data folder from the bucket on a
scratch path and runs a smoke check (README, "Backups and the restore drill").
"""
import gzip
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("campaignify")

SECRETS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
           "BACKUP_CRYPT_PASSWORD", "BACKUP_CRYPT_SALT")
STATUS_META = "backup_status"     # JSON of the last run, for the System card
ATTEMPT_META = "backup_attempt_at"  # epoch of the last start, so a failing run retries hourly, not every minute
BASE_REMOTE = "cfybr2"            # the R2 (S3) remote, defined only in rclone's environment
REMOTE = "cfybackup"              # the crypt remote on top of it: everything goes through this one
MEDIA_DIRS = ("uploads", "media", "presenters")
KEY_FILES = ("secret_key",)
EXCLUDES = ("*.part", "*.part.mp4", "*.wm.mp4")
SNAP_PREFIX = "rs-"
SNAP_SUFFIX = ".db.gz"


class BackupError(Exception):
    pass


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def local_dir() -> str:
    return os.path.join(db.DATA, "backups")


def enabled() -> bool:
    return os.environ.get("BACKUPS", "on").strip().lower() not in ("off", "0", "false", "no")


def missing() -> list[str]:
    return [k for k in SECRETS if not (os.environ.get(k) or "").strip()]


def configured() -> bool:
    return not missing()


def prefix() -> str:
    p = (os.environ.get("BACKUP_PREFIX") or "campaignify").strip().strip("/")
    return p or "campaignify"


def keep_days() -> float:
    return _num("BACKUP_KEEP_DAYS", 30)


_warned = [False]


def warn_once():
    if not _warned[0]:
        _warned[0] = True
        log.warning("off-site backups are off: %s not set. Nothing is uploaded; see README "
                    "'Backups and the restore drill'.", ", ".join(missing()))


# ---------------------------------------------------------------- rclone

def rclone_bin() -> str:
    return os.environ.get("RCLONE_BIN") or "rclone"


def _secret_values() -> list[str]:
    return [v for v in (os.environ.get(k) or "" for k in SECRETS[:3] + SECRETS[4:]) if len(v) >= 4]


def _redact(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, "***")
    return text


def _obscure(value: str) -> str:
    """rclone's own obscuring of a config password (crypt remotes need it), read from stdin
    so the password never appears in a process list."""
    try:
        p = subprocess.run([rclone_bin(), "obscure", "-"], input=value + "\n", capture_output=True, text=True,
                           timeout=30)
    except FileNotFoundError:
        raise BackupError(f"rclone isn't installed ({rclone_bin()})") from None
    out = (p.stdout or "").strip()
    if p.returncode != 0 or not out:
        raise BackupError("rclone obscure failed: " + _redact((p.stderr or "").strip()[-300:]))
    return out


def rclone_env() -> dict:
    """The environment that defines both remotes for one rclone call. Kept out of
    os.environ, so no other child process sees it."""
    e = os.environ
    env = {k: v for k, v in e.items() if not k.startswith("RCLONE_CONFIG_")}
    b, c = BASE_REMOTE.upper(), REMOTE.upper()
    env.update({
        f"RCLONE_CONFIG_{b}_TYPE": "s3",
        f"RCLONE_CONFIG_{b}_PROVIDER": "Cloudflare",
        f"RCLONE_CONFIG_{b}_ACCESS_KEY_ID": e["R2_ACCESS_KEY_ID"].strip(),
        f"RCLONE_CONFIG_{b}_SECRET_ACCESS_KEY": e["R2_SECRET_ACCESS_KEY"].strip(),
        f"RCLONE_CONFIG_{b}_ENDPOINT": f"https://{e['R2_ACCOUNT_ID'].strip()}.r2.cloudflarestorage.com",
        f"RCLONE_CONFIG_{b}_REGION": "auto",
        f"RCLONE_CONFIG_{b}_ACL": "private",
        # the API token is scoped to one bucket: it may not list or create buckets
        f"RCLONE_CONFIG_{b}_NO_CHECK_BUCKET": "true",
        f"RCLONE_CONFIG_{c}_TYPE": "crypt",
        f"RCLONE_CONFIG_{c}_REMOTE": f"{BASE_REMOTE}:{e['R2_BUCKET'].strip()}/{prefix()}",
        f"RCLONE_CONFIG_{c}_FILENAME_ENCRYPTION": "standard",
        f"RCLONE_CONFIG_{c}_DIRECTORY_NAME_ENCRYPTION": "true",
        f"RCLONE_CONFIG_{c}_PASSWORD": _obscure(e["BACKUP_CRYPT_PASSWORD"]),
        f"RCLONE_CONFIG_{c}_PASSWORD2": _obscure(e["BACKUP_CRYPT_SALT"]),
    })
    return env


def rclone(env: dict, *args, timeout: float = 1800, ok_codes=(0,)) -> str:
    cmd = [rclone_bin(), "-q", "--retries", "3", "--low-level-retries", "10"]
    bw = (os.environ.get("BACKUP_BWLIMIT") or "").strip()
    if bw:
        cmd += ["--bwlimit", bw]
    cmd += list(args)
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise BackupError(f"rclone isn't installed ({rclone_bin()})") from None
    except subprocess.TimeoutExpired:
        raise BackupError(f"rclone {args[0]} took over {int(timeout // 60)} minutes") from None
    if p.returncode not in ok_codes:
        raise BackupError(f"rclone {args[0]} failed ({p.returncode}): "
                          + _redact((p.stderr or p.stdout or "").strip()[-400:]))
    return p.stdout or ""


def remote(path: str = "") -> str:
    return f"{REMOTE}:{path}"


def list_snapshots(env: dict) -> list[str]:
    # exit 3 = the folder doesn't exist yet (first run)
    out = rclone(env, "lsf", "--files-only", remote("db"), timeout=300, ok_codes=(0, 3))
    return sorted(n.strip() for n in out.splitlines()
                  if n.strip().startswith(SNAP_PREFIX) and n.strip().endswith(SNAP_SUFFIX))


def snapshot_time(name: str):
    try:
        return datetime.strptime(name[len(SNAP_PREFIX):-len(SNAP_SUFFIX)], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def prune_remote(env: dict, now: datetime | None = None) -> tuple[int, int]:
    """Delete snapshots and trash folders older than BACKUP_KEEP_DAYS. Never the newest
    snapshot. Returns (snapshots kept, items deleted)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=keep_days())
    names = list_snapshots(env)
    gone = snaps_gone = 0
    for n in names[:-1]:
        t = snapshot_time(n)
        if t and t < cutoff:
            rclone(env, "deletefile", remote(f"db/{n}"), timeout=300)
            gone += 1
            snaps_gone += 1
    out = rclone(env, "lsf", "--dirs-only", remote("media-trash"), timeout=300, ok_codes=(0, 3))
    for d in out.splitlines():
        d = d.strip().rstrip("/")
        try:
            t = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if t < cutoff:
            rclone(env, "purge", remote(f"media-trash/{d}"), timeout=1800)
            gone += 1
    return len(names) - snaps_gone, gone


# ---------------------------------------------------------------- the run

def _dir_size(path: str) -> tuple[int, int]:
    total = files = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    return total, files


def snapshot(now: datetime | None = None) -> str:
    """rs.db -> data/backups/rs-<utc>.db.gz (consistent while the app writes). Returns the path."""
    now = now or datetime.now(timezone.utc)
    os.makedirs(local_dir(), exist_ok=True)
    need = sum(os.path.getsize(db.DB_PATH + s) for s in ("", "-wal") if os.path.exists(db.DB_PATH + s))
    free = shutil.disk_usage(local_dir()).free
    if free < 2 * need + 200 * 1024 * 1024:
        raise BackupError(f"not enough free disk for a snapshot ({free / 1e9:.1f} GB free)")
    name = f"{SNAP_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}{SNAP_SUFFIX}"
    raw = os.path.join(local_dir(), name[:-3] + ".tmp")
    gz = os.path.join(local_dir(), name)
    try:
        src = sqlite3.connect(db.DB_PATH, timeout=30)
        try:
            dst = sqlite3.connect(raw)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with open(raw, "rb") as fi, gzip.open(gz + ".part", "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1024 * 1024)
        os.replace(gz + ".part", gz)
    finally:
        for p in (raw, gz + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass
    keep = int(_num("BACKUP_KEEP_LOCAL", 3))
    old = sorted(f for f in os.listdir(local_dir())
                 if f.startswith(SNAP_PREFIX) and f.endswith(SNAP_SUFFIX) and f != name)
    for f in old[:-(keep - 1)] if keep > 1 else old:
        try:
            os.remove(os.path.join(local_dir(), f))
        except OSError:
            pass
    return gz


def media_on() -> bool:
    return os.environ.get("BACKUP_MEDIA", "on").strip().lower() not in ("off", "0", "false", "no")


def sync_media(env: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    trash = f"media-trash/{now.strftime('%Y-%m-%d')}"
    total = files = 0
    timeout = _num("BACKUP_MEDIA_TIMEOUT_MIN", 240) * 60
    for sub in MEDIA_DIRS:
        path = os.path.join(db.DATA, sub)
        if not os.path.isdir(path):
            continue
        args = ["sync", path, remote(f"media/{sub}"), "--backup-dir", remote(f"{trash}/{sub}"),
                "--transfers", "4", "--checkers", "8"]
        for x in EXCLUDES:
            args += ["--exclude", x]
        rclone(env, *args, timeout=timeout)
        b, n = _dir_size(path)
        total, files = total + b, files + n
    for f in KEY_FILES:
        p = os.path.join(db.DATA, f)
        if os.path.isfile(p):
            rclone(env, "copyto", p, remote(f"keys/{f}"), timeout=300)
    return {"media_bytes": total, "media_files": files}


def _save(status: dict):
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (STATUS_META, json.dumps(status)))
        conn.commit()


def run(reason: str = "manual", now: datetime | None = None) -> dict:
    """One full backup: snapshot, upload, prune, media. Never raises; the result is
    stored for the System card and returned."""
    t0 = time.time()
    now = now or datetime.now(timezone.utc)
    st = {"started_at": now.replace(microsecond=0).isoformat(), "reason": reason, "ok": False,
          "offsite": configured(), "snapshot": None, "db_bytes": None, "media_bytes": None, "media_files": None,
          "media": media_on(), "remote_snapshots": None, "pruned": 0, "error": None}
    if not configured():
        warn_once()
        st["error"] = "off-site backups are off: " + ", ".join(missing()) + " not set"
        return st
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (ATTEMPT_META, str(time.time())))
        conn.commit()
    step = "snapshot"
    try:
        gz = snapshot(now)
        st["snapshot"], st["db_bytes"] = os.path.basename(gz), os.path.getsize(gz)
        step = "rclone setup"
        env = rclone_env()
        step = "database upload"
        rclone(env, "copyto", gz, remote(f"db/{st['snapshot']}"), timeout=_num("BACKUP_DB_TIMEOUT_MIN", 30) * 60)
        step = "retention"
        st["remote_snapshots"], st["pruned"] = prune_remote(env, now)
        if media_on():
            step = "media sync"
            st.update(sync_media(env, now))
        st["ok"] = True
    except Exception as e:  # noqa: BLE001 — a backup never takes the app down
        st["error"] = f"{step}: {_redact(str(e))[:400]}"
        log.error("backup failed: %s", st["error"])
    st["finished_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    st["seconds"] = round(time.time() - t0, 1)
    try:
        import alerts
        if st["ok"]:
            with db.connect() as conn:
                alerts.backup_ok(conn)
        else:
            alerts.fire("backup_failed", "Backup failed", f"The off-site backup stopped at {st['error']}. "
                        "The app is fine; the last good backup is on the admin System card.")
    except Exception:  # noqa: BLE001
        log.exception("couldn't record the backup result")
    try:
        with db.connect() as conn:
            good = _stored(conn).get("good")
        if st["ok"]:
            good = {k: st[k] for k in ("finished_at", "snapshot", "db_bytes", "media_bytes", "media_files",
                                       "remote_snapshots")}
        _save({"last": st, "good": good})
    except Exception:  # noqa: BLE001
        log.exception("couldn't save the backup status")
    log.info("backup %s: %s", "ok" if st["ok"] else "failed",
             {k: st[k] for k in ("snapshot", "db_bytes", "media_bytes", "remote_snapshots", "pruned", "seconds")})
    return st


# ---------------------------------------------------------------- schedule (worker loop)

_running = threading.Lock()


def _stored(conn) -> dict:
    """{"last": the last run, "good": the last good run} (either may be missing)."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (STATUS_META,)).fetchone()
    try:
        v = json.loads(row["value"]) if row and row["value"] else {}
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def due(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        good = _stored(conn).get("good") or {}
        row = conn.execute("SELECT value FROM meta WHERE key=?", (ATTEMPT_META,)).fetchone()
    try:
        attempt = float(row["value"]) if row and row["value"] else 0.0
    except (ValueError, TypeError):
        attempt = 0.0
    if now.timestamp() - attempt < _num("BACKUP_RETRY_MIN", 60) * 60:
        return False
    ok_at = good.get("finished_at")
    if not ok_at:
        return True
    try:
        ok = datetime.fromisoformat(ok_at)
    except ValueError:
        return True
    age = now - ok
    if age >= timedelta(hours=24):
        return True
    return now.hour >= int(_num("BACKUP_HOUR_UTC", 8)) and ok.date() < now.date() and age >= timedelta(hours=6)


def maybe_run():
    """From the worker loop, every pass: start the nightly run in its own thread when
    it's due. Never raises, never blocks."""
    try:
        if not enabled():
            return
        if not configured():
            warn_once()
            return
        if _running.locked() or not due():
            return
        if not _running.acquire(blocking=False):
            return

        def _go():
            try:
                run("nightly")
            finally:
                _running.release()

        threading.Thread(target=_go, name="rs-backup", daemon=True).start()
    except Exception:  # noqa: BLE001
        log.exception("backup scheduling failed")


def status() -> dict:
    """For the admin System card."""
    try:
        with db.connect() as conn:
            stored = _stored(conn)
    except Exception:  # noqa: BLE001
        stored = {}
    last, good = stored.get("last"), stored.get("good")
    ok_at = (good or {}).get("finished_at")
    late = None
    if ok_at:
        try:
            late = datetime.now(timezone.utc) - datetime.fromisoformat(ok_at) > timedelta(
                hours=_num("OPS_BACKUP_MAX_HOURS", 26))
        except ValueError:
            late = True
    return {"configured": configured(), "enabled": enabled(), "missing": missing(), "last": last, "good": good,
            "ok_at": ok_at, "late": late, "running": _running.locked(), "keep_days": keep_days(),
            "prefix": prefix(), "media": media_on(), "media_dirs": list(MEDIA_DIRS)}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if a.status:
        print(json.dumps(status(), indent=2))
        return 0
    st = run("cli")
    print(json.dumps({k: st.get(k) for k in ("ok", "snapshot", "db_bytes", "media_bytes", "media_files",
                                              "remote_snapshots", "pruned", "seconds", "error")}))
    return 0 if st["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
