"""Ad-account setup hub: step-by-step setup per ad platform, tracking basics,
export sizes, and official links.

Every URL here was checked against the platform's own site (October 2026):
only first-party domains, no affiliate or shortened links. Where we couldn't
confirm a deep link (some help centers block automated checks), the guide
links the platform's own tool or help-center home instead of guessing.
Platform UIs change; each guide says to follow the platform's current screens.
"""

PLATFORMS = {
    "meta": {
        "name": "Facebook & Instagram (Meta)",
        "manager": ("Ads Manager", "https://www.facebook.com/adsmanager"),
        "steps": [
            ("Set up a business portfolio", "In Meta Business Suite, create a business portfolio for your brand, or "
             "use the one you have. Add the people who will run ads.", "https://business.facebook.com/"),
            ("Create the ad account", "In the business portfolio's settings, add an ad account. Pick the currency and "
             "time zone carefully: they're hard to change later.", None),
            ("Add a payment method", "In the ad account's billing settings. Ads won't run without one.", None),
            ("Connect your Page and Instagram", "Ads run from a Facebook Page, and from an Instagram professional "
             "account if you link it.", None),
            ("Install the Meta Pixel", "Add the pixel to your site and set up the events that matter (Purchase, "
             "Lead, AddToCart). The Conversions API sends the same events from your server.",
             "https://developers.facebook.com/documentation/meta-pixel/get-started"),
            ("Create the campaign", "In Ads Manager, choose the objective that matches your goal (Sales, Leads, "
             "Traffic), set the budget and audience, and upload your ads in 4:5 and 9:16.", None),
            ("Read the advertising standards", "Most rejected ads break a rule in here.",
             "https://transparency.meta.com/policies/ad-standards/"),
        ],
        "pixel": ("Meta Pixel and Conversions API", [
            ("Get started with the Meta Pixel", "https://developers.facebook.com/documentation/meta-pixel/get-started"),
            ("Conversions API", "https://developers.facebook.com/docs/marketing-api/conversions-api")]),
        "specs": None,
        "policy": ("Meta Advertising Standards", "https://transparency.meta.com/policies/ad-standards/"),
        "exports": [("feed", "Feed 4:5", 1080, 1350), ("story", "Stories & Reels 9:16", 1080, 1920),
                    ("square", "Square 1:1", 1080, 1080)],
    },
    "tiktok": {
        "name": "TikTok",
        "manager": ("TikTok Ads Manager", "https://ads.tiktok.com/"),
        "steps": [
            ("Create a TikTok Ads Manager account", "Sign up with your business email, then fill in your business "
             "details and billing country (it sets your currency and can't be changed).",
             "https://ads.tiktok.com/resources/help/article/create-tiktok-ads-manager-account"),
            ("Add payment", "Choose automatic or manual payment in Ads Manager's payment settings.", None),
            ("Set up the TikTok Pixel", "Install the pixel on your site and verify the events you'll optimize for.",
             "https://ads.tiktok.com/resources/help/article/get-started-pixel"),
            ("Make 9:16 videos with sound", "In-feed ads are full-screen vertical video. Keep text out of the edges "
             "where the caption and buttons sit.",
             "https://ads.tiktok.com/resources/help/article/tiktok-auction-in-feed-ads"),
            ("Create the campaign", "Pick the objective, set budget and audience, add the video, ad text (up to "
             "100 characters) and call to action.", None),
            ("Read the advertising policies", "Rules for ad formats, landing pages and restricted products.",
             "https://ads.tiktok.com/resources/help/article/advertising-on-tiktok-first-things-to-note"),
        ],
        "pixel": ("TikTok Pixel", [("Set up and verify the pixel",
                                    "https://ads.tiktok.com/resources/help/article/get-started-pixel")]),
        "specs": ("In-feed ad specs", "https://ads.tiktok.com/resources/help/article/tiktok-auction-in-feed-ads"),
        "policy": ("TikTok Advertising Policies",
                   "https://ads.tiktok.com/resources/help/article/advertising-on-tiktok-first-things-to-note"),
        "exports": [("vertical", "Vertical 9:16", 1080, 1920), ("square", "Square 1:1", 1080, 1080)],
    },
    "youtube": {
        "name": "YouTube & Google Ads",
        "manager": ("Google Ads", "https://ads.google.com/"),
        "steps": [
            ("Create a Google Ads account", "Sign in with a Google account, add your business and billing details.",
             "https://support.google.com/google-ads/answer/6366720"),
            ("Link your YouTube channel", "Video ads run from videos on a YouTube channel. Link the channel to Google "
             "Ads so you can use its videos and audiences.", "https://support.google.com/google-ads/answer/3063482"),
            ("Set up conversion measurement", "Add the Google tag to your site and create conversion actions for "
             "purchases or leads.", "https://support.google.com/google-ads/answer/1722022"),
            ("Upload your videos", "Vertical 9:16 for Shorts, 16:9 for in-stream. Upload to your channel (they can "
             "be unlisted).", "https://support.google.com/google-ads/answer/2375464"),
            ("Search ads too?", "Responsive search ads take up to 15 headlines (30 characters) and 4 descriptions "
             "(90 characters). Campaignify's copy studio writes them.", "https://support.google.com/google-ads/answer/7684791"),
            ("Read the advertising policies", "", "https://support.google.com/adspolicy/answer/6008942"),
        ],
        "pixel": ("Google tag and conversion actions", [
            ("About conversion measurement", "https://support.google.com/google-ads/answer/1722022")]),
        "specs": ("Video ad formats", "https://support.google.com/google-ads/answer/2375464"),
        "policy": ("Google Ads policies", "https://support.google.com/adspolicy/answer/6008942"),
        "exports": [("shorts", "Shorts 9:16", 1080, 1920), ("instream", "In-stream 16:9", 1920, 1080),
                    ("square", "Square 1:1", 1080, 1080)],
    },
    "x": {
        "name": "X",
        "manager": ("X Ads", "https://ads.x.com/"),
        "steps": [
            ("Create the ads account", "Sign in to X Ads with the brand's X account, choose your country and time "
             "zone, and add payment.", "https://business.x.com/en/help/account-setup/ads-account-creation"),
            ("Set up conversion tracking", "Install the X Pixel and define the events you want to count.",
             "https://business.x.com/en/help/campaign-measurement-and-analytics/conversion-tracking-for-websites"),
            ("Check the creative specs", "Image and video sizes, lengths and text limits.",
             "https://business.x.com/en/help/campaign-setup/creative-ad-specifications"),
            ("Create the campaign", "Pick the objective, audience and budget, then add your post text (280 "
             "characters) and media.", None),
            ("Read the ads policies", "", "https://business.x.com/en/help/ads-policies.html"),
        ],
        "pixel": ("X Pixel", [("Conversion tracking for websites",
                               "https://business.x.com/en/help/campaign-measurement-and-analytics/conversion-tracking-for-websites")]),
        "specs": ("Creative ad specs", "https://business.x.com/en/help/campaign-setup/creative-ad-specifications"),
        "policy": ("X Ads policies", "https://business.x.com/en/help/ads-policies.html"),
        "exports": [("square", "Square 1:1", 1080, 1080), ("wide", "Landscape 16:9", 1920, 1080)],
    },
    "linkedin": {
        "name": "LinkedIn",
        "manager": ("Campaign Manager", "https://www.linkedin.com/campaignmanager/"),
        "steps": [
            ("Create the ad account", "In Campaign Manager, create an ad account tied to your company's LinkedIn "
             "Page, with currency and billing.", "https://www.linkedin.com/help/lms/answer/a426102"),
            ("Add the Insight Tag", "The LinkedIn Insight Tag measures conversions and builds website audiences.",
             "https://www.linkedin.com/help/lms/answer/a418880"),
            ("Pick the format", "Single image ads use 1200 x 627; carousel cards 1080 x 1080. Intro text can run to "
             "600 characters, but the first 150 or so show before 'see more'.",
             "https://business.linkedin.com/advertise/ads/ads-guide"),
            ("Create the campaign", "Choose the objective, audience by job, industry or company, and budget.", None),
            ("Read the advertising policies", "", "https://www.linkedin.com/legal/ads-policy"),
        ],
        "pixel": ("LinkedIn Insight Tag", [("Add the Insight Tag to your website",
                                            "https://www.linkedin.com/help/lms/answer/a418880")]),
        "specs": ("LinkedIn Ads Guide", "https://business.linkedin.com/advertise/ads/ads-guide"),
        "policy": ("LinkedIn Advertising Policies", "https://www.linkedin.com/legal/ads-policy"),
        "exports": [("single", "Single image 1.91:1", 1200, 627), ("square", "Square 1:1", 1080, 1080)],
    },
    "pinterest": {
        "name": "Pinterest",
        "manager": ("Pinterest Ads", "https://ads.pinterest.com/"),
        "steps": [
            ("Get a business account", "Create a Pinterest business account, or convert your personal one.",
             "https://help.pinterest.com/en/business/article/get-a-business-account"),
            ("Install the Pinterest tag", "Track checkouts, sign-ups and page visits from your ads.",
             "https://help.pinterest.com/en/business/article/install-the-pinterest-tag"),
            ("Make 2:3 Pins", "Standard image Pins work best at 2:3 (1000 x 1500). Titles take up to 100 characters; "
             "the first 40 show in most feeds.", "https://help.pinterest.com/en/business/article/pinterest-product-specs"),
            ("Create the campaign", "In Pinterest Ads, choose the objective, targeting (interests and keywords "
             "work well), budget and Pins.", None),
            ("Read the advertising guidelines", "", "https://policy.pinterest.com/en/advertising-guidelines"),
        ],
        "pixel": ("Pinterest tag", [("Install the Pinterest tag",
                                     "https://help.pinterest.com/en/business/article/install-the-pinterest-tag")]),
        "specs": ("Ad specs", "https://help.pinterest.com/en/business/article/pinterest-product-specs"),
        "policy": ("Advertising guidelines", "https://policy.pinterest.com/en/advertising-guidelines"),
        "exports": [("pin", "Standard Pin 2:3", 1000, 1500), ("full", "Full screen 9:16", 1080, 1920),
                    ("square", "Square 1:1", 1000, 1000)],
    },
    "snapchat": {
        "name": "Snapchat",
        "manager": ("Snapchat Ads Manager", "https://ads.snapchat.com/"),
        "steps": [
            ("Create the ad account", "Sign up in Snapchat Ads Manager with your business details.",
             "https://businesshelp.snapchat.com/s/article/create-ad-account"),
            ("Install the Snap Pixel", "Add the pixel to your website to measure and optimize for conversions.",
             "https://businesshelp.snapchat.com/s/article/pixel-website-install"),
            ("Make full-screen vertical ads", "Single image or video ads are full-screen 9:16. Keep logos and text "
             "away from the top and bottom edges.", None),
            ("Create the campaign", "Pick the goal, audience and budget, then add a brand name and headline.", None),
            ("Read the advertising policies", "", "https://www.snap.com/ad-policies"),
        ],
        "pixel": ("Snap Pixel", [("Install the Snap Pixel on your website",
                                  "https://businesshelp.snapchat.com/s/article/pixel-website-install")]),
        "specs": None,
        "policy": ("Snap Advertising Policies", "https://www.snap.com/ad-policies"),
        "exports": [("vertical", "Full screen 9:16", 1080, 1920)],
    },
    "reddit": {
        "name": "Reddit",
        "manager": ("Reddit Ads", "https://ads.reddit.com/"),
        "steps": [
            ("Create the ads account", "Sign up for Reddit Ads with a Reddit account, then add business and payment "
             "details.", "https://ads.reddit.com/"),
            ("Install the Reddit Pixel", "Measure conversions and build retargeting audiences.",
             "https://business.reddithelp.com/s/article/reddit-pixel"),
            ("Pick your communities", "Target by community and interest. Write like a member of the community, not "
             "a brochure.", None),
            ("Create the campaign", "Choose the objective and budget, add a headline and your image or video.", None),
            ("Read the advertising policy", "",
             "https://business.reddithelp.com/s/article/Reddit-Advertising-Policy-Overview"),
        ],
        "pixel": ("Reddit Pixel", [("About the Reddit Pixel", "https://business.reddithelp.com/s/article/reddit-pixel")]),
        "specs": None,
        "policy": ("Reddit Advertising Policy", "https://business.reddithelp.com/s/article/Reddit-Advertising-Policy-Overview"),
        "exports": [("square", "Square 1:1", 1080, 1080), ("portrait", "Portrait 4:5", 1080, 1350),
                    ("wide", "Landscape 16:9", 1920, 1080)],
    },
}
ORDER = ["meta", "tiktok", "youtube", "x", "linkedin", "pinterest", "snapchat", "reddit"]

