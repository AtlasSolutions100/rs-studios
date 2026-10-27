"""What RS Studios can make: the tools (each backed by one model), what they
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
        "name": "Image ad",
        "blurb": "A finished ad image from your product photos and a short brief.",
        "model": "higgsfield/ads-studio/v1.0",
        "kind": "image",
        "usd_each": 0.1071,          # per generation
        "max_photos": 10,
        "aspects": ["9:16", "1:1", "4:5", "16:9"],
    },
    "photo_to_video": {
        "name": "Photo to video",
        "blurb": "Bring one product photo to life: a 5 or 10 second clip.",
        "model": "bytedance/seedance-2.5/image-to-video",
        "kind": "video",
        "usd_per_second": 0.206,     # 720p list price
        "durations": [5, 10],
        "max_photos": 1,
    },
    "motion_copy": {
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
    if tool == "photo_to_video":
        return t["usd_per_second"] * duration
    if tool == "motion_copy":
        secs = math.ceil(min(t["max_seconds"], ref_seconds or t["max_seconds"]))
        return t["usd_per_second"] * secs
    raise KeyError(tool)


def price_credits(tool: str, **kw) -> int:
    return int(math.ceil(cost_usd(tool, **kw) * MARKUP * 100))


# Templates: the angles from the playbook. {product} and {brand} are filled from
# the workspace's brand kit; members can edit the brief before generating.
TEMPLATES = [
    {"id": "swap", "name": "The swap", "tool": "photo_to_video", "duration": 5,
     "brief": "Morning kitchen, soft window light. A hand slides a drip coffee maker aside, scoops "
              "{product} from the bag into a mug of hot water and stirs. Steam rises. The bag stays "
              "in frame with its label facing camera, unchanged.",
     "overlay": "One scoop. Hot water. 30 seconds."},
    {"id": "label", "name": "Label read", "tool": "photo_to_video", "duration": 5,
     "brief": "Slow, steady camera pan across the {product} bag's ingredient panel on a wooden "
              "counter. Shallow depth of field, warm light. The label text stays sharp and unchanged.",
     "overlay": "What's in it, as printed on the bag"},
    {"id": "how", "name": "How to make it", "tool": "photo_to_video", "duration": 10,
     "brief": "Real-time how-to: one scoop of {product} into a mug, hot water poured from a kettle, "
              "a quick stir, first sip. Bright, clean kitchen. Product label unchanged.",
     "overlay": "No machine, no pods"},
    {"id": "two-pm", "name": "The 2 PM moment", "tool": "ad_image", "aspect": "9:16",
     "brief": "Afternoon desk scene: laptop, notebook, a fresh mug of coffee beside the {product} bag, "
              "warm 2 PM light. Headline: \"Your afternoon cup, in 30 seconds.\" Clean, premium, "
              "no people's faces.",
     "audience": "Remote workers and office workers 25-45"},
    {"id": "iced", "name": "Iced", "tool": "ad_image", "aspect": "4:5",
     "brief": "Tall glass of iced coffee on a sunny patio table next to the {product} bag. Ice, "
              "condensation, a few coffee beans. Headline: \"Iced in 30 seconds.\"",
     "audience": "Women 22-40 who love iced coffee"},
    {"id": "cost", "name": "Cost per cup", "tool": "ad_image", "aspect": "1:1",
     "brief": "Split layout: a $7 café cup on the left, a mug made with {product} on the right with "
              "the bag behind it. Headline: \"$2.33 a morning on Subscribe & Save.\"",
     "audience": "Budget-minded daily coffee drinkers"},
    {"id": "gym", "name": "Gym bag", "tool": "ad_image", "aspect": "9:16",
     "brief": "Gym bag packed for an early morning: shaker, towel, earbuds and the {product} bag. "
              "Morning light through a window. Headline: \"Packed the night before.\"",
     "audience": "Gym-goers 22-35"},
    {"id": "copy-trend", "name": "Copy a trending video", "tool": "motion_copy",
     "brief": "Same moves and timing as the reference video, with the {product} bag as the hero "
              "product. Keep the label and bag colors exactly as in the photo.",
     "overlay": ""},
]

TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}


def fill(text: str, brand: dict) -> str:
    return (text or "").replace("{product}", brand.get("product") or "the product") \
                       .replace("{brand}", brand.get("name") or "")
