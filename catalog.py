"""What Campaignify can make: the tools (each backed by one model), what they
cost, and the ready-made templates (the angles that sell for us).

Costs are the vendor's list prices in USD at the settings we use. Members pay
in RS credits: 1 credit = $0.01, price = cost x MARKUP, rounded up, so the
markup is the margin. Change MARKUP in settings, not here.
"""
import math
import os

MARKUP = float(os.environ.get("RS_MARKUP", "2.0"))

TOOLS = {
    "ad_image": {
        "preview": "ad_image",
        "name": "Image ad",
        "blurb": "A finished ad image from your product photos and a short brief.",
        "model": "higgsfield/ads-studio/v1.0",
        "kind": "image",
        "usd_each": 0.1071,          # per generation
        "max_photos": 10,
        "aspects": ["9:16", "1:1", "4:5", "16:9"],
    },
    "photo_to_video": {
        "preview": "photo_to_video",
        "name": "Photo to video",
        "blurb": "Bring one product photo to life: a 5 or 10 second clip.",
        "model": "bytedance/seedance-2.5/image-to-video",
        "kind": "video",
        "usd_per_second": 0.206,     # 720p list price
        "durations": [5, 10],
        "max_photos": 1,
    },
    "ugc_video": {
        "preview": "how",
        "name": "Talking video",
        "blurb": "A presenter or narrator says your script, with your product on screen. Speech is generated with the video.",
        # Reference-to-video with native audio: product photos as references, the
        # presenter, voice and spoken lines in the prompt. Price assumed equal to
        # image-to-video until the vendor's estimate says otherwise; the worker
        # checks the vendor's free estimate before submitting (see worker.py).
        "model": "bytedance/seedance-2.5/reference-to-video",
        "kind": "video",
        "usd_per_second": float(os.environ.get("RS_TALKING_USD_PER_SECOND", "0.206")),
        "durations": [8, 10, 15],
        "max_photos": 4,
        "aspects": ["9:16", "1:1", "16:9"],
    },
    "motion_copy": {
        "preview": "motion_copy",
        "name": "Copy a video's motion",
        "blurb": "Re-create a reference video's moves with your product and presenter.",
        "model": "higgsfield/genjutsu-light/motion-transfer/v1.0",
        "kind": "video",
        "usd_per_second": 0.10,      # 720p, per second of reference used (max 15)
        "max_seconds": 15,
        "max_photos": 8,
    },
}


def cost_usd(tool: str, *, batch: int = 1, duration: float = 5, ref_seconds: float = 0) -> float:
    t = TOOLS[tool]
    if tool == "ad_image":
        return t["usd_each"] * max(1, batch)
    if tool in ("photo_to_video", "ugc_video"):
        return t["usd_per_second"] * duration
    if tool == "motion_copy":
        secs = math.ceil(min(t["max_seconds"], ref_seconds or t["max_seconds"]))
        return t["usd_per_second"] * secs
    raise KeyError(tool)


def price_credits(tool: str, **kw) -> int:
    return int(math.ceil(cost_usd(tool, **kw) * MARKUP * 100))


# ---------------------------------------------------------------- creation flow
# What the backend can do. Talking videos use the reference-to-video model's
# native speech (the presenter and voice are described in the prompt). Not in
# the vendor's API today: lip-sync to an uploaded voice track, a text-to-speech
# voice library, and a fixed presenter identity across videos (each video draws
# its presenter from the description). RS_TALKING=0 hides talking formats.
def capabilities() -> dict:
    return {"talking": os.environ.get("RS_TALKING", "1") != "0",
            "lipsync_own_audio": False, "voice_library": False, "fixed_presenter": False}


