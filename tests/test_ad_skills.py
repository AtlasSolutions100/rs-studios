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
             "no text, logos or graphics added"):
    check(must in gs, f"google-search covers: {must}")
check("Text limits: headline: up to 30 characters; description: up to 90 characters." in gs,
      "google-search: limits from adspec, without a primary-text line")
check(ad_skills.playbook("nope") is None and ad_skills.markdown("nope") is None, "unknown platform: None")

print("== Campaignify tool playbooks")
import tool_skills  # noqa: E402
check(ad_skills.ALL_KEYS == ad_skills.KEYS + tool_skills.TOOL_KEYS and len(tool_skills.TOOL_KEYS) == 7,
      "7 tool playbooks after the 9 platforms")
for k in tool_skills.TOOL_KEYS:
    p_ = ad_skills.playbook(k)
    md = p_["markdown"]
    check(p_["kind"] == "tool" and p_["name"] == f"campaignify-{k}" and p_["description"].startswith("Use when"),
          f"{k}: a tool skill")
    for part in ("## How to do it in Campaignify", "## Checklist", "## Avoid", "## Rules for everything"):
        check(part in md, f"{k}: {part[3:]}")
    check(md.count("- [ ] ") >= 3 and "http" not in md and "\u2014" not in md and "\u2013" not in md
          and "Rector" not in md, f"{k}: a checklist, no links, no long dashes")
lf = ad_skills.markdown("lead-follow-up") or tool_skills.markdown("lead-follow-up")
for must in ("SPF, DKIM and DMARC", "one-click", "within 2 days", "0.1%", "written consent", "STOP and HELP",
             "8 am and 9 pm", "Florida and Oklahoma", "10DLC", "not legal advice"):
    check(must in lf, f"lead follow-up covers: {must}")
ug = tool_skills.markdown("ugc-video-ads")
check("can't pose as customers" in ug and "AI-generated content toggle" in ug and "first 2 seconds" in ug,
      "UGC video playbook: no fake customers, AI labels, the hook")
seo = tool_skills.markdown("seo-aeo-geo")
check("OAI-SearchBot" in seo and "Googlebot" in seo and "doesn't use it" in seo, "SEO playbook: crawlers and llms.txt")
gs2 = ad_skills.markdown("google-search")
for must in ("60 days old", "at least 4 different images", "middle 80%", "AI label setting", "AI Max",
             "no longer has a manual"):
    check(must in gs2, f"google-search 2026 update: {must}")
check("Creative variety is the biggest lever" in ad_skills.markdown("meta") and "AI-generated content toggle" in
      ad_skills.markdown("tiktok"), "Meta creative variety and TikTok AI label")
check(all("Label AI creative" in ad_skills.markdown(k) for k in ad_skills.KEYS), "every platform: label AI creative")
check(ad_skills.playbook("nope") is None, "unknown key: None")

print("== skill files and zip")
for k in ad_skills.ALL_KEYS:
    s = ad_skills.skill_md(k)
    m = re.match(r"---\nname: ([a-z0-9-]+)\ndescription: (.+)\n---\n\n# ", s)
    check(m and len(m.group(1)) <= 64 and "claude" not in m.group(1) and "anthropic" not in m.group(1),
          f"{k}: skill name {m.group(1) if m else '?'}")
    check(m and len(m.group(2)) <= 1024 and "<" not in m.group(2) and m.group(2).startswith("Use when"),
          f"{k}: skill description says when to use it")
z = zipfile.ZipFile(io.BytesIO(ad_skills.zip_bytes()))
names = z.namelist()
check(sorted(n for n in names if n.endswith("/SKILL.md")) == sorted([f"campaignify-{k}-ads/SKILL.md" for k in ad_skills.KEYS]
                                                                     + [f"campaignify-{k}/SKILL.md" for k in tool_skills.TOOL_KEYS]
                                                                     + ["campaignify-mr-campaign/SKILL.md"])
      and "README.md" in names, "zip: one folder per platform and per tool, Mr. Campaign and a README")

print("== Mr. Campaign")
import consultant  # noqa: E402
sk = z.read("campaignify-mr-campaign/SKILL.md").decode()
check(re.match(r"---\nname: campaignify-mr-campaign\ndescription: Use when [^<\n]{20,1000}\n---\n\nYou are Mr. Campaign", sk),
      "Mr. Campaign skill file")
for f in consultant.FOCUS:
    t = consultant.opening(f, "")
    check(consultant.FOCUS[f] in t and "## Start" in t, f"focus {f}")
t = consultant.opening("offer", "How do I price my candles?")
check("## The member's question\nHow do I price my candles?" in t and "## Start" not in t, "the member's question goes in")
check(consultant.opening("nonsense") == consultant.opening("general"), "unknown focus: general")
low = consultant.BRIEF.lower()
for banned in ("hormozi", "$100m", "grand slam", "acquisition.com"):
    check(banned not in low, f"no real person's name or branded terms: {banned}")
for must in ("not a\nreal person", "Never invent results", "No guaranteed outcomes", "Next 3 moves", "get_product",
             "credits", "not a lawyer"):
    check(must in consultant.BRIEF, f"brief has: {must!r}")
check("\u2014" not in consultant.BRIEF and "\u2013" not in consultant.BRIEF, "no long dashes")

print("== web")
import app as appmod  # noqa: E402
c = appmod.app.test_client()
r = c.get("/claude-skills.zip")
check(r.status_code == 200 and r.mimetype == "application/zip" and "campaignify-ad-skills.zip" in
      r.headers.get("Content-Disposition", "") and zipfile.ZipFile(io.BytesIO(r.data)).testzip() is None,
      "/claude-skills.zip downloads")
d = c.get("/docs/mcp").get_data(as_text=True)
check("get_ad_playbook" in d and 'href="/claude-skills.zip"' in d, "the MCP docs page explains the playbooks")
check("<code>/consultant</code>" in d and "Mr. Campaign" in d, "the MCP docs page explains Mr. Campaign")
import mcp_tools  # noqa: E402
check("get_ad_playbook" in mcp_tools.CampaignifyAdapter.instructions, "the MCP instructions point Claude at the playbooks")
check("mr_campaign" in mcp_tools.CampaignifyAdapter.instructions, "the MCP instructions point Claude at Mr. Campaign")

print(f"\n{passed} checks passed")
