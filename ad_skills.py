"""Ad-platform playbooks for Claude: one per ad platform, so a member using Campaignify through Claude (the MCP
server, mcp_tools.get_ad_playbook) or a Claude skill pack (/claude-skills.zip) gets the same complete,
nothing-missed procedure: set up the account, make the creative with Campaignify, check every launch item, then
tune it after launch.

Account-setup steps, specs, tag and policy links come from adhub.PLATFORMS (first-party links, checked there and
by its tests); text limits from adspec.TEXT_LIMITS. What this file adds is the launch checklist and the
after-launch routine per platform. Google Search gets its own playbook (adhub's "youtube" entry is Google's
video side), written from running Campaignify's own search campaigns (Nov 2026).

    ad_skills.KEYS                    platform playbook keys, in order (ALL_KEYS adds tool_skills.TOOL_KEYS)
    ad_skills.playbook(key)           {"key", "name", "title", "description", "markdown"} or None
    ad_skills.skill_md(key)           the SKILL.md text (front matter + markdown) for a skill folder
    ad_skills.zip_bytes()             every playbook as a skill folder in one zip

Nothing here talks to a platform or spends credits. Platform screens change: every playbook tells Claude to follow
the platform's current screens and official help where they differ.
"""
import io
import zipfile

import adhub
import adspec
import tool_skills

# The Campaignify part of every playbook: making the creative through the MCP tools (mcp_tools.TOOLS).
MAKE_WITH_CAMPAIGNIFY = [
    "Find the product: `list_brands_products`, then `get_product` for its approved claims, audience and photos. "
    "Only the claims saved on the product go into ads.",
    "Write the words: `generate_ad_copy` with kind `ad` (or `headlines`, `hooks`) and platform `{copy}`. It "
    "sizes copy to the platform's limits. Spends a few credits, so say so first.",
    "Make the visuals: `list_ad_options` for presets, presenters and shapes; `quote_ad_batch` to price the batch; "
    "show the user the total; then `generate_image_ads` or `generate_video_ads` with `confirm_total_credits`.",
    "Collect the files: `get_batch` until ready, then `get_asset_download_url` for each file. Every file name says "
    "its shape.",
    "Need a plan first? `start_campaign_plan` writes angles, hooks, a budget split and a launch checklist.",
]

UNIVERSAL_RULES = [
    "Never invent results, reviews, testimonials, prices, discounts, deadlines or popularity. Discounts and offers "
    "go in only when the user says so, and come out the day they end.",
    "AI presenters are generated people. They never speak as a real customer or claim to have used the product "
    "(the FTC treats AI posing as a real customer or reviewer as deceptive).",
    "Label AI creative where the platform asks: Campaignify's presenter videos and generated scenes show realistic "
    "AI people, voices and settings. Turn on the platform's AI disclosure (Google's AI label setting, Meta's AI "
    "disclosure, TikTok's AI-generated content toggle). TikTok can remove or limit unlabeled realistic AI ads.",
    "No health, income, before-and-after or guaranteed-outcome claims unless the user's product page proves them "
    "and the platform allows them.",
    "Don't put another company's logo, a celebrity or someone else's footage in an ad.",
    "Every link gets UTM tags (utm_source, utm_medium=paid, utm_campaign) so results show up in the user's own "
    "analytics.",
    "The user presses the final Publish / Launch unless they told you to. Paying, adding cards and accepting terms "
    "are theirs to do.",
]