# Ad types in Create, in the order they're offered. `script` says what the
# script step writes: spoken lines, an image headline + concept, or scenes.
AD_TYPES = [
    {"id": "ugc", "name": "UGC presenter", "tool": "ugc_video", "script": "spoken", "presenter": True, "talking": True,
     "line": "A presenter holds your product and says your script to camera, selfie-style.", "preview": "site/ugc-coffee"},
    {"id": "voiceover", "name": "Product video + voiceover", "tool": "ugc_video", "script": "spoken", "presenter": False,
     "talking": True, "line": "Your product on screen while a narrator reads the script.", "preview": "site/show-amber"},
    {"id": "image", "name": "Image ad", "tool": "ad_image", "script": "image",
     "line": "Finished ad images with a headline, in up to three shapes.", "preview": "previews/ad_image"},
    {"id": "product_video", "name": "Product video", "tool": "photo_to_video", "script": "scene",
     "line": "A 5 or 10 second clip that brings one product photo to life.", "preview": "previews/photo_to_video"},
    {"id": "motion", "name": "Copy a video's motion", "tool": "motion_copy", "script": "scene",
     "line": "Your product in the moves and timing of a reference clip.", "preview": "previews/motion_copy"},
]
AD_TYPE_BY_ID = {a["id"]: a for a in AD_TYPES}
TOOL_AD_TYPE = {"ad_image": "image", "photo_to_video": "product_video", "motion_copy": "motion", "ugc_video": "ugc"}


def ad_types() -> list[dict]:
    caps = capabilities()
    return [a for a in AD_TYPES if not a.get("talking") or caps["talking"]]


# AI presenters: described, never real people. Each video draws its presenter
# from the description, so two videos with the same presenter look alike but
# not identical.
PRESENTERS = [
    {"id": "maya", "name": "Maya", "look": "woman in her late 20s, curly dark hair, warm smile, casual knit sweater",
     "vibe": "Warm, casual", "setting": "a bright, lived-in kitchen", "hue": 28},
    {"id": "jordan", "name": "Jordan", "look": "man in his early 30s, short beard, plain t-shirt, relaxed posture",
     "vibe": "Easygoing", "setting": "a sunny living room with plants", "hue": 200},
    {"id": "priya", "name": "Priya", "look": "woman in her mid 30s, long straight black hair, minimal makeup, linen shirt",
     "vibe": "Calm, expert", "setting": "a tidy home office with soft daylight", "hue": 300},
    {"id": "sam", "name": "Sam", "look": "person in their mid 20s, short bleached hair, oversized hoodie, energetic",
     "vibe": "Upbeat, Gen Z", "setting": "a bedroom with fairy lights", "hue": 140},
    {"id": "elena", "name": "Elena", "look": "woman in her 50s, silver bob haircut, glasses, cardigan",
     "vibe": "Trusted, grounded", "setting": "a cozy reading nook", "hue": 15},
    {"id": "marcus", "name": "Marcus", "look": "man in his 40s, close-cropped hair, button-up shirt with sleeves rolled",
     "vibe": "Straight-talking", "setting": "a modern kitchen island", "hue": 220},
    {"id": "lena", "name": "Lena", "look": "woman in her early 20s, freckles, messy bun, gym clothes",
     "vibe": "Sporty, quick", "setting": "a bright home gym corner", "hue": 90},
    {"id": "theo", "name": "Theo", "look": "man in his late 20s, wavy hair, denim jacket, friendly grin",
     "vibe": "Friendly, chatty", "setting": "a cafe table by the window", "hue": 260},
]
PRESENTER_BY_ID = {p["id"]: p for p in PRESENTERS}

VOICES = [
    {"id": "warm_f", "name": "Warm & friendly", "desc": "a warm, friendly female voice, natural American accent", "who": "female"},
    {"id": "upbeat_f", "name": "Upbeat", "desc": "an upbeat, energetic female voice, natural American accent", "who": "female"},
    {"id": "calm_m", "name": "Calm & assured", "desc": "a calm, assured male voice, natural American accent", "who": "male"},
    {"id": "chatty_m", "name": "Casual & chatty", "desc": "a casual, chatty male voice, natural American accent", "who": "male"},
    {"id": "narrator_deep", "name": "Narrator, deep", "desc": "a deep, smooth narrator voice", "who": "narrator"},
    {"id": "narrator_bright", "name": "Narrator, bright", "desc": "a bright, clear narrator voice", "who": "narrator"},
]
VOICE_BY_ID = {v["id"]: v for v in VOICES}
WORDS_PER_SECOND = 2.4


def max_words(duration: int) -> int:
    """Roughly how many spoken words fit a clip (natural pace)."""
    return int(duration * WORDS_PER_SECOND)


