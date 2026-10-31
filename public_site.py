"""The public site's search metadata and content: titles, descriptions, canonical
URLs, share cards, schema.org JSON-LD, the sitemap list, the FAQs and the
answer-first pages for search intent (/ai-ugc-ad-generator and friends).

Everything here describes what Campaignify does today (plans.FEATURES,
catalog.TOOLS, catalog.rates()); prices come from the plans and the rate card,
never typed in. No invented customers, numbers or testimonials, and nothing about
other companies' products.
"""
import json
import os
import re

from markupsafe import Markup, escape

import catalog
import plans
import platforms

SITE_URL = (os.environ.get("RS_SITE_URL") or "https://campaignify.ai").rstrip("/")
OG_IMAGE = "img/og-campaignify.png"
OG_IMAGE_ALT = "campaignify: AI UGC ads from your product page, beside three phones showing AI-made ads."
CONTACT = "hello@campaignify.ai"


def usd(credits: int) -> str:
    cents = int(credits or 0)
    return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def facts() -> dict:
    """Live numbers for the copy (str.format keys)."""
    r, sc = catalog.rates(), catalog.starter_credits()
    p = plans.PLANS
    names = [platforms.PLATFORMS[k]["name"] for k in platforms.ORDER if k not in ("email", "landing")]
    talk = catalog.TOOLS["ugc_video"]
    return {"talk8": f"{r['talk8']:,}", "talk8_usd": usd(r["talk8"]), "talk10": f"{r['talk10']:,}",
            "talk10_usd": usd(r["talk10"]), "image": r["image"], "image_usd": usd(r["image"]),
            "video5_usd": usd(r["video5"]), "video10_usd": usd(r["video10"]),
            "sc": f"{sc:,}", "sc_images": sc // max(r["image"], 1),
            "starter_usd": p["starter"]["usd"], "growth_usd": p["growth"]["usd"], "scale_usd": p["scale"]["usd"],
            "starter_credits": f"{p['starter']['credits']:,}",
            "talk_lengths": ", ".join(str(d) for d in talk["durations"][:-1]) + f" or {talk['durations'][-1]}",
            "talk_max": talk["durations"][-1], "talk_shapes": ", ".join(talk["aspects"][:-1]) + f" or {talk['aspects'][-1]}",
            "image_shapes": ", ".join(catalog.TOOLS["ad_image"]["aspects"]),
            "presenters": len(catalog.PRESENTERS), "max_batch": _max_batch(),
            "platform_list": ", ".join(names[:-1]) + f" and {names[-1]}", "platform_count": len(names)}


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
     "1 credit is $0.01. An image ad is {image} credits, a 5-second video {video5}, a 10-second video {video10}, and "
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
            ("Prices before you press Generate", "Paid in credits (1 credit = $0.01): an 8-second talking video is "
                                                 "{talk8} credits ({talk8_usd}) at 720p. Failed generations are "
                                                 "refunded."),
            ("Compliance checks on every brief", "Income, health, discount and recruiting claims are blocked, and "
                                                 "AI presenters never speak as customers."),
            ("Sizes for every feed", "On Starter and up, export each finished ad at each platform's sizes and get "
                                     "step-by-step setup for eight ad platforms."),
        ],
        "media": [("site/ugc-coffee.jpg", "An AI presenter in a sunny kitchen holds up a coffee bag."),
                  ("site/ugc-serum.jpg", "An AI presenter in a bright bathroom shows a face serum."),
                  ("site/ugc-sneakers.jpg", "An AI presenter in a gym shows off a sage-green running sneaker.")],
        "faq": [
            ("What is a UGC ad?",
             "A UGC (user-generated content) ad looks like a post someone filmed on their phone: a person holding the "
             "product and talking to camera. Campaignify makes that style with AI presenters. They're described "
             "characters generated with each video, not real people, and they don't pose as customers or describe "
             "personal results."),
            ("How much does an AI UGC video cost?",
             "Talking videos are paid in credits, 1 credit = $0.01: an 8-second video is {talk8} credits ({talk8_usd}) "
             "and a 10-second one {talk10} credits ({talk10_usd}) at 720p. New accounts get {sc} free credits, enough "
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
        "related": ["ai-product-ads-for-shopify", "ai-ad-copy-generator", "arcads-alternative"],
    },
    "arcads-alternative": {
        "query": "Arcads alternative",
        "nav": "Arcads alternative",
        "title": "Arcads Alternative for AI UGC Video Ads · Campaignify",
        "description": "Comparing Arcads alternatives? Campaignify makes UGC-style video and image ads, copy and a "
                       "landing page from your product page. Try {sc_images} image ads free.",
        "eyebrow": "Comparing tools",
        "h1": "Looking for an Arcads alternative?",
        "answer": "Campaignify is an AI ad studio that makes UGC-style video ads and image ads from your product page, "
                  "and keeps the copy, landing page and launch checklist in the same workspace. Free sign-up includes "
                  "{sc_images} image ads with no card, and you see the price of every ad before it's made. This page only describes what "
                  "Campaignify does, so you can hold it up against Arcads or any other tool on your list.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's name, description, price and photos from its page. You approve the "
             "claims and pick the photos."),
            ("Make a test ad",
             "Pick a UGC presenter video, a voiceover video or an image ad, let AI write the script, and check the "
             "price before you generate."),
            ("Judge it on your own product",
             "Compare the result with what you get elsewhere. Your free credits cover {sc_images} image ads; talking "
             "videos start at {talk8_usd}."),
        ],
        "compare": [
            ("Can it start from my product page?",
             "Yes. Paste the link and it fills in the product from the page; Shopify product pages are recognised. "
             "You can also upload photos."),
            ("Do I see the price before I generate?",
             "Yes, in credits (1 credit = $0.01), and failed generations are refunded automatically."),
            ("Who are the presenters?",
             "{presenters} AI presenters, each drawn from a written description with every video. They're not real "
             "people, so two videos with the same presenter look alike but not identical."),
            ("What else does it make?",
             "Image ads in {image_shapes}; 5 or 10 second product videos; motion copied from a reference clip; and on "
             "paid plans, copy for {platform_count} ad platforms, a campaign planner and landing pages."),
            ("What stops risky claims?",
             "Every brief, piece of copy and landing page is checked for income, health, discount and recruiting "
             "claims before it goes out."),
            ("What does it cost?",
             "Free to start with {sc} credits and no card. Plans are ${starter_usd}, ${growth_usd} and ${scale_usd} a "
             "month with monthly credits; credit packs top up any account."),
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
             "No. Campaignify is an independent product made by Rector & Stull LLC. Arcads is a trademark of its "
             "owner, and this page doesn't describe their product, pricing or results."),
            ("Can I try Campaignify before I pay?",
             "Yes. A new account gets {sc} free credits, enough for {sc_images} image ads, with no card. Buy a credit "
             "pack or pick a plan when you want to make more."),
            ("How long can a talking video be?",
             "{talk_lengths} seconds, in {talk_shapes}. Make a batch of variants to test different hooks and "
             "presenters."),
            ("Can I use the ads in paid campaigns?",
             "Yes, you can use the ads you make in your own marketing, including paid ads. You're responsible for "
             "what you post; see the [terms](/terms)."),
        ],
        "related": ["ai-ugc-ad-generator", "ai-product-ads-for-shopify", "ai-ad-copy-generator"],
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
                  "each ad first, and download finished ads for Meta, TikTok, Pinterest and the other feeds.",
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
            ("Product videos", "5 or 10 second clips that bring one product photo to life, from {video5_usd}."),
            ("UGC-style talking videos", "An AI presenter holds your product and says a script written for it, "
                                         "from {talk8_usd}."),
            ("Every product in one place", "Each product keeps its photos, offer, approved claims and audience; "
                                           "Growth and Scale add more brands."),
            ("Copy and a landing page", "On Starter and up: ad copy for each platform's fields and a landing page "
                                        "published on campaignify.ai."),
            ("Checks before anything runs", "Prices shown before you generate, failures refunded, and income, health "
                                            "and discount claims blocked."),
        ],
        "media": [("site/show-amber.jpg", "A gold coffee bag floating before a wall of type."),
                  ("site/headphones.jpg", "Blue over-ear headphones floating against a bright orange background."),
                  ("site/candle.jpg", "A lit candle in an amber glass jar beside dried eucalyptus.")],
        "faq": [
            ("Do I need to install a Shopify app?",
             "No. Paste the product's link; Campaignify reads the public page. Nothing is added to your store and it "
             "never asks for store access."),
            ("Does it work with stores that aren't on Shopify?",
             "Yes. It reads any product page that states its details plainly (product data, share tags and images), "
             "and you can always upload photos and type the details yourself."),
            ("Will the ads look exactly like my product?",
             "They're generated from your product photos, but AI can still get details wrong, such as small label "
             "text. Check every ad before you post it. Failed generations are refunded; finished ones are charged."),
            ("How much does a Shopify product ad cost?",
             "An image ad is {image} credits ({image_usd}), a 5-second product video {video5_usd} and an 8-second "
             "talking video {talk8_usd}. New accounts get {sc} free credits; plans start at ${starter_usd} a month."),
            ("Can it run the ads or sync them to my store?",
             "No. You download the ads and launch them in each ad platform's ads manager. Campaignify doesn't change "
             "your store."),
        ],
        "related": ["ai-ugc-ad-generator", "ai-ad-copy-generator", "arcads-alternative"],
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
            ("Compliance checks", "Every variant is checked for income, health, discount and recruiting claims."),
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
             "block income, health, discount and recruiting claims. Still read each line before you use it."),
            ("Can it improve copy I already have?",
             "Yes. On Growth and up the content optimizer scores a draft for a platform (hook, clarity, call to "
             "action, fit and compliance) for free, and an AI review explains the biggest fix and writes three "
             "rewrites."),
        ],
        "related": ["ai-ugc-ad-generator", "ai-product-ads-for-shopify", "arcads-alternative"],
    },
}
SOLUTION_ORDER = ["ai-ugc-ad-generator", "arcads-alternative", "ai-product-ads-for-shopify", "ai-ad-copy-generator"]


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
    return out


