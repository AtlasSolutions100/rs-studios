"""The public site's search metadata and content: titles, descriptions, canonical
URLs, share cards, schema.org JSON-LD, the sitemap list, the FAQs and the
answer-first pages for search intent (/ai-ugc-ad-generator and friends).

Everything here describes what Campaignify does today (plans.FEATURES,
catalog.TOOLS, catalog.rates()); prices come from the plans and the rate card,
never typed in. No invented customers, numbers or testimonials. The comparison
pages (COMPARE) state facts about other tools only with the page they were read
from and the date, and say "not listed" where a page doesn't say; never a guess.
"""
import json
import os
import re

from markupsafe import Markup, escape

import catalog
import liveids
import plans
import platforms

SITE_URL = (os.environ.get("RS_SITE_URL") or "https://campaignify.ai").rstrip("/")
OG_IMAGE = "img/og-campaignify.png"
OG_IMAGE_ALT = "campaignify: AI UGC ads from your product page, beside three phones showing AI-made ads."
CONTACT = "hello@campaignify.ai"

# IndexNow (https://www.indexnow.org): the key is public by design. Search engines
# fetch it from /<key>.txt to confirm a ping came from this site, so it lives here
# rather than in a secret. INDEXNOW_KEY overrides it (8-128 hex characters).
INDEXNOW_KEY = liveids.get("INDEXNOW_KEY") or "0" * 32   # a setting (liveids.py, M7.4)
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"


def indexnow_body(urls: list[str]) -> dict:
    """The JSON an IndexNow ping sends for these URLs (all on this site)."""
    host = SITE_URL.split("://", 1)[-1].split("/", 1)[0]
    return {"host": host, "key": INDEXNOW_KEY, "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY}.txt",
            "urlList": [u for u in urls if u.startswith(SITE_URL + "/")]}


def card_terms(card: bool | None = None) -> dict:
    """How the copy describes free sign-up, by whether it asks for a card (re-audit
    L-4: with the card step on, say so plainly and never contrast it with other
    tools' cards). str.format keys for facts()."""
    import adoffer
    card = adoffer.card_on() if card is None else card
    if card:
        return {"signup_charge": "A card is needed at sign-up, but nothing is charged until you pick a plan or buy a pack",
                "nothing_charged": "Card needed to start, nothing charged until you pick a plan or buy a pack",
                "nothing_charged_lc": "card needed to start, nothing charged until you pick a plan or buy a pack",
                "with_nothing_charged": "with a card needed to start but nothing charged until you pick a plan or buy a pack",
                "one_trial": " One free trial per person."}
    return {"signup_charge": "Nothing is charged at sign-up", "nothing_charged": "Nothing charged",
            "nothing_charged_lc": "nothing charged", "with_nothing_charged": "with nothing charged", "one_trial": ""}


def facts() -> dict:
    """Live numbers for the copy (str.format keys)."""
    r, sc = catalog.rates(), catalog.starter_credits()
    p = plans.PLANS
    names = [platforms.PLATFORMS[k]["name"] for k in platforms.ORDER if k not in ("email", "landing")]
    talk = catalog.TOOLS["ugc_video"]
    # Costs are quoted in credits only; dollars appear only for plan and pack prices.
    return {"talk8": f"{r['talk8']:,}", "talk10": f"{r['talk10']:,}", "image": r["image"],
            "video5": f"{r['video5']:,}", "video10": f"{r['video10']:,}",
            "sc": f"{sc:,}", "sc_images": sc // max(r["image"], 1),
            "starter_usd": p["starter"]["usd"], "growth_usd": p["growth"]["usd"], "scale_usd": p["scale"]["usd"],
            "starter_credits": f"{p['starter']['credits']:,}",
            "talk_lengths": ", ".join(str(d) for d in talk["durations"][:-1]) + f" or {talk['durations'][-1]}",
            "talk_max": talk["durations"][-1], "talk_shapes": ", ".join(talk["aspects"][:-1]) + f" or {talk['aspects'][-1]}",
            "image_shapes": ", ".join(catalog.TOOLS["ad_image"]["aspects"]),
            "presenters": presenter_count(), "max_batch": _max_batch(),
            "platform_list": ", ".join(names[:-1]) + f" and {names[-1]}", "platform_count": len(names),
            "pack_usd": ", ".join(f"${k['usd']}" for k in catalog.PACKS[:-1]) + f" and ${catalog.PACKS[-1]['usd']}",
            "pack_min_usd": catalog.PACKS[0]["usd"], "pack_min_credits": f"{catalog.PACKS[0]['credits']:,}",
            **card_terms()}


def presenter_count() -> int:
    """How many AI presenters a member can pick: the picker's roster (presenters.roster()), else the first eight."""
    try:
        import presenters
        return len(presenters.roster()) or len(catalog.PRESENTERS)
    except Exception:  # noqa: BLE001
        return len(catalog.PRESENTERS)


