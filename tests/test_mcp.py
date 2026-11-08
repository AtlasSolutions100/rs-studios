"""The MCP server (mcpserver.py + mcp_tools.py) on a throwaway database.

    python tests/test_mcp.py

Covers: MCP_SERVER off = 404; keys shown once, stored hashed, revocable;
401 + discovery; initialize / tools/list / tools/call; read-only keys;
quote_ad_batch then generate with the quoted total (a different total or no
total makes nothing); video needs a confirmed email; plan features (copy and
planner on Starter); signed download links; the OAuth code + PKCE flow.
No network: the generation worker is off and AI writing is not configured.
"""
import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_mcp_")
for k in ("MCP_SERVER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MCP_ALLOWED_ORIGINS"):
    os.environ.pop(k, None)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret")

import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import mcpserver  # noqa: E402
import products  # noqa: E402

app = appmod.app
app.testing = True
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client():
    _ip[0] += 1
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.0.{_ip[0]}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email):
    c = client()
    c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword", "name": email[:3]})
    return c


def make_key(c, scope="read"):
    r = c.post("/app/mcp", data={"csrf": csrf(c), "action": "create", "name": f"test {scope}", "scope": scope})
    m = re.search(r"(cfy_[A-Za-z0-9_-]{40,})", r.get_data(as_text=True))
    return m.group(1) if m else None


anon = app.test_client()
N = [0]


def rpc(method, params=None, key=None, headers=None, notify=False):
    N[0] += 1
    msg = {"jsonrpc": "2.0", "method": method}
    if not notify:
        msg["id"] = N[0]
    if params is not None:
        msg["params"] = params
    h = {"Accept": "application/json, text/event-stream"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    h.update(headers or {})
    return anon.post("/mcp", json=msg, headers=h)


def call(key, name, args=None):
    r = rpc("tools/call", {"name": name, "arguments": args or {}}, key=key)
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json()["result"]


def text(res):
    return res["content"][0]["text"]


def png_file():
    from PIL import Image
    p = os.path.join(TMP, f"p{N[0]}.png")
    Image.new("RGB", (64, 64), (200, 120, 40)).save(p, "PNG")
    return p


owner = signup("owner@example.com")
other = signup("other@example.com")
with db.connect() as conn:
    ws = conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                      ("owner@example.com",)).fetchone()
    other_ws = conn.execute("SELECT w.* FROM workspaces w JOIN users u ON u.workspace_id=w.id WHERE u.email=?",
                            ("other@example.com",)).fetchone()
    pid = products.save_kit(conn, ws, brand_name="Acme", product_name="Trail Mug", audience="hikers")
    ph = products.add_photo(conn, ws["id"], pid, png_file(), "mug.png", "image/png")
    conn.commit()
    other_pid = products.save_kit(conn, other_ws, brand_name="Other", product_name="Other thing")
    conn.commit()
    start_balance = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone())

print("== switched off (default)")
check(not mcpserver.enabled(), "MCP_SERVER is off unless set")
check(rpc("initialize", {}).status_code == 404, "/mcp answers 404 while off")
check(owner.get("/app/mcp").status_code == 404, "the key page answers 404 while off")
check("API and Claude" not in owner.get("/app/team").get_data(as_text=True), "no nav entry while off")
r = anon.get("/docs/mcp")
check(r.status_code == 200 and "Not switched on yet" in r.get_data(as_text=True), "docs page says it isn't on yet")
check("/mcp" not in anon.get("/llms.txt").get_data(as_text=True), "llms.txt leaves MCP out while off")

os.environ["MCP_SERVER"] = "on"
print("== auth and discovery")
r = rpc("initialize", {})
check(r.status_code == 401 and "resource_metadata=" in r.headers.get("WWW-Authenticate", ""), "no token: 401")
check(rpc("ping", key="cfy_nope").status_code == 401, "a wrong key: 401")
prm = anon.get("/.well-known/oauth-protected-resource/mcp").get_json()
asm = anon.get("/.well-known/oauth-authorization-server").get_json()
check(prm["authorization_servers"] == [asm["issuer"]] and asm["code_challenge_methods_supported"] == ["S256"],
      "OAuth discovery")
