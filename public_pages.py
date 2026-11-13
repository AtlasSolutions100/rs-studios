"""The public pages added for the SEO launch (Nov 4, 2026): 4 comparison pages,
4 use-case pages, the guides and a sourced roundup.

Same rules as public_site.py, which merges these in:
- Campaignify facts describe only what the app does today (plans.FEATURES,
  catalog rates via {placeholders}); costs are in credits, dollars only on plan
  and pack prices. No invented customers, numbers, ratings or results.
- Facts about other companies come only from the competitor research of
  Nov 4, 2026, with the page each one was read from, written as "as listed on
  Nov 4, 2026", and "not listed" where a page doesn't say. Their names are used
  only to describe them.
- AI presenters are always called AI presenters. Digits for numbers, no long
  dashes, and every page opens with a direct answer.
"""
from copyswitch import P


AS_OF = "2026-11-04"
AS_OF_LABEL = "Nov 4, 2026"

# Sources (all read on Nov 4, 2026 unless the name says otherwise).
SRC = {
    "blaze_pricing": ("Blaze pricing", "https://blaze.ai/pricing"),
    "blaze_growth": ("Blaze, Marketing done for you", "https://www.blaze.ai/growth"),
    "predis_pricing": ("Predis.ai pricing", "https://predis.ai/pricing/"),
    "predis_g2": ("G2, Predis.ai", "https://www.g2.com/products/predis-ai"),
    "heygen_pricing": ("HeyGen pricing", "https://www.heygen.com/pricing"),
    "topview_g2": ("G2, Topview AI Video Creator pricing", "https://www.g2.com/products/topview-ai-video-creator/pricing"),
    "creatify_pricing": ("Creatify pricing", "https://www.creatify.ai/pricing"),
    "creatify_about": ("Creatify about page", "https://creatify.ai/about"),
    "makeugc": ("MakeUGC, Make video ads with AI", "https://www.makeugc.ai/features/make-video-ads-with-ai"),
    "arcads_help": ("Arcads help center: How does Arcads pricing work? (read Nov 3, 2026)",
                    "https://help.arcads.ai/en/articles/17595066-how-does-arcads-pricing-work"),
    "arcads_eesel": ("eesel.ai, Arcads AI pricing in 2026 (updated Jun 17, 2026)", "https://eesel.ai/blog/arcads-ai-pricing"),
    "adcreative": ("AdCreative.ai, AI UGC videos page with pricing", "https://www.adcreative.ai/ai-ugc-videos"),
    "ftc_martech": ("MarTech, U.S. FTC ban on fake, AI-generated reviews",
                    "https://martech.org/u-s-ftc-ban-on-fake-ai-generated-reviews-takes-effect-oct-13"),
    "meta_labels": ("Marketing Dive, Meta adds updated disclosure tags for AI-generated ads (Jul 9, 2026)",
                    "https://marketingdive.com/news/sociable-meta-adds-updated-disclosure-tags-for-ai-generated-ads/824833"),
    "tiktok_idukki": ("Idukki, AI-generated UGC-style content vs. the real thing (Aug 5, 2026; updated Oct 14, 2026)",
                      "https://idukki.io/blog/ai-generated-ugc-style-content-vs-the-real-thing.md"),
    "meta_library": ("Meta Ad Library", "https://www.facebook.com/ads/library/"),
    "tiktok_cc": ("TikTok Creative Center, Top Ads",
                  "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"),
    "reddit_test": ("r/FacebookAds, I tested AI ads vs human UGC with $100K in 3 months (snapshot Mar 5, 2026)",
                    "https://reddit.sentinel-team.org/posts/1rlvjrg/snapshots/2026-03-05T23%3A31%3A13.407127Z"),
    "creatify_blog": ("Creatify blog, The 8 best AI UGC ad tools in 2026 (Aug 25, 2026)",
                      "https://creatify.ai/blog/the-8-best-ai-ugc-ad-tools-in-2026"),
    "heygen_trustpilot": ("Trustpilot, HeyGen reviews (page 2)", "https://www.trustpilot.com/review/heygen.com?page=2"),
    "makeugc_trustpilot": ("Trustpilot, MakeUGC reviews", "https://fi.trustpilot.com/review/makeugc.ai"),
    "adcreative_trustpilot": ("Trustpilot, AdCreative.ai reviews (page 3)", "https://www.trustpilot.com/review/adcreative.ai?page=3"),
    "arcads_trustpilot": ("Trustpilot, Arcads reviews (page 3)", "https://www.trustpilot.com/review/arcads.ai?page=3"),
}


def _src(*keys):
    return [SRC[k] for k in keys]


_NOT_AFFILIATED = ("No. Campaignify is an independent product. {name} is a trademark of its owner. The {name} facts on "
                   "this page are from the pages listed under the table, as listed on Nov 4, 2026; check their site "
                   "for today's terms.")
_TRY = ("Can I try Campaignify before I pay?",
        "Yes. {new_account_gets}, enough for {sc_images} image ads, {with_nothing_charged}{and_no_trial_renews}.{one_trial} {try_tail_more}")
_CANCEL = ("How do I cancel a Campaignify plan?",
           "From Billing in the app: press Manage or cancel plan. The plan stays on until the end of the month you've "
           "paid for. {packs_never_expire}.")
_LIMITS = [
    "It makes AI presenters only; it doesn't book or film real creators.",
    "A talking video is at most {talk_max} seconds long.",
    "It doesn't buy media, post ads or read results from the ad platforms; you launch the ads in each platform's "
    "ads manager.",
]