GOOGLE_SEARCH = {
    "name": "Google Search ads",
    "manager": ("Google Ads", "https://ads.google.com/"),
    "steps": [
        ("Create the Google Ads account", "Sign in with the business's Google account, add billing and pick the "
         "time zone and currency (they can't be changed later). Skip the 'Smart' setup: switch to Expert mode.",
         "https://support.google.com/google-ads/answer/6366720"),
        ("Verify the advertiser", "Google asks most advertisers to verify their identity. Start it early: ads can "
         "pause until it's done. Google Ads shows the steps on its own screen when it asks.", None),
        ("Set up conversion measurement", "Add the Google tag to every page and create a conversion action for the "
         "goal (purchase, sign-up or lead). Turn on enhanced conversions if the site collects emails.",
         "https://support.google.com/google-ads/answer/1722022"),
        ("Create the Search campaign", "Goal: the conversion action. Networks: Search only (turn off the Display "
         "network for a first campaign). Locations where the customers are. Google Search no longer has a manual "
         "language setting: it reads the language of the ads and landing pages, so write each ad group in its "
         "customers' language.", None),
        ("Decide on AI Max", "Google is moving Search campaigns to AI Max (broader query matching, AI-written "
         "headlines and descriptions, and sending clicks to other pages of the site). If it's on, set brand and "
         "location controls and text guidelines, preview the AI-written text, and turn off final URL expansion if "
         "only one page should get the clicks.", None),
        ("Build tight ad groups", "One theme per ad group (for example 'AI UGC ads' and 'Shopify product ads' are "
         "two ad groups), 5 to 20 keywords each in phrase or exact match, and an ad whose landing page is about "
         "exactly that theme.", "https://support.google.com/google-ads/answer/7684791"),
    ],
    "pixel": ("Google tag and conversion actions",
              [("About conversion measurement", "https://support.google.com/google-ads/answer/1722022")]),
    "specs": ("Responsive search ads", "https://support.google.com/google-ads/answer/7684791"),
    "policy": ("Google Ads policies", "https://support.google.com/adspolicy/answer/6008942"),
    "exports": [("square", "Square 1:1 image asset", 1200, 1200),
                ("landscape", "Landscape 1.91:1 image asset", 1200, 628)],
}

