"""Workspace deletion with a 7-day grace (wsdelete.py, deletion_routes.py; roadmap M7.5, SEC-7, SEC-12).

    python tests/test_wsdelete.py      (tests/test_app.py runs it too)

  * only the owner asks, by typing the workspace name; a live plan blocks it (cancel on Billing first; we never
    cancel it); an operator's workspace can't be deleted; cancel during the grace
  * nothing is removed before the grace ends, and nothing is ever removed by age alone
  * after it: queued work stops (refunded), the vendor's copies are deleted (a busy one is retried), ad-account
    access is revoked and tokens deleted, files under the data folder removed, content rows deleted, records kept
    without content, every login anonymized, the workspace renamed; another workspace is untouched
  * one contact's delete clears its messages' address and body and its flow runs' context (SEC-12)
"""
import base64
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_wsdelete_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_EMAIL_VERIFY="0",
                  RS_NUDGE_LOOP="0", RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", OPS_ALERTS="0",
                  TOKEN_KEY=base64.b64encode(b"D" * 32).decode())
for k in ("AD_LAUNCH", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "RS_OWNER_INVITES"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import adplatforms  # noqa: E402
import db  # noqa: E402
import hf  # noqa: E402
import wsdelete  # noqa: E402

appmod._maybe_reconcile = lambda ws_id, force=False: 0
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        shutil.rmtree(TMP, True)
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def q(sql, *a):
    with db.connect() as conn:
        return conn.execute(sql, a).fetchall()


def ex(sql, *a):
    with db.connect() as conn:
        cur = conn.execute(sql, a)
        conn.commit()
        return cur.lastrowid


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.75.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    u = q("SELECT * FROM users WHERE email=?", email)[0]
    ex("UPDATE users SET email_verified_at=? WHERE id=?", db.now(), u["id"])
    return c, u


def flashes(c):
    with c.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


owner, U = signup("delete-owner@example.com")
WS = U["workspace_id"]
ex("UPDATE workspaces SET name='Ember Candle Co', brand_name='Ember', postal_address='1 Main St' WHERE id=?", WS)
from werkzeug.security import generate_password_hash  # noqa: E402
member_id = ex("INSERT INTO users (email, name, pw_hash, is_admin, workspace_id, created_at, role, email_verified_at) "
               "VALUES (?,?,?,?,?,?,?,?)", "delete-member@example.com", "Mo", generate_password_hash("memberpassword"), 0,
               WS, db.now(), "member", db.now())
other, OU = signup("keep-other@example.com")
OWS = OU["workspace_id"]


def seed(ws_id, uid, tag):
    """Files and rows of every kind the purge handles."""
    up = os.path.join(TMP, "uploads", str(ws_id))
    os.makedirs(up, exist_ok=True)
    photo = os.path.join(up, f"{tag}.png")
    open(photo, "wb").write(b"png")
    ex("INSERT INTO photos (workspace_id, filename, path, content_type, remote_url, created_at) VALUES (?,?,?,?,?,?)",
       ws_id, f"{tag}.png", photo, "image/png", "https://cdn.vendor.test/p.png", db.now())
    jid = ex("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, request_id, created_at, "
             "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ws_id, uid, "ad_image", f"{tag} secret brief", '{"x": 1}', "done",
             43, f"req-{tag}-done", db.now(), db.now())
    md = os.path.join(TMP, "media", str(ws_id), str(jid))
    os.makedirs(md, exist_ok=True)
    out = os.path.join(md, "campaignify-1.png")
    open(out, "wb").write(b"img")
    ex("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", jid, "image", "https://cdn.vendor.test/o.png", out)
    pend = ex("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, created_at, updated_at) "
              "VALUES (?,?,?,?,?,?,?,?,?)", ws_id, uid, "ad_image", "queued one", "{}", "pending", 43, db.now(), db.now())
    cid = ex("INSERT INTO contacts (workspace_id, email, name, first_seen, last_seen) VALUES (?,?,?,?,?)",
             ws_id, f"lead-{tag}@example.com", "Lee", db.now(), db.now())
    ex("INSERT INTO messages (workspace_id, contact_id, channel, to_addr, subject, body_html, status, created_at) "
       "VALUES (?,?,?,?,?,?,?,?)", ws_id, cid, "email", f"lead-{tag}@example.com", "Hi", "<p>Hi Lee</p>", "sent", db.now())
    ex("INSERT INTO flow_runs (flow_id, version_id, workspace_id, contact_id, started_at, context_json) VALUES (?,?,?,?,?,?)",
       1, 1, ws_id, cid, db.now(), json.dumps({"email": f"lead-{tag}@example.com"}))
    ex("INSERT INTO ad_connections (workspace_id, platform, external_account_id, account_name, external_user_id, token_enc, "
       "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", ws_id, "meta", f"acc-{tag}", "Shop", f"fbuser-{tag}",
       adplatforms.seal_token(ws_id, f"EAAtoken{tag}"), "active", db.now(), db.now())
    ex("INSERT INTO ledger (workspace_id, delta, reason, created_at) VALUES (?,?,?,?)", ws_id, 100, "Pack", db.now())
    return {"photo": photo, "out": out, "job": jid, "pending": pend, "contact": cid, "media": md}


A = seed(WS, U["id"], "a")
B = seed(OWS, OU["id"], "b")

# ================================================================ 1. asking for it
print("asking for it")
html = owner.get("/app/workspace/delete").get_data(as_text=True)
check("Delete this workspace in 7 days" in html and "Ember Candle Co" in html and "What we keep" in html,
      "the owner sees what is removed and kept, and the confirm box")
check("Delete this workspace" in owner.get("/app/team").get_data(as_text=True), "linked from the Team page")
mc = client()
mc.post("/login", data={"csrf": csrf(mc), "email": "delete-member@example.com", "password": "memberpassword"})
check("Only the workspace owner can delete" in mc.get("/app/workspace/delete").get_data(as_text=True),
      "a member sees the page but no button")
r = mc.post("/app/workspace/delete", data={"csrf": csrf(mc), "confirm": "Ember Candle Co"})
check(r.status_code == 403 and not q("SELECT delete_after FROM workspaces WHERE id=?", WS)[0]["delete_after"],
      "and can't ask for it")
owner.post("/app/workspace/delete", data={"csrf": csrf(owner), "confirm": "Ember"})
check(not q("SELECT delete_after FROM workspaces WHERE id=?", WS)[0]["delete_after"] and "Type the workspace name" in
      flashes(owner), "the name must be typed exactly")
ex("UPDATE workspaces SET stripe_subscription_id='sub_live', plan_status='active', plan='growth' WHERE id=?", WS)
html = owner.get("/app/workspace/delete").get_data(as_text=True)
check("Cancel your plan first" in html and "Delete this workspace in 7 days" not in html, "a live plan: cancel it first")
owner.post("/app/workspace/delete", data={"csrf": csrf(owner), "confirm": "Ember Candle Co"})
w = q("SELECT * FROM workspaces WHERE id=?", WS)[0]
check(not w["delete_after"] and w["plan_status"] == "active" and w["stripe_subscription_id"] == "sub_live"
      and "Cancel your plan on the Billing page first" in flashes(owner),
      "and posting anyway schedules nothing and never touches the plan")
ex("UPDATE workspaces SET plan_cancel_at_period_end=1 WHERE id=?", WS)
owner.post("/app/workspace/delete", data={"csrf": csrf(owner), "confirm": "ember candle co"})
w = q("SELECT * FROM workspaces WHERE id=?", WS)[0]
after = datetime.fromisoformat(w["delete_after"])
check(w["delete_after"] and timedelta(days=6, hours=23) < after - datetime.now(timezone.utc) <= timedelta(days=7)
      and w["delete_requested_by"] == U["id"], "a plan set to end: scheduled 7 days out")
page = owner.get("/app").get_data(as_text=True)
check("This workspace will be deleted on" in page and "Cancel the deletion" in page, "every page shows the banner")
owner.post("/app/workspace/delete/cancel", data={"csrf": csrf(owner)})
check(not q("SELECT delete_after FROM workspaces WHERE id=?", WS)[0]["delete_after"], "the owner cancels it")
owner.post("/app/workspace/delete", data={"csrf": csrf(owner), "confirm": "Ember Candle Co"})
check(q("SELECT delete_after FROM workspaces WHERE id=?", WS)[0]["delete_after"], "and can ask again")

# ================================================================ 2. the vendor call
print("the vendor's delete call")


class R:
    def __init__(self, code):
        self.status_code, self.text, self.headers = code, "{}", {}

    def json(self):
        return {}


sent = []
real_delete = hf.requests.delete
for code, want in ((200, hf.DELETED), (404, hf.GONE), (409, hf.BUSY), (400, "error: 400")):
    hf.requests.delete = lambda url, code=code, **kw: (sent.append((url, kw.get("headers", {}).get("Authorization"))),
                                                         R(code))[1]
    check(hf.delete_request("11111111-2222-3333-4444-555555555555") == want, f"DELETE /requests/<id> {code}: {want}")
hf.requests.delete = real_delete
check(sent[0][0].endswith("/requests/11111111-2222-3333-4444-555555555555") and sent[0][1] == "Key test:secret",
      "the documented endpoint, with our key")

# ================================================================ 3. the grace, and no age-based deletion
print("the grace")
calls = []
answers = {}


def fake_delete(rid):
    calls.append(rid)
    return answers.get(rid, hf.DELETED)


hf.delete_request = fake_delete
revoked = []


class FakeAd:
    def configured(self):
        return True

    def revoke(self, token):
        revoked.append(token)
        return True


real_adapter = adplatforms.adapter
adplatforms.adapter = lambda p: FakeAd()
check(wsdelete.tick() == [] and os.path.exists(A["photo"]) and q("SELECT 1 FROM contacts WHERE workspace_id=?", WS),
      "before the grace ends nothing is removed")
ex("UPDATE workspaces SET created_at='2001-01-01T00:00:00+00:00' WHERE id=?", OWS)
ex("UPDATE jobs SET created_at='2001-01-01T00:00:00+00:00' WHERE workspace_id=?", OWS)
far = datetime.now(timezone.utc) + timedelta(days=400)
_res = wsdelete.tick(far)
check(all(r_["workspace_id"] == WS for r_ in _res) and os.path.exists(B["photo"]),
      "a workspace nobody asked to delete is never touched, however old its data")

# ================================================================ 4. the purge
print("the purge")
# (that tick ran the purge for WS already; check it, with a running job to finish)
ws_row = q("SELECT * FROM workspaces WHERE id=?", WS)[0]
check(ws_row["deleted_at"] and ws_row["name"] == f"Deleted workspace #{WS}" and ws_row["brand_name"] is None
      and ws_row["postal_address"] is None, "the workspace row stays, renamed, its details cleared")
check(not os.path.exists(A["photo"]) and not os.path.exists(A["out"]) and not os.path.exists(A["media"]),
      "its files are removed")
check(f"req-a-done" in calls and q("SELECT vendor_deleted_at FROM jobs WHERE id=?", A["job"])[0]["vendor_deleted_at"],
      "the vendor's copy of each finished request is deleted")
check(revoked == ["EAAtokena"] and not q("SELECT 1 FROM ad_connections WHERE workspace_id=?", WS),
      "ad-account access revoked at the platform and the tokens deleted")
for t in ("photos", "contacts", "messages", "flow_runs", "ad_connections"):
    check(not q(f"SELECT 1 FROM {t} WHERE workspace_id=?", WS), f"{t}: deleted")
check(not q("SELECT 1 FROM outputs WHERE job_id=?", A["job"]), "outputs: deleted")
j = q("SELECT * FROM jobs WHERE id=?", A["job"])[0]
check(j["brief"] == "" and j["params_json"] == "{}" and j["credits"] == 43, "jobs stay as records, without content")
check(q("SELECT status FROM jobs WHERE id=?", A["pending"])[0]["status"] == "failed", "queued work was stopped (and refunded)")
check(q("SELECT COUNT(*) n FROM ledger WHERE workspace_id=?", WS)[0]["n"] >= 1, "the ledger stays")
us = q("SELECT * FROM users WHERE workspace_id=?", WS)
check(all(u_["email"] == f"deleted-{u_['id']}@deleted.invalid" and u_["name"] is None and u_["pw_hash"].startswith("!")
          for u_ in us) and len(us) == 2, "every login of the workspace is anonymized")
c2 = client()
c2.post("/login", data={"csrf": csrf(c2), "email": "delete-owner@example.com", "password": "longpassword"})
check(c2.get("/app").status_code == 302 and owner.get("/app").status_code == 302,
      "the old login no longer works, and the open session is ended")
runs = q("SELECT * FROM deletion_runs WHERE workspace_id=?", WS)
check(runs and runs[-1]["done"] == 1 and "lead-a@" not in (runs[-1]["summary"] or ""), "the run is logged, ids and counts only")
check(os.path.exists(B["photo"]) and os.path.exists(B["out"]) and q("SELECT 1 FROM contacts WHERE workspace_id=?", OWS)
      and q("SELECT 1 FROM ad_connections WHERE workspace_id=?", OWS) and "req-b-done" not in calls,
      "another workspace is untouched")

print("a job still running, a vendor copy still busy")
cw, CU = signup("delete-second@example.com")
W2 = CU["workspace_id"]
C = seed(W2, CU["id"], "c")
ex("UPDATE jobs SET status='running', request_id='req-c-run' WHERE id=?", C["pending"])
answers["req-c-done"] = hf.BUSY
ex("UPDATE workspaces SET delete_after='2000-01-01T00:00:00+00:00' WHERE id=?", W2)
res = [r_ for r_ in wsdelete.tick() if r_["workspace_id"] == W2][0]
check(not res["done"] and res["running_jobs"] == 1 and res["vendor"]["left"] >= 1
      and not q("SELECT deleted_at FROM workspaces WHERE id=?", W2)[0]["deleted_at"],
      "a job running at the vendor and a busy vendor copy: not done yet")
check(not os.path.exists(C["photo"]) and not q("SELECT 1 FROM contacts WHERE workspace_id=?", W2),
      "but everything else is already gone")
ex("UPDATE jobs SET status='done' WHERE id=?", C["pending"])
answers.pop("req-c-done")
wsdelete._last[0] = 0
res = [r_ for r_ in wsdelete.tick() if r_["workspace_id"] == W2][0]
check(res["done"] and q("SELECT deleted_at FROM workspaces WHERE id=?", W2)[0]["deleted_at"], "the next run finishes it")

print("an operator's workspace")
op, OPU = signup("delete-operator@example.com")
ex("UPDATE users SET is_admin=1 WHERE id=?", OPU["id"])
with db.connect() as conn:
    st = wsdelete.request(conn, OPU["workspace_id"], OPU["id"])
check(st["blocked"] == "operator" and not st["scheduled"], "can't be scheduled")
ex("UPDATE workspaces SET delete_after='2000-01-01T00:00:00+00:00' WHERE id=?", OPU["workspace_id"])
res = [r_ for r_ in wsdelete.tick() if r_["workspace_id"] == OPU["workspace_id"]][0]
check(res.get("blocked") == "operator" and q("SELECT email FROM users WHERE id=?", OPU["id"])[0]["email"]
      == "delete-operator@example.com", "and is never purged, even if one were due")
ex("UPDATE workspaces SET delete_after=NULL WHERE id=?", OPU["workspace_id"])
adplatforms.adapter = real_adapter

# ================================================================ 5. one contact (SEC-12)
print("one contact's delete (SEC-12)")
ex("UPDATE workspaces SET plan='scale', plan_status='active' WHERE id=?", OWS)
r = other.post(f"/app/contacts/{B['contact']}", data={"csrf": csrf(other), "action": "delete"})
m = q("SELECT * FROM messages WHERE workspace_id=?", OWS)
fr = q("SELECT * FROM flow_runs WHERE workspace_id=?", OWS)
check(r.status_code == 302 and not q("SELECT 1 FROM contacts WHERE id=?", B["contact"]), "the contact is deleted")
check(m and m[0]["to_addr"] is None and m[0]["body_html"] is None and m[0]["subject"] is None,
      "its messages keep no address or body")
check(fr and fr[0]["context_json"] == "{}", "its flow runs keep no context")

shutil.rmtree(TMP, True)
print(f"\nall {passed} checks passed")