# ---------------------------------------------------------------- comparison and use-case pages
SOLUTIONS = {
    "blaze-alternative": {
        "query": "Blaze alternative",
        "nav": "Blaze alternative",
        "title": "Blaze Alternative With AI Presenter Ads · Campaignify",
        "description": "Comparing Blaze alternatives? Campaignify adds UGC-style AI presenter videos to ads, copy, "
                       "pages, forms and follow-up flows. {ads_free}.",
        "eyebrow": "Comparing tools",
        "h1": "Campaignify is a Blaze alternative that adds AI presenter video ads to the rest of your marketing",
        "answer": "Campaignify and Blaze both cover more than ads: planning, copy, SEO, landing pages and follow-up. "
                  "The main difference is video. Campaignify makes UGC-style video ads with AI presenters who hold "
                  "your product and say a script, and AI presenter video was not listed on the Blaze pages read on "
                  "Nov 4, 2026. Blaze lists post publishing and automated ad campaigns; Campaignify doesn't post or run ads. "
                  "{signup_includes} {sc_images} image ads, {with_nothing_charged}.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's name, description, price and photos from its page. You approve the "
             "claims and pick the photos every ad uses."),
            ("Plan, then make the ads",
             "On Starter and up the AI planner turns your offer, audience and budget into angles, scripts and a "
             "launch checklist. Then make AI presenter videos, product videos and image ads, each priced in credits "
             "before you generate."),
            ("Send the clicks somewhere",
             "Publish a landing page with a form, share smart links with your UTM tags, and follow up with a flow of "
             "emails and texts. You launch the ads yourself in each platform's ads manager."),
        ],
        "table": {
            "competitor": "Blaze",
            "source": "https://blaze.ai/pricing",
            "source_name": "Blaze's pricing page",
            "date": AS_OF,
            "sources": _src("blaze_growth"),
            "rows": [
                ("Trying it",
                 "A 7-day free trial, \"No credit card required\" (as listed on Nov 4, 2026).",
                 "{trying_no_end}"),
                ("Plans",
                 "Starter $79 a month (600 generation credits, 3 posting accounts, automated ad campaigns), Growth "
                 "$149 a month (1,500 credits, unlimited users) and a managed All-In-One tier at $999 a month, as "
                 "listed on Nov 4, 2026.",
                 "Starter ${starter_usd}, Growth ${growth_usd} and Scale ${scale_usd} a month, or one-time packs "
                 "from ${pack_min_usd}. No managed tier: you run it yourself."),
                ("What a credit buys",
                 "Listed credit costs: a static post 1, an email 3, a blog 3 and an AI video 15 (Nov 4, 2026).",
                 "A fixed price per ad, shown before you generate: {image} credits an image ad, {video5} a 5-second "
                 "product video, {talk8} an 8-second AI presenter video. Failed generations are refunded."),
                ("AI presenter (UGC) video",
                 "Not listed on blaze.ai/pricing or blaze.ai/growth (as of Nov 4, 2026).",
                 "{presenters} AI presenters hold your product and say your script, in {talk_lengths} second videos."),
                ("Posting and running ads",
                 "Lists content publishing to posting accounts and Google and Meta ad campaigns.",
                 "Doesn't post or run ads. It exports each ad at each platform's sizes; you launch it in that "
                 "platform's ads manager."),
            ],
        },
        "h2": {"how": "How this Blaze alternative works", "compare": "Blaze and Campaignify, side by side",
               "faq": "Blaze alternative questions"},
        "compare": [
            ("Does it cover more than ads?",
             "Yes: a campaign planner, competitor research, copy for {platform_count} ad platforms, an SEO, AEO and "
             "GEO optimizer, landing pages, smart links, forms and contacts, and email and text follow-up flows."),
            ("Do I see the price before I generate?",
             "Yes, in credits, on the button, and failed generations are refunded automatically."),
            ("Can I cancel myself?",
             "Yes. Billing has a Manage or cancel plan button; the plan stays on until the end of the paid month."),
            ("Who are the presenters?",
             "{presenters} AI presenters, each drawn from a written description with every video. They're not real "
             "people and never speak as customers."),
        ],
        "limits": [
            "If you want a tool that publishes your posts on a calendar or runs ad campaigns for you, Blaze lists "
            "both and Campaignify does neither.",
            "There's no managed service or dedicated marketing expert; Campaignify is self-serve.",
            "It doesn't manage reviews or a Google Business Profile.",
        ],
        "media": [("site/ugc-bakery.jpg", "An AI presenter behind a bakery counter shows a tray of croissants."),
                  ("site/ugc-serum.jpg", "An AI presenter in a bright bathroom shows a face serum."),
                  ("site/show-amber.jpg", "A gold coffee bag floating before a wall of type.")],
        "faq": [
            ("Is Campaignify affiliated with Blaze?", _NOT_AFFILIATED.replace("{name}", "Blaze")),
            ("What does Campaignify have that Blaze didn't list?",
             "UGC-style video ads with AI presenters, product videos from one photo and motion copied from a "
             "reference clip, each with a fixed credit price shown before you generate. These weren't listed on the "
             "Blaze pages read on Nov 4, 2026."),
            ("What does Blaze list that Campaignify doesn't do?",
             "Publishing posts to social accounts, running Google and Meta ad campaigns, review management and a "
             "managed tier with a marketing expert. Campaignify doesn't post or run ads, and it's self-serve."),
            _TRY,
            _CANCEL,
        ],
        "related": ["ai-marketing-platform-for-small-business", "ai-ads-for-local-businesses", "best-ai-ugc-ad-generators"],
    },
    "predis-ai-alternative": {
        "query": "Predis.ai alternative",
        "nav": "Predis.ai alternative",
        "title": P("Predis.ai Alternative, Nothing Charged · Campaignify",
                   "Predis.ai Alternative, Every Ad Priced First · Campaignify"),
        "description": P("Comparing Predis.ai alternatives? Campaignify charges nothing at sign-up, prices each ad in "
                         "credits first and adds pages, forms and follow-up flows.",
                         "Comparing Predis.ai alternatives? Campaignify prices each ad in credits first and adds pages, "
                         "forms and follow-up flows. {try_ads_free}."),
        "eyebrow": "Comparing tools",
        "h1": P("Campaignify is a Predis.ai alternative with nothing charged to start and each ad priced before you make it",
                "Campaignify is a Predis.ai alternative with each ad priced before you make it"),
        "answer": "Campaignify makes UGC-style videos with AI presenters, product videos and image ads from your "
                  "product page, then keeps the copy, landing page, forms and follow-up flows in the same workspace. "
                  "{predis_start} Predis.ai lists a 3-day free "
                  "start with a card taken at sign-up, as listed on Nov 4, 2026.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's name, description, price and photos. You approve the claims and pick "
             "the photos."),
            ("See the price, then generate",
             "Pick an AI presenter video, a product video or an image ad. The price in credits is on the button "
             "before anything runs."),
            ("{pay_only_title}",
             "{free_cover}. {after_that_billing}"),
        ],
        "table": {
            "competitor": "Predis.ai",
            "source": "https://predis.ai/pricing/",
            "source_name": "Predis.ai's pricing page",
            "date": AS_OF,
            "sources": _src("predis_g2"),
            "rows": [
                ("Trying it",
                 "\"Every plan starts with 3 days free\", with a card taken at sign-up; payments after that are "
                 "listed as \"non-refundable\" (Nov 4, 2026).",
                 "{trying_no_end}"),
                ("Plans",
                 "Core $19 a month (struck from $32, 1,300 credits, 1 brand), Rise $40 a month (struck from $79, "
                 "3,200 credits, up to 4 brands), Enterprise+ $212 a month (10,000 credits); yearly totals of $230, "
                 "$474 and $2,540, as listed on Nov 4, 2026.",
                 "Starter ${starter_usd}, Growth ${growth_usd} and Scale ${scale_usd} a month, billed monthly; packs "
                 "at {pack_usd}."),
                ("What an ad costs",
                 "Credits per plan are listed; a per-ad credit price is not listed on predis.ai/pricing (as of Nov "
                 "4, 2026).",
                 "A fixed price per ad, shown before you generate: {image} credits an image ad, {video5} a 5-second "
                 "product video, {talk8} an 8-second AI presenter video."),
                ("Posting",
                 "Auto posting is listed on Rise and above, not on Core.",
                 "Doesn't post. You export each ad at each platform's sizes and launch it in the platform's ads "
                 "manager."),
            ],
        },
        "h2": {"how": "How this Predis.ai alternative works", "compare": "Predis.ai and Campaignify, side by side",
               "faq": "Predis.ai alternative questions"},
        "compare": [
            ("Is there a trial that bills you?",
             "{trial_bills_answer}"),
            ("Does it go past the ad?",
             "Yes. On Starter and up: landing pages, smart links and flows. On Growth: forms and contacts, email "
             "sending and the SEO, AEO and GEO optimizer. On Scale: texts in flows and community and courses."),
            ("Can it research competitors?",
             "Yes, on every account. Competitor Snapshot reads up to 5 competitors on the web and cites a source "
             "for every claim."),
            ("Who are the presenters?",
             "{presenters} AI presenters, drawn from a written description with every video. They're not real people "
             "and never speak as customers."),
        ],
        "limits": _LIMITS[:2] + [
            "It doesn't auto post to social accounts or run ads; you launch the ads in each platform's ads manager."],
        "media": [("site/ugc-sneakers.jpg", "An AI presenter in a gym shows off a sage-green running sneaker."),
                  ("site/show-pastel.jpg", "A coffee bag among floating pastel cubes on a white pedestal."),
                  ("site/ugc-coffee.jpg", "An AI presenter in a sunny kitchen holds up a coffee bag.")],
        "faq": [
            ("Is Campaignify affiliated with Predis.ai?", _NOT_AFFILIATED.replace("{name}", "Predis.ai")),
            _TRY,
            ("What does a credit buy?",
             "An image ad is {image} credits, a 5-second product video {video5}, an 8-second AI presenter video "
             "{talk8} and a 10-second one {talk10}. Plans are ${starter_usd}, ${growth_usd} and ${scale_usd} a month; "
             "packs are {pack_usd}."),
            ("How many brands can I run?",
             "1 on Free and Starter, 3 on Growth and unlimited on Scale. Each brand keeps its own products, photos "
             "and approved claims."),
            _CANCEL,
        ],
        "related": ["ai-ads-for-coaches", "ai-ugc-ad-generator", "best-ai-ugc-ad-generators"],
    },
    "heygen-alternative": {
        "query": "HeyGen alternative",
        "nav": "HeyGen alternative",
        "title": "HeyGen Alternative for Product Ads · Campaignify",
        "description": "Looking for a HeyGen alternative for ads? Campaignify makes short ads where an AI presenter "
                       "holds your product, priced in credits first. {ads_free}.",
        "eyebrow": "Comparing tools",
        "h1": "Campaignify is a HeyGen alternative built for short product ads, not long avatar videos",
        "answer": "HeyGen is an AI avatar video generator for creators and teams, and its pricing page did not "
                  "mention UGC or product ads on Nov 4, 2026. Campaignify is built for ads: an AI presenter holds "
                  "your product and says a script in a {talk_lengths} second video, next to product videos, image "
                  "ads, ad copy and a landing page. If you need long training or multi-language avatar videos, "
                  "HeyGen lists those and Campaignify doesn't make them.",
        "steps": [
            ("Paste a product page",
             "Campaignify fills in the product from its page, Shopify product pages included, and uses its photos."),
            ("Pick a presenter and an angle",
             "Choose one of {presenters} AI presenters or a narrator over your product. AI writes the spoken lines "
             "for the angle; edit any line before you generate."),
            ("Make variants and download",
             "Make one video or a batch of up to {max_batch} to test hooks, in {talk_shapes}. The price in credits "
             "is shown first."),
        ],
        "table": {
            "competitor": "HeyGen",
            "source": "https://www.heygen.com/pricing",
            "source_name": "HeyGen's pricing page",
            "date": AS_OF,
            "rows": [
                ("Trying it",
                 "A free plan with 3 watermarked videos a month, 1 minute each, no card (as listed on Nov 4, 2026).",
                 "{nothing_charged}: {free_credits_n} ({sc_images} image ads), no watermark on image ads.{one_trial}"),
                ("Plans",
                 "Creator $29 a month or $24 billed annually (600 credits), Pro $49 a month (1,000 credits), "
                 "Business $149 a month plus $20 a seat (1,500 credits), Enterprise custom, as listed on Nov 4, 2026.",
                 "Starter ${starter_usd}, Growth ${growth_usd} and Scale ${scale_usd} a month; packs at {pack_usd}."),
                ("Video length and quality",
                 "Free videos up to 1 minute; export up to 4K on Pro.",
                 "AI presenter videos of {talk_lengths} seconds at 720p, or 1080p for more credits."),
                ("Product and UGC ads",
                 "UGC or product ads: not mentioned on heygen.com/pricing (as of Nov 4, 2026).",
                 "The presenter holds your product and says a script written for it, selfie-style."),
            ],
        },
        "h2": {"how": "How this HeyGen alternative works", "compare": "HeyGen and Campaignify, side by side",
               "faq": "HeyGen alternative questions"},
        "compare": [
            ("Is it built for ads?",
             "Yes. Every video starts from a product and an ad angle, and the copy studio writes the text for "
             "{platform_count} ad platforms."),
            ("Do I see the price before I generate?",
             "Yes, in credits, on the button, and failed generations are refunded automatically."),
            ("What stops risky claims?",
             "Every brief, piece of copy and landing page is checked for income, health, discount and recruiting "
             "claims, and AI presenters never speak as customers."),
            ("What else is in the workspace?",
             "Image ads, product videos, a campaign planner, landing pages, smart links, forms and email and text "
             "follow-up flows, depending on the plan."),
        ],
        "limits": [
            "Talking videos are {talk_lengths} seconds; it doesn't make long explainer, training or course videos.",
            "There are {presenters} AI presenters; it doesn't make a custom avatar of you or translate videos.",
            "It doesn't post ads or read results from the ad platforms.",
        ],
        "clips": ["ugc-serum", "ugc-app", "ugc-sneakers"],
        "price_line": "8-second AI presenter video: {talk8} credits. 10 seconds: {talk10} credits. Shown before you "
                      "press Generate.",
        "faq": [
            ("Is Campaignify affiliated with HeyGen?", _NOT_AFFILIATED.replace("{name}", "HeyGen")),
            ("Can I make a 1-minute video?",
             "No. AI presenter videos are {talk_lengths} seconds, the length of a short ad. Make several variants "
             "and test which opener works."),
            ("Do the presenters look like real customers?",
             "They're AI presenters, not real people, and they're labelled as AI on this site. Put #ad first in "
             "the caption and turn on the platform's AI label when you post."),
            _TRY,
        ],
        "related": ["ai-ugc-ad-generator", "make-ai-ugc-ads-that-dont-look-fake", "topview-alternative"],
    },
    "topview-alternative": {
        "query": "Topview alternative",
        "nav": "Topview alternative",
        "title": "Topview Alternative, Every Ad Priced First · Campaignify",
        "description": "Comparing Topview alternatives? Campaignify pack credits never expire, each ad is priced "
                       "first, and copy, pages and flows sit beside the ads.",
        "eyebrow": "Comparing tools",
        "h1": "Campaignify is a Topview alternative where pack credits never expire",
        "answer": "Campaignify makes product videos, image ads and UGC-style videos with AI presenters from your "
                  "product page. Every ad has a fixed price in credits shown before you generate, and credits from "
                  "packs never expire. Topview's plans list credits that expire monthly, according to its G2 listing "
                  "read on Nov 4, 2026.",
        "steps": [
            ("Paste a product page",
             "Campaignify reads the product's details and photos from its page. You approve the claims and pick the "
             "photos."),
            ("Make a batch to test",
             "Make up to {max_batch} variants at once: different hooks, presenters or shapes. The price is shown "
             "before you generate."),
            ("Export and launch",
             "On Starter and up, export each ad at each platform's sizes and launch it in that platform's ads "
             "manager."),
        ],
        "table": {
            "competitor": "Topview",
            "source": "https://www.g2.com/products/topview-ai-video-creator/pricing",
            "source_name": "Topview's pricing on G2",
            "date": AS_OF,
            "rows": [
                ("Trying it",
                 "A free tier with 10 one-time credits and a watermark, as listed on G2 on Nov 4, 2026.",
                 "{nothing_charged}: {free_credits_n} ({sc_images} image ads), no watermark on image ads.{one_trial}"),
                ("Plans",
                 "Pro $29 a month (80 credits) and Business $75 a month (250 credits), as listed on G2 on Nov 4, 2026. "
                 "G2 shows 2 different update dates for this listing, so confirm on Topview's own site.",
                 "Starter ${starter_usd}, Growth ${growth_usd} and Scale ${scale_usd} a month; packs at {pack_usd}."),
                ("Do credits expire?",
                 "Credits expire monthly, per the G2 listing.",
                 "Plan credits reset at each monthly renewal. {packs_never_expire}."),
                ("Features listed",
                 "Video Agent, Product Avatars, 2,000+ avatars and voices on paid plans, and TikTok Ad Library access.",
                 "{presenters} AI presenters, product videos, image ads, motion copy, ad copy, a planner, landing "
                 "pages and flows, by plan."),
            ],
        },
        "h2": {"how": "How this Topview alternative works", "compare": "Topview and Campaignify, side by side",
               "faq": "Topview alternative questions"},
        "compare": [
            ("Do I see the price before I generate?",
             "Yes, in credits, on the button, and failed generations are refunded automatically."),
            ("Can I research competitors' ads?",
             "Competitor Snapshot, on every account, researches up to 5 competitors on the web and links you to "
             "each one's Meta Ad Library and TikTok Top Ads pages. It never makes up ads it couldn't read."),
            ("Does it go past the video?",
             "Yes: copy for {platform_count} ad platforms, landing pages, smart links, forms and follow-up flows, "
             "depending on the plan."),
            ("Can I cancel myself?",
             "Yes, from Billing in the app. The plan stays on until the end of the paid month."),
        ],
        "limits": [
            "There are {presenters} AI presenters, far fewer than a library of thousands of avatars.",
            "A talking video is at most {talk_max} seconds long.",
            "It doesn't post ads or read results from the ad platforms.",
        ],
        "media": [("site/headphones.jpg", "Blue over-ear headphones floating against a bright orange background."),
                  ("site/ugc-app.jpg", "An AI presenter at a desk shows a to-do list app on his phone."),
                  ("site/candle.jpg", "A lit candle in an amber glass jar beside dried eucalyptus.")],
        "faq": [
            ("Is Campaignify affiliated with Topview?", _NOT_AFFILIATED.replace("{name}", "Topview")),
            ("Why is the Topview column from G2?",
             "Topview's own pricing page wasn't read for this comparison, so the facts are from its G2 pricing "
             "listing on Nov 4, 2026. Check Topview's site for current terms."),
            ("Do Campaignify credits expire?",
             "Plan credits arrive with each monthly payment and expire at your next renewal, and they're used first. "
             "{credits_never_expire}."),
            _TRY,
        ],
        "related": ["ai-ads-for-ecommerce", "credit-based-ai-ad-pricing", "heygen-alternative"],
    },
    "ai-ads-for-local-businesses": {
        "query": "AI ads for local businesses",
        "nav": "AI ads for local businesses",
        "title": "AI Ads for Local Businesses · Campaignify",
        "description": "Make ads from photos of your shop, menu or service, then a landing page, form and follow-up "
                       "emails. Plans from ${starter_usd} a month; {ads_free}.",
        "eyebrow": "For local businesses",
        "h1": "AI ads for local businesses",
        "answer": "Campaignify helps a local business make ads from photos of what it sells, then catch the people "
                  "who click. Upload a photo of a dish, a product or your shop, and it makes image ads, short "
                  "product videos and UGC-style videos with an AI presenter. On paid plans it writes the ad copy, "
                  "publishes a landing page with a form and sends follow-up emails. You launch the ads yourself in "
                  "each platform's ads manager.",
        "clips": ["ex-pressure-washing", "ugc-bakery", "ex-pet-grooming"],
        "price_line": "Image ad: {image} credits. 5-second product video: {video5} credits. Shown before you press "
                      "Generate.",
        "steps": [
            ("Add what you sell",
             "Upload photos and type the details, or paste the page it's on. Suggested claims wait for your approval."),
            ("Make the ads and the copy",
             "Make image ads in {image_shapes}, product videos or AI presenter videos. On Starter and up, the copy "
             "studio writes the text for Facebook & Instagram, Google Search and the other platforms."),
            ("Catch the clicks",
             "Send ad traffic to a landing page with a form. Each sign-up lands in your contacts, and a flow sends "
             "the follow-up."),
        ],
        "gets": [
            ("Ads from your own photos", "A croissant, a haircut, a candle: if you can photograph it, it can be an "
                                         "ad. Failed generations are refunded."),
            ("Copy for local search and social", "Every text field each platform takes, sized to its display lengths, "
                                                 "including Google Search headlines and descriptions."),
            ("A landing page with a form", "On Starter, 1 page published on campaignify.ai; forms and contacts on "
                                           "Growth (${growth_usd} a month)."),
            ("Follow-up that runs itself", "Flows of emails, waits and tags after someone signs up. Growth sends the "
                                           "emails; Scale also sends texts to people who agreed to them."),
            ("Smart links", "Short links with your UTM tags for posts, emails and anywhere else you share the offer."),
            ("Know your competition", "Competitor Snapshot reads up to 5 competitors' public pages and cites a source "
                                      "for every claim."),
        ],
        "h2": {"how": "How a local business makes AI ads", "gets": "What's in it for a local business",
               "faq": "AI ads for local businesses questions"},
        "faq": [
            ("Does it work for a service business?",
             "Yes, if you can photograph the work or the place: a finished room, a plate, a storefront. Use your own "
             "brief or the Product hero and Daily routine templates."),
            ("Will it run my Facebook or Google ads?",
             "No. It writes the copy, makes the ads and walks you through setting up each ad account. You launch the "
             "ads in each platform's ads manager, where your ad spend stays."),
            ("How much does it cost?",
             "{signup_includes} {sc} credits ({sc_images} image ads), {with_nothing_charged}. Plans are ${starter_usd}, "
             "${growth_usd} and ${scale_usd} a month. Each ad's price in credits is shown before you make it."),
            ("Does it manage my reviews or Google Business Profile?",
             "No. Campaignify doesn't manage reviews or a Google Business Profile."),
            ("Will it bring me customers?",
             "Campaignify doesn't promise sales, leads or visits. How ads do depends on your offer, your area and "
             "your ad spend."),
        ],
        "related": ["ai-marketing-platform-for-small-business", "plan-a-campaign-with-ai", "blaze-alternative"],
    },
    "ai-ads-for-coaches": {
        "query": "AI ads for coaches",
        "nav": "AI ads for coaches",
        "title": "AI Ads for Coaches and Course Creators · Campaignify",
        "description": "Ads, copy, a sign-up page and follow-up emails for your coaching program, with income and "
                       "health claims blocked. {ads_free_nc}.",
        "eyebrow": "For coaches",
        "h1": "AI ads for coaches and course creators",
        "answer": "Campaignify gives a coach the whole path from ad to client: image and video ads for your program, "
                  "copy for each platform, a landing page with a sign-up form, and follow-up emails. On Scale it "
                  "also hosts your community and courses. Every brief and page is checked for income and health "
                  "claims, and AI presenters never pose as your clients.",
        "clips": ["ex-trainer-presenter", "ugc-app", "ugc-coffee"],
        "price_line": "Image ad: {image} credits. 8-second AI presenter video: {talk8} credits. Shown before you "
                      "press Generate.",
        "steps": [
            ("Add your program as a product",
             "Give it a name, an offer and a photo: a workbook, a cover image or a screenshot of your app or course. "
             "Approve the claims your ads may use."),
            ("Make the ads",
             "Image ads, short videos, or an AI presenter showing your workbook or app and saying a script. AI "
             "writes the lines; you edit them."),
            ("Book the call or the sign-up",
             "Send clicks to a landing page with a form. A flow emails each new lead and can invite them to your "
             "community."),
        ],
        "gets": [
            ("Ads without filming yourself", "AI presenters say your script, and image ads and product videos need "
                                             "only a photo. You can still post your own videos alongside."),
            ("Claims checked for you", "Income, earnings, health and before-and-after language is blocked in every "
                                       "brief, copy variant and page."),
            ("A sign-up page and form", "A landing page on Starter; forms and a contacts list with tags and notes on "
                                        "Growth."),
            ("Follow-up flows", "Emails, waits, tags and branches, drafted by AI from a sentence. Growth sends the "
                                "emails; Scale adds texts."),
            ("Community and courses", "On Scale: spaces and posts, courses with lessons, members with their own "
                                      "logins and events."),
            ("Copy for every platform", "Hooks, captions, headlines and emails for {platform_count} ad platforms."),
        ],
        "h2": {"how": "How coaches use AI ads", "gets": "What coaches get", "faq": "AI ads for coaches questions"},
        "faq": [
            ("Can an AI presenter give a testimonial for my coaching?",
             "No, and you shouldn't post one. The FTC bans testimonials from people who don't exist. AI presenters "
             "can explain your offer, but they never speak as clients or describe personal results. See [the guide "
             "to FTC and Meta rules](/guides/ftc-meta-rules-for-ai-ads)."),
            ("Can I promise results in my ads?",
             "Campaignify blocks income and health result claims. Describe what the program includes, not what "
             "someone will earn or lose."),
            ("Where do my courses live?",
             "On Scale, at campaignify.ai/c/your-community, with lessons that can use video from your library, "
             "YouTube, Vimeo or Loom."),
            ("Does it post or run my ads?", "{runs_short}"),
            _TRY,
        ],
        "related": ["ftc-meta-rules-for-ai-ads", "ugc-ad-hooks", "predis-ai-alternative"],
    },
    "ai-ads-for-ecommerce": {
        "query": "AI ads for ecommerce",
        "nav": "AI ads for ecommerce",
        "title": "AI Ads for Ecommerce Brands · Campaignify",
        "description": "Turn any product page into image ads, product videos and AI presenter videos, in batches "
                       "of up to {max_batch} variants, each priced in credits before you make it.",
        "eyebrow": "For ecommerce",
        "h1": "AI ads for ecommerce brands",
        "answer": "Campaignify turns any product page into ads you can test: image ads in 4 shapes, 5 or 10 second "
                  "product videos, and UGC-style videos where an AI presenter holds the product. Make up to "
                  "{max_batch} variants at once to test hooks, see every price in credits first, and keep each "
                  "product's photos and approved claims in one place. It works with any store whose product pages "
                  "state their details, not just Shopify.",
        "clips": ["ex2-tryon-denim-jacket", "ex2-unboxing-pov-bottle", "show-amber"],
        "price_line": "Image ad: {image} credits. 5-second product video: {video5} credits. 8-second AI presenter "
                      "video: {talk8} credits.",
        "steps": [
            ("Import products from their pages",
             "Paste a product link and it reads the name, description, price and photos. Do it for each product you "
             "want to advertise."),
            ("Make a test batch",
             "Pick a template or your own brief, then make up to {max_batch} variants at once across hooks, "
             "presenters and shapes."),
            ("Export for every feed",
             "On Starter and up, export each winner at each platform's sizes and write its copy in the copy studio."),
        ],
        "gets": [
            ("Product-in-hand videos", "An AI presenter holds your product and talks about it, in {talk_shapes}."),
            ("Product videos from 1 photo", "5 or 10 second clips that bring a product photo to life, and motion "
                                            "copied from a reference clip."),
            ("Image ads in 4 shapes", "{image_shapes}, with only the headline, subline and button text you type drawn "
                                      "on the image."),
            ("Every product in one place", "Photos, offer, approved claims and audience per product; more brands on "
                                           "Growth and Scale."),
            ("Copy per platform", "Every text field each platform takes, sized to its display lengths."),
            ("A page and a follow-up", "A landing page, smart links and a follow-up flow for launches and offers."),
        ],
        "h2": {"how": "How ecommerce brands make AI ads", "gets": "What ecommerce brands get",
               "faq": "AI ads for ecommerce questions"},
        "faq": [
            ("How is this different from the Shopify page?",
             "The [Shopify page](/ai-product-ads-for-shopify) covers importing from a Shopify store. This page is "
             "about testing at volume for any store: batches, many products and exports for every feed."),
            ("Will the product look right?",
             "Ads are generated from your product photos, but AI can still get small details wrong, such as label "
             "text. Check every ad before you post it."),
            ("Can it make ads for many products?",
             "Yes. Each product keeps its own photos and claims. Growth includes 3 brands and Scale unlimited."),
            ("Does it sync with my store or run ads?",
             "No. It reads public product pages and never changes your store. You launch the ads in each platform's "
             "ads manager."),
            ("What does a test batch cost?",
             "Each ad's price is fixed and shown before you generate: {image} credits an image ad, {video5} a "
             "5-second product video, {talk8} an 8-second AI presenter video. Failed generations are refunded."),
        ],
        "related": ["ai-product-ads-for-shopify", "ugc-ad-hooks", "topview-alternative"],
    },
    "ai-marketing-for-agencies": {
        "query": "AI marketing for agencies",
        "nav": "AI marketing for agencies",
        "title": "AI Marketing Platform for Agencies · Campaignify",
        "description": "Run ads, copy, pages, forms and follow-up for several client brands in one workspace. Growth "
                       "has 3 brands; Scale has unlimited brands and 10 seats.",
        "eyebrow": "For agencies",
        "h1": "AI marketing platform for agencies",
        "answer": "Campaignify lets a small agency run the marketing side for several client brands from one "
                  "workspace. Each brand keeps its own products, photos and approved claims, and you make its ads, "
                  "copy, landing pages, forms and follow-up flows from there. Growth includes 3 brands and 3 seats; "
                  "Scale includes unlimited brands, 10 seats and the priority queue. Your team shares the "
                  "workspace's credits.",
        "media": [("site/ugc-coffee.jpg", "An AI presenter in a sunny kitchen holds up a coffee bag."),
                  ("site/show-y2k.jpg", "A coffee bag on denim among chains, charms and a pink flip phone."),
                  ("site/ugc-bakery.jpg", "An AI presenter behind a bakery counter shows a tray of croissants.")],
        "steps": [
            ("Add each client as a brand",
             "Each brand holds its products, photos, offer, approved claims and audience. Switch brands in 1 click."),
            ("Plan and make the campaign",
             "Run Competitor Snapshot for the brand, then the planner: angles, scripts, ads with a credit quote each, "
             "copy and a launch checklist."),
            ("Hand off the launch",
             "Export ads at each platform's sizes and work through the ad-account setup hub with your team, ticking "
             "steps off together."),
        ],
        "gets": [
            ("Brands and seats", "Growth: 3 brands and 3 seats. Scale: unlimited brands and 10 seats. Invite teammates "
                                 "with a link."),
            ("Shared credits", "Everyone in the workspace spends from the same credits, with every price shown "
                               "before a generation."),
            ("Competitor research per brand", "Up to 5 competitors per run, every claim with its source, feeding the "
                                              "campaign planner."),
            ("Pages, forms and flows", "Unlimited landing pages on Growth, forms and contacts, and follow-up flows "
                                       "that send email (texts on Scale)."),
            ("SEO, AEO and GEO audits", "On Growth: audit a client page for search and AI answers and get the fixes, "
                                        "schema and an llms.txt draft."),
            ("Priority queue", "On Scale, your generations start ahead of the queue."),
        ],
        "h2": {"how": "How agencies run AI marketing for clients", "gets": "What agencies get",
               "compare": "AI marketing for agencies: what to check", "faq": "AI marketing for agencies questions"},
        "compare": [
            ("Can each client have its own claims?",
             "Yes. Approved claims belong to each product in each brand, and every ad, copy variant and page starts "
             "from them. Income, health and recruiting claims and invented discounts are blocked on every account."),
            ("Can the team see what everyone made?",
             "Yes. The workspace's library holds every finished ad with full-quality downloads, and the ad-account "
             "setup hub shows the steps your team has ticked off."),
            ("How is spend controlled?",
             "Every generation shows its price in credits before it runs, and AI writing tools show the most a run "
             "can cost. Failed generations are refunded."),
            ("What if we need more credits mid-month?",
             "Buy a one-time pack ({pack_usd}). Pack credits never expire; plan credits are spent first."),
        ],
        "limits": [
            "It doesn't post or run ads, or read results from the ad platforms; report ad results from each "
            "platform's ads manager.",
            "It doesn't bill your clients or keep separate credit balances per client: the workspace shares one "
            "balance.",
            "AI presenters only; it doesn't book or film real creators.",
        ],
        "faq": [
            ("How many client brands can I run?",
             "3 on Growth (${growth_usd} a month) and unlimited on Scale (${scale_usd} a month). Free and Starter "
             "include 1."),
            ("Can clients log in?",
             "Teammates join with an invite link and use a seat. Community members on Scale have their own logins "
             "and don't use your team's seats."),
            ("Can it white-label pages?",
             "Pages are published at campaignify.ai/p/your-page with your button color. Email in flows can send from your "
             "own sending domain."),
            ("Does it report ad results?",
             "No. It reports clicks on smart links, page views and form submissions. Ad results stay in each "
             "platform's ads manager."),
            ("Does it run the ads for clients?",
             "No. You launch the ads in each platform's ads manager."),
        ],
        "related": ["ai-marketing-platform-for-small-business", "research-competitor-ads", "blaze-alternative"],
    },
    # ---- Feature and industry pages, Nov 10 2026. Same shape as the pages above; every number is a placeholder
    # filled from the live rates, and nothing here names a feature the product does not have.
    "ai-image-ad-generator": {
        "query": "AI image ad generator",
        "nav": "AI image ad generator",
        "title": "AI Image Ad Generator from Your Product Photo · Campaignify",
        "description": "Turn one product photo into finished image ads in feed, story and listing sizes. {image} credits "
                       "an ad, price shown first. {ads_free_nc}.",
        "eyebrow": "Image ads",
        "h1": "AI image ad generator that starts from your product photo",
        "answer": "Campaignify's AI image ad generator makes a finished ad from your own product photo: pick a format such "
                  "as an offer, a callout list, a comparison or a seasonal scene, write the headline, and get the ad in "
                  "the sizes the feeds, stories and listings ask for. An image ad costs {image} credits, and you see the "
                  "price before anything is made. {signup_includes} {sc_images} image ads, {with_nothing_charged}.",
        "clips": ["ad_image", "product-video", "ugc-coffee"],
        "price_line": "Image ad: {image} credits. Shown before you press Generate.",
        "steps": [
            ("Add your product", "Paste your product page or upload a photo. Campaignify reads the name, the price and what "
                                 "the page says, and you approve the claims your ads may make."),
            ("Pick a format and write the headline", "Choose from the image presets: offers, callouts, comparisons, reviews "
                                                     "you supply, seasonal scenes and more. Each shows its price in credits."),
            ("Download in every size", "Each ad comes in the shapes you picked: {image_shapes}. A render that fails is "
                                       "refunded automatically."),
        ],
        "gets": [
            ("Your real product in the picture", "The ad is built on your own photo, so the label and the shape stay yours."),
            ("Formats that already work", "Presets for the layouts advertisers use most, from a price badge to a side by "
                                          "side comparison."),
            ("Checked before it is made", "Income, health and recruiting claims and invented discounts are blocked at the brief."),
            ("{no_sub_title}", "{use_free_or_pack} from ${starter_usd} a month."),
            ('Several versions at once',
             'Make two or more versions of the same headline in one go and keep the one that reads best. Each version is priced before you confirm, and a batch shows its total first.'),
            ('A brand you set once',
             'Save the product with its photo, its offer and the claims you approved, and every new ad starts from it instead of from a blank page.'),
        ],
        "h2": {"how": "How the AI image ad generator works", "gets": "What the AI image ad generator gives you",
               "faq": "AI image ad generator questions"},
        "faq": [
            ("Does it invent a picture of my product?",
             "No. It starts from the product photo you upload, so the ad shows your real product."),
            ("Which sizes do I get?",
             "You choose from {image_shapes} when you make the ad, and each size is priced before you confirm."),
            ("Can it write a customer review for the ad?",
             "No. The review format only takes a review you supply, word for word. Campaignify never writes one."),
            ("Does it post the ad for me?",
             "No. You download the ad and launch it in the platform's own ads manager. See [how the hooks guide pairs "
             "with image ads](/guides/ugc-ad-hooks)."),
            ('How long does an image ad take?',
             'Usually one to three minutes. You can leave the page; the ad is waiting in your library when it is done, and a render that fails is refunded automatically.'),
        ],
        "related": ["ai-ugc-ad-generator", "ai-ad-copy-generator", "credit-based-ai-ad-pricing"],
    },
    "ai-product-video-generator": {
        "query": "AI product video generator",
        "nav": "AI product video generator",
        "title": "AI Product Video Generator from One Photo · Campaignify",
        "description": "Make a 5 or 10 second product video from one product photo. {video5} credits for 5 seconds, "
                       "price shown first. {start_free_with}.",
        "eyebrow": "Product videos",
        "h1": "AI product video generator that needs one photo",
        "answer": "Campaignify's AI product video generator turns a single product photo into a short video for your ads "
                  "or your product page: a turn, a label read, a how-it-works shot or a scene you describe. A 5 second "
                  "video costs {video5} credits and a 10 second one {video10}, shown before you press Generate. {signup_includes} {sc} credits, {with_nothing_charged}.",
        "clips": ["product-video", "ex2-showcase-steel-bottle", "ex2-showcase-planter"],
        "price_line": "5-second product video: {video5} credits. 10 seconds: {video10}. Shown before you press Generate.",
        "steps": [
            ("Upload one product photo", "A clean photo of the product is enough. Add the product once and reuse it for "
                                         "every video."),
            ("Pick a template or describe the scene", "Choose a ready shot or write what should happen. Pick 5 or 10 "
                                                      "seconds and whether you want sound."),
            ("See the price, then make it", "The price in credits is on the button. Videos take a few minutes, and a "
                                            "render that fails is refunded automatically."),
        ],
        "gets": [
            ("Video without a shoot", "No studio, no camera and no editor. The video is made from the photo you have."),
            ("Shots built for ads", "Templates for the product turn, the label, the swap and the how-it-works shot."),
            ("Sized for where it runs", "Vertical for Reels, TikTok and Shorts, or the shape your listing needs."),
            ('One product, many videos',
             'Save the product once with its photo and offer, then make a turn for the product page, a label read for an ad and a how-it-works shot for a listing from the same record.'),
            ('The price before the video',
             'Every video shows its price in credits on the button. Nothing is made, and nothing is charged, until you press it.'),
            ('Claims checked first',
             'The brief is checked for income, health and recruiting claims and invented discounts before a video is made, so an ad is not built on a line a platform will reject.'),
        ],
        "h2": {"how": "How the AI product video generator works", "gets": "What the AI product video generator gives you",
               "faq": "AI product video generator questions"},
        "faq": [
            ("How long can a product video be?", "5 or 10 seconds. For longer talking videos, use an AI presenter video, "
                                                 "which runs up to {talk_max} seconds."),
            ("Do I need more than one photo?", "No. One clear product photo is enough to start."),
            ("Can I use the video on my product page?", "Yes. The video is yours to download and use in ads, on your "
                                                        "product page or in a marketplace listing."),
            ('Does the video have sound?',
             'You choose. A product video can be made silent or with sound, and the price for each is shown before you confirm.'),
            ('What if the video comes out wrong?',
             'A render that fails is refunded automatically. If the product in the video does not match your photo, make it again from a clearer photo or a different template.'),
        ],
        "related": ["ai-product-ads-for-shopify", "ai-image-ad-generator", "credit-based-ai-ad-pricing"],
    },
    "ai-avatar-ads": {
        "query": "AI avatar ads",
        "nav": "AI presenters and avatars",
        "title": "AI Avatar Ads: Presenters Who Say Your Script · Campaignify",
        "description": "{presenters} AI presenters say your script to camera with your product beside them. An 8 second "
                       "video is {talk8} credits, price shown first. None is a real person.",
        "eyebrow": "AI presenters",
        "h1": "AI avatar ads: presenters who say your script",
        "answer": "Campaignify makes AI avatar ads with {presenters} original presenters. Pick a presenter and a place "
                  "they film in, such as a kitchen, a car or a desk, write what they say, and get a selfie-style video "
                  "with your product in the shot. An 8 second video costs {talk8} credits, shown before you press "
                  "Generate. None of the presenters is a real person, and they never speak as customers.",
        "clips": ["ugc-coffee", "ugc-serum", "ugc-app"],
        "price_line": "8-second AI presenter video: {talk8} credits. Shown before you press Generate.",
        "steps": [
            ("Pick a presenter and a setting", "Choose from {presenters} presenters, each with a few places they film "
                                               "in: a kitchen, a car, a desk and more."),
            ("Write the script", "Write what they say, or start from a hook. Scripts are checked for claims the product "
                                 "cannot make."),
            ("See the price, then make it", "Pick the length, from 8 to {talk_max} seconds. The price is on the button, "
                                            "and a failed render is refunded automatically."),
        ],
        "gets": [
            ("Presenters without a shoot", "No casting, no filming and no usage rights to chase."),
            ("Your product in the shot", "The product sits beside the presenter or in their hand, from your own photo."),
            ("Honest by design", "Presenters explain the offer. They never give testimonials or describe personal results."),
            ('Several presenters, one script',
             'Run the same script with up to four presenters and see which one your audience watches. Each video is priced on its own line before you confirm.'),
            ('Captions when you want them',
             'Video presets made in beats can burn the spoken words in as captions, so the ad still works with the sound off.'),
            ('The price before the video',
             'Every video shows its price in credits before it is made, priced by its length. {signup_includes} {sc} credits, {with_nothing_charged}, and a render that fails is refunded automatically.'),
        ],
        "h2": {"how": "How AI avatar ads are made", "gets": "What you get with AI avatar ads", "faq": "AI avatar ads questions"},
        "faq": [
            ("Are the presenters real people?", "No. Every presenter is an original AI character made for Campaignify. "
                                                "None is based on a real person."),
            ("Can a presenter give a testimonial?",
             "No. The FTC bans testimonials from people who don't exist. See [the guide to FTC and Meta rules]"
             "(/guides/ftc-meta-rules-for-ai-ads)."),
            ("How long can the video be?", "From 8 to {talk_max} seconds, priced by length before you confirm."),
            ("Can I pick the voice?", "Yes. Each presenter has a voice, and you can choose another from the voice list."),
            ('Can the presenter hold my product?',
             'You choose where the product is: on the table beside the presenter, or held up in their hand. Both start from your own product photo.'),
        ],
        "related": ["ai-ugc-ad-generator", "make-ai-ugc-ads-that-dont-look-fake", "ftc-meta-rules-for-ai-ads"],
    },
    "ai-facebook-ad-generator": {
        "query": "AI Facebook ad generator",
        "nav": "AI Facebook ad generator",
        "title": "AI Facebook Ad Generator: Image, Video, Copy · Campaignify",
        "description": "Make Facebook and Instagram ads from your product page: image ads, presenter videos and the "
                       "primary text and headlines to go with them. {ads_free}.",
        "eyebrow": "Facebook and Instagram",
        "h1": "AI Facebook ad generator for the picture, the video and the words",
        "answer": "Campaignify is an AI Facebook ad generator that makes the whole ad: image ads and short videos in feed "
                  "and story sizes, plus primary text and headlines written for Meta. It starts from your product page, "
                  "shows the price of every ad in credits before it is made, and blocks income, health, discount and "
                  "recruiting claims at the brief. You upload the finished ads in Meta Ads Manager; Campaignify does not run them.",
        "clips": ["ad_image", "ugc-serum", "product-video"],
        "price_line": "Image ad: {image} credits. 8-second AI presenter video: {talk8} credits. Shown before you press Generate.",
        "steps": [
            ("Paste your product page", "Campaignify reads the product, the price and the claims, and you approve what "
                                        "your ads may say."),
            ("Make the creative", "Image ads in 4:5 and 1:1 for the feed and 9:16 for stories and Reels, or a presenter "
                                  "video. Each shows its price first."),
            ("Add the copy and launch", "Get primary text and headlines for each ad, then upload everything in Meta Ads "
                                        "Manager."),
        ],
        "gets": [
            ("Feed and story sizes", "Every ad in the shapes Facebook and Instagram placements ask for."),
            ("Copy written for Meta", "Primary text and headlines within Meta's limits, in several variants to test."),
            ("Claims checked first", "Income, health and recruiting claims and invented discounts are blocked before an ad is made."),
            ("A plan to test with", "On a plan, the campaign planner lays out angles, hooks and a first test."),
            ('Your product, not a stock picture',
             'Image ads are built on your own product photo, and presenter videos show the product from that same photo, so what people see is what they get.'),
            ('Sized before you download',
             'Pick the placements you run and get each ad in those shapes, so nothing is cropped by the feed or cut off in stories.'),
        ],
        "h2": {"how": "How the AI Facebook ad generator works", "gets": "What the AI Facebook ad generator gives you",
               "faq": "AI Facebook ad generator questions"},
        "faq": [
            ("Does Campaignify publish to Facebook?", "{runs_short} Your ad spend stays in your own ad account."),
            ("Do the ads work for Instagram too?", "Yes. The feed, story and Reels sizes are the same placements Meta "
                                                   "uses across Facebook and Instagram."),
            ("Will Meta approve AI-made ads?",
             "Meta allows AI-made ads that follow its rules. See [what the FTC and Meta expect]"
             "(/guides/ftc-meta-rules-for-ai-ads)."),
            ('How much does a Facebook ad cost to make?',
             'An image ad is {image} credits and an 8 second presenter video is {talk8}. {signup_includes} {sc_images} image ads, {with_nothing_charged}. What you spend on running the ad is paid to Meta.'),
            ('Can it help me set up my ad account?',
             "On a plan, the ad-account setup hub walks you through each step in Meta's own tools. Campaignify does not log in to your ad account."),
        ],
        "related": ["ai-ad-copy-generator", "ftc-meta-rules-for-ai-ads", "ai-image-ad-generator"],
    },
    "ai-tiktok-ad-generator": {
        "query": "AI TikTok ad generator",
        "nav": "AI TikTok ad generator",
        "title": "AI TikTok Ad Generator: Videos and Hooks · Campaignify",
        "description": "Make vertical TikTok ads from your product page: presenter videos, product videos, hooks and "
                       "captions. An 8 second video is {talk8} credits, price first.",
        "eyebrow": "TikTok",
        "h1": "AI TikTok ad generator for vertical videos that open with a hook",
        "answer": "Campaignify is an AI TikTok ad generator: it makes 9:16 presenter videos and product videos from your "
                  "product page, and writes the hooks and captions to go with them. An 8 second presenter video costs "
                  "{talk8} credits and a 5 second product video {video5}, shown before anything is made. You upload the "
                  "finished videos in TikTok Ads Manager; Campaignify does not run them.",
        "clips": ["ex2-tryon-midi-dress", "ugc-sneakers", "ex2-unboxing-mug"],
        "price_line": "8-second AI presenter video: {talk8} credits. 5-second product video: {video5}. Shown before you press Generate.",
        "steps": [
            ("Paste your product page", "Campaignify reads the product and what the page says, and you approve the claims."),
            ("Start from a hook", "Pick a video recipe and write the opening line, or get hook drafts first. A presenter "
                                  "says it to camera with your product in the shot."),
            ("Download vertical", "Videos come out 9:16 with captions when the recipe has them, ready to upload."),
        ],
        "gets": [
            ("Videos that look native", "Selfie-style presenter videos in everyday settings, not studio spots."),
            ("Hooks and captions", "Hook drafts and captions with hashtags from the copy studio on a plan."),
            ("Several versions to test", "Make the same script with different presenters or hooks and see each price first."),
            ('Your product in every video',
             'Presenter videos and product videos both start from your own product photo, so the thing in the ad is the thing you sell.'),
            ('The price before the video',
             'Each video shows its price in credits before it is made, and a batch shows its total first. A render that fails is refunded automatically.'),
            ('Claims checked first',
             'Scripts are checked for income, health and recruiting claims and invented discounts before a video is made.'),
        ],
        "h2": {"how": "How the AI TikTok ad generator works", "gets": "What the AI TikTok ad generator gives you",
               "faq": "AI TikTok ad generator questions"},
        "faq": [
            ("Does Campaignify post to TikTok?", "No. It makes the videos. You upload and launch them in TikTok Ads Manager."),
            ("How long are the videos?", "Presenter videos run from 8 to {talk_max} seconds and product videos 5 or 10."),
            ("How do I write a hook that holds?",
             "Start with the problem or the result in the first second. See [hooks for UGC ads](/guides/ugc-ad-hooks)."),
            ('Do the videos have captions?',
             'Video presets made in beats can burn the spoken words in as captions, which helps on a feed most people watch with the sound off.'),
            ('Can I make the same ad for Reels and Shorts?',
             'Yes. The vertical 9:16 video you make for TikTok is the same shape Reels and Shorts use.'),
        ],
        "related": ["ugc-ad-hooks", "ai-ugc-ad-generator", "ai-avatar-ads"],
    },
    "ai-ads-for-saas": {
        "query": "AI ads for SaaS",
        "nav": "AI ads for SaaS",
        "title": "AI Ads for SaaS from Your Own Screens · Campaignify",
        "description": "Ads for your software from your own screenshots: image ads, presenter videos, copy and a page "
                       "with a sign-up form. {ads_free_nc}.",
        "eyebrow": "For SaaS",
        "h1": "AI ads for SaaS, made from your own screens",
        "answer": "Campaignify makes AI ads for SaaS from what you already have: your product page and your screenshots. "
                  "It makes image ads built around your screenshot, presenter videos that explain the offer, copy for "
                  "each platform, and on a plan a landing page with a sign-up form and follow-up emails. In the videos "
                  "that use them, your screens are shown as you uploaded them. Every ad shows its price in credits first.",
        "clips": ["ex2-web-app-demo", "ugc-app", "ad_image"],
        "price_line": "Image ad: {image} credits. 8-second AI presenter video: {talk8} credits. Shown before you press Generate.",
        "steps": [
            ("Add your product and screenshots", "Paste your site and upload the screens you want to show. Approve the "
                                                 "claims your ads may make."),
            ("Make the ads", "Image ads with your screen in the frame, or a presenter who explains what the product does."),
            ("Send clicks to a sign-up page", "On a plan, build a landing page with a form, and let a flow email each new "
                                              "lead."),
        ],
        "gets": [
            ("Your real interface", "Videos that show your screens use the screenshots or the screen recording you "
                                    "uploaded, as they are."),
            ("A presenter for the pitch", "AI presenters explain the offer in a short vertical video."),
            ("Copy for each platform", "Ad copy, hooks, headlines and emails from the copy studio on a plan."),
            ("Leads in one place", "Forms and a contacts list with tags on Growth."),
            ('A plan for the first test',
             'On a plan, the campaign planner turns a brief into angles, hooks, scripts and a launch checklist, so the first two weeks of ads have an order to them.'),
            ('The price before each ad',
             'Every ad shows its price in credits before it is made. {signup_includes} {sc_images} image ads, {with_nothing_charged}.'),
            ('Claims checked first',
             'Briefs and copy are checked for income, health and recruiting claims and invented discounts before anything is made.'),
        ],
        "h2": {"how": "How SaaS teams use AI ads", "gets": "What SaaS teams get from AI ads", "faq": "AI ads for SaaS questions"},
        "faq": [
            ("Will it make up screenshots of my app?", "No. The videos that show your screens use the screenshots or "
                                                       "the screen recording you uploaded, as they are. Nothing of "
                                                       "your interface is generated."),
            ("Can it promise results like 10x productivity?",
             "Describe what the product does, not a number you cannot back. Income and earnings claims are blocked."),
            ("Does it connect to my CRM?", "Forms can send each submission out by webhook on Growth, and contacts live in "
                                           "Campaignify with tags."),
            ('Does it run the ads or track sign-ups from them?',
             "No. You launch the ads in each platform's ads manager. Campaignify shows page views, form submissions and smart-link clicks for what runs on Campaignify."),
            ('Can it compare us with a competitor in an ad?',
             'Competitor research gives you a brief on what a competitor offers and how they pitch it, each point with its source. Keep the ad about what your product does.'),
        ],
        "related": ["ai-ad-copy-generator", "plan-a-campaign-with-ai", "ai-marketing-platform-for-small-business"],
    },
    "ai-ads-for-mobile-apps": {
        "query": "AI ads for mobile apps",
        "nav": "AI ads for mobile apps",
        "title": "AI Ads for Mobile Apps from Your Screenshots · Campaignify",
        "description": "App ads from your own screenshots: vertical presenter videos, image ads and hooks for TikTok, "
                       "Reels and Shorts. 8 seconds is {talk8} credits, price first.",
        "eyebrow": "For mobile apps",
        "h1": "AI ads for mobile apps that show the real app",
        "answer": "Campaignify makes AI ads for mobile apps from your own screenshots. A presenter talks about the app "
                  "in a vertical video with your screens shown as you uploaded them, or an image ad pairs your screen "
                  "with a headline. An 8 second presenter video costs "
                  "{talk8} credits and an image ad {image}, shown before anything is made.",
        "clips": ["ugc-app", "team-app-demo", "ad_image"],
        "price_line": "Image ad: {image} credits. 8-second AI presenter video: {talk8} credits. Shown before you press Generate.",
        "steps": [
            ("Add the app and its screenshots", "Add the app as a product and upload the screens you want people to see."),
            ("Pick the ad", "A presenter video, or an image ad with your screen and a headline."),
            ("Download vertical and launch", "Upload the ads in the ad platform you use. Campaignify does not run them."),
        ],
        "gets": [
            ("The real app on screen", "Videos that show your screens use the screenshots you uploaded, as they are."),
            ("Vertical by default", "9:16 for TikTok, Reels and Shorts."),
            ("Hooks to test", "Several openings for the same app, each priced before it is made."),
            ('Several presenters, one script',
             'Run the same script with different presenters and see which one people watch. Each video is priced on its own line before you confirm.'),
            ('Claims checked first',
             'Scripts are checked for income, health and recruiting claims and invented discounts before a video is made, which matters most for finance and wellness apps.'),
            ('The price before each ad',
             'Every ad shows its price in credits first. {signup_includes} {sc_images} image ads, {with_nothing_charged}.'),
            ('A plan for the first test',
             'On a plan, the campaign planner turns a brief into angles, hooks and scripts, so you know which three openings to try first and what to keep.'),
        ],
        "h2": {"how": "How app teams use AI ads", "gets": "What app teams get from AI ads", "faq": "AI ads for mobile apps questions"},
        "faq": [
            ("Can it record a screen walkthrough?", "No. It shows the screenshots you upload. For a moving walkthrough, "
                                                    "record your screen and post it alongside."),
            ("Can the presenter hold a phone with my app?", "The presenter talks to camera with your screens shown as "
                                                            "they are in the video."),
            ("Does it handle health or finance app claims?",
             "Health and income claims are blocked. Describe what the app does, not what someone will gain or lose."),
            ('Which sizes do the ads come in?',
             'Videos are vertical 9:16. Image ads come in the shapes you pick when you make them: {image_shapes}.'),
            ('Does it link to the App Store?',
             'The ad is a file you upload to the ad platform, where you set the install link. Smart links on a plan give you a short link with click counts.'),
        ],
        "related": ["ai-tiktok-ad-generator", "ugc-ad-hooks", "ai-avatar-ads"],
    },
    "ai-ads-for-lead-generation": {
        "query": "AI ads for lead generation",
        "nav": "AI ads for lead generation",
        "title": "AI Ads for Lead Generation, Page and Form · Campaignify",
        "description": "The whole lead path in one place: ads, a landing page with a form, a contacts list and "
                       "follow-up emails. Plans from ${starter_usd} a month; {ads_free}.",
        "eyebrow": "For lead generation",
        "h1": "AI ads for lead generation, with the page and the follow-up",
        "answer": "Campaignify covers the lead path from the ad to the follow-up. It makes the ads, builds a landing page "
                  "with a form, keeps every lead in a contacts list with tags, and sends the emails you wrote when "
                  "someone signs up. Landing pages and flows come with Starter; forms, contacts and email sending come "
                  "with Growth. You see the price of every ad before it is made.",
        "clips": ["ugc-bakery", "ad_image", "ex-accountant-presenter"],
        "price_line": "Image ad: {image} credits. 8-second AI presenter video: {talk8} credits. Shown before you press Generate.",
        "steps": [
            ("Make the ads", "Image ads and short videos for the offer: a guide, a quote, a call or a trial."),
            ("Build the page and the form", "A landing page from blocks with a form on it, published on campaignify.ai."),
            ("Follow up on its own", "A flow emails each new lead: right away, then on the days you set."),
        ],
        "gets": [
            ("One path, not five tools", "The ad, the page, the form, the contact and the email in the same place."),
            ("Every lead kept", "A contacts list with tags and each contact's history."),
            ("Numbers for what you run here", "Page views, form submissions and smart-link clicks."),
            ("Leads sent where you work", "Webhooks out from forms on Growth."),
            ('Copy for every step',
             'On a plan, the copy studio writes the ad copy, the page copy and the follow-up emails in the same voice, so the promise in the ad matches the page it lands on.'),
            ('Claims checked along the way',
             'Ad briefs, copy and pages are checked for income, health and recruiting claims and invented discounts before they go out.'),
            ('The price before each ad',
             'Every ad shows its price in credits before it is made. {signup_includes} {sc_images} image ads, {with_nothing_charged}.'),
        ],
        "h2": {"how": "How AI ads for lead generation work", "gets": "What you get for lead generation with AI ads",
               "faq": "AI ads for lead generation questions"},
        "faq": [
            ("Does it use Facebook lead forms?", "No. Leads come through the form on your Campaignify page. You launch "
                                                 "the ads in the platform's ads manager and point them at that page."),
            ("Can it text my leads?", "Text messages in flows come with Scale. Email sending comes with Growth."),
            ("Can I see cost per lead?", "Campaignify shows page views, submissions and link clicks. Ad spend and cost "
                                         "per lead are in the ad platform."),
            ('Which plan do I need for lead generation?',
             'Starter has landing pages, flows and smart links. Growth adds forms, the contacts list and email sending. Text messages in flows come with Scale.'),
            ('Where do my leads go?',
             'Into your contacts list in Campaignify, each with tags and its history. On Growth a form can also send every submission out by webhook.'),
        ],
        "related": ["ai-ads-for-local-businesses", "plan-a-campaign-with-ai", "ai-ad-copy-generator"],
    },
    "ai-ads-for-real-estate": {
        "query": "AI ads for real estate",
        "nav": "AI ads for real estate",
        "title": "AI Ads for Real Estate Agents and Teams · Campaignify",
        "description": "Listing and lead ads from your own photos, with a page and a form to catch the enquiry. "
                       "{ads_free_nc}.",
        "eyebrow": "For real estate",
        "h1": "AI ads for real estate agents and teams",
        "answer": "Campaignify makes AI ads for real estate from your own photos: image ads for a listing or your "
                  "service, short videos with an AI presenter, the copy for each platform, and on a plan a landing page "
                  "with an enquiry form and follow-up emails. Housing ads carry extra rules on Meta and under fair "
                  "housing law; Campaignify does not check those for you, so keep the wording about the property.",
        "clips": ["ex-realtor-presenter", "ad_image", "ex-house-painter"],
        "price_line": "Image ad: {image} credits. 8-second AI presenter video: {talk8} credits. Shown before you press Generate.",
        "steps": [
            ("Add the listing or your service", "Upload your own photos and the facts: the area, the price, what is "
                                                "included."),
            ("Make the ads", "Image ads in feed and story sizes, or a presenter who introduces the listing or your team."),
            ("Catch the enquiry", "On a plan, send clicks to a page with a form and let a flow reply to each lead."),
        ],
        "gets": [
            ("Ads from your own photos", "The property in the ad is the property in your photo."),
            ("An enquiry page", "A landing page on Starter; forms and a contacts list on Growth."),
            ("Follow-up that does not slip", "Emails go out on their own when someone fills in the form."),
            ('Copy for each platform',
             'On a plan, the copy studio writes the ad copy, headlines and follow-up emails for the listing or the service, in several variants to test.'),
            ('The price before each ad',
             'Every ad shows its price in credits before it is made. {signup_includes} {sc_images} image ads, {with_nothing_charged}.'),
            ('One place for enquiries',
             'Each enquiry lands in a contacts list with tags, so a lead from one listing can be followed up about the next.'),
        ],
        "h2": {"how": "How real estate teams use AI ads", "gets": "What real estate teams get from AI ads",
               "faq": "AI ads for real estate questions"},
        "faq": [
            ("Does it check fair housing wording?",
             "No. Campaignify blocks income, health and recruiting claims and invented discounts, but not housing wording. Describe "
             "the property, not who should live there, and choose Meta's housing category in Ads Manager."),
            ("Can it make a video tour?", "No. It makes short videos from photos and presenter videos, not a walk-through "
                                          "of the home."),
            ("Can an AI presenter pose as a client?", "No. Presenters never speak as customers or give testimonials."),
            ('Does it post to the listing sites?',
             "No. It makes the ads, the page and the emails. You upload the ads in each ad platform's own ads manager."),
            ('Which plan do I need?',
             '{free_makes} Starter adds the landing page and flows; Growth adds forms, contacts and email sending.'),
        ],
        "related": ["ai-ads-for-local-businesses", "ai-ads-for-lead-generation", "ftc-meta-rules-for-ai-ads"],
    },
}
SOLUTION_ORDER = ["ai-ads-for-local-businesses", "ai-ads-for-coaches", "ai-ads-for-ecommerce", "ai-marketing-for-agencies",
                  "blaze-alternative", "predis-ai-alternative", "heygen-alternative", "topview-alternative",
                  "ai-image-ad-generator", "ai-product-video-generator", "ai-avatar-ads", "ai-facebook-ad-generator",
                  "ai-tiktok-ad-generator", "ai-ads-for-saas", "ai-ads-for-mobile-apps", "ai-ads-for-lead-generation",
                  "ai-ads-for-real-estate"]
