"""Playbooks for each Campaignify tool, the companion to ad_skills.py (which covers the ad platforms). Same use:
Claude loads one through the MCP tool get_ad_playbook (key = the tool) or from the skills zip, and works through it
with the member so nothing is missed.

Each playbook says how to do the job in Campaignify (MCP tools where they exist, app sections otherwise), the
checklist of what good looks like, and what to avoid. The rules come from the platforms' and regulators' own
guidance as of November 2026 (AI disclosure, email sender requirements, SMS consent) and from what Campaignify
itself enforces. Not legal advice: the email and SMS playbook says so and tells the member to check their own
situation.
"""

TOOL_SKILLS = {
    "ugc-video-ads": {
        "title": "Make AI presenter (UGC-style) video ads that convert",
        "description": "Use when making AI presenter, UGC-style, try-on, unboxing, app demo or product videos with "
                       "Campaignify, or writing scripts and hooks for short vertical video ads.",
        "how": [
            "`get_product` for the product's approved claims, audience and photos; `list_ad_options` for the "
            "presenters (pick one who looks like the buyer), voices, video formats and lengths.",
            "Write 3 to 5 scripts with different hooks (`generate_ad_copy` kind `hooks` helps), not one script with "
            "small changes.",
            "`quote_ad_batch` for the batch, show the user the total, then `generate_video_ads` with "
            "`confirm_total_credits`. Videos need a confirmed email.",
            "`get_batch` until ready; `get_asset_download_url` for the files.",
        ],
        "checklist": [
            "Hook in the first 2 seconds: a problem, a surprising fact the product page backs up, or the result "
            "shown. No logo intro.",
            "Script shape: hook, the problem in one line, the product doing its job on screen, one proof point the "
            "product page backs up, one clear call to action.",
            "15 to 30 seconds for TikTok, Reels and Shorts; vertical 9:16; captions on (most people watch muted).",
            "The presenter explains the product; never 'I've used this for months' or any personal result. AI "
            "presenters are generated people and can't pose as customers.",
            "Show the real product: the product photo or screen recording stays as it is (try-on and app-demo "
            "formats keep the real product shot and the real screen).",
            "Make several angles: problem-solution, demo, comparison with the old way, objection answered, offer "
            "(only if the user gave one).",
            "Turn on each platform's AI disclosure when the ad runs (Google AI label, Meta AI disclosure, TikTok "
            "AI-generated content toggle).",
        ],
        "avoid": [
            "Health, weight, income or before-and-after claims; 'guaranteed' anything.",
            "Celebrities, other brands' logos, copyrighted music or someone else's footage.",
            "Ten near-identical videos: platforms treat them as one ad.",
        ],
    },
    "image-ads": {
        "title": "Make image ads for every platform",
        "description": "Use when making static image ads with Campaignify for Meta, TikTok, Pinterest, LinkedIn, "
                       "Google or other platforms, or choosing shapes, headlines and angles for image ads.",
        "how": [
            "`get_product` for photos and approved claims; `list_ad_options` for image templates and shapes.",
            "`quote_ad_batch` (shapes 1:1, 4:5, 9:16, 16:9), show the total, then `generate_image_ads` with "
            "`confirm_total_credits`.",
            "Download each shape with `get_asset_download_url`; file names say the shape.",
        ],
        "checklist": [
            "One message per image: the product, one benefit, one call to action.",
            "The product is clearly visible and recognizable at phone size.",
            "Shapes per platform: Meta feed 4:5 and Stories/Reels 9:16; TikTok 9:16; Pinterest 2:3; LinkedIn "
            "1.91:1 or 1:1; Google Search image assets 1:1 and 1.91:1 with no text on the image.",
            "Make 3 to 5 different angles (benefit, problem, lifestyle, product close-up, offer if the user gave one).",
            "Headline text on the image is fine on Meta, TikTok and Pinterest; Google Search and Performance Max "
            "image assets need clean versions without text.",
            "AI disclosure on where the image shows realistic AI-generated people or scenes.",
        ],
        "avoid": ["Prices or discounts the user didn't give.", "Tiny text, busy backgrounds, collages.",
                  "Fake buttons or anything that looks like a system message."],
    },
    "ad-copy": {
        "title": "Write ad copy that fits each platform",
        "description": "Use when writing ad copy, headlines, hooks, captions, primary text or responsive search "
                       "ad lines with Campaignify for any ad platform.",
        "how": [
            "`get_product` for the approved claims, audience and price.",
            "`generate_ad_copy` with kind (`ad`, `headlines`, `hooks`, `captions`) and the platform; it sizes "
            "every field to the platform's limits and checks claims. Say it spends a few credits first.",
        ],
        "checklist": [
            "Lead with the outcome the buyer wants, in their words, not the feature name.",
            "Write several angles: outcome, problem, objection, comparison, proof, offer (offer only if the user "
            "gave one).",
            "Google responsive search ads: 8 to 15 headlines of 30 characters and 4 descriptions of 90, the main "
            "keyword in at least 2 headlines.",
            "Meta: the hook in the first 125 characters of primary text; headline under 40.",
            "TikTok: ad text 12 to 100 characters; write like a person talking, not a brochure.",
            "Every number, claim and price in the copy is on the landing page today.",
            "One clear call to action that matches the page ('Start the free trial', 'Get a quote').",
        ],
        "avoid": ["'Best', '#1' or superlatives the user can't back up.",
                  "Personal attributes ('Struggling with your weight?').", "ALL CAPS and clickbait the page doesn't deliver."],
    },
    "campaign-plan": {
        "title": "Plan a campaign and research competitors",
        "description": "Use when planning an ad campaign, splitting a budget across platforms, choosing angles, "
                       "or researching competitors' ads and offers with Campaignify.",
        "how": [
            "`start_competitor_snapshot` with 2 or 3 competitor sites (spends credits; ask first), then "
            "`get_competitor_snapshot` for their offers, angles and gaps.",
            "`start_campaign_plan` with the offer, audience, goal, budget and platforms (spends credits; `quote_ai_tasks` "
            "shows the most it can use), then `get_campaign_plan`.",
            "For each platform in the plan, load its playbook with `get_ad_playbook`.",
        ],
        "checklist": [
            "One goal per campaign (sales, leads or traffic) and one conversion event that measures it.",
            "Start with one or two platforms where the buyers already are, at a budget that buys real data "
            "(enough for at least 10 clicks a day per platform), before spreading wider.",
            "Several distinct angles to test, each with its own creative, and a date to judge them.",
            "Know the numbers before scaling: cost per lead or sale the business can afford, from its price and margin.",
            "Use competitor research to find gaps (a promise they don't make, a buyer they ignore), not to copy.",
        ],
        "avoid": ["Spreading a small budget across five platforms.", "Changing everything every day.",
                  "Copying a competitor's claims, wording or creative."],
    },
    "landing-pages": {
        "title": "Build landing pages and smart links that convert ad clicks",
        "description": "Use when building a Campaignify landing page, choosing what goes on it, adding a form, or "
                       "setting up smart links with UTM tags for ads.",
        "how": [
            "In the Campaignify app: Pages to build the page from blocks, Forms to add a lead form, Smart links for "
            "tracked links with UTM tags.",
            "`generate_ad_copy` with kind `landing` writes the headline, benefits, FAQ and call to action.",
        ],
        "checklist": [
            "Message match: the page's headline says what the ad promised, in the same words.",
            "One goal and one call to action, visible without scrolling on a phone.",
            "The offer, price and any trial terms in plain words, including whether a card is needed.",
            "Real proof only: real numbers with a date, real reviews the user has, real photos of the product.",
            "Short forms: ask only what the next step needs (name, email or phone).",
            "Fast on a phone: compressed images, no heavy video at the top.",
            "A privacy policy link, and a clear consent line on any form that leads to texts or marketing emails.",
            "Every ad link is a smart link or carries utm_source, utm_medium and utm_campaign.",
        ],
        "avoid": ["Sending ad clicks to the home page.", "Countdown timers or 'only 3 left' that aren't true.",
                  "Menus and links that lead away from the one action."],
    },
    "lead-follow-up": {
        "title": "Follow up with leads by email and text without breaking the rules",
        "description": "Use when setting up Campaignify forms, contacts and follow-up flows (email and SMS), writing "
                       "follow-up emails or texts, or checking email and text compliance.",
        "how": [
            "In the Campaignify app: Forms collect the lead, Contacts holds them with where they came from, Flows "
            "send the follow-up emails and texts (credits per message are shown before sending turns on).",
            "`generate_ad_copy` with kind `email` writes each email (welcome, nurture, launch, re-engagement).",
        ],
        "checklist": [
            "Reply fast: the first follow-up goes out within minutes of the form, while the lead still remembers.",
            "A short sequence with one idea per message: welcome and next step, a useful tip, an answer to the main "
            "objection, then a clear offer to take the next step.",
            "Email senders: the domain has SPF, DKIM and DMARC set up; marketing emails carry a one-click "
            "unsubscribe that's honored within 2 days; keep spam complaints well under 0.1% (Gmail and Yahoo "
            "start blocking near 0.3%).",
            "Texts: written consent for marketing texts, collected with a clear, unchecked opt-in that says who "
            "will text, about what and how often; never bought lists or 'consent' implied by a form.",
            "Texts: the business name in the message, STOP and HELP work, and an opt-out is honored right away.",
            "Texts: send between 8 am and 9 pm in the person's time zone (8 pm in Florida and Oklahoma; Oklahoma "
            "also caps 3 a day).",
            "US texting from a regular number needs 10DLC brand and campaign registration with the carriers first.",
            "Keep consent records: where, when and the exact wording the person agreed to.",
        ],
        "avoid": ["Texting people who only gave an email.", "Link shorteners like bit.ly in texts (carriers filter them).",
                  "Sex, hate, alcohol, firearms, tobacco, vape or cannabis content in texts (carriers block it)."],
        "note": "This is general guidance, not legal advice; email and text rules vary by country and state, so the "
                "member should check their own situation.",
    },
    "seo-aeo-geo": {
        "title": "Get found in Google and in AI answers (SEO, AEO, GEO)",
        "description": "Use when auditing a page or site for search engines and AI assistants, writing content to "
                       "be cited by ChatGPT, Google AI Overviews, Perplexity or Claude, or using Campaignify's SEO, "
                       "AEO and GEO optimizer.",
        "how": [
            "In the Campaignify app: the SEO, AEO and GEO optimizer audits a page and gives the fixes in order, a "
            "better title and description, an FAQ block and ready-to-paste schema.",
            "`generate_ad_copy` with kind `landing` helps rewrite the page copy.",
        ],
        "checklist": [
            "Normal SEO first: Google says its AI features use the same ranking systems, so a page that ranks well "
            "and answers well is the base. There's no special 'AI schema'.",
            "Answer first: each section opens with a direct, one- or two-sentence answer, then the detail.",
            "Questions as headings, with a short FAQ for the questions buyers really ask.",
            "Original, specific content: real prices, real steps, real examples from the business. Generic text "
            "that any writer could produce rarely gets cited.",
            "The same business name, address, phone and description everywhere (site, Google Business Profile, "
            "directories, social profiles).",
            "Let AI search crawlers in: Googlebot (AI Overviews and AI Mode), OAI-SearchBot (ChatGPT search), "
            "PerplexityBot and Claude-SearchBot. Blocking training crawlers like GPTBot is a separate choice.",
            "Standard schema that matches the page: Organization, Product, LocalBusiness, Article. Don't expect "
            "FAQ or HowTo rich results in Google.",
            "Measure: Search Console's AI reporting, Bing Webmaster Tools' AI performance, AI referrals in "
            "analytics, and asking the main questions in ChatGPT, Perplexity and Google each month.",
            "llms.txt is optional: Google says it doesn't use it; it costs little for other AI tools.",
        ],
        "avoid": ["Buying mentions or fake reviews.", "Stuffing keywords or FAQ blocks nobody asked.",
                  "Pages that hide the price or the answer behind a form."],
    },
}
TOOL_KEYS = list(TOOL_SKILLS)

COMMON = [
    "Never invent results, reviews, testimonials, prices, discounts, deadlines or popularity.",
    "Ask before anything spends credits, sends messages, publishes or launches.",
    "AI presenters are generated people; label AI creative where the platform asks.",
]


def markdown(key: str) -> str | None:
    s = TOOL_SKILLS.get(key)
    if not s:
        return None
    L = [f"# {s['title']}", "",
         "A Campaignify playbook. Work through it with the member and tick every item, so nothing is missed.", "",
         "## How to do it in Campaignify", ""]
    L += [f"{i}. {x}" for i, x in enumerate(s["how"], 1)]
    L += ["", "## Checklist", ""] + [f"- [ ] {x}" for x in s["checklist"]]
    L += ["", "## Avoid", ""] + [f"- {x}" for x in s["avoid"]]
    L += ["", "## Rules for everything", ""] + [f"- {x}" for x in COMMON]
    if s.get("note"):
        L += ["", f"_{s['note']}_"]
    return "\n".join(L) + "\n"
