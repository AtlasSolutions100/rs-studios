"""MCP preset gallery: data, prompts/list exposure, and page render."""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RS_DATA", "/tmp/test_mcp_presets_data")
os.environ.setdefault("MCP_SERVER", "on")
import mcp_presets, mcp_tools
ok = fail = 0
def check(c, m):
    global ok, fail
    if c: ok += 1
    else: fail += 1; print("FAIL", m)
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ids = [p.id for p in mcp_presets.PRESETS]
check(len(ids) == len(set(ids)), "unique ids")
check(len(ids) >= 20, "at least 20 presets")
tool_names = {t.name for t in mcp_tools.TOOLS} if hasattr(mcp_tools, "TOOLS") else None
tabs = {t[0] if isinstance(t, (tuple, list)) else t for t in mcp_presets.TABS}
for p in mcp_presets.PRESETS:
    check(p.tab in tabs, f"{p.id} tab valid")
    check(p.prompt.strip() and p.title and p.line, f"{p.id} has text")
    for media in (p.example, p.video):
        if media:
            check(os.path.exists(os.path.join(root, media.lstrip("/"))), f"{p.id} media {media}")
    if tool_names:
        for t in p.tools:
            check(t in tool_names, f"{p.id} tool {t}")
check(any(p.popular for p in mcp_presets.PRESETS), "has popular")
txt = mcp_presets.prompt_text(mcp_presets.BY_ID["talking-ugc-review"] if hasattr(mcp_presets,"BY_ID") else [p for p in mcp_presets.PRESETS if p.id=="talking-ugc-review"][0], {"product": "Trail Mug"})
check("Trail Mug" in txt and "<my product>" not in txt, "product substituted")
# launch preset hidden when launching is off
off = [p["id"] for p in mcp_tools.presets_for(False)[0]]
on = [p["id"] for p in mcp_tools.presets_for(True)[0]]
check("launch-on-meta" not in off and "launch-on-meta" in on, "launch preset gating")
# page render
import app as appmod
c = appmod.app.test_client()
r = c.get("/mcp", headers={"Accept": "text/html"})
body = r.get_data(as_text=True)
check(r.status_code == 200, "/mcp 200")
check("Pick one, paste it, done." in body, "gallery heading")
check("/talking-ugc-review" in body, "preset tag rendered")
check("launch-on-meta" not in body, "launch preset hidden while off")
print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