# The footer's two columns of answer-first pages: what it does, and who it is for (public_site.footer_groups).
FEATURE_PAGES = ["ai-ugc-ad-generator", "ai-image-ad-generator", "ai-product-video-generator", "ai-avatar-ads",
                 "ai-ad-copy-generator", "ai-facebook-ad-generator", "ai-tiktok-ad-generator",
                 "ai-marketing-platform-for-small-business"]
INDUSTRY_PAGES = ["ai-ads-for-ecommerce", "ai-product-ads-for-shopify", "ai-ads-for-saas", "ai-ads-for-mobile-apps",
                  "ai-ads-for-lead-generation", "ai-ads-for-local-businesses", "ai-ads-for-real-estate",
                  "ai-ads-for-coaches", "ai-marketing-for-agencies"]


# ---------------------------------------------------------------- guides and the roundup
GUIDES_INDEX = {
    "title": "Guides to AI Ads and Marketing · Campaignify",
    "description": "Plain guides to AI UGC ads, ad rules, competitor research, credit pricing and planning a full "
                   "campaign with AI, each with its sources.",
    "h1": "Guides to AI ads and marketing",
    "answer": "These guides explain how to make AI ads that hold up, what the FTC and Meta expect, how to research "
              "competitors' ads, how credit pricing works and how to plan a campaign from ad to follow-up. Each one "
              "lists its sources.",
    "h2": "All guides",
}

