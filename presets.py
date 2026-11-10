"""The preset library: one record per thing Campaignify can make (quality wave 2, the audit's "preset object").

A preset is a fixed recipe. The member supplies a product and a choice; the preset owns the rest. Every record
holds the same contract (the quality-bar report's nine parts), so a new format is one more record, not new code:

    format        "image", "video" or "talking"
    aspects       the shapes it can be made in (aspect = the one it starts in)
    inputs        what the member must supply ("product_photo", "headline", "script", "presenter", "badge", ...)
    recipe        the prompt template: a brief with {product} and {brand}, or the name of the prompt builder
    model/params  the vendor model (from its tool, catalog.TOOLS) and the settings the worker sends with it
    beats         the shot or beat structure
    text_layers   the words drawn on the image afterwards by textlayer.py (never by the image model); () = none
    qa            the automatic checks a finished asset gets (image_qa.CHECKS; VIDEO_CHECKS aren't automated yet)
    locks         what has to stay identical between assets (product, presenter, voice)
    price         how it is priced: price_credits() is catalog.price_credits for its tool, nothing new

Three kinds:

    type      the five ad types of Create (UGC presenter, voiceover, image ad, product video, motion copy)
    template  a ready-made scene on one ad type: the 14 that existed before this file, kept word for word
              (tests/fixtures/presets_snapshot.json is their output before the move; a test compares), plus
              scene statics for every product category
    static    an image whose words are set as real type by textlayer.py: clean headline, offer, feature
              callouts, review card, comparison, and the text-free marketplace image (TEXT_LAYERS, default on)

catalog.AD_TYPES and catalog.TEMPLATES are built from here (legacy_types(), legacy_templates()) and are the same
dicts as before; catalog.TEMPLATE_BY_ID also knows the new presets, so "?template=<id>", a job's template name
and its thumbnail work for all of them.

Presenter identity (paid test, Nov 2026, run on the vendor by the owner's session): an image-model keyframe of a
presenter with the product kept the product label exactly; Seedance 2.5 omni_reference with that keyframe as its
reference gave good lip-sync on a scripted line, and the same still as an image reference held the presenter's
face in a new scene. So a presenter's identity is ONE approved still per presenter situation (IDENTITY below);
the talking presets carry it as a lock. The keyframe-first video pipeline that uses it is the next build: nothing
here changes what a talking video sends to the vendor today.
"""
import os
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- the records as they were (word for word)
# Ad types in Create, in the order they're offered. `script` says what the
# script step writes: spoken lines, an image headline + concept, or scenes.
_TYPES = [
    {"id": "ugc", "name": "UGC presenter", "tool": "ugc_video", "script": "spoken", "presenter": True, "talking": True,
     "line": "A presenter says your script to camera, selfie-style, with your product beside them.", "preview": "site/ugc-coffee"},
    {"id": "voiceover", "name": "Product video + voiceover", "tool": "ugc_video", "script": "spoken", "presenter": False,
     "talking": True, "line": "Your product on screen while a narrator reads the script.", "preview": "site/show-amber"},
    {"id": "image", "name": "Image ad", "tool": "ad_image", "script": "image",
     "line": "Finished ad images with a headline, in up to three shapes.", "preview": "previews/ad_image"},
    {"id": "product_video", "name": "Product video", "tool": "photo_to_video", "script": "scene",
     "line": "A 5 or 10 second clip that brings one product photo to life.", "preview": "previews/photo_to_video"},
    {"id": "motion", "name": "Copy a video's motion", "tool": "motion_copy", "script": "scene",
     "line": "Your product in the moves and timing of a reference clip.", "preview": "previews/motion_copy"},
]