def basket(credits: int) -> dict:
    """What a month's (or a pack's) credits buy as a mixed basket, for the pricing
    copy: 1 talking video and 2 product videos per 2,000 credits, the rest image
    ads. E.g. 2,000 credits: 1 talking video, 2 product videos and 15 image ads."""
    r = catalog.rates()
    talk = max(1, credits // 2000)
    vids = 2 * talk
    rest = credits - talk * r["talk8"] - vids * r["video5"]
    images = max(0, rest // max(r["image"], 1))
    return {"talk": talk, "videos": vids, "images": images, "credits": credits,
            "line": f"e.g. {talk} talking video{'s' if talk != 1 else ''}, {vids} product videos and {images} image ads"}


def _usd(x: float) -> str:
    """A rounded "about" dollar amount: cents under $1, dimes under $10, dollars above."""
    if x < 1:
        return f"${x:.2f}"
    if x < 10:
        v = round(x, 1)
        return f"${v:.0f}" if v == int(v) else f"${v:.2f}"
    return f"${round(x):,}"


def _n(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def ad_math(credits: int, usd: float) -> dict:
    """What `credits` (a plan's month or a pack) makes, from the rate card: how many of
    each kind of ad. The one pricing sum for /pricing and Billing (COPY-5); never typed
    in. Counts only: what things cost to make is shown in credits, never in dollars
    (Koby, Nov 8). E.g. Starter, 2,000 credits: about 90 image ads, or 4 product videos,
    or 2 talking videos. `usd` is kept for callers and ignored."""
    r = catalog.rates()
    images, videos, talks = credits // max(r["image"], 1), credits // max(r["video5"], 1), credits // max(r["talk8"], 1)
    return {"images": images, "videos": videos, "talks": talks,
            "counts": f"About {_n(images, 'image ad', 'image ads')}, or {_n(videos, 'product video', 'product videos')}, "
                      f"or {_n(talks, 'talking video', 'talking videos')}, in any mix"}


def _max_batch() -> int:
    try:
        import create_routes
        return create_routes.MAX_BATCH
    except Exception:  # noqa: BLE001
        return 12


_LINK = re.compile(r"\[([^\]]+)\]\((/[^)\s]*)\)")


def answer_html(text: str) -> Markup:
    """FAQ answers are plain text with optional [label](/path) links."""
    return Markup(_LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', str(escape(text))))


def answer_text(text: str) -> str:
    return _LINK.sub(lambda m: m.group(1), text)


def _fmt(items, f):
    return [(q.format(**f), a.format(**f)) for q, a in items]


# ---------------------------------------------------------------- FAQs shown on the home page and /pricing

HOME_FAQ = [
    ("Does Campaignify post or run my ads?",
     "No. It plans the campaign, makes the ads, writes the copy and walks you through setting up each ad account, "
     "then exports every ad at that platform's sizes. You upload and launch them in the platform's own ads manager, "
     "and your ad spend stays there too."),
    ("Can I see how my campaign is doing?",
     "For what you run on Campaignify, yes: clicks on your smart links (per day, referrer, country, device and UTM "
     "source), views on your pages and submissions to your forms, with each contact's history. Campaignify doesn't "
     "read results from the ad platforms; see those in each platform's ads manager."),
    ("Which plan do I need?",
     "The free studio makes ads with your starter credits and credit packs. Starter adds the AI campaign planner, "
     "copy studio, ad-account setup hub, one landing page and smart links. Growth adds the SEO, AEO & GEO optimizer, "
     "the content optimizer, forms and contacts, link analytics and unlimited pages. Scale adds community and "
     "courses and the priority queue."),
    ("Do I need a subscription?",
     "No. Start with your free credits and buy credit packs when you need more. Plans add monthly credits and the "
     "campaign tools; you can cancel a plan anytime and it stays on until the end of the month you've paid for."),
    ("Do unused credits roll over?",
     "Plan credits arrive with each monthly payment and expire at your next renewal, and they're used before any "
     "other credits. Credits from packs and your free starter credits never expire."),
    ("What does a credit buy?",
     "An image ad is {image} credits, a 5-second video {video5}, a 10-second video {video10}, and "
     "copying a reference video's motion is {motion_per_s} credits per second of the reference (up to {motion_max}). "
     "The AI writing tools charge what the AI actually used. You always see the price before you generate."),
    ("What if a generation fails?", "Your credits come back automatically. You only pay for ads that finish."),
    ("Does it work for my product?",
     "If you can photograph it, yes. Product hero, Daily routine and your own brief work for anything; a few "
     "templates were written for coffee and say so."),
    ("Who owns the ads I make?",
     "You can use the ads you make in your own marketing, including paid ads. They're AI-generated, so similar "
     "images can come out for other people, and you're responsible for what you post. See the [terms](/terms)."),
    ("Does Campaignify guarantee results?",
     "No. Campaignify helps you plan, make and run a campaign. It doesn't promise sales, views or income, and your "
     "ads shouldn't either."),
    ("Is this from my direct-sales company?",
     "No. Campaignify is an independent product. It isn't a company program and isn't sponsored or endorsed by any "
     "direct-sales company. Check your company's policies on advertising and AI content before you post."),
    ("Do my ads say they're AI?",
     "Put #ad first in your caption and turn on the platform's AI label. Don't present AI people as real customers "
     "or let them describe personal results."),
    ("My team lead sent me an invite",
     "Use the invite link to sign up and you'll join their workspace and share its credits."),
]
PRICING_FAQ_QS = ("Do I need a subscription?", "Which plan do I need?", "Do unused credits roll over?",
                  "What does a credit buy?", "What if a generation fails?")


def home_faq():
    r = catalog.rates()
    return [(q, a.format(**r)) for q, a in HOME_FAQ]


def pricing_faq():
    return [qa for qa in home_faq() if qa[0] in PRICING_FAQ_QS]


# ---------------------------------------------------------------- answer-first pages for search intent
# slug -> page. `query` is the search it answers; `related` links the others.
SOLUTIONS = {
    "ai-ugc-ad-generator": {
        "query": "AI UGC ad generator",
        "nav": "AI UGC ad generator",
        "title": "AI UGC Ad Generator: UGC-Style Video Ads · Campaignify",
        "description": "Turn a product page into UGC-style video ads: an AI presenter holds your product and says your "
                       "script, priced up front. Free sign-up includes {sc_images} image ads.",
        "eyebrow": "AI UGC ads",
        "h1": "AI UGC ad generator",
        "answer": "Campaignify turns your product page into UGC-style video ads. Paste the link, pick an AI presenter "
                  "and an angle, and you get a vertical, square or wide video of the presenter holding your product "
                  "and saying a script written for it, selfie-style. You see the price before anything is made, and "
                  "failed videos are refunded automatically.",
        "steps": [
            ("Paste your product page",
             "Campaignify reads the name, description, price and photos from the page, Shopify product pages "
             "included. Suggested claims wait for your approval, and you pick the photos the ads use."),
            ("Pick a presenter and an angle",
             "Choose one of {presenters} AI presenters or a narrator over your product, pick an angle, and let AI "
             "write the spoken lines. Rewrite any line before you generate."),
            ("Generate, review and download",
             "Make one video first or a batch of up to {max_batch}. Talking videos run {talk_lengths} seconds in "
             "{talk_shapes}; everything lands in your library with full-quality downloads."),
        ],
        "gets": [
            ("UGC presenter videos", "A presenter holds your product and says your script to camera. The speech is "
                                     "generated with the video, so the lips and the words match."),
            ("Product video + voiceover", "Your product on screen while a narrator reads the script, for when you'd "
                                          "rather not show a face."),
            ("Image ads and product clips", "Image ads in {image_shapes}; 5 or 10 second clips from one product "
                                            "photo; and your product in a reference video's motion."),
            ("Prices before you press Generate", "Paid in credits: an 8-second talking video is {talk8} credits "
                                                 "at 720p. Failed generations are refunded."),
            ("Compliance checks on every brief", "Income, health and recruiting claims and invented discounts are blocked, and "
                                                 "AI presenters never speak as customers."),
            ("Sizes for every feed", "On Starter and up, export each finished ad at each platform's sizes and get "
                                     "step-by-step setup for eight ad platforms."),
        ],
        "clips": ["ugc-coffee", "ugc-serum", "ugc-sneakers"],
        "price_line": "8-second talking video: {talk8} credits. Image ad: {image} credits. Shown before you press Generate.",
        "h2": {"how": "How the AI UGC ad generator works", "gets": "What the AI UGC ad generator makes",
               "faq": "AI UGC ad generator questions"},
        "faq": [
            ("What is a UGC ad?",
             "A UGC (user-generated content) ad looks like a post someone filmed on their phone: a person holding the "
             "product and talking to camera. Campaignify makes that style with AI presenters. They're described "
             "characters generated with each video, not real people, and they don't pose as customers or describe "
             "personal results."),
            ("How much does an AI UGC video cost?",
             "Talking videos are paid in credits: an 8-second video is {talk8} credits "
             "and a 10-second one {talk10} credits at 720p. New accounts get {sc} free credits, enough "
             "for {sc_images} image ads. Plans start at ${starter_usd} a month with {starter_credits} credits."),
            ("Can I write my own script?",
             "Yes. AI drafts the spoken lines from your product and the angle you pick, and you can edit or replace "
             "every line before you generate."),
            ("Do I have to say the ad uses AI?",
             "Follow each platform's rules and the FTC's: put #ad first in the caption and turn on the platform's AI "
             "label. Campaignify reminds you on every ad and flags briefs where an AI presenter speaks as a customer."),
            ("Does Campaignify post the ads for me?",
             "No. You download or export each ad and launch it in the platform's own ads manager, where your ad "
             "spend and results stay."),
        ],
        "related": ["ai-product-ads-for-shopify", "arcads-alternative", "creatify-alternative"],
    },
    "arcads-alternative": {
        "query": "Arcads alternative",
        "nav": "Arcads alternative",
        "title": "Arcads Alternative for AI UGC Video Ads · Campaignify",
        "description": "Comparing Arcads alternatives? Campaignify makes UGC-style video and image ads from your "
                       "product page, nothing charged, price first. Try {sc_images} image ads free.",
        "eyebrow": "Comparing tools",
        "h1": "Campaignify is an Arcads alternative for AI UGC video ads, priced before you generate",
        "answer": "Campaignify is an AI ad studio that makes UGC-style video ads and image ads from your product page, "
                  "and keeps the copy, landing page and launch checklist in the same workspace. Free sign-up includes "
                  "{sc_images} image ads {with_nothing_charged}, and you see the price of every ad before it's made. The table "
                  "below puts the two side by side; every Arcads fact is quoted from Arcads' own help center, with the "
                  "date it was read.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's name, description, price and photos from its page. You approve the "
             "claims and pick the photos."),
            ("Make a test ad",
             "Pick a UGC presenter video, a voiceover video or an image ad, let AI write the script, and check the "
             "price before you generate."),
            ("Judge it on your own product",
             "Compare the result with what you get elsewhere. Your free credits cover {sc_images} image ads; talking "
             "videos start at {talk8} credits."),
        ],
        "table": {
            "competitor": "Arcads",
            "source": "https://help.arcads.ai/en/articles/17595066-how-does-arcads-pricing-work",
            "source_name": "Arcads' help center: How does Arcads pricing work?",
            "date": "2026-11-03",
            "rows": [
                ("Trying it",
                 "A 72-hour trial of every feature with 1,000 credits. \"A card is required at sign-up but isn't charged "
                 "during the trial.\" \"After 72 hours, the Starter monthly plan starts automatically.\"",
                 "{signup_charge}. Free sign-up includes {sc} credits ({sc_images} image ads).{one_trial} Nothing starts by itself: you "
                 "buy a pack or pick a plan when you want more."),
                ("Prices on the public site",
                 "Not listed on the public site (as of 2026-11-03): \"Current prices are on the pricing page in the "
                 "app.\" Plans are named Starter, Creator, Pro, Scale, Business and Enterprise.",
                 "On this site: plans at ${starter_usd}, ${growth_usd} and ${scale_usd} a month and packs at "
                 "{pack_usd}, and every ad's price in credits before you generate."),
                ("Credits per talking video",
                 "800 credits per Arcads 1.0 talking-actor video (up to 2 minutes); 800 per started minute for "
                 "audio-driven and OmniHuman 1.5 videos; 800 per 8-second Veo 3.1 clip.",
                 "8-second UGC videos from {talk8} credits, 10 seconds {talk10}; a 5-second product video {video5}; "
                 "an image ad {image}. Failed generations are refunded."),
            ],
        },
        "h2": {"how": "How this Arcads alternative works", "compare": "Arcads and Campaignify, side by side",
               "faq": "Arcads alternative questions"},
        "compare": [
            ("Can it start from my product page?",
             "Yes. Paste the link and it fills in the product from the page; Shopify product pages are recognised. "
             "You can also upload photos."),
            ("Do I see the price before I generate?",
             "Yes, in credits, and failed generations are refunded automatically."),
            ("Who are the presenters?",
             "{presenters} AI presenters, each drawn from a written description with every video. They're not real "
             "people, so two videos with the same presenter look alike but not identical."),
            ("What else does it make?",
             "Image ads in {image_shapes}; 5 or 10 second product videos; motion copied from a reference clip; and on "
             "paid plans, copy for {platform_count} ad platforms, a campaign planner and landing pages."),
            ("What stops risky claims?",
             "Every brief, piece of copy and landing page is checked for income, health, discount and recruiting "
             "claims before it goes out."),
        ],
        "limits": [
            "It makes AI presenters only; it doesn't book or film real creators.",
            "A talking video is at most {talk_max} seconds long.",
            "It doesn't buy media, post ads or read results from the ad platforms; you launch the ads in each "
            "platform's ads manager.",
        ],
        "media": [("site/ugc-bakery.jpg", "An AI presenter behind a bakery counter shows a tray of croissants."),
                  ("site/ugc-app.jpg", "An AI presenter at a desk shows a to-do list app on his phone."),
                  ("site/show-kawaii.jpg", "A coffee bag on a pink gingham beach picnic with glass hearts.")],
        "faq": [
            ("Is Campaignify affiliated with Arcads?",
             "No. Campaignify is an independent product with no connection to them. Arcads is a trademark of its "
             "owner. The Arcads facts on this page are quoted from Arcads' own help center with the date they were "
             "read; check their site for today's terms."),
            ("Can I try Campaignify before I pay?",
             "Yes. A new account gets {sc} free credits, enough for {sc_images} image ads, {with_nothing_charged}.{one_trial} Buy a credit "
             "pack or pick a plan when you want to make more."),
            ("How long can a talking video be?",
             "{talk_lengths} seconds, in {talk_shapes}. Make a batch of variants to test different hooks and "
             "presenters."),
            ("Can I use the ads in paid campaigns?",
             "Yes, you can use the ads you make in your own marketing, including paid ads. You're responsible for "
             "what you post; see the [terms](/terms)."),
        ],
        "related": ["ai-ugc-ad-generator", "creatify-alternative", "makeugc-alternative"],
    },
    "ai-product-ads-for-shopify": {
        "query": "AI product ads for Shopify stores",
        "nav": "AI ads for Shopify stores",
        "title": "AI Product Ads for Shopify Stores · Campaignify",
        "description": "Paste a Shopify product link and get image ads, product videos and UGC-style video ads made "
                       "from your own product photos. Prices shown first.",
        "eyebrow": "For Shopify stores",
        "h1": "AI product ads for Shopify stores",
        "answer": "Paste a Shopify product page into Campaignify and it pulls the product's name, description, price "
                  "and photos from the store, then makes image ads, short product videos and UGC-style talking "
                  "videos from those photos. There's no app to install. You approve the claims, see the price of "
                  "each ad first, and download finished ads for Meta, TikTok, Pinterest and the other feeds. "
                  "Sidekick writes your descriptions. Campaignify makes the ads.",
        "clips": ["ugc-coffee", "product-video", "show-amber"],
        "price_line": "8-second talking video: {talk8} credits. 5-second product video: {video5} credits. Image ad: "
                      "{image} credits. Shown before you press Generate.",
        "h2": {"how": "How the Shopify ad creator works", "gets": "What you get: image ads, a product video for Shopify, "
               "UGC videos", "faq": "Shopify ad creator questions"},
        "steps": [
            ("Paste the product link",
             "No Shopify app needed: Campaignify reads the public product page and the store's own product data for "
             "that item's photos."),
            ("Approve the claims and photos",
             "Suggested claims come from the page's own wording and wait for your approval. Pick the photos every ad "
             "should use."),
            ("Make the ads",
             "Choose image ads, product videos or a UGC presenter, see the price, generate, then download or export "
             "them at each platform's sizes."),
        ],
        "gets": [
            ("Image ads from your photos", "Finished ad images in {image_shapes}. Only the headline, subline and "
                                           "button text you type and your brand name are drawn on the image."),
            ("A product video for Shopify", "5 or 10 second clips that bring one product photo to life, from {video5} "
                                            "credits: the sneaker clip above is a 5-second product video from one photo."),
            ("UGC-style talking videos", "An AI presenter holds your product and says a script written for it, "
                                         "from {talk8} credits."),
            ("Every product in one place", "Each product keeps its photos, offer, approved claims and audience; "
                                           "Growth and Scale add more brands."),
            ("Copy and a landing page", "On Starter and up: ad copy for each platform's fields and a landing page "
                                        "published on campaignify.ai."),
            ("Checks before anything runs", "Prices shown before you generate, failures refunded, and income, health "
                                            "claims and invented discounts blocked."),
        ],
        "faq": [
            ("Is this a Shopify ad creator or a Shopify app?",
             "A Shopify ad creator that works from the product page: paste the link and it reads the public page and the "
             "store's own product data for that item's photos. It isn't an app and never asks for store access. Shopify "
             "Sidekick can write your product descriptions; Campaignify makes the ads from the page those descriptions "
             "live on."),
            ("Does it work with stores that aren't on Shopify?",
             "Yes. It reads any product page that states its details plainly (product data, share tags and images), "
             "and you can always upload photos and type the details yourself."),
            ("Will the ads look exactly like my product?",
             "They're generated from your product photos, but AI can still get details wrong, such as small label "
             "text. Check every ad before you post it. Failed generations are refunded; finished ones are charged."),
            ("How much does a Shopify product ad cost?",
             "An image ad is {image} credits, a 5-second product video {video5} credits and an 8-second "
             "talking video {talk8} credits. New accounts get {sc} free credits; plans start at ${starter_usd} a month."),
            ("Can it run the ads or sync them to my store?",
             "No. You download the ads and launch them in each ad platform's ads manager. Campaignify doesn't change "
             "your store."),
        ],
        "related": ["ai-ugc-ad-generator", "ai-ad-copy-generator", "adcreative-ai-alternative"],
    },
    "ai-ad-copy-generator": {
        "query": "AI ad copy generator",
        "nav": "AI ad copy generator",
        "title": "AI Ad Copy Generator for Every Ad Platform · Campaignify",
        "description": "Write ad copy sized to each platform's text fields, plus hooks, captions, headlines and "
                       "emails, with compliance checks on every variant.",
        "eyebrow": "AI copy",
        "h1": "AI ad copy generator",
        "answer": "Campaignify's copy studio writes ad copy for the exact text fields each platform takes, sized to "
                  "their display lengths, from your product's details and approved claims. Pick the platform and the "
                  "goal and you get several variants, each run through the compliance checks. It also writes hooks, "
                  "captions, headlines, emails and landing-page copy.",
        "steps": [
            ("Add your product",
             "Paste its page or fill in the details. The approved claims are the only claims the copy may use."),
            ("Pick the platform, the copy type and the goal",
             "{platform_list}, plus email and landing pages. You see the most a run can cost before you start."),
            ("Get variants, then sharpen them",
             "Copy the ones you like. On Growth and up, paste any draft into the content optimizer for a score and "
             "three rewrites."),
        ],
        "gets": [
            ("Ad copy per platform", "Every text field each platform takes (primary text, headlines, descriptions "
                                     "and the rest), sized to its display lengths."),
            ("Hooks for video ads", "Opening lines for the first three seconds, with the on-screen text."),
            ("Captions and headlines", "Post captions with a call to action and a few hashtags, and short headlines "
                                       "to test against each other."),
            ("Emails and landing copy", "Subject line, preview text and a short email; or a headline, benefits, FAQ "
                                        "and call to action for a page."),
            ("Compliance checks", "Every variant is checked for income, health and recruiting claims and invented discounts."),
            ("Pay for what the AI used", "Each run costs the AI's actual usage in credits, at least 1 credit; the "
                                         "copy studio is on Starter (${starter_usd} a month) and up."),
        ],
        "shot": ("site/shot-copy", "Ad copy variants for Facebook & Instagram and TikTok hooks in the copy studio."),
        "faq": [
            ("Which platforms does it write for?",
             "{platform_list}, plus emails and landing pages. Each one's fields and display lengths are built in."),
            ("Is the AI copy generator free?",
             "The copy studio is on Starter (${starter_usd} a month) and up. Each run is charged at what the AI "
             "actually used, in credits, and you see the most it can cost before you run it."),
            ("Will it write claims my product can't make?",
             "It writes from the claims you approved, and every variant goes through the compliance checks, which "
             "block income, health and recruiting claims and invented discounts. Still read each line before you use it."),
            ("Can it improve copy I already have?",
             "Yes. On Growth and up the content optimizer scores a draft for a platform (hook, clarity, call to "
             "action, fit and compliance) for free, and an AI review explains the biggest fix and writes three "
             "rewrites."),
        ],
        "h2": {"how": "How the AI ad copy generator works", "gets": "What the AI ad copy generator writes",
               "faq": "AI ad copy generator questions"},
        "related": ["ai-ugc-ad-generator", "ai-product-ads-for-shopify", "makeugc-alternative"],
    },
    "ai-marketing-platform-for-small-business": {
        "query": "AI marketing platform for small business",
        "nav": "AI marketing for small business",
        "title": "AI Marketing Platform for Small Business · Campaignify",
        "description": "Ads, ad copy, landing pages, forms and email follow-ups in one place for your small business, "
                       "no agency. Plans from ${starter_usd} a month; {sc_images} image ads free.",
        "eyebrow": "For small businesses",
        "h1": "AI marketing platform for small business",
        "answer": "Campaignify is an AI marketing platform for small businesses: local shops, service businesses and "
                  "small online stores. In one workspace you make image and video ads from photos of what you sell, "
                  "write the ad copy, publish a landing page with a form, share smart links and draft the email "
                  "follow-ups, without hiring an agency. You download the ads and launch them yourself in each "
                  "platform's ads manager. Plans start at ${starter_usd} a month, and free sign-up includes "
                  "{sc_images} image ads, {with_nothing_charged}.",
        "clips": ["ugc-bakery", "product-video", "ugc-app"],
        "price_line": "Plans from ${starter_usd} a month. Image ad: {image} credits. 8-second talking video: {talk8} "
                      "credits. Shown before you press Generate.",
        "h2": {"how": "How a small business uses the AI marketing platform",
               "gets": "What's in the marketing platform for small business",
               "faq": "Small business marketing platform questions"},
        "steps": [
            ("Add what you sell",
             "Paste the page it's on, or upload your own photos and type the details. Suggested claims wait for your "
             "approval, and every ad, line of copy and page starts from them."),
            ("Make the ads and write the copy",
             "Make image ads, short product videos or UGC-style videos with an AI presenter, and see each price in "
             "credits first. On Starter and up, the copy studio writes the text for each platform's ad fields."),
            ("Launch the ads, then follow up",
             "Download or export each ad at the platform's sizes and launch it in that platform's ads manager. Send "
             "the clicks to your landing page, collect sign-ups with a form and follow up with a flow."),
        ],
        "gets": [
            ("Ads from your own photos", "Image ads in {image_shapes}, 5 or 10 second product videos from one photo, "
                                         "and talking videos with {presenters} AI presenters. Failed generations are "
                                         "refunded."),
            ("A campaign plan and the copy", "On Starter (${starter_usd} a month) and up: an AI planner for angles, "
                                             "scripts and a launch checklist, and copy for {platform_count} ad "
                                             "platforms, captions and emails."),
            ("Landing pages and smart links", "On Starter and up: a page built from blocks and published on "
                                              "campaignify.ai (1 on Starter, unlimited on Growth), and short links "
                                              "with your UTM tags."),
            ("Forms and contacts", "On Growth (${growth_usd} a month) and up: forms on your pages or as a hosted link, "
                                   "and a contacts list with tags, notes and the page, link and form each person "
                                   "came through."),
            ("Email follow-ups with Flows", "Emails, waits, tags and branches after someone signs up, drafted by AI "
                                            "from a sentence. On Starter each email becomes a task with the copy "
                                            "ready; Growth and up send them."),
            ("Ad-account setup and SEO", "On Starter and up: step-by-step setup for 8 ad platforms, with exports at "
                                         "each one's sizes. On Growth: the SEO & AEO optimizer for search and AI "
                                         "answers."),
        ],
        "faq": [
            ("Does Campaignify post or run my ads?",
             "No. You download or export each ad at the platform's sizes and launch it yourself in that platform's "
             "ads manager, where your ad spend and results stay. Campaignify's own analytics cover the pages, smart "
             "links and forms you run on it."),
            ("How much does it cost for a small business?",
             "Free sign-up includes {sc} credits, enough for {sc_images} image ads, {with_nothing_charged}. Plans are "
             "${starter_usd}, ${growth_usd} and ${scale_usd} a month. Each ad has a price in credits shown before you "
             "generate: an image ad is {image} credits, a 5-second product video {video5} and an 8-second talking "
             "video {talk8}. Your ad spend is paid to the ad platforms, not to Campaignify."),
            ("Which plan does a small business need?",
             "Free lets you try the ad studio. Starter (${starter_usd} a month) adds the campaign planner, copy "
             "studio, ad-account setup hub, 1 landing page, smart links and flows. Growth (${growth_usd} a month) "
             "adds forms and contacts, email sending in flows, the SEO & AEO optimizer, link analytics and unlimited "
             "pages. See [pricing](/pricing)."),
            ("Does it work for a local shop or a service business?",
             "If you can photograph what you sell, yes: a product, a dish or your shop. Paste the page it's on, or "
             "upload photos and type the details yourself. Product hero, Daily routine and your own brief work for "
             "anything."),
            ("Will it get me customers?",
             "Campaignify doesn't promise sales, leads or views. It helps you plan, make and organize the campaign; "
             "how the ads do depends on your offer, your audience and your ad spend."),
        ],
        "related": ["ai-product-ads-for-shopify", "ai-ad-copy-generator", "ai-ugc-ad-generator"],
    },
    "creatify-alternative": {
        "query": "Creatify alternative",
        "nav": "Creatify alternative",
        "title": "Creatify Alternative: Price Shown First · Campaignify",
        "description": "Comparing Creatify alternatives? Campaignify shows each ad's price in credits before you "
                       "generate, nothing charged, pack credits never expire. {sc_images} ads free.",
        "eyebrow": "Comparing tools",
        "h1": "Campaignify is a Creatify alternative with a fixed credit price per ad, shown before you generate",
        "answer": "Campaignify makes UGC-style video ads, product videos and image ads from your product page. Every "
                  "ad has a fixed price in credits that you see before you press Generate, failed generations are "
                  "refunded, and pack credits never expire. Free sign-up includes {sc_images} image ads, {with_nothing_charged}. "
                  "The Creatify facts below are quoted from Creatify's pricing page, with the date it was read.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's name, description, price and photos from its page. You approve the "
             "claims and pick the photos."),
            ("See the price, then generate",
             "Pick a UGC presenter video, a product video or an image ad. The price in credits is on the button "
             "before anything runs."),
            ("Judge it on your own product",
             "Your free credits cover {sc_images} image ads. Talking videos start at {talk8} credits, product videos "
             "at {video5}."),
        ],
        "table": {
            "competitor": "Creatify",
            "source": "https://creatify.ai/pricing",
            "source_name": "Creatify's pricing page",
            "date": "2026-11-03",
            "rows": [
                ("Trying it",
                 "A free plan with 10 credits a month, \"up to 2 video ads or 20 image ads\", exports with a watermark. "
                 "Whether a card is needed: not listed on creatify.ai/pricing (as of 2026-11-03).",
                 "{signup_charge}. Free sign-up includes {sc} credits ({sc_images} image ads), no watermark on image ads.{one_trial}"),
                ("What an ad costs",
                 "\"Credit usage varies by the model, feature, and type of output\"; the cost is shown before you "
                 "confirm a generation. Plans listed from $10 a month (Standard, 55 credits) to $136 a month "
                 "(Business, 1,500 credits); the page shows a \"save 30% on annual plan\" banner.",
                 "A fixed price per ad: {image} credits an image ad, {video5} a 5-second product video, {talk8} an "
                 "8-second talking video, shown before you generate. Plans at ${starter_usd}, ${growth_usd} and "
                 "${scale_usd} a month; packs from ${pack_min_usd}."),
                ("Do credits expire?",
                 "\"Credits expire after 2 months.\"",
                 "Plan credits reset at each monthly renewal. Pack credits and the free starter credits never expire."),
            ],
        },
        "h2": {"how": "How this Creatify alternative works", "compare": "Creatify and Campaignify, side by side",
               "faq": "Creatify alternative questions"},
        "compare": [
            ("Do I see the price before I generate?",
             "Yes, in credits, on the button, and failed generations are refunded automatically."),
            ("Can it start from my product page?",
             "Yes. Paste the link and it fills in the product from the page; Shopify product pages are recognised."),
            ("Who are the presenters?",
             "{presenters} AI presenters, each drawn from a written description with every video. They're not real "
             "people and never speak as customers."),
            ("What else does it make?",
             "Image ads in {image_shapes}; 5 or 10 second product videos; motion copied from a reference clip; and on "
             "paid plans, copy for {platform_count} ad platforms, a campaign planner and landing pages."),
        ],
        "limits": [
            "It makes AI presenters only; it doesn't book or film real creators.",
            "A talking video is at most {talk_max} seconds long.",
            "It doesn't buy media, post ads or read results from the ad platforms.",
        ],
        "media": [("site/ugc-serum.jpg", "An AI presenter in a bright bathroom shows a face serum."),
                  ("site/show-pastel.jpg", "A coffee bag among floating pastel cubes on a white pedestal."),
                  ("site/ugc-sneakers.jpg", "An AI presenter in a gym shows off a sage-green running sneaker.")],
        "faq": [
            ("Is Campaignify affiliated with Creatify?",
             "No. Campaignify is an independent product with no connection to them. Creatify is a trademark of its "
             "owner. The Creatify facts on this page are quoted from Creatify's pricing page with the date it was "
             "read; check their site for today's terms."),
            ("Can I try Campaignify before I pay?",
             "Yes. A new account gets {sc} free credits, enough for {sc_images} image ads, {with_nothing_charged} and nothing "
             "that starts billing by itself.{one_trial}"),
            ("What does a credit buy?",
             "An image ad is {image} credits, a 5-second product video {video5}, an 8-second talking video {talk8} "
             "and a 10-second one {talk10}. Plans are ${starter_usd}, ${growth_usd} and ${scale_usd} a month; packs "
             "are {pack_usd}."),
            ("Can I use the ads in paid campaigns?",
             "Yes, in your own marketing, including paid ads. You're responsible for what you post; see the "
             "[terms](/terms)."),
        ],
        "related": ["ai-ugc-ad-generator", "arcads-alternative", "makeugc-alternative"],
    },
    "makeugc-alternative": {
        "query": "MakeUGC alternative",
        "nav": "MakeUGC alternative",
        "title": "MakeUGC Alternative: No Trial That Bills You · Campaignify",
        "description": "Comparing MakeUGC alternatives? Campaignify charges nothing to start, has no trial that renews, "
                       "and each ad's price shown first. Try {sc_images} image ads free.",
        "eyebrow": "Comparing tools",
        "h1": "Campaignify is a MakeUGC alternative with nothing charged at sign-up, no trial that renews, and the price shown first",
        "answer": "Campaignify makes UGC-style video ads, product videos and image ads from your product page. {signup_charge}, "
                  "and there's no trial that turns into a plan: you start with {sc_images} free image ads, "
                  "see every ad's price in credits before you generate, and buy a pack or a plan only when you want to. "
                  "The MakeUGC facts below are quoted from MakeUGC's pricing page, with the date it was read.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's name, description, price and photos from its page. You approve the "
             "claims and pick the photos."),
            ("See the price, then generate",
             "Pick a UGC presenter video, a product video or an image ad. The price in credits is on the button "
             "before anything runs."),
            ("Pay only when you want more",
             "Your free credits cover {sc_images} image ads. After that, a one-time pack from ${pack_min_usd} or a plan "
             "from ${starter_usd} a month, cancel anytime."),
        ],
        "table": {
            "competitor": "MakeUGC",
            "source": "https://www.makeugc.ai/pricing",
            "source_name": "MakeUGC's pricing page",
            "date": "2026-11-03",
            "rows": [
                ("Trying it",
                 "\"Try any plan for $1.\" It \"renews at the plan price unless you cancel from your account\"; the "
                 "trial's length is shown at checkout. A free plan: not listed on makeugc.ai/pricing (as of "
                 "2026-11-03).",
                 "Free sign-up, {nothing_charged_lc}: {sc} credits ({sc_images} image ads).{one_trial} Nothing renews unless "
                 "you pick a plan."),
                ("What an ad costs",
                 "\"Each generation costs credits based on its model, duration and resolution\"; a per-video price is "
                 "not listed on the page (as of 2026-11-03). Plans listed at $29, $39 and $79 a month billed yearly "
                 "(500, 1,000 and 2,000 to 5,000 credits); monthly billing prices not listed per plan.",
                 "A fixed price per ad, shown before you generate: {image} credits an image ad, {video5} a 5-second "
                 "product video, {talk8} an 8-second talking video. Plans at ${starter_usd}, ${growth_usd} and "
                 "${scale_usd} a month, billed monthly."),
                ("Do credits expire?",
                 "Credits \"refresh monthly\" and \"do not roll over\".",
                 "Plan credits reset at each monthly renewal. Pack credits and the free starter credits never expire."),
            ],
        },
        "h2": {"how": "How this MakeUGC alternative works", "compare": "MakeUGC and Campaignify, side by side",
               "faq": "MakeUGC alternative questions"},
        "compare": [
            ("Is there a trial that turns into a subscription?",
             "No. {signup_charge}, and the free credits have no end date. A plan starts only when you choose one, and you can cancel "
             "it anytime."),
            ("Do I see the price before I generate?",
             "Yes, in credits, on the button, and failed generations are refunded automatically."),
            ("Who are the presenters?",
             "{presenters} AI presenters, each drawn from a written description with every video. They're not real "
             "people and never speak as customers."),
            ("What else does it make?",
             "Image ads in {image_shapes}; 5 or 10 second product videos; motion copied from a reference clip; and on "
             "paid plans, copy for {platform_count} ad platforms, a campaign planner and landing pages."),
        ],
        "limits": [
            "It makes AI presenters only; it doesn't book or film real creators.",
            "A talking video is at most {talk_max} seconds long.",
            "It doesn't buy media, post ads or read results from the ad platforms.",
        ],
        "media": [("site/ugc-bakery.jpg", "An AI presenter behind a bakery counter shows a tray of croissants."),
                  ("site/show-y2k.jpg", "A coffee bag on denim among chains, charms and a pink flip phone."),
                  ("site/ugc-coffee.jpg", "An AI presenter in a sunny kitchen holds up a coffee bag.")],
        "faq": [
            ("Is Campaignify affiliated with MakeUGC?",
             "No. Campaignify is an independent product with no connection to them. MakeUGC is a trademark of its "
             "owner. The MakeUGC facts on this page are quoted from MakeUGC's pricing page with the date it was "
             "read; check their site for today's terms."),
            ("Can I try Campaignify before I pay?",
             "Yes. A new account gets {sc} free credits, enough for {sc_images} image ads, {with_nothing_charged} and no trial "
             "that renews.{one_trial}"),
            ("What does a credit buy?",
             "An image ad is {image} credits, a 5-second product video {video5}, an 8-second talking video {talk8} "
             "and a 10-second one {talk10}. Plans are ${starter_usd}, ${growth_usd} and ${scale_usd} a month; packs "
             "are {pack_usd}."),
            ("Can I use the ads in paid campaigns?",
             "Yes, in your own marketing, including paid ads. You're responsible for what you post; see the "
             "[terms](/terms)."),
        ],
        "related": ["ai-ugc-ad-generator", "arcads-alternative", "creatify-alternative"],
    },
    "adcreative-ai-alternative": {
        "query": "AdCreative.ai alternative",
        "nav": "AdCreative.ai alternative",
        "title": "AdCreative.ai Alternative, Priced First · Campaignify",
        "description": "Comparing AdCreative.ai alternatives? Image and video ads from a product page, nothing charged, "
                       "no auto-billing trial, price first. Try {sc_images} image ads free.",
        "eyebrow": "Comparing tools",
        "h1": "Campaignify is an AdCreative.ai alternative with nothing charged at sign-up, no auto-billing trial, and every ad priced first",
        "answer": "Campaignify makes image ads, product videos and UGC-style talking videos from your product page, "
                  "then writes the copy for each platform. {signup_charge}, and there's no trial that turns into a "
                  "plan: you start with {sc_images} free image ads, see every ad's price in credits before you "
                  "generate, and buy a pack or a plan only when you want to. The AdCreative.ai facts below are quoted "
                  "from AdCreative.ai's site, with the date it was read.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's name, description, price and photos from its page. You approve the "
             "claims and pick the photos."),
            ("See the price, then generate",
             "Pick an image ad, a product video or a UGC presenter video. The price in credits is on the button "
             "before anything runs."),
            ("Pay only when you want more",
             "Your free credits cover {sc_images} image ads. After that, a one-time pack from ${pack_min_usd} or a plan "
             "from ${starter_usd} a month, cancel anytime."),
        ],
        "table": {
            "competitor": "AdCreative.ai",
            "source": "https://www.adcreative.ai/",
            "source_name": "AdCreative.ai's site (its pricing section)",
            "date": "2026-11-03",
            "rows": [
                ("Trying it",
                 "\"7 day free trial. Cancel anytime.\" with 20 credits. Whether a card is needed, and whether the "
                 "trial turns into a paid plan by itself: not stated on adcreative.ai (as of 2026-11-03); the page "
                 "says \"We'll remind you before trial ends\".",
                 "Free sign-up, {nothing_charged_lc}, no end date: {sc} credits ({sc_images} image ads).{one_trial} Nothing "
                 "starts billing by itself."),
                ("What a plan costs",
                 "Monthly plans listed at $39 (Starter, 20 credits, 1 brand), $249 (Professional, 300 credits) and "
                 "$999 (Ultimate, 300 credits, 25 brands); quarterly and yearly discounts shown.",
                 "Plans at ${starter_usd}, ${growth_usd} and ${scale_usd} a month ({starter_credits} credits on Starter), "
                 "or a one-time pack from ${pack_min_usd} with {pack_min_credits} credits that never expire."),
                ("What an ad costs",
                 "Credits per plan are listed; a per-ad credit price is not listed on the page (as of 2026-11-03).",
                 "A fixed price per ad, shown before you generate: {image} credits an image ad, {video5} a 5-second "
                 "product video, {talk8} an 8-second talking video. Failed generations are refunded."),
            ],
        },
        "h2": {"how": "How this AdCreative.ai alternative works", "compare": "AdCreative.ai and Campaignify, side by side",
               "faq": "AdCreative.ai alternative questions"},
        "compare": [
            ("Is there a trial that turns into a subscription?",
             "No. {signup_charge}, and the free credits have no end date. A plan starts only when you choose one, and you can cancel "
             "it anytime."),
            ("Do I see the price before I generate?",
             "Yes, in credits, on the button, and failed generations are refunded automatically."),
            ("Does it make video as well as image ads?",
             "Yes: image ads in {image_shapes}, 5 or 10 second product videos, UGC-style talking videos with "
             "{presenters} AI presenters, and motion copied from a reference clip."),
            ("What about the copy?",
             "On Starter and up, the copy studio writes every text field each platform takes, sized to its display "
             "lengths, with compliance checks on every variant."),
        ],
        "limits": [
            "It makes AI presenters only; it doesn't book or film real creators.",
            "It doesn't score ads, buy media, post ads or read results from the ad platforms.",
        ],
        "media": [("site/headphones.jpg", "Blue over-ear headphones floating against a bright orange background."),
                  ("site/ugc-app.jpg", "An AI presenter at a desk shows a to-do list app on his phone."),
                  ("site/candle.jpg", "A lit candle in an amber glass jar beside dried eucalyptus.")],
        "faq": [
            ("Is Campaignify affiliated with AdCreative.ai?",
             "No. Campaignify is an independent product with no connection to them. AdCreative.ai is a trademark of "
             "its owner. The AdCreative.ai facts on this page are quoted from AdCreative.ai's site with the date it "
             "was read; check their site for today's terms."),
            ("Can I try Campaignify before I pay?",
             "Yes. A new account gets {sc} free credits, enough for {sc_images} image ads, {with_nothing_charged} and no trial "
             "that renews.{one_trial}"),
            ("What does a credit buy?",
             "An image ad is {image} credits, a 5-second product video {video5}, an 8-second talking video {talk8} "
             "and a 10-second one {talk10}. Plans are ${starter_usd}, ${growth_usd} and ${scale_usd} a month; packs "
             "are {pack_usd}."),
            ("Can I use the ads in paid campaigns?",
             "Yes, in your own marketing, including paid ads. You're responsible for what you post; see the "
             "[terms](/terms)."),
        ],
        "related": ["ai-product-ads-for-shopify", "ai-ad-copy-generator", "creatify-alternative"],
    },
}
SOLUTION_ORDER = ["ai-ugc-ad-generator", "arcads-alternative", "ai-product-ads-for-shopify", "ai-ad-copy-generator",
                  "ai-marketing-platform-for-small-business", "creatify-alternative", "makeugc-alternative", "adcreative-ai-alternative"]