# Platform playbooks: (title, description for the skill, adhub key or the dict above, limits key, checklist,
# after-launch routine, traps).
SKILLS = {
    "google-search": {
        "title": "Set up, launch and optimize Google Search ads",
        "description": "Use when setting up a Google Ads account, building or launching a Google Search campaign, "
                       "writing responsive search ads, adding images, sitelinks or other assets, fixing a "
                       "disapproval, or improving ad strength.",
        "hub": GOOGLE_SEARCH,
        "limits": "google",
        "checklist": [
            "Responsive search ad: 8 to 15 headlines (30 characters each) and 4 descriptions (90 each). Put the main "
            "keyword in at least 2 headlines, plus the brand name, the offer, a price or proof point the user "
            "confirmed, and a call to action. Fill both display paths (15 characters each). Don't pin unless a line "
            "must legally show.",
            "Ad strength 'Good' or 'Excellent' before launch. Hover the rating: Google lists what's missing (more "
            "keywords in headlines, more unique headlines, more sitelinks).",
            "One website per ad group: every ad in an ad group, paused ones included, must use the same domain. "
            "After a domain move, remove (not just pause) the old-domain ads or the new ones get disapproved.",
            "Sitelinks: at least 4, ideally 6 or more, each with text (25 characters), two description lines "
            "(35 each) and a live page on the same domain. Check every final URL loads.",
            "Callouts: at least 4 short facts (25 characters each), like the trial, cancel anytime, what's "
            "included. Nothing that has ended or isn't on the site today.",
            "Structured snippet: a header (Types, Services, Styles...) with 3 or more values.",
            "Images: a square 1:1 is required (1200 x 1200 recommended) and a landscape 1.91:1 (1200 x 628) is "
            "recommended; Google suggests at least 4 different images, up to 20 per campaign, PNG or JPG up to "
            "5 MB, the subject in the middle 80%. Use photo-style images with no text, logos or graphics added: "
            "added text, collages, blur, odd crops and lots of blank space get images disapproved. Make 1:1 and "
            "16:9 visuals in Campaignify (no headline text on them) and crop the 16:9 to 1.91:1 in Google's picker. "
            "Image assets only open to accounts at least 60 days old with recent Search spend and a clean policy "
            "history; a newer account skips this item.",
            "AI label: images or videos made or edited with AI get Google's AI label setting turned on (a label added "
            "for this isn't treated as a text overlay).",
            "Business name (25 characters; must match the domain or the verified advertiser name) and a square "
            "business logo (1200 x 1200, the brand mark on a solid background).",
            "Price or promotion assets only for prices and offers on the site today.",
            "Negative keywords for searches that never buy (free, jobs, login, competitor support pages...).",
            "Conversion action firing (Tag Assistant or the Goals page shows recent conversions) before choosing "
            "Maximize conversions. No conversion data yet: start with Maximize clicks and a max CPC, switch once "
            "conversions come in.",
            "Daily budget that pays for at least 10 clicks a day at the keyword's expected CPC, or the campaign "
            "learns too slowly.",
        ],
        "after": [
            "Days 1 to 3: check every ad and asset says Eligible or Approved. Fix disapprovals, then appeal from the "
            "status (hover > Appeal > 'Made changes to comply').",
            "Weekly: read the Search terms report. Add good terms as keywords, bad ones as negatives.",
            "Weekly: Assets > Performance. Replace assets rated 'Low' after two weeks; keep the 'Best' ones.",
            "Smart Bidding's learning status lasts up to about a week; budget and target edits on an established "
            "campaign don't restart it. Still judge changes over 2 or more weeks, not days.",
            "With AI Max on: check the search terms and ad combinations report for queries and AI-written lines "
            "that don't fit, and add negatives or text guidelines.",
            "Monthly: pause keywords with spend and no conversions over 2 to 3 times the target cost per conversion.",
            "Keep the ad copy, callouts and sitelinks in step with the site: when an offer ends, remove it everywhere.",
        ],
        "traps": [
            "'One website per ad group' (paused ads on an old domain count).",
            "Destination not working or mismatched: every final URL must load, on the ad's domain.",
            "Unverified claims: 'best', '#1' or numbers the site doesn't back up.",
            "Misleading offers: a free trial that needs a card must say the card is needed somewhere in the ad or page.",
        ],
    },
    "meta": {
        "title": "Set up, launch and optimize Facebook and Instagram ads",
        "description": "Use when setting up Meta Business Suite or Ads Manager, launching a Facebook or Instagram "
                       "campaign, preparing ads for feed, Stories or Reels, or improving Meta ad results.",
        "hub": "meta", "limits": "meta",
        "checklist": [
            "Pixel and Conversions API sending the goal event (Purchase, Lead, CompleteRegistration). Check it in "
            "Events Manager before launch; verify the domain in Business settings.",
            "Campaign objective matches the goal (Sales, Leads, Traffic). Optimize the ad set for the conversion "
            "event, not link clicks, once the pixel has events.",
            "Special ad category set if the ads are about housing, credit, employment or social issues.",
            "Each ad in 4:5 for feeds and 9:16 for Stories and Reels (1080 x 1350 and 1080 x 1920). Keep text, faces "
            "and logos out of the top and bottom of the 9:16 frame, where Meta's buttons and captions sit.",
            "Primary text: the hook in the first 125 characters. Headline under 40 characters. A clear call-to-action "
            "button that matches the page.",
            "Creative variety is the biggest lever on Meta now: 6 to 10 or more genuinely different ads per ad set "
            "(different hooks, formats, people, problems and angles: presenter video, product video, static image, "
            "try-on), not one ad with a changed headline. Meta groups near-identical ads and they compete with "
            "each other.",
            "Few ad sets: a broad prospecting ad set (plus retargeting if there's traffic) beats many small ones. "
            "Keep one primary conversion event and no duplicate events between the pixel and Conversions API.",
            "AI disclosure on for presenter videos and AI-generated scenes.",
            "Advantage+ placements on unless there's a reason not to; don't narrow the audience too far for a new "
            "pixel.",
            "Landing page loads fast on a phone and says the same offer as the ad.",
            "Payment method on the ad account and the Facebook Page (and Instagram account) connected.",
        ],
        "after": [
            "Let each ad set leave the learning phase (about 50 optimization events in a week) before judging it; "
            "avoid big edits while it learns.",
            "After 3 to 7 days, turn off ads with a high cost per result and low click-through; make new variants of "
            "the winner's hook.",
            "Refresh creative every 2 to 3 weeks, or sooner when click-through falls week over week, cost per result "
            "rises, or frequency passes about 3. Add new concepts, not only new versions of old ones.",
            "Scale budgets by about 20% at a time on ad sets that hit the target cost.",
        ],
        "traps": [
            "Personal attributes: never 'Are you overweight?' or anything that implies you know who the viewer is.",
            "Before-and-after and health or income claims.",
            "Ads that look like a different site or a fake button.",
        ],
    },
    "tiktok": {
        "title": "Set up, launch and optimize TikTok ads",
        "description": "Use when creating a TikTok Ads Manager account, installing the TikTok Pixel, launching "
                       "TikTok in-feed or Spark ads, or improving TikTok ad results.",
        "hub": "tiktok", "limits": "tiktok",
        "checklist": [
            "TikTok Pixel (and the Events API if possible) firing the goal event; check it in Events Manager.",
            "Videos are 9:16, 1080 x 1920, with sound, up to 60 seconds; 15 to 30 seconds usually works best. The "
            "hook lands in the first 2 seconds; captions are on.",
            "Keep text and faces away from the bottom and right edge, where the caption, profile and buttons sit.",
            "Ad text 12 to 100 characters; brand name 2 to 20 characters; profile picture set to the brand.",
            "Turn on the AI-generated content toggle for presenter videos and other realistic AI scenes or voices.",
            "Smart+ (TikTok's automated targeting, budget and placements) can be switched on module by module; feed "
            "it many distinct videos (an asset group takes up to 50).",
            "3 to 5 videos per ad group with different hooks. AI presenter videos work well here: say on screen or in "
            "the caption what the product is, not that a customer is speaking.",
            "Optimize for the conversion event once the pixel has events; otherwise for clicks to start.",
            "Comments: decide who answers them; they show on in-feed ads.",
        ],
        "after": [
            "Give each ad group a few days and enough conversions to leave learning before big changes.",
            "TikTok creative tires fast: plan new hooks every 1 to 2 weeks.",
            "Turn off videos with a weak 2-second view rate; make new versions of the best hook.",
        ],
        "traps": [
            "Claims about health, weight or income.",
            "Music the brand doesn't have rights to: use TikTok's commercial sounds or the video's own audio.",
        ],
    },
    "youtube": {
        "title": "Set up, launch and optimize YouTube video ads",
        "description": "Use when running YouTube or Shorts ads through Google Ads (video or Demand Gen campaigns), "
                       "linking a YouTube channel, or improving YouTube ad results.",
        "hub": "youtube", "limits": None,
        "checklist": [
            "YouTube channel linked to Google Ads; videos uploaded to it (unlisted is fine).",
            "Google tag and a conversion action firing, the same as for Search.",
            "A 9:16 version for Shorts and a 16:9 version for in-stream. Brand and offer in the first 5 seconds, "
            "before the Skip button.",
            "A headline, description and call to action on the ad; companion images in 1:1 and 1.91:1 for Demand Gen.",
            "Audience: start from the customer list, website visitors or custom segments built from search terms.",
            "Demand Gen adds AI text overlays to videos by default: check Asset optimization and turn it off if the "
            "brand needs full control of what's on screen.",
            "Turn on the AI label setting for presenter videos and AI-generated scenes.",
        ],
        "after": [
            "Judge by conversions and cost per conversion, not views. Engagement now counts from 10 seconds watched "
            "(it was 5), so engagement numbers look lower than older reports.",
            "Replace videos with a low view rate after a week; keep the hook that holds attention.",
        ],
        "traps": ["Copyrighted music or footage in the video.", "Claims the landing page doesn't back up."],
    },
    "linkedin": {
        "title": "Set up, launch and optimize LinkedIn ads",
        "description": "Use when creating a LinkedIn Campaign Manager account, adding the Insight Tag, launching "
                       "LinkedIn sponsored content or lead gen form ads, or improving LinkedIn results.",
        "hub": "linkedin", "limits": None,
        "checklist": [
            "Insight Tag on every page and a conversion set up in Campaign Manager.",
            "Ad account tied to the company's LinkedIn Page.",
            "Audience by job function, seniority, industry or company list. Keep it above the minimum size LinkedIn "
            "shows; don't stack too many filters.",
            "Single image 1200 x 627 or square 1080 x 1080. Intro text: the point in the first 150 characters.",
            "Lead gen form ads if the goal is leads: short form, prefilled fields, a clear thank-you step.",
        ],
        "after": [
            "LinkedIn clicks cost more: judge on cost per lead or demo, not clicks.",
            "Refresh creative every few weeks; small audiences see the same ad often.",
        ],
        "traps": ["Personal-attribute targeting language in the ad text.", "Lead forms that ask for more than needed."],
    },
    "pinterest": {
        "title": "Set up, launch and optimize Pinterest ads",
        "description": "Use when creating a Pinterest business account, installing the Pinterest tag, launching "
                       "promoted Pins, or improving Pinterest ad results.",
        "hub": "pinterest", "limits": None,
        "checklist": [
            "Pinterest tag on the site sending checkout, sign-up or lead events.",
            "Standard Pins at 2:3 (1000 x 1500). Title: the key words in the first 40 characters.",
            "Keywords and interests that match what people search on Pinterest (ideas, gifts, how-to).",
            "The Pin links to the exact product or page shown.",
        ],
        "after": [
            "Pinterest is slower to build: judge over 2 to 4 weeks.",
            "Add new Pins regularly; seasonal ideas run months ahead on Pinterest.",
        ],
        "traps": ["Images with lots of text or a misleading price.", "Links to a page that doesn't show the Pin's product."],
    },
    "snapchat": {
        "title": "Set up, launch and optimize Snapchat ads",
        "description": "Use when creating a Snapchat Ads Manager account, installing the Snap Pixel, launching "
                       "Snapchat single image or video ads, or improving Snapchat results.",
        "hub": "snapchat", "limits": "snapchat",
        "checklist": [
            "Snap Pixel on the site sending the goal event.",
            "Full-screen 9:16 (1080 x 1920) image or video; logos and text away from the top and bottom edges.",
            "Brand name and a headline of 34 characters or fewer.",
            "Sound on for video; the point made in the first 2 seconds.",
        ],
        "after": ["Refresh creative often; judge on cost per conversion once the pixel has data."],
        "traps": ["Claims the landing page doesn't support.", "Content that isn't suitable for younger audiences."],
    },
    "reddit": {
        "title": "Set up, launch and optimize Reddit ads",
        "description": "Use when creating a Reddit Ads account, installing the Reddit Pixel, choosing communities, "
                       "launching Reddit promoted posts, or improving Reddit ad results.",
        "hub": "reddit", "limits": None,
        "checklist": [
            "Reddit Pixel (and Conversions API if possible) sending the goal event.",
            "Community and interest targeting that matches the product; write the headline like a member of that "
            "community would.",
            "Image in 1:1, 4:5 or 16:9; no hype.",
            "Decide whether comments are on, and who answers them.",
        ],
        "after": ["Read the comments: they tell you what the community thinks. Answer questions plainly.",
                  "Drop communities with clicks and no conversions after a week or two."],
        "traps": ["Sales-speak that the community calls out.", "Claims without proof."],
    },
    "x": {
        "title": "Set up, launch and optimize X (Twitter) ads",
        "description": "Use when creating an X Ads account, installing the X Pixel, launching promoted posts on X, "
                       "or improving X ad results.",
        "hub": "x", "limits": None,
        "checklist": [
            "X Pixel on the site and the conversion events defined.",
            "Post text up to 280 characters with the point up front; image 1:1 or 16:9.",
            "Keywords, follower look-alikes or interests that match the customer.",
        ],
        "after": ["Rotate creative weekly; judge on cost per conversion."],
        "traps": ["Claims the landing page doesn't support."],
    },
}
# platforms.PLATFORMS key that generate_ad_copy takes for each playbook.
COPY_PLATFORM = {"google-search": "google", "meta": "meta", "tiktok": "tiktok", "youtube": "youtube",
                 "linkedin": "linkedin", "pinterest": "pinterest", "snapchat": "snapchat", "reddit": "reddit", "x": "x"}