# ---------------------------------------------------------------- per-page meta

PAGES = {  # endpoint -> (path, title, description)
    "index": ("/", "Campaignify: AI UGC Ads and Your Whole Campaign",
              "UGC-style video and image ads from a product page, then the copy, landing page and launch checklist. "
              "Free sign-up includes {sc_images} image ads, no card."),
    "features": ("/features", "Features: Every Campaignify Tool, Step by Step",
                 "Every Campaignify tool in the order a campaign runs: plan, make the ads, write the copy, get found, "
                 "launch, capture leads, grow and measure."),
    "pricing": ("/pricing", "Pricing: Plans From ${starter} a Month · Campaignify",
                "Free sign-up includes {sc_images} image ads, no card. Plans at ${starter}, ${growth} and ${scale} a month, or "
                "one-time credit packs. See every price before you generate."),
    "privacy": ("/privacy", "Privacy Policy · Campaignify",
                "What Campaignify collects to run your account and make your ads, the services it uses, the Google "
                "Ads tag, and how to see or delete your data."),
    "terms": ("/terms", "Terms of Service · Campaignify",
              "The rules for using Campaignify: your account, credits and plans, what you can't make with it, and "
              "who's responsible for the ads you post."),
    "messaging.policy": ("/messaging-policy", "Messaging Policy for Emails and Texts · Campaignify",
                         "The rules for every email and text sent through Campaignify flows: consent, opt-outs, "
                         "content limits and what happens when they're broken."),
}


def sitemap_paths() -> list[str]:
    paths = [PAGES[e][0] for e in ("index", "features", "pricing")]
    paths += [f"/{s}" for s in SOLUTION_ORDER]
    paths += [PAGES[e][0] for e in ("privacy", "terms", "messaging.policy")]
    return paths


def absolute(path: str) -> str:
    return SITE_URL + path


def meta(endpoint: str | None, view_args: dict | None) -> dict | None:
    """title, description, url (canonical) and jsonld for a public page, or None."""
    if endpoint == "solution" and (view_args or {}).get("slug") in SOLUTIONS:
        s = solution(view_args["slug"])
        path, title, desc = f"/{s['slug']}", s["title"], s["description"]
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
            "email": CONTACT, "logo": absolute("/static/img/campaignify-icon-512.png"),
            "legalName": "Rector & Stull LLC"}


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


SOFTWARE_DESCRIPTION = ("An AI marketing studio: UGC-style video ads and image ads from a product page, ad copy for "
                        "every platform, a campaign planner, landing pages, smart links, forms and a community.")


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
    if endpoint != "index":
        name = title.split(" · ")[0].split(": ")[0]
        graph.append({"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": SITE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": name, "item": url}]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")