# The pages added for the SEO launch (comparisons, use cases, guides) live in
# public_pages.py under the same rules; they join the lists here.
import public_pages  # noqa: E402

SOLUTIONS.update(public_pages.SOLUTIONS)
SOLUTION_ORDER += public_pages.SOLUTION_ORDER
COMPARE = ["arcads-alternative", "creatify-alternative", "makeugc-alternative", "adcreative-ai-alternative",
           "blaze-alternative", "predis-ai-alternative", "heygen-alternative", "topview-alternative"]
GUIDES = public_pages.GUIDES
GUIDE_ORDER = public_pages.GUIDE_ORDER
GUIDES_INDEX = public_pages.GUIDES_INDEX


def guide_path(slug: str) -> str:
    return GUIDES[slug].get("path") or f"/guides/{slug}"


def _fmt_block(b, f):
    if isinstance(b, str):
        return b.format(**f)
    kind, items = b
    return (kind, items.format(**f) if isinstance(items, str) else [x.format(**f) for x in items])


def guide(slug: str) -> dict:
    """A guide (or the roundup) with its copy filled in from the live numbers."""
    g, f = GUIDES[slug], facts()
    out = dict(g, slug=slug, path=guide_path(slug))
    for k in ("title", "description", "h1", "answer"):
        out[k] = g[k].format(**f)
    out["sections"] = [(h.format(**f), re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-"), [_fmt_block(b, f) for b in blocks])
                       for h, blocks in g["sections"]]
    out["faq"] = _fmt(g["faq"], f)
    if "table" in g:
        t = dict(g["table"])
        t["rows"] = [tuple(c.format(**f) for c in row) for row in g["table"]["rows"]]
        out["table"] = t
    return out


def footer_groups() -> list:
    """The footer's link columns: (heading, [(href, label)])."""
    feats = [s for s in public_pages.FEATURE_PAGES if s in SOLUTIONS]
    inds = [s for s in public_pages.INDUSTRY_PAGES if s in SOLUTIONS]
    inds += [s for s in SOLUTION_ORDER if s not in COMPARE and s not in feats and s not in inds]   # nothing is left out
    comp = COMPARE + [k for k in GUIDE_ORDER if GUIDES[k].get("path")]
    guides = [k for k in GUIDE_ORDER if not GUIDES[k].get("path")]
    return [("Features", [(f"/{s}", SOLUTIONS[s]["nav"]) for s in feats]),
            ("Industries", [(f"/{s}", SOLUTIONS[s]["nav"]) for s in inds]),
            ("Compare", [(f"/{s}", SOLUTIONS[s]["nav"]) if s in SOLUTIONS else (guide_path(s), GUIDES[s]["nav"])
                         for s in comp]),
            ("Guides", [("/guides", "All guides")] + [(guide_path(k), GUIDES[k]["nav"]) for k in guides]
             + [(PAGES["hook_tool"][0], "Free AI hook generator")])]


def solution(slug: str) -> dict:
    """A solution page with its copy filled in from the live numbers."""
    s, f = SOLUTIONS[slug], facts()
    out = dict(s, slug=slug)
    out["answer"] = s["answer"].format(**f)
    out["description"] = s["description"].format(**f)
    out["steps"] = _fmt(s["steps"], f)
    for key in ("gets", "compare", "faq"):
        if key in s:
            out[key] = _fmt(s[key], f)
    if "limits" in s:
        out["limits"] = [x.format(**f) for x in s["limits"]]
    if "price_line" in s:
        out["price_line"] = s["price_line"].format(**f)
    if "table" in s:
        t = dict(s["table"])
        t["rows"] = [(label, theirs.format(**f), ours.format(**f)) for label, theirs, ours in s["table"]["rows"]]
        out["table"] = t
    q = s["query"]
    out["h2"] = {"how": f"How the {q} works", "gets": f"What you get with the {q}", "compare": f"{q}: side by side",
                 "faq": f"{q} questions", **s.get("h2", {})}
    return out


# "Related pages" blocks on the public pages that aren't answer-first pages.
RELATED = {
    "features": ["ai-marketing-platform-for-small-business", "plan-a-campaign-with-ai", "research-competitor-ads"],
    "pricing": ["credit-based-ai-ad-pricing", "best-ai-ugc-ad-generators", "ai-marketing-platform-for-small-business"],
    "for_reps": ["ai-ugc-ad-generator", "ftc-meta-rules-for-ai-ads", "ai-ads-for-coaches"],
}


def card(key: str) -> dict:
    """{href, title, description} for a related-pages card (an answer-first page or a guide)."""
    if key.startswith("/"):
        ep = next(e for e, v in PAGES.items() if v[0] == key)
        m = meta(ep, {})
        return {"href": key, "title": m["title"].split(" · ")[0].split(": ")[0], "description": m["description"]}
    if key in GUIDES:
        g = guide(key)
        return {"href": g["path"], "title": g["nav"], "description": g["description"]}
    s = solution(key)
    return {"href": f"/{key}", "title": s["nav"], "description": s["description"]}


def related_cards(endpoint: str) -> list[dict]:
    return [card(k) for k in RELATED.get(endpoint, [])]


# ---------------------------------------------------------------- the showcase: real outputs on the home page (M15)
# static/showcase/manifest.json lists real pipeline outputs: {"items": [{"file", "kind": "image" | "video", "preset",
# "caption", "alt", and optionally "poster" (a video's still) and "aspect"}]}. The files sit in the same folder.
# Nothing else is ever shown there: with no items the page draws neutral tiles that only say which shapes exist.
SHOWCASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "showcase")
SHOWCASE_MANIFEST = "manifest.json"
SHOWCASE_MAX = 12
SHOWCASE_EXT = {"image": (".jpg", ".jpeg", ".png", ".webp", ".avif"), "video": (".mp4", ".webm")}
SHOWCASE_ASPECTS = {"9:16": (9, 16), "4:5": (4, 5), "1:1": (1, 1), "16:9": (16, 9)}
# What the tiles stand for while the manifest is empty: the kinds and shapes Campaignify makes (catalog.TOOLS).
SHOWCASE_SHAPES = [("Image ad", "4:5"), ("Presenter video", "9:16"), ("Marketplace image", "1:1"), ("Image ad", "9:16"),
                   ("Product video", "9:16"), ("Offer image", "4:5"), ("Voiceover video", "9:16"), ("Image ad", "1:1")]
