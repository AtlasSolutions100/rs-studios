"""Storage quotas, body limits and the disk guard (storage.py; roadmap M1.4), on a
throwaway database. Big files are sparse (they report their size but take no disk).

What it pins down:
  * quotas by plan (Free 200 MB, Starter 2 GB, Growth 10 GB, Scale 50 GB), checked
    on upload before anything is kept, again with the real size, and on render save
    (an ad over quota keeps the vendor's link); deleted ads don't count;
  * body limits per route: uploads 155 MB, webhooks 256 KB, everything else 1 MB (413);
  * under 5 GB free disk, writes are refused with 507 and the message (never a 500),
    and the operator is alerted;
  * the daily purge removes temp leftovers and old exports only (deleted ads' files
    only with RS_STORAGE_PURGE_DELETED=1).

    python tests/test_storage.py      (tests/test_app.py runs it too)
"""
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_storage_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")  # sign-ups here go straight in; test_login.py checks the emailed-link step
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x", RS_ACCESS_LOG="0")
for k in ("RS_STORAGE_QUOTAS", "RS_DISK_MIN_GB", "RS_STORAGE_PURGE_DELETED", "OPS_ALERT_EMAIL"):
    os.environ.pop(k, None)

import alerts  # noqa: E402
import app as appmod  # noqa: E402
import db  # noqa: E402
import exporter  # noqa: E402
import hf  # noqa: E402
import storage  # noqa: E402
import worker  # noqa: E402
from PIL import Image  # noqa: E402

passed = 0
MB = storage.MB

# The guard is checked at its default (5 GB) against a faked free-disk figure, so this file
# passes on a small CI disk whatever RS_DISK_MIN_GB the runner has (N14). The real
# reading is only checked to work; the disk-full section below fakes 4 GB.
_real_free = storage.disk_free_bytes
_real_free_now = _real_free()
PLENTY = 50 * 1024 * MB


def _plenty():
    return PLENTY


storage.disk_free_bytes = _plenty


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


check(isinstance(_real_free_now, int), "the real free-disk reading works (the checks below fake 50 GB free)")

_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    with db.connect() as conn:
        ws = conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                          (email,)).fetchone()
    return c, ws


def ws_row(wid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()


def sparse(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    return path


def png(noise_mb=0.0):
    b = io.BytesIO()
    if noise_mb:
        side = int((noise_mb * MB / 3) ** 0.5) + 1
        Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(b, "PNG")
    else:
        Image.new("RGB", (64, 64), (200, 120, 40)).save(b, "PNG")
    return b.getvalue()


def upload(c, data, name="p.png", n=1, **headers):
    files = [(io.BytesIO(data), f"{i}{name}") for i in range(n)]
    return c.post("/app/photos", data={"csrf": csrf(c), "photos": files}, content_type="multipart/form-data",
                  headers=headers)


def photo_count(wid):
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM photos WHERE workspace_id=?", (wid,)).fetchone()[0]


print("== quotas by plan")
check(storage.QUOTA_MB == {"free": 200, "starter": 2048, "growth": 10240, "scale": 51200},
      "Free 200 MB, Starter 2 GB, Growth 10 GB, Scale 50 GB")
c, ws = signup("quota@example.com")
wid = ws["id"]
check(storage.quota_bytes(ws) == 200 * MB, "a new account is on the free quota")
for code, mb in (("starter", 2048), ("growth", 10240), ("scale", 51200)):
    check(storage.quota_bytes(dict(ws, plan_override=code)) == mb * MB, f"{code}: {mb} MB")

print("== uploads")
r = upload(c, png())
check(r.status_code == 302 and photo_count(wid) == 1, "a photo uploads under quota")
filler = sparse(os.path.join(TMP, "uploads", str(wid), "filler.bin"), 200 * MB - 1000)
check(storage.used_bytes(wid) >= 199 * MB, "usage counts the workspace's uploads")
r = upload(c, png(1.0))
check(r.status_code == 507, "an upload past the free quota: 507")
body = r.get_data(as_text=True)
check("Your storage is full" in body and "of 200 MB used on the Free plan" in body and "Billing" in body,
      "with the usage, the limit and what to do")
check(photo_count(wid) == 1, "nothing was kept")
with db.connect() as conn:
    nprod = conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=?", (wid,)).fetchone()[0]
r = c.post("/app/products/new", data={"csrf": csrf(c), "name": "Lamp", "photos": [(io.BytesIO(png(1.0)), "a.png")]},
           content_type="multipart/form-data")
with db.connect() as conn:
    check(r.status_code == 507 and conn.execute("SELECT COUNT(*) FROM products WHERE workspace_id=?",
                                                (wid,)).fetchone()[0] == nprod,
          "a new product with photos that don't fit: refused before the product (or an AI read) is made")
r = upload(c, png(), n=3)
check(r.status_code == 507 and photo_count(wid) == 1,
      "a 3-photo upload that doesn't fit is refused before any photo is kept")

with appmod.app.test_request_context("/"):
    small = sparse(os.path.join(TMP, "uploads", str(wid), "late.bin"), 2 * MB)
    try:
        storage.check_saved(ws_row(wid), small)
        ok = False
    except storage.QuotaFull:
        ok = True
    check(ok and not os.path.exists(small), "a file that only turns out too big once saved is removed and refused")

with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='starter' WHERE id=?", (wid,))
    conn.commit()
r = upload(c, png())
check(r.status_code == 302 and photo_count(wid) == 2, "the same usage fits on Starter")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override=NULL WHERE id=?", (wid,))
    conn.commit()

os.environ["RS_STORAGE_QUOTAS"] = "0"
r = upload(c, png())
check(r.status_code == 302 and photo_count(wid) == 3, "RS_STORAGE_QUOTAS=0 switches quotas off")
os.environ.pop("RS_STORAGE_QUOTAS")
os.remove(filler)

print("== deleted ads don't count")
with db.connect() as conn:
    j1 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at) "
                      "VALUES (?, 1, 'ad_image', 'done', ?, ?)", (wid, db.now(), db.now())).lastrowid
    j2 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at, deleted_at) "
                      "VALUES (?, 1, 'ad_image', 'done', ?, ?, ?)", (wid, db.now(), db.now(), db.now())).lastrowid
    conn.commit()