KEYS = ["google-search", "meta", "tiktok", "youtube", "linkedin", "pinterest", "snapchat", "reddit", "x"]
ALL_KEYS = KEYS + tool_skills.TOOL_KEYS          # platforms, then Campaignify's own tools


def _hub(key):
    h = SKILLS[key]["hub"]
    return adhub.PLATFORMS[h] if isinstance(h, str) else h


def _limits(key) -> list:
    lk = SKILLS[key]["limits"]
    out = []
    for field, (soft, hard) in (adspec.TEXT_LIMITS.get(lk) or {}).items():
        if (not soft and not hard) or (lk == "google" and field == "primary_text"):
            continue
        name = field.replace("_", " ")
        if soft and hard and soft != hard:
            out.append(f"{name}: shows about {soft} characters, takes up to {hard}")
        else:
            out.append(f"{name}: up to {hard or soft} characters")
    return out


def markdown(key: str) -> str | None:
    s = SKILLS.get(key)
    if not s:
        return None
    h = _hub(key)
    L = [f"# {s['title']}", "",
         f"A Campaignify playbook for {h['name']}. Work through it in order and tick every item: it's built so "
         f"nothing gets missed. Platform screens change; where {h['manager'][0]} looks different, follow its "
         "current screens and its own help pages.", "",
         "## 1. Set up the account", ""]
    for i, (title, text, url) in enumerate(h["steps"], 1):
        line = f"{i}. **{title}.** {text}".rstrip()
        L.append(line + (f" ([official guide]({url}))" if url else ""))
    L += ["", f"Ads manager: [{h['manager'][0]}]({h['manager'][1]}). Tracking: "
          + ", ".join(f"[{t}]({u})" for t, u in h["pixel"][1]) + ".", "",
          "## 2. Make the ads with Campaignify", ""]
    L += [f"- {x}".replace("{copy}", COPY_PLATFORM[key]) for x in MAKE_WITH_CAMPAIGNIFY]
    sizes = ", ".join(f"{label} ({w} x {hgt})" for _, label, w, hgt in h["exports"])
    L += [f"- Shapes for {h['name']}: {sizes}."]
    lim = _limits(key)
    if lim:
        L += ["- Text limits: " + "; ".join(lim) + "."]
    L += ["", "## 3. Before you launch (check every item)", ""]
    L += [f"- [ ] {x}" for x in s["checklist"]]
    L += [f"- [ ] {x}" for x in (
        "Every link has UTM tags and loads on a phone.",
        "Every claim, price and offer in the ad is on the landing page today.",
        "The user has reviewed the ads, the budget and the audience, and pressed launch (or told you to).")]
    L += ["", "## 4. After launch", ""]
    L += [f"- {x}" for x in s["after"]]
    L += ["", "## What gets ads rejected here", ""]
    L += [f"- {x}" for x in s["traps"]]
    if h.get("policy"):
        L += [f"- Read the [{h['policy'][0]}]({h['policy'][1]}) before the first launch."]
    L += ["", "## Rules for every platform", ""]
    L += [f"- {x}" for x in UNIVERSAL_RULES]
    links = [h["manager"]] + list(h["pixel"][1]) + [x for x in (h.get("specs"), h.get("policy")) if x]
    L += ["", "## Official links", ""] + [f"- [{t}]({u})" for t, u in links]
    return "\n".join(L) + "\n"