_showcase = {"key": None, "items": []}


def _showcase_file(name) -> str | None:
    """`name` as a path inside the showcase folder that exists, else None (no folders above it, no absolute paths)."""
    name = str(name or "").strip().replace("\\", "/")
    if not name or name.startswith("/") or ".." in name.split("/") or name == SHOWCASE_MANIFEST:
        return None
    return name if os.path.isfile(os.path.join(SHOWCASE_DIR, name)) else None


def _image_size(path: str):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001  (an unreadable picture is skipped, never an error page)
        return None


def showcase(limit: int = SHOWCASE_MAX) -> list[dict]:
    """The home page's gallery, from the manifest: [{"src" (under static/), "kind", "preset", "preset_name",
    "caption", "alt", "aspect", "width", "height", "poster"}]. An item is dropped when its file is missing, its
    kind or extension is wrong, or it has neither a caption nor an alt text. Read again when the manifest changes."""
    path = os.path.join(SHOWCASE_DIR, SHOWCASE_MANIFEST)
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return []
    if _showcase["key"] != key:
        items = []
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        import presets
        for it in (raw.get("items") if isinstance(raw, dict) else None) or []:
            if not isinstance(it, dict):
                continue
            kind, name = str(it.get("kind") or "").lower(), _showcase_file(it.get("file"))
            caption, alt = " ".join(str(it.get("caption") or "").split())[:140], " ".join(str(it.get("alt") or "").split())[:220]
            if kind not in SHOWCASE_EXT or name is None or not name.lower().endswith(SHOWCASE_EXT[kind]) or not (caption or alt):
                continue
            pre = presets.get(str(it.get("preset") or ""))
            aspect = str(it.get("aspect") or "") if str(it.get("aspect") or "") in SHOWCASE_ASPECTS else ""
            size = _image_size(os.path.join(SHOWCASE_DIR, name)) if kind == "image" else None
            if kind == "image" and size is None:
                continue
            if size is None:
                aspect = aspect or (pre.aspect if pre is not None and pre.aspect in SHOWCASE_ASPECTS else "9:16")
                size = tuple(x * 120 for x in SHOWCASE_ASPECTS[aspect])
            poster = _showcase_file(it.get("poster")) if kind == "video" else None
            if poster is not None and not poster.lower().endswith(SHOWCASE_EXT["image"]):
                poster = None
            items.append({"src": "showcase/" + name, "kind": kind, "preset": pre.id if pre is not None else "",
                          "preset_name": pre.name if pre is not None else "", "caption": caption, "alt": alt or caption,
                          "aspect": aspect, "width": size[0], "height": size[1],
                          "poster": "showcase/" + poster if poster else ""})
        _showcase.update(key=key, items=items)
    return list(_showcase["items"][:max(0, limit)])