GUIDES = {
    "best-ai-ugc-ad-generators": {
        "path": "/best-ai-ugc-ad-generators",
        "nav": "Best AI UGC ad generators",
        "eyebrow": "Roundup",
        "title": "Best AI UGC Ad Generators in 2026, Compared",
        "description": "8 AI UGC ad tools side by side: entry prices as listed on Nov 4, 2026, free plans or trials, "
                       "and what each is built for. Sourced, including ours.",
        "h1": "The best AI UGC ad generators in 2026, compared with sources",
        "answer": "The right AI UGC ad generator depends on what you need after the video. Arcads, Creatify, "
                  "MakeUGC, HeyGen and Topview focus on AI presenter video. AdCreative.ai and Predis.ai mix image "
                  "ads, video and social content. Campaignify adds a planner, copy, landing pages, forms and "
                  "follow-up flows around the ads. Every price below is as listed on Nov 4, 2026, with its source.",
        "disclosure": "We make Campaignify, so read our entry with that in mind.",
        "published": AS_OF, "updated_label": AS_OF_LABEL, "sources_date": AS_OF_LABEL,
        "clips": ["ugc-coffee", "ugc-serum", "ugc-sneakers"],
        "table": {
            "caption": "Prices as listed on Nov 4, 2026 at the sources below. Credits aren't comparable across tools.",
            "cols": ["Tool", "Entry paid price", "Free plan or trial", "Built for"],
            "rows": [
                ("Campaignify", "${starter_usd} a month ({starter_credits} credits); packs from ${pack_min_usd}",
                 "{free_credits_n}, {nothing_charged_lc}", "Ads plus planner, copy, pages, forms and follow-up flows"),
                ("Arcads", "Not listed on its public site; $110 a month for 10 videos per eesel.ai (Jun 17, 2026)",
                 "72-hour trial, card required", "AI actor UGC video ads"),
                ("Creatify", "$10 a month (struck from $15), 55 credits", "Free, 10 credits a month, watermark",
                 "UGC video ads plus an ad launcher"),
                ("MakeUGC", "$29 a month billed yearly, 500 credits", "\"Try any plan for $1\", renews",
                 "UGC, product and image ads"),
                ("HeyGen", "$29 a month ($24 billed annually), 600 credits", "Free, 3 watermarked videos a month",
                 "AI avatar video for creators and teams"),
                ("Topview", "$29 a month, 80 credits (G2 listing)", "10 one-time credits, watermark",
                 "Product avatars and product video"),
                ("AdCreative.ai", "$39 a month, 20 credits", "Free trial",
                 "Ad creatives, UGC videos and copy"),
                ("Predis.ai", "$19 a month (struck from $32), 1,300 credits", "3 days free, card at sign-up",
                 "Social posts, ads and UGC-style video"),
            ],
        },
        "sections": [
            ("The 8 tools at a glance", [
                ("table", ""),
                "Credits mean different things at each company, so compare what a credit buys, not how many you get. "
                "Several tools also show annual prices as monthly ones, so check whether a price is billed monthly "
                "or yearly before you compare.",
            ]),
            ("How we compared them", [
                "We read each company's own pricing or product page on Nov 4, 2026, and used third-party pages only "
                "where the company doesn't publish prices (Arcads) or where its own page wasn't read (Topview, via "
                "G2). We didn't rate output quality, because that depends on your product and your script. Make a "
                "test ad on your own product in 2 or 3 tools before you pay for a plan.",
            ]),
            ("Campaignify", [
                "Campaignify is an AI marketing platform that makes UGC-style video ads with {presenters} AI "
                "presenters, product videos and image ads from your product page, then writes the copy for "
                "{platform_count} ad platforms and keeps the landing page, forms and follow-up flows in the same "
                "workspace. Every ad has a fixed price in credits shown before you generate, and failed "
                "generations are refunded. Plans are ${starter_usd}, ${growth_usd} and ${scale_usd} a month, and "
                "you cancel from Billing. Limits: talking videos are {talk_lengths} seconds, it doesn't post or run "
                "ads, and there's no managed service.",
            ]),
            ("Arcads", [
                "Arcads makes UGC-style video ads with AI actors. Its help center says prices are on the pricing page "
                "inside the app, and describes a 72-hour trial that needs a card and turns into the Starter monthly "
                "plan. A third-party article (eesel.ai, updated Jun 17, 2026) lists $110 a month for 10 videos. "
                "Compare it with Campaignify on the [Arcads alternative page](/arcads-alternative).",
            ]),
            ("Creatify", [
                "Creatify lists URL to Video, AI Avatar, Ad Clone, an Ad Launcher and an Agent that \"Researches, "
                "creates, launches and optimizes your ads\". Its free plan gives 10 credits a month with a watermark, "
                "and paid credits stay valid for 2 months. It's the closest tool here to covering both the video and "
                "the launch. See the [Creatify alternative page](/creatify-alternative).",
            ]),
            ("MakeUGC", [
                "MakeUGC offers UGC ads, product videos and image ads with \"every leading AI model in one studio\", "
                "and 1,000+ AI actors on Pro. Its $1 trial \"renews at the plan price unless you cancel\", so "
                "set a reminder if you try it. See the [MakeUGC alternative page](/makeugc-alternative).",
            ]),
            ("HeyGen", [
                "HeyGen is an AI avatar video generator for creators and teams, with custom avatars and export up to "
                "4K on Pro. Its pricing page didn't mention UGC or product ads. It suits longer avatar videos better "
                "than short product ads. See the [HeyGen alternative page](/heygen-alternative).",
            ]),
            ("Topview", [
                "Topview lists a Video Agent, Product Avatars, 2,000+ avatars and voices on paid plans, and TikTok Ad "
                "Library access. On its G2 listing, credits expire monthly. Its G2 listing shows conflicting update "
                "dates, so confirm prices on Topview's own site. See the [Topview alternative page](/topview-alternative).",
            ]),
            ("AdCreative.ai and Predis.ai", [
                "AdCreative.ai lists AI UGC videos, ad creatives, product photoshoots, ad copy, a Compliance Checker "
                "and Competitor Insights; its Starter plan is $39 a month for 20 credits, monthly billed. Predis.ai "
                "makes social posts, ads and UGC-style ads with AI actors, with auto posting on Rise and above; every "
                "plan starts with 3 days free and a card at sign-up. See the [AdCreative.ai](/adcreative-ai-alternative) "
                "and [Predis.ai](/predis-ai-alternative) pages.",
            ]),
            ("How to choose", [
                ("ul", [
                    "Only need presenter videos and already have copy, pages and email? A video specialist may be "
                    "enough.",
                    "Want the ad, the landing page and the follow-up in one place? Look at an all-in-one tool, such "
                    "as Campaignify.",
                    "Watch the trial terms: some take a card and renew by themselves.",
                    "Check when credits expire and whether you see the cost before each generation.",
                    "Read [how credit-based pricing works](/guides/credit-based-ai-ad-pricing) before you compare "
                    "plans.",
                ]),
            ]),
        ],
        "faq_h2": "AI UGC ad generator questions",
        "faq": [
            ("Which AI UGC ad generator is cheapest?",
             "By entry price as listed on Nov 4, 2026, Creatify's $10 Standard plan was the lowest paid plan here, "
             "and Creatify, HeyGen and Topview had free tiers. Credits buy different things at each tool, so compare "
             "the cost of the ad you actually want."),
            ("Is this list neutral?",
             "We make Campaignify, so no list we write is fully neutral. Every fact about other tools is from the "
             "sources listed below, as of Nov 4, 2026, and we haven't rated anyone's output quality."),
            ("Are AI UGC ads allowed on Meta and TikTok?",
             "They can be, with care: don't present AI people as real customers, and use the platform's AI labels. "
             "See [the guide to FTC and Meta rules](/guides/ftc-meta-rules-for-ai-ads)."),
            ("Do AI UGC ads work as well as human ones?",
             "There's little public data. One self-reported test on Reddit found AI ads won on ROAS and human UGC "
             "won on click-through rate. Test both on your own product."),
        ],
        "sources": _src("arcads_help", "arcads_eesel", "creatify_pricing", "creatify_about", "makeugc",
                        "heygen_pricing", "topview_g2", "adcreative", "predis_pricing", "reddit_test"),
        "related": ["arcads-alternative", "creatify-alternative", "ai-ugc-ad-generator"],
    },
    "make-ai-ugc-ads-that-dont-look-fake": {
        "nav": "AI UGC ads that don't look fake",
        "eyebrow": "How to",
        "title": "How to Make AI UGC Ads That Don't Look Fake",
        "description": "Practical steps for AI UGC ads that look like real phone videos: short scripts, real "
                       "product photos, one idea per ad, and honest AI labels.",
        "h1": "How to make AI UGC ads that don't look fake",
        "answer": "AI UGC ads look fake when the script is long, the product is wrong and the presenter pretends to "
                  "be a customer. Keep each video to one idea in 8 to 15 seconds, start from real photos of your "
                  "product, write lines a person would actually say, and label the ad as AI. Then check every frame "
                  "before you post.",
        "published": AS_OF, "updated_label": AS_OF_LABEL, "sources_date": AS_OF_LABEL,
        "clips": ["ugc-bakery", "ugc-app", "ugc-coffee"],
        "sections": [
            ("Why AI UGC ads look fake", [
                "Most fake-looking AI ads fail in the same few places. Reviewers of AI UGC tools complain about lips "
                "that don't match the words, movement that doesn't look natural and output that glitches. Viewers "
                "also notice when a script sounds like a brochure, or when the product in the video doesn't look "
                "like the one in the shop.",
                "None of these are fixed by a better AI model alone. They're fixed by shorter scripts, better inputs "
                "and checking the result.",
            ]),
            ("1. Keep it short and about one thing", [
                "A UGC ad copies a phone video: 1 person, 1 product, 1 point. Long takes give AI more time to drift. "
                "Aim for 8 to 15 seconds, and put the hook in the first line. If you have 3 points, make 3 ads and "
                "test them against each other.",
                "In Campaignify, talking videos run {talk_lengths} seconds, and you can make a batch of up to "
                "{max_batch} variants to test different openers.",
            ]),
            ("2. Start from real product photos", [
                "The product is the part viewers check most. Use clear, well-lit photos of the real product from more "
                "than 1 angle, with the label readable. Avoid photos with other products or busy backgrounds.",
                "Campaignify reads the photos from your product page and lets you pick the ones every ad uses. AI "
                "can still get small details wrong, such as label text, so check them.",
            ]),
            ("3. Write lines people actually say", [
                ("ul", [
                    "Use short sentences and everyday words. Read the script out loud; if you wouldn't say it, cut it.",
                    "Describe what the product is and does, not adjectives about it.",
                    "Skip claims you can't back up. Campaignify blocks income, health, discount and recruiting "
                    "claims in every brief.",
                    "End with 1 clear next step, such as \"link in bio\" or \"tap to shop\".",
                ]),
                "AI can draft the lines from your product and angle. Edit them until they sound like you.",
            ]),
            ("4. Never fake a customer", [
                "An AI presenter can show and explain your product. It shouldn't claim to be a customer, share "
                "\"my results\" or give a review. The FTC bans fake testimonials, including ones from people who "
                "don't exist. Read [the guide to FTC and Meta rules](/guides/ftc-meta-rules-for-ai-ads) for the "
                "details.",
                "Campaignify flags briefs where an AI presenter speaks as a customer.",
            ]),
            ("5. Check every video before you post", [
                ("ol", [
                    "Watch with sound: do the lips match the words?",
                    "Pause on the product: is it your product, with the right shape, color and label?",
                    "Watch the hands: AI often bends fingers or objects.",
                    "Read any text on screen for spelling.",
                    "If anything is off, regenerate or drop it. Don't post a video you'd be embarrassed to explain.",
                ]),
                "In Campaignify the speech is generated with the video, so the words and lips are made together. "
                "Failed generations are refunded; finished ones you don't like are still charged, so start with 1 "
                "test video before a batch.",
            ]),
            ("6. Label it and test it", [
                "Put #ad first in the caption and turn on the platform's AI label. Then treat AI UGC as a fast way to "
                "test hooks. One self-reported test on Reddit found AI ads won on return on ad spend while human UGC "
                "won on click-through rate. Find the hook that works with AI, then decide whether to film it with a "
                "real person.",
            ]),
        ],
        "faq_h2": "AI UGC ad questions",
        "faq": [
            ("How long should an AI UGC ad be?",
             "Short: 8 to 15 seconds with 1 idea. Make several short variants instead of 1 long video."),
            ("Can AI UGC ads use testimonials?",
             "Not from AI presenters. They're not real people and have no experience of your product, so they "
             "shouldn't speak as customers."),
            ("Do I have to say the ad uses AI?",
             "Follow each platform's rules: Meta labels detected AI ad media. Put #ad first and turn on the AI label "
             "when you post."),
            ("What does an AI UGC video cost in Campaignify?",
             "An 8-second AI presenter video is {talk8} credits and a 10-second one {talk10} at 720p, shown before "
             "you generate."),
        ],
        "sources": _src("arcads_trustpilot", "ftc_martech", "meta_labels", "reddit_test"),
        "related": ["ai-ugc-ad-generator", "ugc-ad-hooks", "heygen-alternative"],
    },
    "ftc-meta-rules-for-ai-ads": {
        "nav": "FTC and Meta rules for AI ads",
        "eyebrow": "Rules",
        "title": "FTC and Meta Rules for AI Ads and Testimonials",
        "description": "What the FTC's fake review rule and Meta's AI labels mean for AI UGC ads and testimonials, "
                       "in plain language, with sources. Not legal advice.",
        "h1": "FTC and Meta rules for AI ads and testimonials",
        "answer": "AI presenters can advertise your product, but they can't pose as customers. The FTC bans fake "
                  "reviews and testimonials, including ones from people who don't exist, and Meta labels ad media it "
                  "detects as made with AI. Disclose that an ad is an ad, label AI where the platform asks, and keep "
                  "claims to what you can prove.",
        "published": AS_OF, "updated_label": AS_OF_LABEL, "sources_date": AS_OF_LABEL,
        "not_advice": "This guide is general information, not legal advice. Rules change and differ by country; "
                      "check the FTC's and each platform's own pages, or ask a lawyer about your ads.",
        "sections": [
            ("The FTC rule on fake reviews and testimonials", [
                "The US Federal Trade Commission's rule bans fake reviews and testimonials. As MarTech reported, that "
                "includes reviews and testimonials attributed to people who don't exist or who have no real "
                "experience with the business or product, and ones generated by AI.",
                "For AI UGC ads, the takeaway is simple. An AI presenter is a character, not a customer. It can say "
                "\"this serum comes in a 30 ml bottle\". It can't say \"I've used this for 3 months and my skin "
                "cleared up\", because nobody had that experience.",
            ]),
            ("What that means for your scripts", [
                ("ul", [
                    "Write as a presenter, not a reviewer: describe the product, show it, explain how it's used.",
                    "No first-person results: no \"I lost\", \"I earned\", \"it fixed my\".",
                    "No star ratings, review counts or quotes you didn't collect from real customers.",
                    "Keep health, income and before-and-after claims out unless you can prove them, and even then "
                    "check the platform's rules.",
                    "If you use real customer reviews elsewhere, keep them real and unedited in meaning.",
                ]),
            ]),
            ("Meta's AI labels", [
                "Meta adds disclosure tags to ads with AI-generated media. Marketing Dive reported in July 2026 that "
                "Meta shows an AI label in the \"About this ad\" information for ad media it detects as made "
                "with AI. Expect your AI UGC ads to carry that label, and don't try to hide it.",
                "Check Meta's own advertising standards and its current disclosure settings for your category before "
                "you launch.",
            ]),
            ("TikTok and other platforms", [
                "One industry summary (Idukki, updated Oct 14, 2026) says TikTok rejects AI ads that aren't "
                "disclosed. Check TikTok's own ad policies "
                "before you launch, and do the same for every other platform you use.",
            ]),
            ("A checklist before you post", [
                ("ol", [
                    "Put #ad (or the platform's paid partnership tool) first in the caption.",
                    "Turn on the platform's AI label.",
                    "Read the script: does the AI presenter speak as a customer anywhere? Rewrite it.",
                    "Check every claim against your label or product page.",
                    "Keep a copy of the script and the claims you approved.",
                ]),
            ]),
            ("How Campaignify handles this", [
                "Campaignify was built around these rules. AI presenters are described as AI presenters, they never "
                "speak as customers, and it flags briefs where one would. Every brief, copy variant and landing page "
                "is checked for income, health and recruiting claims and invented discounts. Each ad comes with the reminder to "
                "put #ad first and turn on the AI label. You still post the ads yourself, so the final check is "
                "yours. More on this on the [page for independent reps](/for-reps).",
            ]),
        ],
        "faq_h2": "AI ad rules questions",
        "faq": [
            ("Can I use an AI presenter in my ads?",
             "Yes, as a presenter. Don't present the AI person as a real customer or have it describe personal "
             "results."),
            ("Do I have to label AI ads on Meta?",
             "Meta labels AI-generated ad media it detects. Don't try to hide it, and check Meta's own pages for "
             "the current rules."),
            ("Are AI-written reviews allowed?",
             "No. The FTC rule bans fake reviews, including AI-generated ones and ones from people who don't exist."),
            ("Is this legal advice?",
             "No. It's a plain summary of public reporting. Check the regulator's and platform's own pages, or ask a "
             "lawyer."),
        ],
        "sources": _src("ftc_martech", "meta_labels", "tiktok_idukki"),
        "related": ["/for-reps", "make-ai-ugc-ads-that-dont-look-fake", "ai-ads-for-coaches"],
    },
    "research-competitor-ads": {
        "nav": "Research your competitors' ads",
        "eyebrow": "How to",
        "title": "How to Research Your Competitors' Ads",
        "description": "Use the Meta Ad Library and TikTok Creative Center to see competitors' ads, what to write "
                       "down, and how Competitor Snapshot does the rest.",
        "h1": "How to research your competitors' ads",
        "answer": "Start with the 2 free libraries: Meta's Ad Library shows ads running on Facebook and Instagram, "
                  "and TikTok's Creative Center shows top ads by industry. Search each competitor, note their "
                  "offers, hooks and formats, then read their pricing and reviews. Campaignify's Competitor "
                  "Snapshot does the web part for you and links you to both libraries.",
        "published": AS_OF, "updated_label": AS_OF_LABEL, "sources_date": AS_OF_LABEL,
        "about_app": True,
        "sections": [
            ("Step 1: Pick 3 to 5 real competitors", [
                "Choose the businesses your customers compare you with, not the biggest names in your category. "
                "Search the way a customer would, such as \"[your product] near me\" or \"best [product] for "
                "[need]\", and note who shows up in ads and results.",
            ]),
            ("Step 2: Search the Meta Ad Library", [
                "The Meta Ad Library lists ads that are running on Facebook, Instagram and Meta's other apps. Set the "
                "country, choose all ads, and search the competitor's name or page.",
                ("ul", [
                    "How many ads are active, and how long each has run. An ad that has run for months is likely "
                    "working.",
                    "The format: video, image or carousel, and the aspect ratio.",
                    "The first line of text and the first 3 seconds of video: that's the hook.",
                    "The offer and the call to action.",
                    "Where the ad sends people: a product page, a landing page or a form.",
                ]),
            ]),
            ("Step 3: Check TikTok's Creative Center", [
                "TikTok's Creative Center has a Top Ads section where you can filter by region, industry and goal. "
                "It doesn't always show a specific brand's ads, so search by category and study the hooks, pacing "
                "and on-screen text of the ads that perform in your space.",
            ]),
            ("Step 4: Read the rest of their funnel", [
                "Ads are only the start. Visit each competitor's site and write down their prices, plans and trial "
                "terms, their main claims, and what reviewers praise and complain about. The complaints are where "
                "your ads can win: if buyers complain about surprise renewals, an ad about easy cancelling speaks to "
                "them directly.",
            ]),
            ("Step 5: Turn notes into angles", [
                ("ol", [
                    "List the 3 hooks you saw most often.",
                    "List 3 complaints you can honestly answer.",
                    "Write 1 ad angle for each complaint.",
                    "Make your own creative. Never copy a competitor's video, music or wording.",
                ]),
            ]),
            ("How Competitor Snapshot does it", [
                "Competitor Snapshot, on every Campaignify account, researches up to 5 competitors on the live web, or "
                "finds your closest 3 to 5 if you leave the list empty. For each one it runs a few web searches and "
                "page reads and records their offers and prices, positioning, audience, funnel, what reviewers praise "
                "and complain about, and what they target in search. Then it suggests gaps, angles, objections and "
                "what to avoid.",
                "Every claim must carry a source URL that the run actually opened; claims without one are moved to "
                "an \"unverified\" list. Meta Ad Library and TikTok pages can't be read automatically, so the report "
                "links you to each competitor's Ad Library search and to TikTok Top Ads instead of guessing their "
                "ads. The most a run can cost is shown in credits before you start, you pay what it actually used, "
                "and a run that fails costs nothing. The [campaign planner](/guides/plan-a-campaign-with-ai) reads "
                "your latest snapshot.",
            ]),
        ],
        "faq_h2": "Competitor ad research questions",
        "faq": [
            ("Is the Meta Ad Library free?",
             "Yes. Anyone can search it without an ad account."),
            ("Can I see a competitor's ad spend or results?",
             "Generally no. The libraries show the ads, not their results. Long-running ads are a rough signal."),
            ("Can Campaignify copy a competitor's ad?",
             "No. It researches them and suggests angles; you make your own ads."),
            ("Which plan includes Competitor Snapshot?",
             "Every account, including Free. Each run is charged in credits, shown before you start."),
        ],
        "sources": _src("meta_library", "tiktok_cc"),
        "related": ["plan-a-campaign-with-ai", "ugc-ad-hooks", "ai-marketing-for-agencies"],
    },
    "credit-based-ai-ad-pricing": {
        "nav": "How credit-based pricing works",
        "eyebrow": "Pricing",
        "title": "How Credit-Based AI Ad Pricing Works",
        "description": "How AI ad tools charge in credits, where surprise charges come from (trials, expiry, "
                       "rounding) and a checklist to avoid them before you pay.",
        "h1": "How credit-based AI ad pricing works, and how to avoid surprise charges",
        "answer": "Most AI ad tools sell credits: a plan gives you a number each month, and each video or image "
                  "spends some of them. Surprise charges come from 4 places: trials that renew into a plan, credits "
                  "that expire, costs you don't see until after a generation, and cancelling that's hard to find. "
                  "Check all 4 before you pay.",
        "published": AS_OF, "updated_label": AS_OF_LABEL, "sources_date": AS_OF_LABEL,
        "sections": [
            ("What a credit is", [
                "A credit is a tool's own unit of spend. One tool's credit isn't worth the same as another's: 1 "
                "credit might be a static post in one tool and a fraction of a second of video in another. So "
                "\"1,000 credits\" tells you nothing until you know what a single ad costs.",
                "Ask every tool: how many credits does the ad I want cost? For video, ask per second and per "
                "resolution.",
            ]),
            ("Where surprise charges come from", [
                ("h3", "Trials that turn into a plan"),
                "Some trials take a card and start a paid plan by themselves. As listed on Nov 4, 2026, MakeUGC's $1 "
                "trial \"renews at the plan price unless you cancel\", and Predis.ai takes a card at sign-up for its "
                "3 free days, with later payments listed as non-refundable.",
                ("h3", "Credits that expire"),
                "Creatify lists that paid credits stay valid for 2 months; Topview's G2 listing says credits expire "
                "monthly; HeyGen's pricing page says credits don't carry over after cancellation. Expiring credits "
                "aren't wrong, but buy only what you'll use.",
                ("h3", "Costs you only see afterwards"),
                "When a cost depends on the model, length and resolution, it can be hard to predict. Creatify's own "
                "blog says it rounds generation length when charging credits. Credit drain is a common complaint in "
                "reviews of AI video tools, such as HeyGen users being told to top up.",
                ("h3", "Cancelling that's hard to find"),
                "Billing complaints in public reviews often come down to people who couldn't find how to cancel. "
                "Find the cancel button before you subscribe.",
            ]),
            ("A checklist before you pay", [
                ("ol", [
                    "Does the trial need a card, and does it turn into a plan by itself?",
                    "What does 1 ad of the kind I want cost, in credits?",
                    "Do I see that cost before I press Generate?",
                    "Do I get credits back if a generation fails?",
                    "When do plan credits expire? Do one-time credits expire?",
                    "Is the price billed monthly or yearly?",
                    "Where is the cancel button, and when does cancelling take effect?",
                ]),
            ]),
            ("How Campaignify prices ads", [
                "Campaignify answers each of those questions on the button. Every ad has a fixed price in credits "
                "that you see before you generate: {image} credits an image ad, {video5} a 5-second product video, "
                "{talk8} an 8-second AI presenter video and {talk10} a 10-second one. AI writing tools charge what "
                "the AI actually used, with the most a run can cost shown first. Failed generations are refunded "
                "automatically.",
                ("ul", [
                    "{free_signup_list}{one_trial}",
                    "Plans: ${starter_usd}, ${growth_usd} and ${scale_usd} a month, billed monthly. Plan credits "
                    "arrive with each payment and expire at the next renewal; they're spent first.",
                    "Packs: {pack_usd}, one-time. Pack credits never expire.",
                    "Cancel from Billing with Manage or cancel plan. The plan stays on until the end of the paid "
                    "month.",
                ]),
                "See every number on the [pricing page](/pricing).",
            ]),
        ],
        "faq_h2": "Credit pricing questions",
        "faq": [
            ("Are credits the same across AI tools?",
             "No. Each tool sets its own value, so compare the cost of a specific ad, not the credit count."),
            ("Why do credits expire?",
             "Monthly plan credits are often tied to the billing period. Look for one-time packs that don't expire "
             "if you make ads in bursts."),
            ("Do Campaignify credits expire?",
             "Plan credits expire at your next renewal. {credits_never_expire}."),
            ("What if a Campaignify generation fails?",
             "Your credits come back automatically. You only pay for ads that finish."),
        ],
        "sources": _src("makeugc", "predis_pricing", "creatify_pricing", "topview_g2", "heygen_pricing",
                        "creatify_blog", "heygen_trustpilot", "makeugc_trustpilot", "adcreative_trustpilot"),
        "related": ["/pricing", "best-ai-ugc-ad-generators", "topview-alternative"],
    },
    "plan-a-campaign-with-ai": {
        "nav": "Plan a full campaign with AI",
        "eyebrow": "How to",
        "title": "How to Plan a Full Marketing Campaign With AI",
        "description": "Plan a campaign with AI from brief to ads, landing page and follow-up flow, step by step, "
                       "and what to check yourself at each stage.",
        "h1": "How to plan a full marketing campaign with AI",
        "answer": "A full campaign has 4 parts: a plan, the ads, a page that catches the click, and a follow-up. "
                  "AI can draft each part from a short brief: your offer, audience, goal, budget and platforms. You "
                  "still decide the offer, approve every claim and launch the ads. In Campaignify the planner, "
                  "studio, pages and flows share the same product and claims.",
        "published": AS_OF, "updated_label": AS_OF_LABEL, "sources_date": AS_OF_LABEL,
        "about_app": True,
        "clips": ["ugc-coffee", "product-video", "ugc-bakery"],
        "sections": [
            ("Step 1: Write a 5-line brief", [
                ("ul", [
                    "Offer: what you sell and the price or deal.",
                    "Audience: who it's for, in 1 sentence.",
                    "Goal: sales, sign-ups or bookings.",
                    "Budget: what you'll spend on ads this month.",
                    "Platforms: where your audience is.",
                ]),
                "A short brief beats a long one. If you can't fill a line, decide it before you plan.",
            ]),
            ("Step 2: Research before you plan", [
                "Look at 3 to 5 competitors first: their offers, hooks and complaints in their reviews. Our [guide "
                "to researching competitors' ads](/guides/research-competitor-ads) walks through it. In Campaignify, "
                "Competitor Snapshot does the web research with a source for every claim, and the planner reads it.",
            ]),
            ("Step 3: Let AI draft the plan", [
                "Give the brief to an AI planner and ask for 3 angles, each with hooks, a script and the ads to make. "
                "Campaignify's planner, on Starter and up, does this and adds a credit quote for each ad, copy for "
                "each platform, a budget split, what to watch and a launch checklist. You watch every step, and "
                "ads are only generated when you confirm them at the price shown. Planning costs what the AI used, "
                "capped and shown before you start.",
            ]),
            ("Step 4: Make the ads and the copy", [
                "Make a small test batch for each angle: an image ad, a product video and an AI presenter video. "
                "Then write the copy for each platform's fields. Check every ad for product details and every line "
                "for claims you can't prove. See [how to make AI UGC ads that don't look "
                "fake](/guides/make-ai-ugc-ads-that-dont-look-fake).",
            ]),
            ("Step 5: Build the page that catches the click", [
                "Send ad traffic to a page about this one offer, not your home page. Put the promise from the ad at "
                "the top, then benefits, an FAQ and 1 call to action. Add a form if the goal is sign-ups. In "
                "Campaignify, pages are built from blocks and published on campaignify.ai (1 on Starter, unlimited "
                "on Growth), and forms and contacts are on Growth. Smart links add your UTM tags.",
            ]),
            ("Step 6: Set up the follow-up flow", [
                "Most people won't buy on the first click. A flow follows up after they sign up:",
                ("ol", [
                    "Right away: a thank-you email with what they asked for.",
                    "After 1 day: an email answering the main objection.",
                    "After 3 days: a reminder with the offer, and a tag for anyone who clicked.",
                    "If they clicked but didn't buy: a text, only if they agreed to texts.",
                ]),
                "Campaignify's flows have emails, texts, waits, tags, branches and A/B splits, and AI can draft one "
                "from a sentence. On Starter each email becomes a task with the copy ready; Growth sends emails and "
                "Scale sends texts.",
            ]),
            ("Step 7: Launch and watch", [
                "Set up each ad account, export the ads at each platform's sizes and launch them in that platform's "
                "ads manager. Watch ad results there, and watch link clicks, page views and form sign-ups in "
                "Campaignify. After a week, keep the angle that works and make new variants of it.",
            ]),
        ],
        "faq_h2": "AI campaign planning questions",
        "faq": [
            ("Can AI run the whole campaign for me?",
             "It can draft the plan, ads, copy, page and follow-up. You approve claims, spend the budget and launch "
             "the ads. Campaignify doesn't post or run ads."),
            ("Which Campaignify plan has the planner?",
             "Starter (${starter_usd} a month) and up. Forms, email sending and unlimited pages are on Growth."),
            ("Can the planner use my competitor research?",
             "Yes. It reads your latest Competitor Snapshot for the brand before it proposes angles."),
            ("Does it promise results?",
             "No. Results depend on your offer, audience and ad spend."),
        ],
        "sources": [],
        "related": ["ai-marketing-platform-for-small-business", "research-competitor-ads", "ai-ads-for-local-businesses"],
    },
    "ugc-ad-hooks": {
        "nav": "UGC ad hooks that work",
        "eyebrow": "Ideas",
        "title": "10 UGC Ad Hooks That Work, With Examples",
        "description": "10 UGC ad hook ideas you can adapt to any product, each with an example opening line and "
                       "the buyer worry it answers. Test them, don't trust them.",
        "h1": "10 UGC ad hooks that work, and how to test them",
        "answer": "A hook is the first line or first 3 seconds of an ad, and it decides whether anyone watches the "
                  "rest. The hooks that work best name a real problem or worry your buyer already has. Here are 10 "
                  "hook ideas drawn from common buyer complaints, each with an example line. Treat them as tests, "
                  "not guarantees.",
        "published": AS_OF, "updated_label": AS_OF_LABEL, "sources_date": AS_OF_LABEL,
        "clips": ["ugc-sneakers", "ugc-serum", "ugc-app"],
        "sections": [
            ("What makes a hook work", [
                "Good hooks are specific, short and about the viewer. They answer a worry the buyer already has, "
                "which is why reading competitors' reviews is the fastest way to find them. They also avoid promises "
                "you can't keep: no income, health or \"guaranteed\" claims.",
            ]),
            ("10 hook ideas with examples", [
                ("ol", [
                    "Too many tools. \"You're paying for 5 apps to do 1 job.\" Answers: tool fatigue.",
                    "The surprise bill. \"Tired of apps that keep billing you after you cancel?\" Answers: trust in "
                    "billing.",
                    "Price before you click. \"Know the price before you press the button.\" Answers: fear of hidden "
                    "costs.",
                    "Many tries for little. \"Test 10 openers today, keep the winner tomorrow.\" Answers: the cost of "
                    "testing.",
                    "Find it, then scale it. \"Find the message that works before you spend big.\" Answers: risk.",
                    "Pro work without the invoice. \"The marketing team you can't afford to hire.\" Answers: budget.",
                    "Then what? \"Your ad gets the click. Then what?\" Answers: leads that go nowhere.",
                    "Honest by default. \"Presenters, not fake customers.\" Answers: worry about fake ads.",
                    "Your take on what works. \"Here's what everyone in [your category] gets wrong.\" Answers: "
                    "sameness.",
                    "Product in hand. \"Here's what it actually looks like.\" Answers: doubt about the real product.",
                ]),
                "Swap in your own product and your buyers' words. The best version of each hook uses a phrase "
                "your customers already say.",
            ]),
            ("Where these ideas come from", [
                "Each idea maps to a complaint that shows up again and again in public reviews of software and "
                "products: billing after cancelling, costs that drain, tools that stop halfway, and ads that look "
                "fake. Your category will have its own. Read 20 reviews of your top 3 competitors and write down the "
                "phrases that repeat. Our [competitor research guide](/guides/research-competitor-ads) shows how.",
            ]),
            ("How to test hooks", [
                ("ol", [
                    "Keep everything else the same: same product, same presenter, same offer.",
                    "Change only the first line, and make 3 to 5 versions.",
                    "Run them at the same time with the same budget.",
                    "Judge on the metric that matches your goal, not only clicks.",
                    "Make new variants of the winner and test again.",
                ]),
                "AI makes this cheap. One self-reported test on Reddit put AI UGC at about $3 a video against $420 "
                "for human UGC, and found AI won on return on ad spend while human UGC won on click-through rate. "
                "That's one person's test, but it suggests using AI to find the hook, then deciding whether to film "
                "the winner with a real person.",
            ]),
            ("Testing hooks in Campaignify", [
                "Pick a product and an angle, let AI write the script, then edit the first line for each version. "
                "Make a batch of up to {max_batch} variants with different hooks or presenters; an 8-second AI "
                "presenter video is {talk8} credits and an image ad {image}, shown before you generate. On Starter "
                "and up, the copy studio writes hooks with on-screen text for video ads, and the content optimizer "
                "on Growth scores a hook and suggests rewrites.",
            ]),
        ],
        "faq_h2": "UGC hook questions",
        "faq": [
            ("How long should a hook be?",
             "1 short sentence, said in the first 3 seconds, with on-screen text that says the same thing."),
            ("How many hooks should I test?",
             "Start with 3 to 5 versions of 1 ad. Change only the hook so you know what made the difference."),
            ("Can I use a competitor's hook?",
             "Learn from the idea, but write your own words and make your own video. Never copy their creative."),
            ("Will these hooks work for my product?",
             "No hook is guaranteed. They're starting points to test against your own audience."),
        ],
        "sources": _src("reddit_test"),
        "related": ["make-ai-ugc-ads-that-dont-look-fake", "research-competitor-ads", "ai-ads-for-ecommerce"],
    },
}
GUIDE_ORDER = ["best-ai-ugc-ad-generators", "make-ai-ugc-ads-that-dont-look-fake", "ftc-meta-rules-for-ai-ads",
               "research-competitor-ads", "credit-based-ai-ad-pricing", "plan-a-campaign-with-ai", "ugc-ad-hooks"]