# Templates: the angles from the playbook. {product} and {brand} are filled from
# the workspace's brand kit; members can edit the brief before generating.
# "fits": "any" works for any product you can photograph; "coffee" templates
# were written for a coffee product and assume one.
_TEMPLATES = [
    {"id": "hero", "preview": "tpl-hero", "line": "Clean studio shot, headline on top", "name": "Product hero", "tool": "ad_image",
     "aspect": "4:5", "fits": "any",
     "brief": "Premium studio product shot: the {product} centred on a clean surface, soft directional light, a "
              "subtle shadow and plenty of empty space for a headline. Packaging, label and colors exactly as in "
              "the photo. Headline: \"{product}\" in clean, modern type.",
     "audience": ""},
    {"id": "routine", "preview": "tpl-routine", "line": "Your product in an everyday moment", "name": "Daily routine", "tool": "ad_image",
     "aspect": "9:16", "fits": "any",
     "brief": "Everyday lifestyle scene in a bright, tidy kitchen: the {product} on the counter as part of someone's "
              "morning routine, hands in frame, no faces. Natural window light. The label stays sharp and "
              "unchanged. Headline: \"Part of the morning.\"",
     "audience": ""},
    {"id": "flatlay", "preview": "tpl-flatlay", "line": "Overhead flat lay with a few props", "name": "Flat lay",
     "tool": "ad_image", "aspect": "4:5", "fits": "any",
     "brief": "Overhead flat lay on a soft neutral surface: the {product} in the centre, a few simple everyday props "
              "around it with plenty of breathing room, soft even daylight. Packaging, label and colors exactly as in "
              "the photo. Headline: \"Made for your day.\"",
     "audience": ""},
    {"id": "inhand", "preview": "tpl-inhand", "line": "A hand holding it, outdoors", "name": "In hand", "tool": "ad_image",
     "aspect": "9:16", "fits": "any",
     "brief": "A hand holds the {product} up against a bright, softly blurred outdoor background, natural light, no "
              "face in frame. The label faces the camera, sharp and unchanged. Headline: \"Take it with you.\"",
     "audience": ""},
    {"id": "gift", "preview": "tpl-gift", "line": "Gift moment, ribbon and card", "name": "Gift idea", "tool": "ad_image",
     "aspect": "1:1", "fits": "any",
     "brief": "Warm gifting scene: the {product} next to a ribbon and a blank card on a wooden table, cozy evening light. "
              "Packaging exactly as in the photo. Headline: \"An easy gift.\"",
     "audience": ""},
    {"id": "turn", "preview": "photo_to_video", "line": "Slow turn on a clean surface", "name": "Slow turn",
     "tool": "photo_to_video", "duration": 5, "fits": "any",
     "brief": "The {product} on a clean surface, the camera slowly circles it in soft studio light. The label and colors "
              "stay sharp and unchanged.",
     "overlay": ""},
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

# ---------------------------------------------------------------- scene statics for every category (new)
# Same shape and same pipeline as the image templates above (the image model sets the headline from the brief).
# "fits" is "any" or the product categories (products.CATEGORIES) the scene was written for.
_SCENES = [
    {"id": "shelf", "name": "On the shelf", "line": "At eye level on a tidy shelf", "aspect": "4:5", "fits": "any",
     "brief": "The {product} on a tidy open shelf at eye level among a few plain, unbranded everyday objects, soft "
              "daylight from the side, shallow depth of field with the {product} sharpest. Packaging, label and colors "
              "exactly as in the photo. Headline: \"Worth the shelf space.\""},
    {"id": "closeup", "name": "Close-up", "line": "Tight on the detail and finish", "aspect": "1:1", "fits": "any",
     "brief": "Tight close-up of the {product} filling most of the frame on a plain surface, soft raking light that "
              "shows its texture and finish, the background softly blurred. The label stays sharp and unchanged. "
              "Headline: \"Look closer.\""},
    {"id": "doorstep", "name": "Just arrived", "line": "Fresh out of the shipping box", "aspect": "4:5", "fits": "any",
     "brief": "The {product} sitting on top of an open, plain cardboard shipping box with kraft paper, on a doormat by "
              "a front door in morning light. The box is blank. Packaging exactly as in the photo. "
              "Headline: \"It's here.\""},
    {"id": "table", "name": "On the table", "line": "In the middle of a table set for two", "aspect": "4:5",
     "fits": ("food_drink",),
     "brief": "A casual table set for two seen from a low angle: the {product} in the middle between simple plates and "
              "glasses, warm evening light, no people's faces. The label faces the camera, sharp and unchanged. "
              "Headline: \"Bring it to the table.\""},
    {"id": "counter", "name": "Kitchen counter", "line": "Morning light on the counter", "aspect": "9:16",
     "fits": ("food_drink", "supplement"),
     "brief": "A bright kitchen counter in the morning: the {product} beside a clean glass and a wooden board, daylight "
              "from a window, no hands in frame. The label stays sharp and unchanged. Headline: \"Start here.\""},
    {"id": "vanity", "name": "Bathroom shelf", "line": "By the mirror, with a towel and a plant", "aspect": "9:16",
     "fits": ("beauty", "supplement"),
     "brief": "A clean bathroom shelf by a mirror: the {product} next to a folded towel and a small plant, soft diffused "
              "light, a few water droplets on the surface. Packaging, label and colors exactly as in the photo. "
              "Headline: \"Made for your shelf.\""},
    {"id": "pastel", "name": "Soft shadows", "line": "Pastel surface, leaf shadows", "aspect": "1:1",
     "fits": ("beauty",),
     "brief": "The {product} lying on a smooth pastel surface with soft shadows of leaves falling across it, bright airy "
              "light and lots of clean space. The label stays sharp and unchanged. Headline: \"Simple, on purpose.\""},
    {"id": "outfit", "name": "With the outfit", "line": "Laid out with a simple outfit", "aspect": "4:5",
     "fits": ("apparel",),
     "brief": "The {product} laid out with a simple outfit on a made bed: folded clothes in neutral colors, natural "
              "window light, seen from above at a slight angle. The {product} is the clear focus, exactly as in the "
              "photo. Headline: \"Goes with everything.\""},
    {"id": "street", "name": "Out and about", "line": "On the street, no face in frame", "aspect": "9:16",
     "fits": ("apparel",),
     "brief": "Street-level city scene in soft afternoon light: the {product} worn or carried by a person seen from the "
              "shoulders down, no face in frame, the background softly blurred. The {product} exactly as in the photo, "
              "its logo sharp. Headline: \"Made to be out.\""},
    {"id": "nightstand", "name": "Nightstand", "line": "Lamp light, a glass of water, a book", "aspect": "4:5",
     "fits": ("supplement", "home"),
     "brief": "A calm bedside table in the evening: the {product} beside a glass of water and a closed book, warm lamp "
              "light. The label stays sharp and unchanged. Headline: \"Part of the evening.\""},
    {"id": "cozy", "name": "Cozy corner", "line": "Side table, armchair, a folded blanket", "aspect": "4:5",
     "fits": ("home",),
     "brief": "A cozy living-room corner: the {product} on a side table next to an armchair with a folded blanket, warm "
              "late-afternoon light. The {product} exactly as in the photo. Headline: \"Makes the room.\""},
    {"id": "windowsill", "name": "By the window", "line": "Sunny windowsill, long shadows", "aspect": "9:16",
     "fits": ("home",),
     "brief": "The {product} on a sunny windowsill with a small plant beside it, light curtains softly blurred, long "
              "morning shadows. The {product} exactly as in the photo. Headline: \"Right where you'll see it.\""},
    {"id": "desk", "name": "On the desk", "line": "A tidy desk, laptop and notebook", "aspect": "4:5",
     "fits": ("app_service",),
     "brief": "A tidy desk seen from a slight angle: an open laptop, a notebook and a cup, with the {product} shown "
              "exactly as in the photo as the clear focus, soft daylight. Headline: \"Built for your workday.\""},
    {"id": "inuse", "name": "In use", "line": "On a phone in a bright cafe", "aspect": "9:16",
     "fits": ("app_service",),
     "brief": "A hand holding a phone in a bright cafe with the {product} shown on its screen exactly as in the "
              "reference photo, nothing added or changed, the background softly blurred and no face in frame. "
              "Headline: \"Right when you need it.\""},
]
for _s in _SCENES:
    _s.update(tool="ad_image", audience="", preview="preset-" + _s["id"])

# ---------------------------------------------------------------- statics with a text layer (new, TEXT_LAYERS)
# The image is made with no words on it, then textlayer.py sets these as real type in the brand kit's colours and
# font. "fields" are the extra things Create asks for; "zone" tells the image model where to leave calm space.
#   field: {id, label, hint, max, type: "text" | "list" | "stars" | "confirm", n (list length), required, default}
_LAYERED = [
    {"id": "clean-headline", "name": "Clean headline", "layout": "headline", "aspect": "4:5",
     "line": "Your headline in real type over a clean product shot",
     "what_for": "For any message: your headline set in real type, so it is never misspelt.",
     "text_layers": ("headline", "subhead", "cta", "logo"), "inputs": ("product_photo", "headline"),
     "brief": "Premium product shot: the {product} in the lower half of the frame on a clean surface, soft directional "
              "light and a subtle shadow. Packaging, label and colors exactly as in the photo.",
     "fields": []},
    {"id": "offer", "name": "Offer", "layout": "offer", "aspect": "4:5",
     "line": "A price or offer badge and a button",
     "what_for": "For a price or an offer you already run: a badge, your headline and a button.",
     "text_layers": ("headline", "subhead", "badge", "cta", "logo"),
     "inputs": ("product_photo", "headline", "badge"),
     "brief": "The {product} centred, slightly low in the frame, on a clean softly lit surface with a plain "
              "single-colour backdrop. Packaging, label and colors exactly as in the photo.",
     "fields": [{"id": "badge", "label": "Badge", "max": 28, "required": True,
                 "hint": "The price or the offer saved on this product, in its words. Nothing else can go here."}]},
    {"id": "callouts", "name": "Feature callouts", "layout": "callouts", "aspect": "4:5",
     "line": "Up to four short features beside the product",
     "what_for": "For showing what it has: up to four short features pointed out beside the product.",
     "text_layers": ("headline", "callouts", "cta", "logo"), "inputs": ("product_photo", "headline", "callouts"),
     "brief": "The {product} standing on the right half of the frame on a clean surface, soft even light, the left "
              "half of the frame plain and empty. Packaging, label and colors exactly as in the photo.",
     "fields": [{"id": "callouts", "label": "Features", "type": "list", "n": 4, "max": 36, "required": True,
                 "hint": "A few words each. Only things that are true of this product."}]},
    {"id": "review", "name": "Review card", "layout": "review", "aspect": "4:5",
     "line": "A real review you paste, with stars",
     "what_for": "For social proof: a real customer review you paste, with its stars, under the product. "
                 "Never written by AI.",
     "text_layers": ("review", "cta", "logo"), "inputs": ("product_photo", "review", "attest"),
     "items_label": "Reviews", "items_hint": "Paste each review exactly as the customer wrote it. Each one makes its own ad.",
     "item_max": 220, "ai_items": False,
     "brief": "The {product} in the upper half of the frame on a clean, softly lit surface, the lower third of the "
              "frame plain and empty. Packaging, label and colors exactly as in the photo.",
     "fields": [{"id": "reviewer", "label": "Reviewer", "max": 40,
                 "hint": "The name as it appears with the review, or leave empty."},
                {"id": "stars", "label": "Stars the customer gave", "type": "stars"},
                {"id": "attest", "type": "confirm", "required": True,
                 "label": "These are real reviews from real customers, word for word, and I may use them in ads."}]},
    {"id": "compare", "name": "Comparison", "layout": "compare", "aspect": "4:5",
     "line": "The usual way against yours, in three rows",
     "what_for": "For a switch: two columns, the usual way against yours, in up to three short rows.",
     "text_layers": ("headline", "compare", "logo"), "inputs": ("product_photo", "headline", "compare"),
     "brief": "The {product} in the upper half of the frame on a clean, softly lit surface, the lower half of the "
              "frame plain and empty. Packaging, label and colors exactly as in the photo.",
     "fields": [{"id": "left_title", "label": "Left column", "max": 24, "default": "The usual way"},
                {"id": "right_title", "label": "Right column", "max": 24, "default": "{product}"},
                {"id": "left", "label": "The usual way", "type": "list", "n": 3, "max": 34, "required": True,
                 "hint": "Don't name another brand."},
                {"id": "right", "label": "With yours", "type": "list", "n": 3, "max": 34, "required": True,
                 "hint": "Only things that are true of this product."}]},
    {"id": "marketplace", "name": "Marketplace image", "layout": "none", "aspect": "1:1",
     "line": "The product alone on pure white, no words",
     "what_for": "For Amazon, Etsy or a shop listing: the product alone on pure white, with no words at all.",
     "text_layers": (), "inputs": ("product_photo",), "headline": False,
     "brief": "The {product} alone, centred and filling most of the frame, on a pure white seamless background with a "
              "soft natural shadow under it. No props, no people, no surface texture. Packaging, label and colors "
              "exactly as in the photo.",
     "fields": []},
]
for _l in _LAYERED:
    _l.update(tool="ad_image", audience="", fits="any", preview="preset-" + _l["id"])

# Where each layout needs calm space, said to the image model after the scene.
ZONES = {
    "headline": "Keep the top third of the frame calm, plain and uncluttered.",
    "offer": "Keep the top third of the frame and the area to the right of the product calm, plain and uncluttered.",
    "callouts": "Keep the top third and the left half of the frame calm, plain and uncluttered.",
    "review": "Keep the lower third of the frame calm, plain and uncluttered.",
    "compare": "Keep the top fifth and the lower half of the frame calm, plain and uncluttered.",
    "none": "",
}

# ---------------------------------------------------------------- the contract parts the old records never said
IMAGE_QA = ("product_present", "label_matches", "no_extra_text", "no_deformed_hands_faces")
VIDEO_QA = ("five_tells", "lip_sync")          # named in the quality bar; not automated yet (image_qa.VIDEO_CHECKS)
IDENTITY = {"presenter": "one approved still per presenter situation, given to the video model as its reference image",
            "voice": "one voice per presenter (catalog.PRESENTER_VOICE)", "wired": False}
_TYPE_CONTRACT = {
    "ugc": {"format": "talking", "inputs": ("product_photo", "script", "presenter"),
            "recipe": "catalog.talking_prompt", "locks": ("product", "presenter_still", "voice"),
            "params": {"bitrate_mode": "high", "generate_audio": True, "resolution": "720p"},
            "beats": ({"beat": "one take", "seconds": "8-30", "shot": "presenter to camera, the product on the table "
                       "beside them, hands off it"},),
            "qa": ("keyframe",) + VIDEO_QA, "price": {"duration": 8},
            "what_for": "For explaining the product person to person: a presenter says your script to camera."},
    "voiceover": {"format": "talking", "inputs": ("product_photo", "script", "voice"),
                  "recipe": "catalog.talking_prompt", "locks": ("product", "voice"),
                  "params": {"bitrate_mode": "high", "generate_audio": True, "resolution": "720p"},
                  "beats": ({"beat": "one take", "seconds": "8-30", "shot": "slow moves over the product while a "
                             "narrator reads the script; no lip-sync"},),
                  "qa": VIDEO_QA[:1], "price": {"duration": 8},
                  "what_for": "For when the label has to be right: the product on screen while a narrator reads."},
    "image": {"format": "image", "inputs": ("product_photo", "headline"), "recipe": "catalog.image_ad_prompt",
              "locks": ("product",), "params": {"language": "English"},
              "beats": ({"beat": "one frame", "shot": "the scene from the brief, the product as photographed"},),
              "qa": IMAGE_QA, "price": {"batch": 1},
              "what_for": "For feeds and stories: a finished ad image with your headline, in up to three shapes."},
    "product_video": {"format": "video", "inputs": ("product_photo", "scene"), "recipe": "catalog.with_fidelity",
                      "locks": ("product",), "params": {"bitrate_mode": "high", "resolution": "720p"},
                      "beats": ({"beat": "one shot", "seconds": "5 or 10", "shot": "the product photo brought to life"},),
                      "qa": VIDEO_QA[:1], "price": {"duration": 5},
                      "what_for": "For showing the product itself: one photo brought to life for 5 or 10 seconds."},
    "motion": {"format": "video", "inputs": ("product_photo", "scene", "reference_clip"), "recipe": "the brief as typed",
               "locks": ("product",), "params": {"resolution": "720p"},
               "beats": ({"beat": "the reference clip's own beats", "seconds": "up to 15"},),
               "qa": VIDEO_QA[:1], "price": {"ref_seconds": 5},
               "what_for": "For riding a trend: your product in the moves and timing of a reference clip."},
}
# One line on what each of the 14 older templates is for (their `line` says what the picture is).
_WHAT_FOR = {
    "hero": "For a first ad or a launch: the product alone, looking its best.",
    "routine": "For everyday products: yours as part of a morning.",
    "flatlay": "For a calm, styled look from above with a few props.",
    "inhand": "For anything carried or held: scale and a sense of taking it along.",
    "gift": "For gifting seasons: your product as an easy present.",
    "turn": "For a short clip that shows every side of the product.",
    "swap": "For coffee: the 30-second swap from the machine to a scoop.",
    "label": "For coffee: letting the ingredient panel speak.",
    "how": "For coffee: the whole how-to in ten seconds.",
    "two-pm": "For coffee: the afternoon slump at a desk.",
    "iced": "For coffee: the iced version on a sunny day.",
    "cost": "For coffee: your cup against the cafe's.",
    "gym": "For coffee: packed for the early gym.",
    "copy-trend": "For coffee: your bag in a trending video's moves.",
    "shelf": "For anything that lives on display: yours among everyday things.",
    "closeup": "For texture, finish and detail the wide shot hides.",
    "doorstep": "For the moment it arrives: straight out of the box.",
    "table": "For food and drink: yours in the middle of a meal.",
    "counter": "For daily food, drink and supplements: the morning counter.",
    "vanity": "For beauty and wellness: yours by the bathroom mirror.",
    "pastel": "For beauty: a light, airy studio look.",
    "outfit": "For apparel and accessories: styled with a simple outfit.",
    "street": "For apparel and accessories: worn out in the city, no face shown.",
    "nightstand": "For evening products: yours on the bedside table.",
    "cozy": "For home goods: yours in a lived-in corner.",
    "windowsill": "For home goods: morning light on the windowsill.",
    "desk": "For apps and services: yours at the centre of a working desk.",
    "inuse": "For apps: yours on a phone, in someone's hand.",
}


# ---------------------------------------------------------------- the record

@dataclass(frozen=True)
class Preset:
    id: str
    kind: str                       # "type" | "template" | "static"
    name: str
    what_for: str                   # one line, shown in Create
    format: str                     # "image" | "video" | "talking"
    tool: str                       # catalog.TOOLS key: the model and its prices
    ad_type: str                    # the Create ad type it runs as
    aspects: tuple = ()
    aspect: str = ""
    inputs: tuple = ()
    recipe: str = ""
    params: dict = field(default_factory=dict)
    beats: tuple = ()
    text_layers: tuple = ()
    layout: str = ""                # textlayer.py layout ("" = the image model sets the words, as before)
    fields: tuple = ()
    qa: tuple = ()
    locks: tuple = ("product",)
    price: dict = field(default_factory=dict)
    fits: object = "any"            # "any", "coffee" or a tuple of products.CATEGORIES codes
    preview: str = ""               # static/previews/<preview>.jpg (new presets: a placeholder made by tools/preset_thumbs.py)
    legacy: dict = field(default_factory=dict)   # the record as it was before this file (types and old templates)
    extra: dict = field(default_factory=dict)

    @property
    def model(self) -> str:
        import catalog
        return catalog.TOOLS[self.tool]["model"]

    def price_credits(self, **kw) -> int:
        """What it costs a member, from the same table as always (catalog.price_credits)."""
        import catalog
        return catalog.price_credits(self.tool, **dict(self.price, **kw))

    def cost_usd(self, **kw) -> float:
        import catalog
        return catalog.cost_usd(self.tool, **dict(self.price, **kw))

    def fits_product(self, product=None) -> bool:
        import catalog
        if self.fits == "any":
            return True
        if self.fits == "coffee":
            return catalog.is_coffee(product)
        return category(product) in self.fits

    def as_template(self) -> dict:
        """The dict shape catalog.TEMPLATES has always had (templates and statics only)."""
        if self.legacy:
            return self.legacy
        d = {"id": self.id, "name": self.name, "line": self.extra.get("line") or self.what_for, "tool": self.tool,
             "aspect": self.aspect, "fits": self.fits, "brief": self.recipe, "audience": "", "preview": self.preview,
             "preset": True}
        if self.layout:
            d["layout"] = self.layout
        return d

    def contract(self) -> dict:
        """The whole record as plain data (docs, MCP, tests)."""
        return {"id": self.id, "kind": self.kind, "name": self.name, "what_for": self.what_for, "format": self.format,
                "aspects": list(self.aspects), "aspect": self.aspect, "inputs": list(self.inputs),
                "recipe": self.recipe, "model": self.model, "params": dict(self.params), "beats": list(self.beats),
                "text_layers": list(self.text_layers), "layout": self.layout, "qa": list(self.qa),
                "locks": list(self.locks), "price": {"tool": self.tool, **self.price, "credits": self.price_credits()},
                "fits": self.fits if isinstance(self.fits, str) else list(self.fits)}


_TOOL_FORMAT = {"ad_image": "image", "photo_to_video": "video", "motion_copy": "video", "ugc_video": "talking"}
_TOOL_TYPE = {"ad_image": "image", "photo_to_video": "product_video", "motion_copy": "motion", "ugc_video": "ugc"}
_IMAGE_ASPECTS = ("9:16", "1:1", "4:5", "16:9")       # catalog.TOOLS["ad_image"]["aspects"]
_TALK_ASPECTS = ("9:16", "1:1", "16:9")


def _from_type(d: dict) -> Preset:
    c = _TYPE_CONTRACT[d["id"]]
    aspects = _IMAGE_ASPECTS if d["tool"] == "ad_image" else _TALK_ASPECTS if d["tool"] == "ugc_video" else ()
    return Preset(id=d["id"], kind="type", name=d["name"], what_for=c["what_for"], format=c["format"], tool=d["tool"],
                  ad_type=d["id"], aspects=aspects, aspect=aspects[0] if aspects else "", inputs=c["inputs"],
                  recipe=c["recipe"], params=c["params"], beats=c["beats"], qa=c["qa"], locks=c["locks"],
                  price=c["price"], preview=d["preview"], legacy=d, extra={"line": d["line"]})


def _from_template(d: dict, legacy: bool) -> Preset:
    tool = d["tool"]
    image = tool == "ad_image"
    price = {"batch": 1} if image else {"duration": d.get("duration") or 5} if tool == "photo_to_video" \
        else {"ref_seconds": 5}
    beats = ({"beat": "one frame", "shot": d["line"]},) if image else \
        ({"beat": "one shot", "seconds": str(d.get("duration") or "up to 15"), "shot": d["line"]},)
    return Preset(id=d["id"], kind="template", name=d["name"], what_for=_WHAT_FOR[d["id"]], format=_TOOL_FORMAT[tool],
                  tool=tool, ad_type=_TOOL_TYPE[tool], aspects=_IMAGE_ASPECTS if image else (),
                  aspect=d.get("aspect") or "", recipe=d["brief"],
                  inputs=("product_photo", "headline") if image else ("product_photo", "scene") if tool == "photo_to_video"
                  else ("product_photo", "scene", "reference_clip"),
                  params={"language": "English"} if image else {"bitrate_mode": "high", "resolution": "720p"},
                  beats=beats, qa=IMAGE_QA if image else VIDEO_QA[:1], price=price, fits=d["fits"],
                  preview=d["preview"], legacy=d if legacy else {}, extra={"line": d["line"]})


def _from_layered(d: dict) -> Preset:
    return Preset(id=d["id"], kind="static", name=d["name"], what_for=d["what_for"], format="image", tool="ad_image",
                  ad_type="image", aspects=_IMAGE_ASPECTS, aspect=d["aspect"], inputs=d["inputs"], recipe=d["brief"],
                  params={"language": "English", "text": "none: set afterwards by textlayer.py"},
                  beats=({"beat": "one frame", "shot": d["line"]},), text_layers=d["text_layers"], layout=d["layout"],
                  fields=tuple(d["fields"]), qa=IMAGE_QA, price={"batch": 1}, preview=d["preview"],
                  extra={k: d[k] for k in ("line", "items_label", "items_hint", "item_max", "ai_items", "headline")
                         if k in d})


TYPES = [_from_type(d) for d in _TYPES]
OLD_TEMPLATES = [_from_template(d, True) for d in _TEMPLATES]
SCENES = [_from_template(d, False) for d in _SCENES]
LAYERED = [_from_layered(d) for d in _LAYERED]
LIBRARY = TYPES + OLD_TEMPLATES + SCENES + LAYERED
BY_ID = {p.id: p for p in LIBRARY}
assert len(BY_ID) == len(LIBRARY), "preset ids must be unique"


def text_layers_on() -> bool:
    """TEXT_LAYERS (default on): the statics whose words are set by textlayer.py. "0" hides them from Create;
    ones already queued still finish."""
    return os.environ.get("TEXT_LAYERS", "1").strip().lower() not in ("0", "off", "false", "no")


def get(preset_id) -> Preset | None:
    return BY_ID.get(str(preset_id or ""))


def legacy_types() -> list[dict]:
    return [p.legacy for p in TYPES]


def legacy_templates() -> list[dict]:
    return [p.legacy for p in OLD_TEMPLATES]


def template_dicts() -> list[dict]:
    """Every template and static as a catalog.TEMPLATES-shaped dict (catalog.TEMPLATE_BY_ID is built from this)."""
    return [p.as_template() for p in OLD_TEMPLATES + SCENES + LAYERED]


def category(product) -> str:
    """The product's category code (products.category_of) from a product row, a brand brief or a name."""
    import products
    if not product:
        return ""
    if isinstance(product, str):
        return products.guess_category(product)
    keys = product.keys() if hasattr(product, "keys") else []
    if "category" in keys and product["category"] in products.CATEGORY_NAMES:
        return product["category"]
    return products.guess_category(" ".join(str(product[k] or "") for k in ("tags", "name", "product", "description")
                                            if k in keys))


def statics_for(product=None, layered: bool | None = None) -> list[Preset]:
    """The image presets that fit this product, best fit first: the ones written for its category, then the
    any-product scenes, then (TEXT_LAYERS) the statics with a text layer, then coffee ones for coffee."""
    lay = text_layers_on() if layered is None else layered
    pool = [p for p in OLD_TEMPLATES + SCENES if p.tool == "ad_image" and p.fits_product(product)]
    own = [p for p in pool if isinstance(p.fits, tuple)]
    coffee = [p for p in pool if p.fits == "coffee"]
    anyp = [p for p in pool if p.fits == "any"]
    return coffee + own + anyp + (list(LAYERED) if lay else [])


def library_for(product=None) -> list[Preset]:
    """Everything Create's preset list offers for this product (the five ad types are its own step)."""
    vids = [p for p in OLD_TEMPLATES if p.tool != "ad_image" and p.fits_product(product)]
    return statics_for(product) + vids


def card(p: Preset, url_for=None) -> dict:
    """One preset for Create's list: name, the one-line "what it's for", its thumbnail and its from-price."""
    thumb = "previews/" + p.preview + ".jpg"
    return {"id": p.id, "name": p.name, "what_for": p.what_for, "line": p.extra.get("line") or "", "kind": p.kind,
            "format": p.format, "tool": p.tool, "type": p.ad_type, "layout": p.layout, "aspect": p.aspect,
            "from": p.price_credits(), "layers": list(p.text_layers),
            "preview": url_for("static", filename=thumb) if url_for else thumb}


def layout_spec(p: Preset) -> dict:
    """What Create needs to draw a layered preset's extra fields."""
    return {"id": p.id, "layout": p.layout, "fields": list(p.fields), "headline": p.extra.get("headline", True),
            "items_label": p.extra.get("items_label") or "", "items_hint": p.extra.get("items_hint") or "",
            "item_max": p.extra.get("item_max") or 0, "ai_items": p.extra.get("ai_items", True),
            "layers": list(p.text_layers)}


# ---------------------------------------------------------------- the words of a layered static

class LayerError(ValueError):
    """Text for a layer that can't be used; the message is for the member."""


def _one(v, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit].strip()


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9$€£%.]+", (s or "").lower()))