base = storage.used_bytes(wid)
sparse(os.path.join(TMP, "media", str(wid), str(j1), "campaignify-1-1.png"), 50 * MB)
sparse(os.path.join(TMP, "media", str(wid), str(j2), "campaignify-2-1.png"), 300 * MB)
check(storage.used_bytes(wid) - base == 50 * MB, "a kept ad counts; a deleted one doesn't")

print("== render save")
_real_get, _real_outputs = worker.requests.get, hf.outputs


class _Got:
    def __init__(self, n):
        self.content = b"x" * n

    def raise_for_status(self):
        pass


worker.requests.get = lambda url, timeout=None: _Got(3 * MB)
hf.outputs = lambda res: [{"kind": "image", "url": "https://vendor.example/a.png"}]
with db.connect() as conn:
    j3 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at) "
                      "VALUES (?, 1, 'ad_image', 'running', ?, ?)", (wid, db.now(), db.now())).lastrowid
    conn.commit()
    worker._save_outputs(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (j3,)).fetchone(), {})
    o = conn.execute("SELECT * FROM outputs WHERE job_id=?", (j3,)).fetchone()
check(o["path"] and os.path.getsize(o["path"]) == 3 * MB, "under quota: the ad is copied to disk")
sparse(os.path.join(TMP, "media", str(wid), str(j1), "campaignify-1-2.png"), 146 * MB)
with db.connect() as conn:
    j4 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at) "
                      "VALUES (?, 1, 'ad_image', 'running', ?, ?)", (wid, db.now(), db.now())).lastrowid
    conn.commit()
    worker._save_outputs(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (j4,)).fetchone(), {})
    o = conn.execute("SELECT * FROM outputs WHERE job_id=?", (j4,)).fetchone()
check(o is not None and o["path"] is None and o["url"] == "https://vendor.example/a.png",
      "over quota: the ad is kept as the vendor's link, not lost")
os.remove(os.path.join(TMP, "media", str(wid), str(j1), "campaignify-1-2.png"))

print("== disk guard (disk-full simulation)")
fired = []
_real_fire = alerts.fire
alerts.fire = lambda key, title, detail="": fired.append(key) or True
storage.disk_free_bytes = lambda: 4 * 1024 * MB
try:
    r = upload(c, png())
    check(r.status_code == 507 and storage.DISK_MESSAGE.split(",")[0] in r.get_data(as_text=True),
          "under 5 GB free: upload refused with 507 and the message")
    check("disk_full" in fired, "the operator is alerted")
    r = upload(c, png(), Accept="application/json")
    check(r.status_code == 507 and r.get_json()["error"] == "storage_full", "a JSON client gets JSON")
    try:
        src = os.path.join(TMP, "src.png")
        Image.new("RGB", (100, 100)).save(src)
        exporter.export(src, "image", "meta", "square", 50, 50)
        ok = False
    except storage.DiskFull:
        ok = True
    check(ok, "exports aren't written either")
    with db.connect() as conn:
        j5 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, created_at, updated_at) "
                          "VALUES (?, 1, 'ad_image', 'running', ?, ?)", (wid, db.now(), db.now())).lastrowid
        conn.commit()
        worker._save_outputs(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (j5,)).fetchone(), {})
        o = conn.execute("SELECT * FROM outputs WHERE job_id=?", (j5,)).fetchone()
    check(o["path"] is None and o["url"], "a finished ad keeps its link when the disk is short")
    try:
        with db.connect() as conn:
            worker._save_preview(conn, conn.execute("SELECT * FROM jobs WHERE id=?", (j5,)).fetchone(),
                                 [{"kind": "video", "url": "https://vendor.example/v.mp4"}])
        ok = False
    except hf.GenError as e:
        ok = "short on space" in str(e)
    check(ok, "a free preview fails (and is refunded by the worker) rather than half-saving")
    r = client().get("/pricing")
    check(r.status_code == 200, "pages that write nothing still work")