# ---------------------------------------------------------------- answer-first guides, Nov 12, 2026
# Ten more pages for questions buyers ask search and AI assistants. Campaignify facts only, all from the live
# numbers ({placeholders} filled by public_site.facts(), so they follow the plan switch); no other company's
# facts, so no sources to date. Same house rules: digits, no long dashes, every page opens with the answer.
AS_OF_NEW = "2026-11-12"
AS_OF_NEW_LABEL = "Nov 12, 2026"
_NEW = {"published": AS_OF_NEW, "updated_label": AS_OF_NEW_LABEL, "sources_date": AS_OF_NEW_LABEL, "sources": [],
        "about_app": True}

GUIDES.update({
    "what-is-campaignify": dict(_NEW, **{
        "nav": "What is Campaignify?",
        "eyebrow": "About",
        "title": "What Is Campaignify? The AI Ad Studio, Explained",
        "description": "Campaignify is an AI marketing platform for small businesses: UGC-style video and image ads "
                       "from a product page, plus copy, pages and follow-up.",
        "h1": "What is Campaignify?",
        "answer": "Campaignify is an AI marketing platform for small businesses. Paste a product page and it makes "
                  "UGC-style video ads with AI presenters, product videos and image ads, writes the ad copy for "
                  "{platform_count} ad platforms, and keeps the campaign plan, landing page, smart links, forms and "
                  "follow-up in one workspace. Every ad shows its price in credits before you make it, and failed "
                  "generations are refunded. Plans are ${starter_usd}, ${growth_usd} and ${scale_usd} a month.",
        "sections": [
            ("Who it's for", [
                "Campaignify is built for people who sell something and run their own ads without an agency: "
                "online stores, local businesses, coaches, apps and SaaS products, and the small agencies and "
                "freelancers who run ads for them. You don't need to film anything, hire creators or learn a video "
                "editor. You need a product page or a few photos and a clear offer.",
            ]),
            ("What it makes", [
                ("ul", [
                    "Image ads: your product photo becomes a finished ad, with the headline, price and callouts set "
                    "in real type on top, in {image_shapes}. {image} credits each.",
                    "Presenter videos: pick one of {presenters} AI presenters, write what they say, and get a "
                    "selfie-style video with your product beside them. {talk_lengths} seconds, in {talk_shapes}. "
                    "An 8-second one is {talk8} credits.",
                    "Ready-made formats: try-ons where a presenter wears your clothing, app demos where your own "
                    "screen recording plays above a presenter, and unboxings where two hands open the box.",
                    "Product videos with no person at all: a 5-second one is {video5} credits.",
                    "Ad copy: hooks, primary text, captions, headlines and emails in your brand's voice, sized for "
                    "{platform_list}.",
                ]),
                "None of the presenters is a real person, and every ad is checked against the claims you approved "
                "for the product, so it doesn't promise something you can't back up.",
            ]),
            ("What else is in the workspace", [
                "Around the ads, Campaignify plans the campaign (what to run, on which platform, week by week), "
                "researches competitors' ads, walks you through setting up each ad account, and builds the landing "
                "page, smart links and forms that catch the click. On Growth, follow-up emails go out from flows "
                "and an SEO, AEO and GEO optimizer checks your pages for search engines and AI assistants. On "
                "Scale, flows can send text messages too.",
                "{runs_short}",
            ]),
            ("What it doesn't do", [
                "It doesn't buy media for you: your ad spend stays in your own ad account. It isn't a managed "
                "service, so there's no one writing your strategy for you. And it can't make claims true: if your "
                "product doesn't do something, the compliance check keeps it out of the ad.",
            ]),
            ("How pricing works", [
                "Every ad has a fixed price in credits, shown on the button before you generate. Plans come with "
                "monthly credits: Starter is ${starter_usd} a month with {starter_credits} credits. Credit packs "
                "({pack_usd}, one-time) top up any plan. {trial_turns_answer}",
                "See every number on the [pricing page](/pricing).",
            ]),
        ],
        "faq_h2": "Questions about Campaignify",
        "faq": [
            ("Is Campaignify an agency?", "No. It's software you use yourself. It plans, makes and writes; you approve "
             "every ad and launch it."),
            ("Are the presenters real people?", "No. All {presenters} AI presenters are made with AI. None of them is "
             "a real person, so there are no creator fees or usage rights to buy."),
            ("Does Campaignify run my ads?", "{runs_short}"),
            ("What does it cost to try?", "{trial_turns_answer}"),
        ],
        "related": ["/pricing", "ai-ugc-ad-generator", "best-ai-ugc-ad-generators"],
    }),
    "how-much-do-ai-ugc-ads-cost": dict(_NEW, **{
        "nav": "What AI UGC ads cost",
        "eyebrow": "Pricing",
        "title": "How Much Do AI UGC Ads Cost? Per Ad, Worked Out",
        "description": "What an AI image ad, product video and presenter video cost in Campaignify credits, and what "
                       "a month of ad testing costs on each plan.",
        "h1": "How much do AI UGC ads cost?",
        "answer": "In Campaignify every ad has a fixed price in credits that you see before you make it: {image} "
                  "credits for an image ad, {video5} for a 5-second product video, {talk8} for an 8-second AI "
                  "presenter video and {talk10} for a 10-second one. Plans start at ${starter_usd} a month with "
                  "{starter_credits} credits, and failed generations are refunded, so you only pay for ads that "
                  "finish.",
        "sections": [
            ("The price of each kind of ad", [
                ("ul", [
                    "Image ad: {image} credits. Your product photo with the headline, price and callouts set in real "
                    "type.",
                    "Product video, 5 seconds: {video5} credits; 10 seconds: {video10}.",
                    "AI presenter video, 8 seconds: {talk8} credits; 10 seconds: {talk10}. Longer ones, up to "
                    "{talk_max} seconds, cost more and show their price before you generate.",
                    "Ad copy, hooks and headlines: AI writing tools charge what the AI actually used, with the most "
                    "a run can cost shown first.",
                ]),
                "The price is on the Generate button, so there's no surprise afterwards. If a generation fails, the "
                "credits come back automatically.",
            ]),
            ("What a month of testing costs", [
                "A simple test plan is 3 image ads, 1 per hook, in week 1, then a presenter video of the hook that "
                "wins. On Starter's {starter_credits} monthly credits that's a small part of the month, which leaves "
                "room for more image variations, product videos and a second round. Growth (${growth_usd} a month) "
                "and Scale (${scale_usd} a month) add more credits and the tools that follow the click: forms, "
                "follow-up flows and link analytics.",
                "If you need a burst of ads in one month, credit packs ({pack_usd}, one-time) top up any plan. "
                "{credits_never_expire}.",
            ]),
            ("Compared with hiring creators", [
                "With human UGC creators you pay per video, plus usage rights if you run it as an ad, and you wait "
                "for filming and edits. AI presenter videos cost the credits above and arrive in minutes, but they "
                "aren't real customers: they can't hold your product in their hands or give a real testimonial. "
                "Many sellers use AI ads to find the hook that works, then pay a creator to film that one.",
            ]),
            ("What isn't in the price", [
                "Credits pay for making ads, not for showing them. Your ad spend is separate and stays in your own "
                "ad account on each platform. {limits_run}",
            ]),
            ("Hidden costs to check in any AI ad tool", [
                ("ol", [
                    "Does the trial turn into a paid plan by itself?",
                    "What does 1 ad of the kind you want cost?",
                    "Do you see the cost before you generate?",
                    "Are failed generations refunded?",
                    "Do credits expire, and when?",
                ]),
                "More on this in [how credit-based AI ad pricing works](/guides/credit-based-ai-ad-pricing).",
            ]),
        ],
        "faq_h2": "AI ad cost questions",
        "faq": [
            ("How much is one AI image ad in Campaignify?", "{image} credits, shown on the button before you make it."),
            ("How much is an AI presenter video?", "{talk8} credits for 8 seconds and {talk10} for 10 seconds. Longer "
             "ones show their price before you generate."),
            ("Do I pay for failed ads?", "No. Failed generations are refunded automatically."),
            ("Is ad spend included?", "No. Credits pay for making ads. What you spend to show them stays in your own "
             "ad account."),
        ],
        "related": ["/pricing", "credit-based-ai-ad-pricing", "ai-ugc-ad-generator"],
    }),
    "ai-ugc-ads-vs-real-creators": dict(_NEW, **{
        "nav": "AI UGC vs real creators",
        "eyebrow": "Compare",
        "title": "AI UGC Ads vs Real UGC Creators: Which to Use",
        "description": "When AI presenter ads beat hiring UGC creators, when they don't, and how to use both: test "
                       "hooks with AI, then film the winner.",
        "h1": "AI UGC ads vs real UGC creators: which should you use?",
        "answer": "Use AI UGC ads to test many hooks, angles and formats quickly and cheaply, and use real creators "
                  "when the ad needs a real person: a genuine testimonial, hands-on use of the product, or a face "
                  "your audience knows. Most small businesses get the best of both by testing with AI first, then "
                  "paying a creator to film the hook that already won.",
        "sections": [
            ("Where AI presenter ads win", [
                ("ul", [
                    "Speed: a presenter video arrives in minutes, not after a shipping, filming and editing cycle.",
                    "Volume: you can try 5 hooks for the cost of a few credits each instead of 5 shoots.",
                    "Control: you write every word, and the compliance check keeps out claims you haven't approved.",
                    "No usage rights to negotiate: the presenters aren't real people.",
                    "Every size: the same idea in {talk_shapes} for each placement.",
                ]),
            ]),
            ("Where real creators win", [
                ("ul", [
                    "Testimonials: only a real customer can honestly say they used the product. An AI presenter "
                    "must never pretend to be one.",
                    "Hands-on proof: texture, fit, taste and results on a real body or in a real kitchen.",
                    "Trust from a known face: an influencer's audience already knows them.",
                    "Unscripted moments that feel lived-in.",
                ]),
            ]),
            ("The rules that apply to AI ads", [
                "Ad platforms and regulators care about honesty. Don't present an AI presenter as a real customer, "
                "don't invent reviews, and label AI-made content where a platform asks you to. Our guide to the "
                "[FTC and Meta rules for AI ads](/guides/ftc-meta-rules-for-ai-ads) covers what's required, with "
                "sources. In Campaignify, every line in a script is checked against the claims you approved for the "
                "product.",
            ]),
            ("A plan that uses both", [
                ("ol", [
                    "Write 3 to 5 hooks from real buyer worries.",
                    "Make an image ad for each, then a presenter video of the 2 that get the most clicks.",
                    "Run them side by side for a week on a small budget.",
                    "Send the winning hook to a real creator to film, or to a customer who agrees to a testimonial.",
                    "Keep the AI versions running for new sizes and placements.",
                ]),
                "See [10 UGC ad hooks that work](/guides/ugc-ad-hooks) for starting points.",
            ]),
            ("What it costs", [
                "In Campaignify an 8-second presenter video is {talk8} credits and an image ad {image}. Plans start "
                "at ${starter_usd} a month. A human creator's price depends on the creator, so ask for the video fee "
                "and the ad usage rights separately before you compare.",
            ]),
        ],
        "faq_h2": "AI vs creator questions",
        "faq": [
            ("Are AI UGC ads allowed on Facebook and TikTok?", "Yes, when they're honest: no fake customers or reviews, "
             "and AI labels where the platform asks for them. Check each platform's current rules."),
            ("Do AI UGC ads look fake?", "They can if the script sounds like an ad. Write like a person talking and "
             "show the product. See [how to make AI UGC ads that don't look fake](/guides/make-ai-ugc-ads-that-dont-look-fake)."),
            ("Can an AI presenter give a testimonial?", "No. Only a real customer can say they used the product. AI "
             "presenters talk about the product, not as a customer."),
        ],
        "related": ["make-ai-ugc-ads-that-dont-look-fake", "ftc-meta-rules-for-ai-ads", "ai-avatar-ads"],
    }),
    "product-video-from-product-page": dict(_NEW, **{
        "nav": "Product video from a product page",
        "eyebrow": "How to",
        "title": "How to Make a Product Video From a Product Page",
        "description": "Turn a product page into a product video ad: pick the shots, write a hook, choose a format "
                       "and size it for each platform. With or without AI.",
        "h1": "How to make a product video ad from your product page",
        "answer": "Start from the page you already have: its photos, name, price and the 2 or 3 benefits buyers care "
                  "about. Pick a format (a product-only video, an unboxing, or a presenter holding up the product), "
                  "write a 1-line hook for the first second, and export it in each platform's size. In Campaignify "
                  "you paste the product page link and it reads the photos and details for you; a 5-second product "
                  "video is {video5} credits.",
        "sections": [
            ("Step 1: Pull what you need from the page", [
                "You need clean product photos (one with a plain background helps), the product's name and price, "
                "and the benefits in plain words. Skip the long description; a video ad has room for 1 idea. In "
                "Campaignify, paste the link and it imports the photos and details into a product, where you "
                "approve the claims the ads may use.",
            ]),
            ("Step 2: Pick a format", [
                ("ul", [
                    "Product only: the product moves through a scene with no person. Good for looks and design.",
                    "Unboxing: two hands open the box and lift the product out. Good for gifts and packaging.",
                    "Try-on: a presenter wears your clothing. Good for fit and how it looks on.",
                    "Presenter: one of {presenters} AI presenters talks about the product beside it. Good for "
                    "explaining why it's better.",
                    "App demo: your own screen recording plays above a presenter. Good for apps and software.",
                ]),
            ]),
            ("Step 3: Write the hook", [
                "The first second decides if anyone keeps watching. Lead with the buyer's problem or the one thing "
                "that's different, not your brand name. \"Lid gone missing again? This one presses in and stays\" "
                "beats \"Introducing our new mug\". Write 3 and test them.",
            ]),
            ("Step 4: Size it for each platform", [
                "Vertical 9:16 for Reels, Stories, TikTok and Shorts, square or 4:5 for feeds, and 16:9 where a "
                "platform wants landscape. Campaignify makes presenter videos in {talk_shapes} and exports every ad "
                "at each platform's sizes, so you don't crop by hand.",
            ]),
            ("Step 5: Add the copy and launch", [
                "Write the primary text, headline and a clear call to action for each platform. {runs_short}",
            ]),
            ("What it costs in Campaignify", [
                "A 5-second product video is {video5} credits and a 10-second one {video10}. An 8-second presenter "
                "video is {talk8}. Prices are on the button before you generate, and failed generations are "
                "refunded. Plans start at ${starter_usd} a month.",
            ]),
        ],
        "faq_h2": "Product video questions",
        "faq": [
            ("Do I need to film anything?", "No. Campaignify works from your product photos and page. The presenters "
             "are AI, and none of them is a real person."),
            ("How long should a product video ad be?", "Short. Most product videos work at 5 to 15 seconds: one "
             "idea, one product, one call to action."),
            ("Can I use my Shopify product page?", "Yes. Paste the product link and Campaignify reads the photos and "
             "details. There's no app to install."),
        ],
        "related": ["ai-product-video-generator", "ai-product-ads-for-shopify", "ai-ads-for-ecommerce"],
    }),
    "virtual-try-on-ads": dict(_NEW, **{
        "nav": "Try-on ads without a photoshoot",
        "eyebrow": "How to",
        "title": "How to Make Try-On Video Ads Without a Photoshoot",
        "description": "Try-on ads for clothing and accessories with an AI presenter instead of a model shoot: what "
                       "to upload, how to script it, and what to check.",
        "h1": "How to make try-on video ads without a photoshoot",
        "answer": "Upload clear photos of the garment, pick an AI presenter, and choose the try-on format: the "
                  "presenter wears your clothing and shows the fit, like a mirror try-on, with a short line about "
                  "what makes it worth buying. In Campaignify it's one click from your product, priced in credits "
                  "before you generate. Check every frame against the real garment before you run it.",
        "sections": [
            ("Why try-on ads work", [
                "Shoppers buying clothes online want to see how it fits, how long the sleeves are and how it moves. "
                "A try-on answers that in a few seconds, in the same selfie style people already watch on Reels "
                "and TikTok, without booking a model, a studio and a photographer.",
            ]),
            ("Step 1: Upload good garment photos", [
                "Use a flat lay or a ghost-mannequin photo on a plain background, front and back if you have them. "
                "The clearer the photo, the closer the try-on stays to the real color, print and cut.",
            ]),
            ("Step 2: Pick the presenter and the scene", [
                "Choose one of {presenters} AI presenters whose look fits your buyer, and a place they film in. "
                "None of them is a real person, so there are no model fees or usage rights.",
            ]),
            ("Step 3: Script one detail", [
                "Say one thing the photo can't: \"the sleeves actually reach my wrists\", \"it doesn't cling\", "
                "\"heavy enough for October\". Keep it true to the product: Campaignify's compliance check holds "
                "each line to the claims you approved for it.",
            ]),
            ("Step 4: Check it against the real thing", [
                ("ul", [
                    "The color and print match the garment.",
                    "The length and fit aren't flattering beyond what the real cut does.",
                    "Logos and text on the garment are right.",
                    "Nothing suggests a real customer is speaking.",
                ]),
                "If a detail is off, change the photo or the prompt and generate again. Failed generations are "
                "refunded automatically.",
            ]),
            ("Where to run them", [
                "Try-ons are made for the vertical feeds where people shop by scrolling: Reels, Stories, TikTok and "
                "Shorts. Use the 9:16 version there and a square or 4:5 one in feeds. Pair each video with copy that "
                "answers the next question a shopper has, such as sizes, fabric or delivery, and send the click to "
                "the product page for that exact item, not your home page. Test 2 or 3 hooks on the same garment "
                "before you make try-ons for the whole range.",
            ]),
            ("Sizes and cost", [
                "Presenter videos come in {talk_shapes}, {talk_lengths} seconds. An 8-second one is {talk8} "
                "credits. Plans start at ${starter_usd} a month.",
            ]),
        ],
        "faq_h2": "Try-on ad questions",
        "faq": [
            ("What products work for try-on ads?", "Clothing and wearables: tops, jackets, dresses, hats and similar. "
             "Products you hold or use work better as presenter or unboxing videos."),
            ("Is the presenter a real model?", "No. Every presenter is made with AI. None of them is a real person."),
            ("Will it look exactly like my garment?", "It follows your photos closely, but check color, print and "
             "fit in every video before you run it, and regenerate anything that's off."),
        ],
        "related": ["ai-ads-for-ecommerce", "ai-avatar-ads", "make-ai-ugc-ads-that-dont-look-fake"],
    }),
    "demo-video-ads-for-apps": dict(_NEW, **{
        "nav": "App demo video ads",
        "eyebrow": "How to",
        "title": "How to Make App Demo Video Ads From Screen Recordings",
        "description": "App and SaaS demo ads that show your real screens: record the flow, add a presenter hook "
                       "and size it for each feed. Step by step.",
        "h1": "How to make app demo video ads from your screen recordings",
        "answer": "Record the one flow that shows your app's main benefit, keep it under 20 seconds, and put a "
                  "presenter's hook to camera before it so people know why to watch. In Campaignify's app demo "
                  "format your own screen recording plays above an AI presenter, and your screens are shown as you "
                  "recorded them, so the ad shows the real product.",
        "sections": [
            ("Why show real screens", [
                "People install apps and buy software when they can see it working. A generated mockup of your "
                "app can misrepresent what it does; a real recording can't. That's why Campaignify never redraws "
                "your screens: the recording you upload is what plays.",
            ]),
            ("Step 1: Pick one flow", [
                "One ad, one job. Pick the flow that delivers the result fastest: creating the first invoice, "
                "finding a match, logging a workout. Start the recording one tap before the good part.",
            ]),
            ("Step 2: Record it clean", [
                ("ul", [
                    "Use demo data that looks real but contains nobody's personal information.",
                    "Turn off notifications and set the clock and battery to something tidy.",
                    "Move slowly enough to follow; pause a beat on the result.",
                    "Record in portrait for a phone app and landscape for a web app.",
                ]),
            ]),
            ("Step 3: Write the hook", [
                "The presenter opens with the problem, to camera, in a line: \"I used to spend Sunday night on "
                "invoices.\" Then the screens show the fix. Keep the claim to what the recording proves.",
            ]),
            ("Step 4: Size and copy", [
                "Make vertical 9:16 for Reels, TikTok and Shorts and square or 4:5 for feeds. Then write the copy "
                "for each platform, including the store or sign-up link. Campaignify writes hooks, primary text and "
                "headlines for {platform_list}.",
            ]),
            ("Mistakes that sink app ads", [
                ("ul", [
                    "Opening on a logo or a splash screen instead of the problem.",
                    "Showing 5 features in 20 seconds, so none of them lands.",
                    "Claiming a result the recording doesn't show, such as \"saves 10 hours a week\".",
                    "Tiny text: zoom in on the part of the screen that matters, or record on a larger font.",
                    "Sending the click to a home page instead of the store listing or sign-up.",
                ]),
            ]),
            ("Cost and limits", [
                "App demos are priced in credits like every format, shown on the button before you generate, and "
                "failed generations are refunded. {runs_short}",
            ]),
        ],
        "faq_h2": "App demo ad questions",
        "faq": [
            ("Will Campaignify change how my app looks?", "No. In an app demo your screens are shown as you recorded "
             "them."),
            ("How long should an app demo ad be?", "Usually 15 to 30 seconds: a hook, the flow and the result."),
            ("Does it work for web apps and SaaS?", "Yes. Record the web app in the browser and use the same format."),
        ],
        "related": ["ai-ads-for-mobile-apps", "ai-ads-for-saas", "ugc-ad-hooks"],
    }),
    "how-many-ad-creatives-to-test": dict(_NEW, **{
        "nav": "How many ads to test",
        "eyebrow": "Strategy",
        "title": "How Many Ad Creatives Should You Test at Once?",
        "description": "How many ad creatives a small budget can test, how to vary them so you learn something, and "
                       "when to retire an ad. A plain testing plan.",
        "h1": "How many ad creatives should you test at once?",
        "answer": "Test 3 to 5 creatives at a time, each with a different hook, and change only one thing between "
                  "them so you can tell what worked. A small budget can't give 20 ads a fair chance. Keep the "
                  "winner, replace the rest with new hooks, and repeat every week or two. Campaignify's planner "
                  "lays out this cycle for you, and an image ad is {image} credits, so a round of tests is cheap "
                  "to make.",
        "sections": [
            ("Why fewer is better on a small budget", [
                "Every ad needs enough people to see it before its numbers mean anything. Split a small daily "
                "budget across 20 ads and none of them gets there. 3 to 5 ads, each with a real difference, give "
                "you an answer within days instead of weeks.",
            ]),
            ("What to vary", [
                ("ol", [
                    "The hook first: the problem or promise in the first line or first second.",
                    "Then the format: image ad, presenter video, product video, unboxing or try-on.",
                    "Then the offer or call to action.",
                    "Last, details like colors and backgrounds.",
                ]),
                "Change one of these at a time. If two ads differ in hook and format, you won't know which one "
                "did the work.",
            ]),
            ("A 3-week cycle", [
                ("ul", [
                    "Week 1: 3 image ads, one per hook.",
                    "Week 2: a presenter video of the best hook, next to the winning image.",
                    "Week 3: keep the winner, retire the rest, write 2 new hooks.",
                ]),
                "That's the shape of the plan Campaignify's AI campaign planner writes, adjusted to your budget "
                "and platforms.",
            ]),
            ("When to retire an ad", [
                "Retire an ad when its cost per result climbs well above your others for several days, or when "
                "frequency rises and clicks fall, which usually means the same people have seen it too often. "
                "Don't kill an ad on day 1; give it the budget you planned.",
            ]),
            ("Read the results the right way", [
                "Judge each ad on the result you actually want, such as purchases, leads or sign-ups, not on likes or "
                "views. A cheap click that never buys is an expensive ad. Compare ads over the same days and the same "
                "audience, write down what each one tested, and keep a running list of hooks that won and lost. After "
                "a few rounds that list is worth more than any single ad, because it tells you what your buyers care "
                "about.",
            ]),
            ("Making enough variations", [
                "Variation is where AI helps most. In Campaignify, one product makes image ads in "
                "{image_shapes}, presenter videos and product videos, and you can make up to {max_batch} at once. "
                "Every ad shows its price before you generate.",
            ]),
        ],
        "faq_h2": "Creative testing questions",
        "faq": [
            ("How long should I run a creative test?", "Long enough for each ad to get the budget you planned, often "
             "about a week. Decide the budget before you start."),
            ("Should every ad have a different hook?", "Yes, when you're testing hooks. Keep everything else the same."),
            ("How many ads can Campaignify make at once?", "Up to {max_batch} in one batch, each priced before you "
             "generate."),
        ],
        "related": ["plan-a-campaign-with-ai", "ugc-ad-hooks", "ai-facebook-ad-generator"],
    }),
    "landing-page-for-ads": dict(_NEW, **{
        "nav": "Do ads need a landing page?",
        "eyebrow": "How to",
        "title": "Do You Need a Landing Page for Your Ads?",
        "description": "When ads should send people to a landing page instead of your home page, what the page needs, "
                       "and how to track which ad brought each lead.",
        "h1": "Do you need a landing page for your ads?",
        "answer": "Yes, whenever the ad makes one specific offer. A landing page that repeats the ad's promise and "
                  "asks for one action converts the click better than a home page full of other choices, and it "
                  "tells you which ad brought each lead. Campaignify builds the landing page, the smart link and "
                  "the form in the same workspace as the ads, so the claims match.",
        "sections": [
            ("When a home page is fine", [
                "If you sell one product and your product page already leads with the same promise as the ad, "
                "send people there. For an online store, the product page often is the landing page.",
            ]),
            ("When you need a landing page", [
                ("ul", [
                    "The ad offers something specific: a free consult, a quote, a discount, a download.",
                    "You run ads for one service out of many.",
                    "You want leads (a name and email or phone) rather than a checkout.",
                    "You want to compare which ad or platform brings the best leads.",
                ]),
            ]),
            ("What the page needs", [
                ("ol", [
                    "A headline that repeats the ad's hook, word for word if you can.",
                    "The offer and what happens next, in 2 or 3 lines.",
                    "Proof you can back up: what's included, how it works, a real guarantee.",
                    "One form or button. One.",
                    "Fast loading on a phone, because that's where most ad clicks happen.",
                ]),
            ]),
            ("Track which ad brought the lead", [
                "Give each ad its own link. Campaignify's smart links record clicks per day, referrer, country, "
                "device and UTM source, and the page and form record views and submissions, so every lead on the "
                "leads board shows where it came from. Smart-link analytics and forms are on Growth.",
            ]),
            ("Follow up fast", [
                "A lead goes cold quickly. Set a flow to send a reply as soon as the form comes in: an email on "
                "Growth, or a text message on Scale. Keep it short and personal, and say what happens next.",
            ]),
            ("Common landing page mistakes", [
                ("ul", [
                    "A headline that doesn't match the ad, so the visitor wonders if they're in the right place.",
                    "A long form that asks for things you don't need yet. Name and email or phone is often enough.",
                    "Menus and links that lead away from the offer.",
                    "Claims on the page that the ad didn't make, or that you can't back up.",
                    "No clear answer to what happens after they submit.",
                ]),
            ]),
            ("In Campaignify", [
                "Landing pages and smart links come with Starter (${starter_usd} a month); forms, contacts, email "
                "in flows and link analytics with Growth (${growth_usd}); text messages in flows with Scale "
                "(${scale_usd}). The page uses the same product and approved claims as your ads.",
            ]),
        ],
        "faq_h2": "Landing page questions",
        "faq": [
            ("Can I use my own website instead?", "Yes. Use a smart link to track clicks and send people to any page."),
            ("Do I need a developer to build the page?", "No. Campaignify builds it from your product and offer, and "
             "you edit the words."),
            ("Which plan has forms?", "Forms and contacts are on Growth and Scale."),
        ],
        "related": ["follow-up-leads-from-ads", "ai-ads-for-lead-generation", "ai-ads-for-local-businesses"],
    }),
    "follow-up-leads-from-ads": dict(_NEW, **{
        "nav": "Follow up with ad leads",
        "eyebrow": "How to",
        "title": "How to Follow Up With Leads From Your Ads",
        "description": "Reply to ad leads fast with an automatic follow-up flow: what to send first, when to send "
                       "the next one, and when to stop.",
        "h1": "How to follow up with leads from your ads automatically",
        "answer": "Send the first reply within minutes of the form coming in, say what happens next, then follow "
                  "up 2 or 3 more times over about a week before you stop. An automatic flow does this for every "
                  "lead, the same way, even at night. In Campaignify, forms feed contacts and flows send the "
                  "emails on Growth and text messages on Scale, from the same workspace as your ads.",
        "sections": [
            ("Why speed matters", [
                "Someone who just filled in your form is thinking about your offer right now. An hour later "
                "they've moved on, and they may have filled in a competitor's form too. A reply in minutes, even "
                "an automatic one, keeps you first in line.",
            ]),
            ("A simple 4-message flow", [
                ("ol", [
                    "Right away: thanks, what happens next, and how to reach you.",
                    "Day 1: the one thing they need to decide, such as a price, a time slot or an example.",
                    "Day 3: an answer to the most common worry, such as cost, time or risk.",
                    "Day 7: a short last note that leaves the door open.",
                ]),
                "Stop when they reply or book. Nobody wants a 12th email.",
            ]),
            ("Email or text?", [
                "Email works for almost every offer and can carry detail. Text messages get read fast and suit "
                "appointments and local services, but they need clear consent, an easy way to opt out and care "
                "with timing. Ask for permission on the form, and follow the messaging rules for your country.",
            ]),
            ("What to write", [
                "Match the ad. If the ad promised a free quote, the first message is about the quote, not your "
                "company history. Write like a person, use their first name, and make the next step one click. "
                "Campaignify's copy studio writes emails in your brand's voice from the same product and claims "
                "as your ads.",
            ]),
            ("Keep it personal and legal", [
                "Automatic doesn't have to sound automatic. Use the name they gave, mention the offer they asked "
                "about, and sign with a real name and a way to reply. Include an unsubscribe link in emails and an "
                "opt-out in texts, honor them right away, and only message people who gave you their details for "
                "this offer. A smaller list of people who want to hear from you beats a big one that marks you as "
                "spam.",
            ]),
            ("Track what works", [
                "Watch replies and bookings per message. If nobody acts on message 2, rewrite it. Every lead "
                "sits on Campaignify's leads board by stage, with the smart link or form it came from.",
            ]),
            ("In Campaignify", [
                "Flows come with Starter (${starter_usd} a month). Email sending in flows, forms and contacts are "
                "on Growth (${growth_usd}), and text messages in flows on Scale (${scale_usd}).",
            ]),
        ],
        "faq_h2": "Lead follow-up questions",
        "faq": [
            ("How fast should I reply to an ad lead?", "Within minutes if you can. An automatic first reply makes "
             "that possible at any hour."),
            ("How many follow-ups should I send?", "3 or 4 over about a week usually covers it. Stop as soon as they "
             "reply."),
            ("Which Campaignify plan sends texts?", "Text messages in flows are on Scale. Email in flows is on Growth "
             "and Scale."),
        ],
        "related": ["landing-page-for-ads", "ai-ads-for-lead-generation", "ai-ads-for-real-estate"],
    }),
    "aeo-geo-for-small-business": dict(_NEW, **{
        "nav": "AEO and GEO for small business",
        "eyebrow": "Search",
        "title": "What Are AEO and GEO? AI Search for Small Business",
        "description": "AEO and GEO explained: how to make your pages the answer search engines and AI assistants "
                       "give, with a checklist for small business sites.",
        "h1": "What are AEO and GEO, and how does a small business show up in AI answers?",
        "answer": "AEO (answer engine optimization) and GEO (generative engine optimization) are SEO for answers: "
                  "making your pages the ones that search engines quote in answer boxes and that AI assistants like "
                  "ChatGPT, Claude and Perplexity read and cite. The basics are answering the question in the first "
                  "lines, clear facts with dates, structured data, and letting AI crawlers in. Campaignify's SEO, "
                  "AEO and GEO optimizer checks a page for all of it on Growth.",
        "sections": [
            ("How AI answers pick sources", [
                "Assistants that search the web read pages much like a search engine does, then quote or summarize "
                "the ones that answer the question plainly. A page that buries the answer under an intro, hides "
                "facts in images, or blocks AI crawlers is easy to skip.",
            ]),
            ("A checklist for each page", [
                ("ol", [
                    "Answer the page's main question in the first 2 or 3 sentences.",
                    "One clear title and one main heading that match what people search.",
                    "Short sections with headings that are questions or plain labels.",
                    "Facts with numbers and dates: prices, hours, service areas, what's included.",
                    "An FAQ block with the questions customers actually ask.",
                    "Structured data (schema.org JSON-LD) for your business, products and FAQs.",
                    "A robots.txt that lets search and AI crawlers read the page.",
                    "An llms.txt file: a plain summary of your site for AI assistants.",
                ]),
            ]),
            ("What not to do", [
                "Don't stuff keywords, don't invent reviews or ratings in structured data, and don't write pages "
                "for questions you can't answer honestly. Search engines and assistants both reward pages people "
                "find useful, and fake signals can get a site ignored.",
            ]),
            ("How Campaignify's optimizer helps", [
                "Paste a page's address and the optimizer reads what search and answer engines read: the title, "
                "description, headings, links, images, structured data and text. It returns pass, warn and fail "
                "checks in 3 groups (SEO, AEO and GEO). Its AI part writes a better title and description, an "
                "answer-first summary, an FAQ block, keyword and topic ideas and the facts for schema.org JSON-LD, "
                "and it can draft an llms.txt file for your site. It's on Growth (${growth_usd} a month) and "
                "Scale.",
            ]),
            ("Where ads fit", [
                "AI answers and search take time to build; ads bring visitors now. Many small businesses run both: "
                "ads for the offer this month, answer-first pages for the questions buyers will keep asking.",
            ]),
        ],
        "faq_h2": "AEO and GEO questions",
        "faq": [
            ("Is AEO different from SEO?", "It's part of it. SEO gets a page found; AEO and GEO make it the answer "
             "that gets quoted or cited."),
            ("What is llms.txt?", "A plain text file at your site's root that summarizes your site and links its key "
             "pages for AI assistants."),
            ("Which Campaignify plan has the SEO, AEO and GEO optimizer?", "Growth and Scale."),
        ],
        "related": ["ai-marketing-platform-for-small-business", "ai-ads-for-local-businesses", "/pricing"],
    }),
})
GUIDE_ORDER += ["what-is-campaignify", "how-much-do-ai-ugc-ads-cost", "ai-ugc-ads-vs-real-creators",
                "product-video-from-product-page", "virtual-try-on-ads", "demo-video-ads-for-apps",
                "how-many-ad-creatives-to-test", "landing-page-for-ads", "follow-up-leads-from-ads",
                "aeo-geo-for-small-business"]