def badge_ok(badge: str, brief: dict) -> bool:
    """An offer badge is only ever the product's own saved price or offer (or a part of it): never typed fresh."""
    b = _norm(badge)
    if not b:
        return False
    return any(b and b in _norm(str(brief.get(k) or "")) for k in ("price", "offer"))


def clean_layers(p: Preset, raw, brief: dict) -> dict:
    """The validated extra words of a layered static, ready for the job's params. Raises LayerError.
    Every line goes through compliance.check; a badge must be the product's saved price or offer; a review
    needs the member's word that it is real."""
    import compliance
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for f in p.fields:
        fid, typ, lim = f["id"], f.get("type") or "text", int(f.get("max") or 60)
        label = f.get("label") or fid
        if typ == "list":
            vals = raw.get(fid) if isinstance(raw.get(fid), list) else []
            vals = [x for x in (_one(v, lim) for v in vals[: int(f.get("n") or 4)]) if x]
            if f.get("required") and not vals:
                raise LayerError(f"{label}: add at least one.")
            for v in vals:
                chk = compliance.check(v)
                if not chk["ok"]:
                    raise LayerError(f"{label}: " + " ".join(chk["blocks"]))
            out[fid] = vals
        elif typ == "stars":
            try:
                n = int(raw.get(fid) or 0)
            except (TypeError, ValueError):
                n = 0
            out[fid] = n if 1 <= n <= 5 else 0
        elif typ == "confirm":
            if f.get("required") and not raw.get(fid):
                raise LayerError("Tick the box to confirm the reviews are real before making this.")
            out[fid] = bool(raw.get(fid))
        else:
            v = _one(raw.get(fid), lim) or _one(str(f.get("default") or "").replace("{product}", brief.get("product") or ""),
                                                lim)
            if f.get("required") and not v:
                raise LayerError(f"{label}: this preset needs one.")
            if v:
                chk = compliance.check(v)
                if not chk["ok"]:
                    raise LayerError(f"{label}: " + " ".join(chk["blocks"]))
            out[fid] = v
    if p.layout == "offer" and not badge_ok(out.get("badge") or "", brief):
        if not (brief.get("price") or brief.get("offer")):
            raise LayerError("Badge: save the price or the offer on the product first; the badge can only show that.")
        raise LayerError("Badge: it can only show the price or the offer saved on this product "
                         f"({' / '.join(x for x in (brief.get('price'), brief.get('offer')) if x)}).")
    if p.layout == "compare" and len(out.get("left") or []) != len(out.get("right") or []):
        raise LayerError("Comparison: give both columns the same number of rows.")
    return out