check("/docs/mcp" in anon.get("/llms.txt").get_data(as_text=True), "llms.txt mentions the MCP server when on")
check("API and Claude" in owner.get("/app/team").get_data(as_text=True), "Settings nav shows API and Claude")

print("== keys")
rkey, wkey = make_key(owner, "read"), make_key(owner, "write")
check(rkey and wkey, "keys are made and shown")
check(wkey not in owner.get("/app/mcp").get_data(as_text=True), "a key is shown only once")
with db.connect() as conn:
    rows = [dict(x) for x in conn.execute("SELECT * FROM mcp_tokens")]
check(wkey not in json.dumps(rows) and any(x["token_hash"] == hashlib.sha256(wkey.encode()).hexdigest() for x in rows),
      "only the SHA-256 is stored")
r = owner.post("/app/mcp", data={"csrf": "bad", "action": "create"})
check(r.status_code == 400, "making a key needs the form token")

print("== handshake")
res = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}, key=rkey).get_json()["result"]
check(res["protocolVersion"] == "2025-11-25" and res["serverInfo"]["name"] == "campaignify", "initialize")
check(rpc("notifications/initialized", key=rkey, notify=True).status_code == 202, "notification: 202")
r = anon.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                            {"jsonrpc": "2.0", "method": "notifications/initialized"},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
              headers={"Authorization": f"Bearer {rkey}"})
check(r.status_code == 200 and [x["id"] for x in r.get_json()] == [1, 2], "a JSON-RPC batch gets one answer per request")
tools = {t["name"]: t for t in rpc("tools/list", {}, key=rkey).get_json()["result"]["tools"]}
check({"quote_ad_batch", "generate_image_ads", "generate_video_ads", "generate_ad_copy", "start_campaign_plan",
       "start_competitor_snapshot", "list_assets", "get_asset_download_url", "get_account_status",
       "list_brands_products"} <= set(tools), "tools/list has the ad, copy, planner, competitor and asset tools")
check(tools["quote_ad_batch"]["annotations"]["readOnlyHint"] and not tools["generate_video_ads"]["annotations"]["readOnlyHint"],
      "quote is read-only, generate is not")
check("confirm_total_credits" in tools["generate_video_ads"]["inputSchema"]["required"], "generate needs the quoted total")
check(rpc("ping", key=rkey, headers={"Origin": "https://evil.example"}).status_code == 403, "foreign Origin refused")

print("== reading")
st = call(rkey, "get_account_status")["structuredContent"]
check(st["workspace"]["id"] == ws["id"] and st["credits"] == start_balance and st["features"]["studio"], "account status")
pl = call(rkey, "list_brands_products")["structuredContent"]
check([p["name"] for p in pl["products"]] == ["Trail Mug"] and pl["products"][0]["photos"] == 1,
      "brands and products (this workspace only)")
check(call(rkey, "get_product", {"product_id": pid})["structuredContent"]["photo_ids"] == [ph], "get_product")
res = call(rkey, "get_product", {"product_id": other_pid})
check(res["isError"] and "Not found" in text(res), "another workspace's product: not found")
check(call(rkey, "list_ad_options")["structuredContent"]["presenters"], "ad options")
check(call(rkey, "quote_ai_tasks")["structuredContent"]["campaign_plan_max"] > 0, "AI task estimates")

print("== image ads: quote, then generate with that total")
img = {"ad_type": "image", "product_id": pid, "lines": ["Coffee that keeps up on the trail"], "aspects": ["9:16"],
       "versions": 1}
q = call(rkey, "quote_ad_batch", img)["structuredContent"]
check(q["total_credits"] == catalog.price_credits("ad_image", batch=1) and q["ads"] == 1, "quote prices the batch")
with db.connect() as conn:
    n_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