# ---------------------------------------------------------------- per-page meta

PAGES = {  # endpoint -> (path, title, description)
    "index": ("/", "Campaignify: AI UGC Ads and Your Whole Campaign",
              "UGC-style video and image ads from a product page, then the copy, landing page and launch checklist. "
              "Free sign-up includes {sc_images} image ads, nothing charged."),
    "features": ("/features", "Features: Every Campaignify Tool, Step by Step",
                 "Every Campaignify tool in the order a campaign runs: plan, make the ads, write the copy, get found, "
                 "launch, capture leads, grow and measure."),
    "pricing": ("/pricing", "Pricing: Plans From ${starter} a Month · Campaignify",
                "Free sign-up includes {sc_images} image ads, nothing charged. Plans at ${starter}, ${growth} and ${scale} a month, or "
                "one-time credit packs. See every price before you generate."),
    "hook_tool": ("/tools/ai-hook-generator", "Free AI Hook Generator for Ads · Campaignify",
                  "Type what you sell and who it is for, and get eight opening lines for TikTok, Reels and Facebook "
                  "ads. Free, no sign-up."),
    "for_reps": ("/for-reps", "For Independent Reps: Compliance Checks · Campaignify",
                 "Selling as an independent rep? Campaignify blocks income, recruiting and health claims and invented discounts in "
                 "every brief, copy variant and page."),
    "llm_info": ("/llm-info", "Campaignify for AI Assistants (llms.txt) · Campaignify",
                 "What Campaignify is, what it makes, what each ad costs in credits and which plan includes each tool, "
                 "in plain text for AI assistants and search engines."),
    "privacy": ("/privacy", "Privacy Policy · Campaignify",
                "What Campaignify collects to run your account and make your ads, the services it uses, the Google "
                "Ads tag, and how to see or delete your data."),
    "terms": ("/terms", "Terms of Service · Campaignify",
              "The rules for using Campaignify: your account, credits and plans, what you can't make with it, and "
              "who's responsible for the ads you post."),
    # /mcp: browsers get the MCP page there (mcpserver.wants_page), so its meta is keyed by the MCP route's endpoint.
    "mcp.mcp_endpoint": ("/mcp", "Campaignify MCP: Make Ads From Claude · Campaignify",
                    "Connect Campaignify to Claude and make ads by asking: price and make image ads and AI presenter "
                    "videos, write copy, plan a campaign. Credits shown first, nothing made until you agree."),
    "mcp_docs": ("/docs/mcp", "Connect Claude and MCP Clients · Campaignify",
                 "Connect Campaignify to Claude and other MCP clients: price and make image ads and AI presenter "
                 "videos, ad copy and campaign plans from a chat."),
    "messaging.policy": ("/messaging-policy", "Messaging Policy for Emails and Texts · Campaignify",
                         "The rules for every email and text sent through Campaignify flows: consent, opt-outs, "
                         "content limits and what happens when they're broken."),
}