def playbook(key: str) -> dict | None:
    if key in tool_skills.TOOL_SKILLS:
        t = tool_skills.TOOL_SKILLS[key]
        return {"key": key, "kind": "tool", "name": f"campaignify-{key}", "title": t["title"], "platform": None,
                "description": t["description"], "markdown": tool_skills.markdown(key)}
    md = markdown(key)
    if md is None:
        return None
    s = SKILLS[key]
    return {"key": key, "kind": "platform", "name": f"campaignify-{key}-ads", "title": s["title"],
            "platform": _hub(key)["name"],
            "description": s["description"], "markdown": md}


def skill_md(key: str) -> str | None:
    p = playbook(key)
    if not p:
        return None
    return f"---\nname: {p['name']}\ndescription: {p['description']}\n---\n\n{p['markdown']}"


def zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in ALL_KEYS:
            p = playbook(k)
            z.writestr(f"{p['name']}/SKILL.md", skill_md(k))
        import consultant
        z.writestr("campaignify-mr-campaign/SKILL.md", consultant.skill_md())
        z.writestr("README.md", "# Campaignify ad skills for Claude\n\n"
                   "One skill per ad platform: account setup, making the ads with Campaignify, a launch checklist so "
                   "nothing is missed, and what to do after launch. One skill per Campaignify tool (video ads, "
                   "image ads, copy, campaign plans, landing pages, lead follow-up, SEO/AEO/GEO). Plus Mr. Campaign, a marketing consultant: ask "
                   "for him (or type /consultant) for advice on offers, leads, ads and pricing.\n\n"
                   "Claude.ai: zip a folder and upload it in Claude's skills settings, one skill per folder.\n"
                   "Claude Code: copy the folders into ~/.claude/skills/.\n\n"
                   "With the Campaignify connector (https://campaignify.ai/mcp) Claude also gets these playbooks "
                   "through the get_ad_playbook tool, no upload needed.\n")
    return buf.getvalue()