check(n_jobs == 0, "quoting makes nothing")
res = call(rkey, "generate_image_ads", dict(img, confirm_total_credits=q["total_credits"]))
check(res["isError"] and "read-only" in text(res), "a read-only key can't spend")
res = call(wkey, "generate_image_ads", img)
check(res["isError"] and "confirm_total_credits" in text(res), "no total: nothing is made")
res = call(wkey, "generate_image_ads", dict(img, confirm_total_credits=q["total_credits"] - 1))
with db.connect() as conn:
    n_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
check(res["isError"] and "total changed" in text(res) and n_jobs == 0, "a different total is refused (409), nothing made")
res = call(wkey, "generate_image_ads", dict(img, confirm_total_credits=q["total_credits"]))
bid = res["structuredContent"]["batch_id"] if not res["isError"] else None
with db.connect() as conn:
    bal = db.balance(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws["id"],)).fetchone())
check(bid and bal == start_balance - q["total_credits"], "the quoted total makes the batch and takes those credits")
b = call(rkey, "get_batch", {"batch_id": bid})["structuredContent"]
check(b["total"] == 1 and b["assets"][0]["status"] == "pending", "get_batch")
res = call(wkey, "generate_video_ads", dict(img, confirm_total_credits=1))
check(res["isError"] and "ad_type" in text(res), "generate_video_ads won't make image ads")

print("== videos: confirmed email and credits, same as the app")
ugc = {"ad_type": "ugc", "product_id": pid, "lines": ["I take this mug on every hike."], "presenters": ["maya"],
       "duration": 8, "aspects": ["9:16"]}
q = call(rkey, "quote_ad_batch", ugc)["structuredContent"]
check(q["videos"] and q["total_credits"] == catalog.price_credits("ugc_video", duration=8, resolution="720p"),
      "a presenter video is priced before anything is made")
res = call(wkey, "generate_video_ads", dict(ugc, confirm_total_credits=q["total_credits"]))
check(res["isError"] and "Confirm the email" in text(res), "an unconfirmed email can't make videos (the app's rule)")
with db.connect() as conn:
    conn.execute("UPDATE users SET email_verified_at=? WHERE email='owner@example.com'", (db.now(),))
    conn.commit()
res = call(wkey, "generate_video_ads", dict(ugc, confirm_total_credits=q["total_credits"]))
check(res["isError"] and "credits" in text(res), "not enough credits: refused")
with db.connect() as conn:
    db.adjust_credits(conn, ws["id"], 5000, "test top-up")
    conn.commit()
res = call(wkey, "generate_video_ads", dict(ugc, confirm_total_credits=q["total_credits"]))
check(not res["isError"] and res["structuredContent"]["queued"] == 1, "with the quoted total the video is queued")

print("== plan features and AI writing")
res = call(wkey, "generate_ad_copy", {"kind": "ad", "platform": "meta", "offer": "Trail Mug"})
check(res["isError"] and "Starter" in text(res), "ad copy needs Starter (plan check)")
res = call(wkey, "start_campaign_plan", {"offer": "Trail Mug", "confirm_max_credits": 1})
check(res["isError"] and "Starter" in text(res), "the planner needs Starter")
with db.connect() as conn:
    conn.execute("UPDATE workspaces SET plan_override='starter' WHERE id=?", (ws["id"],))
    conn.commit()
res = call(wkey, "start_campaign_plan", {"offer": "Trail Mug", "confirm_max_credits": 1})
check(res["isError"] and "up to" in text(res), "the planner wants its credit cap confirmed")
res = call(wkey, "generate_ad_copy", {"kind": "ad", "platform": "meta", "offer": "Trail Mug"})
check(res["isError"] and "switched on" in text(res), "AI writing off on this server: the app's message, nothing charged")
res = call(wkey, "start_competitor_snapshot", {"competitors": ["example.com"], "confirm_max_credits": 0})
check(res["isError"] and "up to" in text(res), "competitor snapshot wants its credit cap confirmed")
import competitors  # noqa: E402
need = competitors.quote(1)
res = call(wkey, "start_competitor_snapshot", {"competitors": ["example.com"], "confirm_max_credits": need})
with db.connect() as conn:
    n_rep = conn.execute("SELECT COUNT(*) FROM competitor_reports").fetchone()[0]