def sitemap_paths() -> list[str]:
    paths = [PAGES[e][0] for e in ("index", "features", "pricing")]
    paths += [f"/{s}" for s in SOLUTION_ORDER]
    paths += ["/guides"] + [guide_path(k) for k in GUIDE_ORDER]
    import hooktool
    if hooktool.enabled():
        paths += [PAGES["hook_tool"][0]]
    paths += [PAGES[e][0] for e in ("for_reps", "llm_info", "privacy", "terms", "messaging.policy")]
    import mcpserver
    if mcpserver.enabled():   # /docs/mcp is noindex until the MCP server is on (SEO-5)
        paths += [PAGES["mcp.mcp_endpoint"][0], PAGES["mcp_docs"][0]]
    return paths


def lastmod() -> str:
    """The sitemap's <lastmod>: the day the public copy (this module, the templates,
    the site media) last changed on disk, which on the server is the deploy."""
    import datetime
    here = os.path.dirname(os.path.abspath(__file__))
    newest = 0.0
    for folder, names in ((here, ("public_site.py", "public_pages.py", "app.py")), (os.path.join(here, "web"), None)):
        for n in (names or os.listdir(folder)):
            try:
                newest = max(newest, os.path.getmtime(os.path.join(folder, n)))
            except OSError:
                pass
    return datetime.datetime.fromtimestamp(newest, datetime.timezone.utc).strftime("%Y-%m-%d")


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)")