# Copy-studio platform key for each hub platform (for "write copy for X").
COPY_PLATFORM = {"meta": "meta", "tiktok": "tiktok", "youtube": "youtube", "x": "x", "linkedin": "linkedin",
                 "pinterest": "pinterest", "snapchat": "snapchat", "reddit": "reddit"}

TRACKING_BASICS = [
    ("Install one tag per platform", "Each ad platform has its own pixel or tag. Put it on every page (most site "
                                    "builders have a field for it) and confirm it fires."),
    ("Track the action you want", "Set up the event that matches your goal (Purchase, Lead, CompleteRegistration) "
                                  "and optimize the campaign for it, not for clicks."),
    ("Add server-side events if you can", "Meta's Conversions API and TikTok's Events API send the same events from "
                                          "your server, which holds up better when browsers block tags."),
    ("Use UTM tags on every link", "utm_source, utm_medium and utm_campaign let you see each platform's traffic in "
                                   "your own analytics. Campaignify smart links pass them through."),
    ("Tell people", "Pixels use cookies. Say so in your privacy policy and ask for consent where the law requires it."),
]


def export_by(platform: str, fmt: str):
    p = PLATFORMS.get(platform)
    if not p:
        return None
    return next((e for e in p["exports"] if e[0] == fmt), None)


def all_urls() -> list[str]:
    """Every external link in the hub (the tests check they're first-party and https)."""
    out = []
    for p in PLATFORMS.values():
        out.append(p["manager"][1])
        out += [s[2] for s in p["steps"] if s[2]]
        out += [u for _, u in p["pixel"][1]]
        for k in ("specs", "policy"):
            if p[k]:
                out.append(p[k][1])
    return out


OFFICIAL_HOSTS = {"www.facebook.com", "business.facebook.com", "developers.facebook.com", "transparency.meta.com",
                  "ads.tiktok.com", "ads.google.com", "support.google.com", "ads.x.com", "business.x.com",
                  "www.linkedin.com", "business.linkedin.com", "ads.pinterest.com", "help.pinterest.com",
                  "policy.pinterest.com", "ads.snapchat.com", "businesshelp.snapchat.com", "www.snap.com",
                  "ads.reddit.com", "business.reddithelp.com"}