check(res["isError"] and "switched on" in text(res) and n_rep == 0,
      "with the cap confirmed, the app's own route runs (research off here: nothing started)")

print("== assets and download links")
with db.connect() as conn:
    jid = conn.execute("SELECT id FROM jobs WHERE batch_id=?", (bid,)).fetchone()["id"]
    f = os.path.join(TMP, "out.png")
    with open(f, "wb") as fh:
        fh.write(b"\x89PNG made")
    conn.execute("UPDATE jobs SET status='done' WHERE id=?", (jid,))
    oid = conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?,?,?)", (jid, "image", f)).lastrowid
    conn.commit()
a = call(rkey, "list_assets", {"status": "done"})["structuredContent"]
check(a["total"] == 1 and a["assets"][0]["outputs"][0]["output_id"] == oid, "list_assets")
link = call(rkey, "get_asset_download_url", {"output_id": oid})["structuredContent"]
r = anon.get(urlsplit(link["url"]).path)
check(r.status_code == 200 and r.data == b"\x89PNG made", "the signed link downloads with no login")
check(anon.get(urlsplit(link["url"]).path + "x").status_code == 404, "a tampered link: 404")
check(call(rkey, "get_asset_download_url", {"output_id": oid + 999})["isError"], "unknown output: refused")

print("== revoke")
with db.connect() as conn:
    kid = conn.execute("SELECT id FROM mcp_tokens WHERE prefix=?", (rkey[:12],)).fetchone()["id"]
other.post("/app/mcp", data={"csrf": csrf(other), "action": "revoke_key", "id": kid})
check(rpc("ping", key=rkey).status_code == 200, "another workspace can't revoke it")
owner.post("/app/mcp", data={"csrf": csrf(owner), "action": "revoke_key", "id": kid})
check(rpc("ping", key=rkey).status_code == 401 and rpc("ping", key=wkey).status_code == 200, "revoke ends that key only")

print("== OAuth 2.1")
reg = anon.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                         "client_name": "Claude", "token_endpoint_auth_method": "client_secret_post"}).get_json()
check(reg["client_id"] and reg["client_secret"], "confidential client registered")
verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
q = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
     "state": "abc", "scope": "read write", "code_challenge": challenge, "code_challenge_method": "S256"}
r = anon.get("/oauth/authorize", query_string=q)
check(r.status_code == 302 and "/login" in r.headers["Location"], "logged out: sent to log in first")
r = owner.get("/oauth/authorize", query_string=q)
check(r.status_code == 200 and "Connect Claude" in r.get_data(as_text=True), "consent page")
r = owner.post("/oauth/authorize", data=dict(q, csrf=csrf(owner), workspace=str(ws["id"]), allow_write="1",
                                             decision="allow"))
code = parse_qs(urlsplit(r.headers["Location"]).query)["code"][0]
t = anon.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                    "redirect_uri": q["redirect_uri"], "code_verifier": verifier})
check(t.status_code == 401, "a confidential client without its secret is refused")
r = owner.post("/oauth/authorize", data=dict(q, csrf=csrf(owner), workspace=str(ws["id"]), allow_write="1",
                                             decision="allow"))
code = parse_qs(urlsplit(r.headers["Location"]).query)["code"][0]
tok = anon.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                                      "client_secret": reg["client_secret"], "redirect_uri": q["redirect_uri"],
                                      "code_verifier": verifier}).get_json()
check(tok["access_token"].startswith("cfy_at_"), "code + verifier + secret: tokens")
check(call(tok["access_token"], "get_account_status")["structuredContent"]["workspace"]["id"] == ws["id"],
      "the OAuth token runs tools")
anon.post("/oauth/revoke", data={"token": tok["refresh_token"], "client_id": reg["client_id"],
                                 "client_secret": reg["client_secret"]})
check(rpc("ping", key=tok["access_token"]).status_code == 401, "revoking the refresh token ends the connection")

print(f"\n{passed} checks passed")