def text_blocks(text: str) -> list:
    """The llms.txt text as (kind, content) blocks for the HTML mirror (/llm-info):
    ("h1" | "h2", title), ("p", Markup), ("ul", [Markup, ...]). Links become <a>."""
    def inline(line: str) -> Markup:
        return Markup(_MD_LINK.sub(lambda m: f'<a href="{escape(m.group(2))}">{escape(m.group(1))}</a>', str(escape(line))))

    out, items = [], []

    def flush():
        if items:
            out.append(("ul", list(items)))
            items.clear()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("# "):
            flush()
            out.append(("h1", line[2:]))
        elif line.startswith("## "):
            flush()
            out.append(("h2", line[3:]))
        elif line.startswith("- "):
            items.append(inline(line[2:]))
        elif re.match(r"^\d+\. ", line):
            items.append(inline(line.split(". ", 1)[1]))
        else:
            flush()
            out.append(("p", inline(line[2:] if line.startswith("> ") else line)))
    flush()
    return out


def absolute(path: str) -> str:
    return SITE_URL + path


def meta(endpoint: str | None, view_args: dict | None) -> dict | None:
    """title, description, url (canonical) and jsonld for a public page, or None."""
    if endpoint == "solution" and (view_args or {}).get("slug") in SOLUTIONS:
        s = solution(view_args["slug"])
        path, title, desc = f"/{s['slug']}", s["title"], s["description"]
    elif endpoint == "guide" and (view_args or {}).get("slug") in GUIDES:
        g = guide(view_args["slug"])
        path, title, desc = g["path"], g["title"], g["description"]
    elif endpoint == "guides":
        path, title, desc = "/guides", GUIDES_INDEX["title"], GUIDES_INDEX["description"].format(**facts())
    elif endpoint in PAGES:
        path, title, desc = PAGES[endpoint]
        prices = {c: plans.PLANS[c]["usd"] for c in ("starter", "growth", "scale")}
        title, desc = title.format(**prices), desc.format(sc_images=facts()["sc_images"], **prices)
    else:
        return None
    url = absolute(path)
    return {"title": title, "description": desc, "url": url, "image": absolute("/static/" + OG_IMAGE),
            "image_alt": OG_IMAGE_ALT, "jsonld": jsonld(endpoint, view_args, url, title, desc)}


