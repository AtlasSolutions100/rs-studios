"""The MCP's launch tools (mcp_tools.py: list_ad_connections, prepare_launch, launch_campaign, get_launch_status)
and the MCP page's "can it run my ads" copy, on a throwaway database.

    python tests/test_mcp_launch.py

Covers: with AD_LAUNCH off the launch tools are hidden from tools/list and refused when called, and the /mcp page
and docs say launching is coming soon; with AD_LAUNCH=beta and the workspace allowlisted they are listed,
list_ad_connections answers, prepare_launch refuses a plan that isn't there, launch_campaign needs a write key
and creates nothing without a real card; with AD_LAUNCH=on and Meta configured the page says it launches, paused.
No network: nothing here reaches an ad platform.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_mcp_launch_")
os.environ.setdefault("RS_SIGNUP_CONFIRM", "0")
for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MCP_ALLOWED_ORIGINS", "AD_LAUNCH", "AD_LAUNCH_WORKSPACES",
          "META_APP_ID", "META_APP_SECRET", "META_CONFIG_ID"):
    os.environ.pop(k, None)
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", MCP_SERVER="on")

import app as appmod  # noqa: E402
import db  # noqa: E402

app = appmod.app
app.testing = True
passed = 0
LAUNCH_TOOLS = {"list_ad_connections", "prepare_launch", "launch_campaign", "get_launch_status"}


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
    c.environ_base["REMOTE_ADDR"] = f"10.8.0.{_ip[0]}"
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


def rpc(method, params=None, key=None):
    N[0] += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": N[0]}
    if params is not None:
        msg["params"] = params
    h = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {key}"}
    return anon.post("/mcp", json=msg, headers=h)


def listed(key):
    return {t["name"] for t in rpc("tools/list", {}, key=key).get_json()["result"]["tools"]}


def call(key, name, args=None):
    r = rpc("tools/call", {"name": name, "arguments": args or {}}, key=key)
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    j = r.get_json()
    return j.get("result") or {"isError": True, "content": [{"text": str(j.get("error"))}]}


def text(res):
    return res["content"][0]["text"]


def page(path):
    return anon.get(path, headers={"Accept": "text/html,application/xhtml+xml"}).get_data(as_text=True)


owner = signup("owner@example.com")
rkey, wkey = make_key(owner, "read"), make_key(owner, "write")
check(bool(rkey) and bool(wkey), "a read key and a read-write key")
with db.connect() as conn:
    ws_id = conn.execute("SELECT workspace_id FROM users WHERE email=?", ("owner@example.com",)).fetchone()[0]

print("== AD_LAUNCH off (default)")
check(not (LAUNCH_TOOLS & listed(rkey)), "launch tools are hidden from tools/list")
res = call(wkey, "prepare_launch", {"campaign_id": 1, "platform": "meta"})
check(res.get("isError"), "calling a hidden launch tool is refused")
body = page("/mcp")
check("coming soon, starting with Meta" in body and "Launch it, paused" not in body,
      "/mcp says launching from Claude is coming soon")
check("coming soon, starting with Meta" in page("/docs/mcp"), "the docs say the same")

print("== AD_LAUNCH=beta, this workspace allowlisted")
os.environ.update(AD_LAUNCH="beta", AD_LAUNCH_WORKSPACES=str(ws_id))
check(LAUNCH_TOOLS <= listed(rkey), "launch tools are listed")
res = call(rkey, "list_ad_connections")
check(not res.get("isError") and '"launching": true' in text(res) and '"connected": false' in text(res),
      "list_ad_connections: launching on, Meta not connected yet")
res = call(rkey, "prepare_launch", {"campaign_id": 999, "platform": "meta"})
print("    prepare_launch said:", text(res)); check(res.get("isError"), "prepare_launch refuses a plan that isn't here")
res = call(rkey, "launch_campaign", {"campaign_id": 999, "platform": "meta", "card_id": 1, "confirm_spec_hash": "x"})
check(res.get("isError") and "read-only" in text(res).lower(), "launch_campaign needs a write key")
res = call(wkey, "launch_campaign", {"campaign_id": 999, "platform": "meta", "card_id": 1, "confirm_spec_hash": "x"})
check(res.get("isError"), "launch_campaign without a real card is refused")
with db.connect() as conn:
    check(conn.execute("SELECT COUNT(*) FROM launches WHERE workspace_id=?", (ws_id,)).fetchone()[0] == 0,
          "and nothing was launched")
res = call(rkey, "get_launch_status", {"launch_id": 999})
check(res.get("isError"), "get_launch_status for a launch that isn't here is refused")

print("== another workspace while beta: still hidden")
other = signup("other@example.com")
okey = make_key(other, "read")
check(not (LAUNCH_TOOLS & listed(okey)), "a workspace off the allowlist doesn't see the launch tools")

print("== AD_LAUNCH=on with Meta configured: the page says it launches, paused")
import public_site  # noqa: E402
public_site.launch_live = lambda: True
body = page("/mcp")
check("Launch it, paused" in body and "Nothing spends until you turn it on" in body
      and "coming soon, starting with Meta" not in body, "/mcp describes launching, paused, owner approval")
check("created PAUSED" in page("/docs/mcp"), "the docs list the launch rules")

print(f"\n{passed} checks passed")
