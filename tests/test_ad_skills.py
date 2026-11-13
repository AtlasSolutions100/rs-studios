"""Ad-platform playbooks for Claude (ad_skills.py): every platform renders a complete playbook, the links are
first-party https, the skill files are valid Claude skills, the zip has one folder per platform, and the public
download and docs page work. No network.

    python tests/test_ad_skills.py
"""
import io
import os
import re
import sys
import tempfile
import zipfile
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_skills_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0")

import ad_skills  # noqa: E402
import adhub  # noqa: E402
import platforms  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print(f"  ok  {msg}")


print("== playbooks")
check(ad_skills.KEYS[0] == "google-search" and set(ad_skills.KEYS) == set(ad_skills.SKILLS), "every playbook is listed")
check(set(adhub.ORDER) <= set(ad_skills.KEYS), "every platform in the setup hub has a playbook")
for k in ad_skills.KEYS:
    md = ad_skills.markdown(k)
    for part in ("## 1. Set up the account", "## 2. Make the ads with Campaignify", "## 3. Before you launch",
                 "## 4. After launch", "## What gets ads rejected here", "## Rules for every platform", "## Official links"):
        check(part in md, f"{k}: {part[3:] if part.startswith('## ') else part}")
    check(md.count("- [ ] ") >= 6, f"{k}: a launch checklist")
    check(f"platform `{ad_skills.COPY_PLATFORM[k]}`" in md and ad_skills.COPY_PLATFORM[k] in platforms.PLATFORMS,
          f"{k}: the copy tool gets a platform it takes")
    for tool in ("quote_ad_batch", "confirm_total_credits", "generate_ad_copy", "get_asset_download_url"):
        check(tool in md, f"{k}: names {tool}")
    for url in re.findall(r"\((https?://[^)\s]+)\)", md):
        u = urlsplit(url)
        check(u.scheme == "https" and u.hostname in adhub.OFFICIAL_HOSTS, f"{k}: first-party link {u.hostname}{u.path[:40]}")
    check("Rector" not in md and "—" not in md and "–" not in md, f"{k}: no owner name, no long dashes")
    check(not re.search(r"\b(guarantee[sd]?|best in class|#1)\b", md, re.I) or "guaranteed-outcome" in md,
          f"{k}: no promises")
gs = ad_skills.markdown("google-search")
for must in ("One website per ad group", "paused ones included", "1200 x 628", "1200 x 1200", "Business name",
             "Sitelinks: at least 4", "Callouts", "Structured snippet", "Appeal", "Search terms report",
             "No text overlays"):
    check(must in gs, f"google-search covers: {must}")
check("Text limits: headline: up to 30 characters; description: up to 90 characters." in gs,
      "google-search: limits from adspec, without a primary-text line")
check(ad_skills.playbook("nope") is None and ad_skills.markdown("nope") is None, "unknown platform: None")

print("== skill files and zip")
for k in ad_skills.KEYS:
    s = ad_skills.skill_md(k)
    m = re.match(r"---\nname: ([a-z0-9-]+)\ndescription: (.+)\n---\n\n# ", s)
    check(m and len(m.group(1)) <= 64 and "claude" not in m.group(1) and "anthropic" not in m.group(1),
          f"{k}: skill name {m.group(1) if m else '?'}")
    check(m and len(m.group(2)) <= 1024 and "<" not in m.group(2) and m.group(2).startswith("Use when"),
          f"{k}: skill description says when to use it")
z = zipfile.ZipFile(io.BytesIO(ad_skills.zip_bytes()))
names = z.namelist()
check(sorted(n for n in names if n.endswith("/SKILL.md")) == sorted(f"campaignify-{k}-ads/SKILL.md" for k in ad_skills.KEYS)
      and "README.md" in names, "zip: one folder per platform plus a README")

print("== web")
import app as appmod  # noqa: E402
c = appmod.app.test_client()
r = c.get("/claude-skills.zip")
check(r.status_code == 200 and r.mimetype == "application/zip" and "campaignify-ad-skills.zip" in
      r.headers.get("Content-Disposition", "") and zipfile.ZipFile(io.BytesIO(r.data)).testzip() is None,
      "/claude-skills.zip downloads")
d = c.get("/docs/mcp").get_data(as_text=True)
check("get_ad_playbook" in d and 'href="/claude-skills.zip"' in d, "the MCP docs page explains the playbooks")
import mcp_tools  # noqa: E402
check("get_ad_playbook" in mcp_tools.CampaignifyAdapter.instructions, "the MCP instructions point Claude at the playbooks")

print(f"\n{passed} checks passed")