# ---------------------------------------------------------------- JSON-LD

def _org():
    return {"@type": "Organization", "@id": SITE_URL + "/#org", "name": "Campaignify", "url": SITE_URL + "/",
            "email": CONTACT, "logo": absolute("/static/img/campaignify-icon-512.png")}


def _website():
    return {"@type": "WebSite", "@id": SITE_URL + "/#website", "name": "Campaignify", "url": SITE_URL + "/",
            "publisher": {"@id": SITE_URL + "/#org"}, "inLanguage": "en"}


def offers() -> list[dict]:
    """One Offer per plan, from plans.PLANS (free + the paid monthly plans)."""
    out = [{"@type": "Offer", "name": "Free", "price": "0", "priceCurrency": "USD",
            "description": f"{catalog.starter_credits():,} starter credits (free sign-up includes "
                           f"{catalog.starter_credits() // max(catalog.rates()['image'], 1)} image ads) and the ad studio",
            "url": absolute("/pricing")}]
    for p in plans.PAID:
        out.append({"@type": "Offer", "name": f"{p['name']} plan", "price": str(p["usd"]), "priceCurrency": "USD",
                    "description": f"{p['credits']:,} credits a month. {p['line']}", "url": absolute("/pricing"),
                    "priceSpecification": {"@type": "UnitPriceSpecification", "price": str(p["usd"]),
                                           "priceCurrency": "USD", "unitCode": "MON", "unitText": "month"}})
    return out


SOFTWARE_DESCRIPTION = ("An AI marketing platform for small businesses: an AI campaign planner, competitor research, "
                        "UGC-style video ads with AI presenters and image ads from a product page, ad copy for every "
                        "platform, an SEO, AEO and GEO optimizer, landing pages, smart links, forms and contacts, email "
                        "and SMS follow-up flows, and community and courses. Every ad's cost is shown in credits "
                        "before it is made.")


def software():
    return {"@type": "SoftwareApplication", "@id": SITE_URL + "/#app", "name": "Campaignify",
            "applicationCategory": "BusinessApplication", "operatingSystem": "Web", "url": SITE_URL + "/",
            "description": SOFTWARE_DESCRIPTION, "publisher": {"@id": SITE_URL + "/#org"},
            "featureList": [f["name"] for f in plans.FEATURES.values() if f["shipped"]], "offers": offers()}


def faq_page(url: str, faq) -> dict:
    return {"@type": "FAQPage", "@id": url + "#faq", "url": url,
            "mainEntity": [{"@type": "Question", "name": q,
                            "acceptedAnswer": {"@type": "Answer", "text": answer_text(a)}} for q, a in faq]}


def jsonld(endpoint, view_args, url, title, desc) -> str:
    graph = [_org(), _website(),
             {"@type": "WebPage", "@id": url, "url": url, "name": title, "description": desc,
              "isPartOf": {"@id": SITE_URL + "/#website"}, "inLanguage": "en"}]
    if endpoint in ("index", "pricing", "features", "solution"):
        graph.append(software())
        graph[2]["about"] = {"@id": SITE_URL + "/#app"}
    if endpoint == "index":
        graph.append(faq_page(url, home_faq()))
    elif endpoint == "pricing":
        graph.append(faq_page(url, pricing_faq()))
    elif endpoint == "solution":
        s = solution(view_args["slug"])
        graph.append(faq_page(url, s["faq"]))
    elif endpoint == "guide":
        g = guide(view_args["slug"])
        graph.append({"@type": "Article", "@id": url + "#article", "headline": g["h1"], "description": desc,
                      "url": url, "mainEntityOfPage": {"@id": url}, "datePublished": g["published"],
                      "dateModified": g.get("updated", g["published"]), "inLanguage": "en",
                      "author": {"@id": SITE_URL + "/#org"}, "publisher": {"@id": SITE_URL + "/#org"},
                      "about": {"@id": SITE_URL + "/#app"} if g.get("about_app") else None,
                      "citation": [u for _, u in g.get("sources", [])] or None})
        graph[-1] = {k: v for k, v in graph[-1].items() if v is not None}
        graph.append(faq_page(url, g["faq"]))
    elif endpoint == "guides":
        graph.append({"@type": "ItemList", "@id": url + "#list", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "url": absolute(guide_path(k)), "name": GUIDES[k]["nav"]}
            for i, k in enumerate(GUIDE_ORDER)]})
    if endpoint == "guide":
        g = guide(view_args["slug"])
        graph.append({"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Guides", "item": SITE_URL + "/guides"},
            {"@type": "ListItem", "position": 3, "name": g["nav"], "item": url}]})
    elif endpoint != "index":
        name = title.split(" · ")[0].split(": ")[0]
        graph.append({"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": name, "item": url}]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")
