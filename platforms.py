"""Ad platforms: the copy fields each one takes, with character limits.

Used by the copy studio (what to write and how long), the content optimizer
(length checks) and the ad-account hub (export specs). `limit` is the hard cap
where the platform enforces one; `rec` is the length that shows in full in most
placements. Numbers come from each platform's own ad-spec pages (linked in
adhub.py); check there when a platform changes its specs.
"""

PLATFORMS = {
    "meta": {"name": "Facebook & Instagram", "short": "Meta",
             "ad_fields": [("Primary text", 125, None), ("Headline", 40, None), ("Description", 30, None)],
             "caption": 2200, "hashtags": (3, 5)},
    "tiktok": {"name": "TikTok", "short": "TikTok",
               "ad_fields": [("Ad text", 100, 100)],
               "caption": 4000, "hashtags": (3, 5)},
    "youtube": {"name": "YouTube", "short": "YouTube",
                "ad_fields": [("Headline", 15, 15), ("Long headline", 90, 90), ("Description", 90, 90)],
                "caption": 5000, "hashtags": (2, 3)},
    "google": {"name": "Google Search", "short": "Google",
               "ad_fields": [("Headline 1", 30, 30), ("Headline 2", 30, 30), ("Headline 3", 30, 30),
                             ("Description 1", 90, 90), ("Description 2", 90, 90)],
               "caption": None, "hashtags": (0, 0)},
    "x": {"name": "X", "short": "X",
          "ad_fields": [("Post text", 280, 280)],
          "caption": 280, "hashtags": (1, 2)},
    "linkedin": {"name": "LinkedIn", "short": "LinkedIn",
                 "ad_fields": [("Introductory text", 150, 600), ("Headline", 70, 200)],
                 "caption": 3000, "hashtags": (2, 3)},
    "pinterest": {"name": "Pinterest", "short": "Pinterest",
                  "ad_fields": [("Title", 40, 100), ("Description", 50, 500)],
                  "caption": 500, "hashtags": (0, 3)},
    "snapchat": {"name": "Snapchat", "short": "Snapchat",
                 "ad_fields": [("Brand name", 25, 25), ("Headline", 34, 34)],
                 "caption": 250, "hashtags": (0, 2)},
    "reddit": {"name": "Reddit", "short": "Reddit",
               "ad_fields": [("Headline", 150, 300)],
               "caption": 40000, "hashtags": (0, 0)},
    "email": {"name": "Email", "short": "Email",
              "ad_fields": [("Subject line", 50, 150), ("Preview text", 90, 150), ("Body", None, None)],
              "caption": None, "hashtags": (0, 0)},
    "landing": {"name": "Landing page", "short": "Page",
                "ad_fields": [("Headline", 70, None), ("Subheadline", 160, None), ("Benefits", None, None),
                              ("Call to action", 25, None)],
                "caption": None, "hashtags": (0, 0)},
}

ORDER = ["meta", "tiktok", "youtube", "google", "x", "linkedin", "pinterest", "snapchat", "reddit", "email", "landing"]
SOCIAL = ["meta", "tiktok", "youtube", "x", "linkedin", "pinterest", "snapchat", "reddit"]

# What the copy studio can write.
KINDS = {
    "ad": {"name": "Ad copy", "blurb": "Every text field of an ad, sized for the platform."},
    "hooks": {"name": "Hooks", "blurb": "Opening lines for the first three seconds of a video, with on-screen text."},
    "captions": {"name": "Captions", "blurb": "Post captions with a call to action and a few hashtags."},
    "headlines": {"name": "Headlines", "blurb": "Short headlines to test against each other."},
    "email": {"name": "Email", "blurb": "Subject line, preview text and a short email."},
    "landing": {"name": "Landing page copy", "blurb": "Headline, subheadline, benefits, FAQ and call to action."},
}


def fields_for(kind: str, platform: str) -> list[tuple]:
    """[(field name, recommended chars or None, hard limit or None)] for one variant."""
    p = PLATFORMS.get(platform) or PLATFORMS["meta"]
    if kind == "ad":
        return p["ad_fields"]
    if kind == "email":
        return PLATFORMS["email"]["ad_fields"]
    if kind == "landing":
        return PLATFORMS["landing"]["ad_fields"] + [("FAQ", None, None)]
    if kind == "hooks":
        return [("Spoken hook", 90, None), ("On-screen text", 40, None)]
    if kind == "captions":
        return [("Caption", min(p["caption"] or 300, 300), p["caption"]), ("Hashtags", None, None)]
    if kind == "headlines":
        hl = next((f for f in p["ad_fields"] if "eadline" in f[0] or f[0] == "Title"), ("Headline", 40, None))
        return [("Headline", hl[1], hl[2])]
    return [("Text", None, None)]


def length_report(fields: list[dict], kind: str, platform: str) -> list[dict]:
    """Annotate generated fields with their length against the platform's numbers."""
    spec = {name.lower(): (rec, hard) for name, rec, hard in fields_for(kind, platform)}
    out = []
    for f in fields:
        name, text = str(f.get("name") or ""), str(f.get("text") or "")
        rec, hard = spec.get(name.lower(), (None, None))
        n = len(text)
        state = "over" if hard and n > hard else "long" if rec and n > rec else "ok"
        out.append({"name": name, "text": text, "chars": n, "rec": rec, "limit": hard, "state": state})
    return out