finally:
    storage.disk_free_bytes = _plenty
    alerts.fire = _real_fire
    worker.requests.get, hf.outputs = _real_get, _real_outputs

print("== body limits")
anon = client()
r = anon.post("/login", data=b"x" * (1 * MB + 10), content_type="application/x-www-form-urlencoded")
check(r.status_code == 413, "1 MB+ to an ordinary route: 413")
r = anon.post("/webhooks/stripe", data=b"{" + b" " * (300 * 1024) + b"}", content_type="application/json")
check(r.status_code == 413 and r.get_json()["error"] == "too_large", "300 KB to a webhook: 413 before any parsing")
r = anon.post("/webhooks/stripe", data=b'{"type": "x"}', headers={"Stripe-Signature": "t=1,v1=bad"},
              content_type="application/json")
check(r.status_code == 400, "a small webhook still reaches its signature check")
r = upload(c, png(2.0))
check(r.status_code == 302, "a 2 MB photo to an upload route is fine")
check(storage.body_limit("upload_photos") == 155 * MB and storage.body_limit("cpools.index") == 155 * MB
      and storage.body_limit("stripe_webhook") == 256 * 1024 and storage.body_limit("anything.else") == MB,
      "limits: uploads 155 MB, webhooks 256 KB, the rest 1 MB")
r = anon.post("/login", data=b"x" * (2 * MB))
check(r.status_code == 413 and "too big" in r.get_data(as_text=True), "the 413 page says what happened")

print("== daily purge")
media = os.path.join(TMP, "media", str(wid), str(j1))
old = time.time() - 9 * 86400
orig = os.path.join(media, "campaignify-1-1.png")
paths = {"temp_old": os.path.join(media, "campaignify-1-1.mp4.part.mp4"),
         "temp_new": os.path.join(media, "campaignify-1-3.mp4.wm.mp4"),
         "export_old": os.path.join(media, "campaignify-1-1.meta-square.png"),
         "export_new": os.path.join(media, "campaignify-1-1.tiktok-vertical.png"),
         "poster": os.path.join(media, "campaignify-1-4.mp4.jpg")}
for k, p in paths.items():
    with open(p, "wb") as f:
        f.write(b"x")
for k in ("temp_old", "export_old", "poster"):
    os.utime(paths[k], (old, old))
os.utime(orig, (old, old))
out = storage.purge()
check(not os.path.exists(paths["temp_old"]) and os.path.exists(paths["temp_new"]),
      "temp leftovers over an hour old go; fresh ones stay")
check(not os.path.exists(paths["export_old"]) and os.path.exists(paths["export_new"]),
      "exports older than 7 days go; recent ones stay")
check(os.path.exists(orig) and os.path.exists(paths["poster"]), "originals and posters are never touched")
check(out["temp"] == 1 and out["exports"] == 1 and out["deleted_jobs"] == 0, "the purge reports what it removed")
deleted_dir = os.path.join(TMP, "media", str(wid), str(j2))
with db.connect() as conn:
    conn.execute("UPDATE jobs SET deleted_at='2020-01-01T00:00:00+00:00' WHERE id=?", (j2,))
    conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?, 'image', 'https://v/x.png', ?)",
                 (j2, os.path.join(deleted_dir, "campaignify-2-1.png")))
    conn.commit()
storage.purge()
check(os.path.isdir(deleted_dir), "deleted ads' files stay unless RS_STORAGE_PURGE_DELETED=1")
os.environ["RS_STORAGE_PURGE_DELETED"] = "1"
out = storage.purge()
os.environ.pop("RS_STORAGE_PURGE_DELETED")
with db.connect() as conn:
    p = conn.execute("SELECT path FROM outputs WHERE job_id=?", (j2,)).fetchone()["path"]
check(not os.path.isdir(deleted_dir) and p is None and out["deleted_jobs"] == 1,
      "with it on, ads deleted over 30 days ago lose their files")
check(os.path.isdir(media), "kept ads are untouched")
with db.connect() as conn:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (storage.PURGE_META, str(time.time())))
    conn.commit()
_calls = []
_real_purge = storage.purge
storage.purge = lambda now=None: _calls.append(1)
storage.maybe_purge()
check(not _calls, "the daily purge doesn't run twice in a day")
with db.connect() as conn:
    conn.execute("UPDATE meta SET value=? WHERE key=?", (str(time.time() - 49 * 3600), storage.PURGE_META))
    conn.commit()
storage.maybe_purge()
check(_calls == [1], "it runs once more than 2 days have passed")
storage.purge = _real_purge

print(f"\n{passed} checks passed")