def talking_prompt(ad_type: str, product: str, script: str, presenter_id: str = "", voice_id: str = "",
                   setting: str = "") -> str:
    """The prompt for a talking video (built by the worker from the job's params)."""
    product = product or "the product"
    voice = VOICE_BY_ID.get(voice_id) or VOICES[0]
    say = " ".join((script or "").split())
    keep = (f"The {product} looks exactly as in the reference photos: same packaging, label, text and colors; never "
            "invent other branding.")
    if ad_type == "voiceover":
        return (f"Short product video of the {product}. Clean, well-lit close-ups and hands using it in a real home; "
                f"smooth, simple camera moves. {keep} A narrator, never seen on camera, says in {voice['desc']}: "
                f"\"{say}\" The narration is clear and matches the pacing of the shots. No on-screen text, no subtitles, "
                "no music.")
    pr = PRESENTER_BY_ID.get(presenter_id) or PRESENTERS[0]
    where = setting or pr["setting"]
    return (f"Vertical selfie-style UGC video filmed on a phone. A {pr['look']}, in {where}, holds the {product} up to "
            f"the camera and talks directly to the viewer in a natural, unscripted tone with {voice['desc']}. They say: "
            f"\"{say}\" Lip movements match the speech exactly. {keep} Natural light, slight handheld movement, "
            "no on-screen text, no subtitles, no music.")


# Templates: the angles from the playbook. {product} and {brand} are filled from
# the workspace's brand kit; members can edit the brief before generating.
# "fits": "any" works for any product you can photograph; "coffee" templates
# were written for a coffee product and assume one.
TEMPLATES = [
    {"id": "hero", "preview": "ad_image", "line": "Clean studio shot, headline on top", "name": "Product hero", "tool": "ad_image",
     "aspect": "4:5", "fits": "any",
     "brief": "Premium studio product shot: the {product} centred on a clean surface, soft directional light, a "
              "subtle shadow and plenty of empty space for a headline. Packaging, label and colors exactly as in "
              "the photo. Headline: \"{product}\" in clean, modern type.",
     "audience": ""},
    {"id": "routine", "preview": "how", "line": "Your product in an everyday moment", "name": "Daily routine", "tool": "ad_image",
     "aspect": "9:16", "fits": "any",
     "brief": "Everyday lifestyle scene in a bright, tidy kitchen: the {product} on the counter as part of someone's "
              "morning routine, hands in frame, no faces. Natural window light. The label stays sharp and "
              "unchanged. Headline: \"Part of the morning.\"",
     "audience": ""},
    {"id": "swap", "fits": "coffee", "preview": "swap", "line": "One scoop, hot water, 30 seconds", "name": "The swap", "tool": "photo_to_video", "duration": 5,
     "brief": "Morning kitchen, soft window light. A hand slides a drip coffee maker aside, scoops "
              "{product} from the bag into a mug of hot water and stirs. Steam rises. The bag stays "
              "in frame with its label facing camera, unchanged.",
     "overlay": "One scoop. Hot water. 30 seconds."},
    {"id": "label", "fits": "coffee", "preview": "label", "line": "Slow pan across the label", "name": "Label read", "tool": "photo_to_video", "duration": 5,
     "brief": "Slow, steady camera pan across the {product} bag's ingredient panel on a wooden "
              "counter. Shallow depth of field, warm light. The label text stays sharp and unchanged.",
     "overlay": "What's in it, as printed on the bag"},
    {"id": "how", "fits": "coffee", "preview": "how", "line": "Real-time how-to, first sip", "name": "How to make it", "tool": "photo_to_video", "duration": 10,
     "brief": "Real-time how-to: one scoop of {product} into a mug, hot water poured from a kettle, "
              "a quick stir, first sip. Bright, clean kitchen. Product label unchanged.",
     "overlay": "No machine, no pods"},
    {"id": "two-pm", "fits": "coffee", "preview": "two-pm", "line": "The afternoon-slump desk shot", "name": "The 2 PM moment", "tool": "ad_image", "aspect": "9:16",
     "brief": "Afternoon desk scene: laptop, notebook, a fresh mug of coffee beside the {product} bag, "
              "warm 2 PM light. Headline: \"Your afternoon cup, in 30 seconds.\" Clean, premium, "
              "no people's faces.",
     "audience": "Remote workers and office workers 25-45"},
    {"id": "iced", "fits": "coffee", "preview": "iced", "line": "Iced coffee, sunny patio", "name": "Iced", "tool": "ad_image", "aspect": "4:5",
     "brief": "Tall glass of iced coffee on a sunny patio table next to the {product} bag. Ice, "
              "condensation, a few coffee beans. Headline: \"Iced in 30 seconds.\"",
     "audience": "Women 22-40 who love iced coffee"},
    {"id": "cost", "fits": "coffee", "preview": "cost", "line": "Café cup vs. your cup at home", "name": "Cost per cup", "tool": "ad_image", "aspect": "1:1",
     "brief": "Split layout: a $7 café cup on the left, a mug made with {product} on the right with "
              "the bag behind it. Headline: \"Skip the café line, keep the café taste.\"",
     "audience": "Budget-minded daily coffee drinkers"},
    {"id": "gym", "fits": "coffee", "preview": "gym", "line": "Packed for the early gym", "name": "Gym bag", "tool": "ad_image", "aspect": "9:16",
     "brief": "Gym bag packed for an early morning: shaker, towel, earbuds and the {product} bag. "
              "Morning light through a window. Headline: \"Packed the night before.\"",
     "audience": "Gym-goers 22-35"},
    {"id": "copy-trend", "fits": "coffee", "preview": "copy-trend", "line": "Your product in a trending video's moves", "name": "Copy a trending video", "tool": "motion_copy",
     "brief": "Same moves and timing as the reference video, with the {product} bag as the hero "
              "product. Keep the label and bag colors exactly as in the photo.",
     "overlay": ""},
]

TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}


def fill(text: str, brand: dict) -> str:
    return (text or "").replace("{product}", brand.get("product") or "the product") \
                       .replace("{brand}", brand.get("name") or "")


def first_run_templates() -> list[dict]:
    """Image templates for the guided first ad: cheapest tool, any-product ones first."""
    imgs = [t for t in TEMPLATES if t["tool"] == "ad_image"]
    return sorted(imgs, key=lambda t: t.get("fits") != "any")


# ---------------------------------------------------------------- credits

def starter_credits() -> int:
    """Free credits for a new self-serve workspace: enough for STARTER_IMAGE_ADS
    single image ads at today's price. RS_STARTER_CREDITS overrides."""
    env = os.environ.get("RS_STARTER_CREDITS", "").strip()
    if env.isdigit():
        return int(env)
    return STARTER_IMAGE_ADS * price_credits("ad_image", batch=1)


STARTER_IMAGE_ADS = int(os.environ.get("RS_STARTER_IMAGE_ADS", "2"))

# Credit packs: one-time Prices on the shared Stripe account (product
# metadata brand=Campaignify, pack=<code>). Face value is 100 credits per dollar;
# the bigger packs add bonus credits, paid for out of the markup (at 2x the
# Studio pack still sells credits at ~1.73x our cost, before Stripe's fee).
# STRIPE_PRICE_<CODE> overrides a price id (e.g. for a test-mode account).
PACKS = [
    {"code": "starter", "name": "Starter", "usd": 29, "credits": 2900, "bonus": 0,
     "price_id": "price_1UVd2ERskSkrZOvOBwL2BJVU"},
    {"code": "pro", "name": "Pro", "usd": 79, "credits": 8500, "bonus": 600,
     "price_id": "price_1UVd2FRskSkrZOvOxLP4KMcR"},
    {"code": "studio", "name": "Studio", "usd": 199, "credits": 23000, "bonus": 3100,
     "price_id": "price_1UVd2GRskSkrZOvOegXnFALF"},
]
for _p in PACKS:
    _p["price_id"] = os.environ.get(f"STRIPE_PRICE_{_p['code'].upper()}", _p["price_id"])
PACK_BY_CODE = {p["code"]: p for p in PACKS}


def rates() -> dict:
    """What credits buy, for pricing pages."""
    return {"talk10": price_credits("ugc_video", duration=10),
            "image": price_credits("ad_image", batch=1),
            "video5": price_credits("photo_to_video", duration=5),
            "video10": price_credits("photo_to_video", duration=10),
            "motion_per_s": price_credits("motion_copy", ref_seconds=1),
            "motion_max": price_credits("motion_copy", ref_seconds=TOOLS["motion_copy"]["max_seconds"])}
