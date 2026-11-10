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
              callouts, review card, comparison, and the text-free marketplace image (TEXT_LAYERS, default on);
              m15 adds seasonal ones and a few more built on the same layouts (STATICS_M15)
    recipe    a fixed VIDEO format (m15, RECIPES): explicit beats of 6 to 8 seconds, each with its shot, what is
              said, the on-screen text and where its first frame comes from, plus an end card. A recipe whose
              `needs` is "" runs as one take through the talking-video path (its `direction` goes into
              catalog.talking_prompt). One that needs "keyframe_video" needs the keyframe-first pipeline
              (keyframe_video.py), which builds the video from one approved still, beat by beat, and stitches
              it. That pipeline is behind KEYFRAME_VIDEO (default off): while it is off those recipes are hidden
              from members and refused by Create, exactly as before; while it is on "keyframe_video" is in
              PIPELINES, they are offered, and every video made from a recipe takes that path.

Every record also carries tags (m15): `category`, the product categories it fits (products.CATEGORIES codes, or
"any"), and `use`, what the ad is for (USES: offer, social-proof, problem-solution, unboxing-pov, hands-only-demo,
talking-head-cutaways, green-screen-product-page, comparison, marketplace, seasonal, founder-story, how-to,
before-after-honest, testimonial-real-only ...). "before-after-honest" is a switch told in the member's own words
(the old way, the new way): compliance.py still blocks before-and-after wording on every line. example_url() / example_video_url() give a preset's example
(static/presets/<id>.webp, .mp4) once a site admin has made and imported one (library.py).

The house rules every recipe is written to: no invented reviews, no countdowns or made-up urgency, no before/after
health or body claims, no real people or lookalikes, and an AI presenter never speaks as a customer.

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

# ---------------------------------------------------------------- m15: seasonal statics and a few more (text layer)
# Same pipeline as _LAYERED: the picture has no words, textlayer.py sets them in the brand kit. "fits" narrows a
# preset to the categories it was written for; "use" and "season" are its tags.
_KEEP = "Packaging, label and colors exactly as in the photo."
_BADGE_FIELD = {"id": "badge", "label": "Badge", "max": 28, "required": True,
                "hint": "The price or the offer saved on this product, in its words. Nothing else can go here."}
_STATICS_M15 = [
    {"id": "season-winter-gift", "name": "Winter gifting", "layout": "headline", "aspect": "4:5", "season": "winter",
     "line": "Plain wrapped parcels, a sprig of pine, warm lights",
     "what_for": "For the winter holidays: your product as the gift, your headline in real type.",
     "use": ("seasonal", "gifting"), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "Winter gifting scene: the {product} in the lower half of the frame on a wooden table among three "
              "parcels wrapped in plain brown paper and twine, a sprig of pine beside it and a string of warm lights "
              "softly blurred behind, cozy evening light. " + _KEEP, "fields": []},
    {"id": "season-fresh-start", "name": "Fresh start", "layout": "headline", "aspect": "4:5", "season": "new-year",
     "line": "A clear desk, a blank notebook, cool morning light",
     "what_for": "For January and new beginnings: your product in a clean, quiet morning.",
     "use": ("seasonal",), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "A bright, uncluttered morning scene: the {product} in the lower half of the frame on a clean white desk "
              "beside an open blank notebook, a pen and a glass of water, cool daylight from a window and long soft "
              "shadows. " + _KEEP, "fields": []},
    {"id": "season-spring", "name": "Spring refresh", "layout": "headline", "aspect": "4:5", "season": "spring",
     "line": "An open window, tulips in a jar, linen",
     "what_for": "For spring: your product in fresh, airy daylight.",
     "use": ("seasonal",), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "Spring scene: the {product} in the lower half of the frame on a pale wooden table by an open window, a "
              "jam jar of fresh tulips and a folded linen cloth beside it, bright airy daylight. " + _KEEP,
     "fields": []},
    {"id": "season-summer", "name": "Summer outdoors", "layout": "headline", "aspect": "9:16", "season": "summer",
     "line": "A picnic blanket, a straw hat, hard sun",
     "what_for": "For summer: your product outside in full sun.",
     "use": ("seasonal",), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "Summer scene outdoors: the {product} in the lower half of the frame on a striped picnic blanket on "
              "grass, a straw hat and a pair of sunglasses beside it, hard midday sun and crisp shadows. " + _KEEP,
     "fields": []},
    {"id": "season-autumn", "name": "Autumn cozy", "layout": "headline", "aspect": "4:5", "season": "autumn",
     "line": "Dark wood, a knitted throw, low sun",
     "what_for": "For autumn: your product in warm, low light.",
     "use": ("seasonal",), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "Autumn scene: the {product} in the lower half of the frame on a dark wooden table with a knitted throw, "
              "a few dry leaves and a small pumpkin beside it, warm low afternoon sun. " + _KEEP, "fields": []},
    {"id": "season-back-to-routine", "name": "Back to routine", "layout": "headline", "aspect": "4:5",
     "season": "back-to-school",
     "line": "A hallway bench, a packed bag, keys",
     "what_for": "For September and the return to work or school: your product by the front door.",
     "use": ("seasonal",), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "Back-to-routine scene by a front door: the {product} in the lower half of the frame on a hallway bench "
              "beside a packed backpack, a set of keys and a plain lunch box, early morning light. " + _KEEP,
     "fields": []},
    {"id": "season-gift-for-them", "name": "A gift for them", "layout": "headline", "aspect": "1:1", "season": "gifting-days",
     "line": "An open box, tissue paper, dried flowers",
     "what_for": "For Mother's Day, Father's Day, Valentine's Day or a birthday: your product as a present for one person.",
     "use": ("seasonal", "gifting"), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "Gift scene for one person: the {product} in the lower half of the frame resting in an open plain box "
              "lined with tissue paper, a loop of plain ribbon and a few dried flowers beside it, soft window "
              "light. " + _KEEP, "fields": []},
    {"id": "season-offer", "name": "Seasonal offer", "layout": "offer", "aspect": "4:5", "season": "any",
     "line": "Your saved offer on a festive, plain backdrop",
     "what_for": "For a seasonal offer you already run: your saved price or offer as the badge. No countdowns, no "
                 "made-up deadlines.",
     "use": ("seasonal", "offer"), "text_layers": ("headline", "subhead", "badge", "cta", "logo"),
     "inputs": ("product_photo", "headline", "badge"),
     "brief": "The {product} centred, slightly low in the frame, on a clean surface with a plain deep-green backdrop, "
              "one loop of plain ribbon and a few small paper stars at the edges of the frame, soft even light. "
              + _KEEP, "fields": [_BADGE_FIELD]},
    {"id": "problem-answer", "name": "Problem, then answer", "layout": "headline", "aspect": "4:5",
     "line": "The problem as the headline, the answer under it",
     "what_for": "For a problem your product solves: the problem as your headline, the answer as the line under it.",
     "use": ("problem-solution",), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline", "subhead"),
     "brief": "The {product} on the right of the lower half of the frame on a clean surface, a soft shadow falling to "
              "the left, a plain single-colour backdrop. " + _KEEP, "fields": []},
    {"id": "three-reasons", "name": "Three reasons", "layout": "callouts", "aspect": "4:5",
     "line": "Three short reasons beside the product",
     "what_for": "For a quick case: three short, true reasons to pick it, set beside the product.",
     "use": ("listicle", "feature-callouts"), "text_layers": ("headline", "callouts", "cta", "logo"),
     "inputs": ("product_photo", "headline", "callouts"),
     "brief": "The {product} standing on the right half of the frame on a pale stone surface, soft daylight from the "
              "right, the left half of the frame plain and empty. " + _KEEP,
     "fields": [{"id": "callouts", "label": "Reasons", "type": "list", "n": 3, "max": 36, "required": True,
                 "hint": "Three short reasons. Only things that are true of this product."}]},
    {"id": "how-it-works", "name": "How it works", "layout": "callouts", "aspect": "9:16",
     "line": "Three steps, in order, beside the product",
     "what_for": "For showing how simple it is: three steps in order, set beside the product.",
     "use": ("how-to",), "text_layers": ("headline", "callouts", "cta", "logo"),
     "inputs": ("product_photo", "headline", "callouts"),
     "brief": "The {product} standing on the right half of the frame on a plain worktop, even daylight, the left half "
              "of the frame plain and empty. " + _KEEP,
     "fields": [{"id": "callouts", "label": "Steps", "type": "list", "n": 3, "max": 36, "required": True,
                 "hint": "Three short steps, in the order you do them."}]},
    # The honest version of a before-and-after: how it was done and how it is done with the product, in the member's
    # own words. The house rule (compliance.py: "No before-and-after content.") still applies to every line, so it
    # never says "before" and "after"; it makes no claim about results, and it isn't offered for anything you eat,
    # swallow or put on your skin.
    {"id": "then-now", "name": "The old way, the new way", "layout": "compare", "aspect": "4:5",
     "fits": ("home", "apparel", "app_service", "other"),
     "line": "How you did it, and how you do it with yours, in three rows",
     "what_for": "For an honest switch story: how it was done, and how it is done with yours, in your own words. "
                 "Never results, health, body or skin claims.",
     "use": ("before-after-honest", "comparison"), "text_layers": ("headline", "compare", "logo"),
     "inputs": ("product_photo", "headline", "compare", "attest"),
     "brief": "The {product} standing in the upper half of the frame on a tidy, softly lit shelf, the lower half of the "
              "frame plain and empty. " + _KEEP,
     "fields": [{"id": "left_title", "label": "Left column", "max": 24, "default": "The old way"},
                {"id": "right_title", "label": "Right column", "max": 24, "default": "With {product}"},
                {"id": "left", "label": "The old way", "type": "list", "n": 3, "max": 34, "required": True,
                 "hint": "How it was done, in plain words. Don't name another brand."},
                {"id": "right", "label": "With yours", "type": "list", "n": 3, "max": 34, "required": True,
                 "hint": "Only things that are true of this product."},
                {"id": "true", "type": "confirm", "required": True,
                 "label": "Both columns are true, I can back them up, and they say nothing about health or the body.",
                 "error": "Tick the box to confirm both columns are true before making this."}]},
    {"id": "app-screen", "name": "Your screen, front and centre", "layout": "headline", "aspect": "9:16",
     "fits": ("app_service",),
     "line": "Your real screen on a phone, your headline above",
     "what_for": "For apps: your real screen on a phone, with your headline in real type above it.",
     "use": ("show-your-app", "product-hero"), "text_layers": ("headline", "subhead", "cta", "logo"),
     "inputs": ("product_photo", "headline"),
     "brief": "A phone standing upright in the lower half of the frame on a plain desk, its screen showing the "
              "{product} exactly as in the reference photo with nothing added or changed, soft daylight and a plain "
              "backdrop.", "fields": []},
]
for _l in _STATICS_M15:
    _l.update(tool="ad_image", audience="", preview="preset-" + _l["id"])
    _l.setdefault("fits", "any")

# ---------------------------------------------------------------- m15: fixed video formats (recipes)
# One record per format. `beats` is the plan the keyframe-first pipeline will render beat by beat:
#   beat      its name
#   seconds   6 to 8 for a spoken beat; the end card is 2 and silent
#   source    where the beat's first frame comes from, which also says how it is made:
#               presenter  the presenter's approved situation still + the product photo -> reference-to-video, lip-sync
#               product    the member's product photo -> image-to-video, the narration runs over it
#               hands      a hands-and-product keyframe made from the product photo -> image-to-video
#               screen     the member's own screenshot, shown as it is (never generated)
#               packshot   the real product photo on the brand colour, words set by textlayer.py (no model)
#   shot      what the camera sees
#   say       what is said in it (a slot of the script, not words we write for the member)
#   text      the on-screen text, set as real type afterwards ("" = none)
# `direction` is the same format as ONE take, for today's talking-video path (needs ""): what the camera sees in a
# voiceover, or what the presenter does. `script` tells the writer the shape and gives the member a starting line.
# What only the keyframe-first pipeline reads (keyframe_video.py):
#   tag        (a beat) a short literal set on that beat as real type: "Step 1", "1"
#   place      (a beat) another presenters.SITUATIONS id: the same person, somewhere new, from the same keyframe
#   captions   burn the spoken words in, a few at a time (a member's config can turn them on or off)
#   crossfade  frames each join dissolves over (0 = hard cuts, the default; 4 to 6 otherwise)
#   product_at where the product is in a presenter's keyframe: "table" (on the situation's surface, the default),
#              "hand" or "shelf"
#   extra_inputs  ("page_screenshot",) / ("app_screens",): the member's own screenshots, shown as they are
# Everything here is written for Campaignify; nothing is taken from another tool's prompts.
_END_CARD = {"beat": "end card", "seconds": 2, "source": "packshot",
             "shot": "the real product photo on the brand colour, the logo under it",
             "say": "", "text": "the brand name and the button text"}
_ALL_BUT_APPS = ("food_drink", "beauty", "apparel", "supplement", "home", "other")
_RECIPES = [
    # ---- these run today, as one take
    {"id": "vo-product-shots", "name": "Voiceover over product shots", "ad_type": "voiceover", "duration": 15,
     "placement": "product_only", "needs": "",
     "line": "Three calm shots of the product while a narrator reads",
     "what_for": "For when the label has to be right: three calm shots of the product while a narrator reads your "
                 "script. No presenter, no lip-sync.",
     "use": ("voiceover", "product-hero"),
     "direction": "Three unhurried shots of it resting on a table in a real home, cut together: a slow push-in from the "
                  "front, a close pass across the label, then the whole product again from slightly above. No people "
                  "and no hands in frame.",
     "script": {"shape": "A hook that says what it is and who it is for, one or two approved facts, then where to "
                         "get it.", "starter": "Meet {product}."},
     "beats": ({"beat": "hook", "seconds": 7, "source": "product", "shot": "a slow push-in on the product, front on",
                "say": "the hook: what it is and who it is for", "text": "the hook in five words or fewer"},
               {"beat": "detail", "seconds": 8, "source": "product",
                "shot": "a close pass across the label, then the whole product from slightly above",
                "say": "one or two approved facts, then where to get it", "text": "one fact, as the product page says it"},
               _END_CARD)},
    {"id": "hands-only-demo", "name": "Hands-only demo", "ad_type": "voiceover", "duration": 15, "needs": "",
     "fits": _ALL_BUT_APPS,
     "line": "Two hands use it on a table; a narrator explains",
     "what_for": "For showing it in use without a face: two hands use it on a table while a narrator explains.",
     "use": ("hands-only-demo", "how-to"),
     "direction": "One wide, steady shot from above and slightly in front of a plain table: two hands pick it up with a "
                  "simple grip, use it the way it is meant to be used, and set it down with its label facing the camera. "
                  "Only hands and forearms are in frame, never a face. The moves are slow, one at a time.",
     "script": {"shape": "What it is, how you use it in one or two plain steps, then where to get it.",
                "starter": "Here is how {product} works."},
     "beats": ({"beat": "set down", "seconds": 7, "source": "hands",
                "shot": "wide from above: the product on the table, two hands come in and pick it up",
                "say": "what it is, in one sentence", "text": "the product's name"},
               {"beat": "in use", "seconds": 8, "source": "hands",
                "shot": "the same wide frame: the hands use it, then set it down label to camera",
                "say": "how it is used, then where to get it", "text": ""},
               _END_CARD)},
    {"id": "pov-unboxing", "name": "POV unboxing", "ad_type": "voiceover", "duration": 15, "needs": "",
     "fits": _ALL_BUT_APPS,
     "line": "First-person: a plain box opens, the product comes out",
     "what_for": "For the moment it arrives: first-person hands open a plain box and lift your product out.",
     "use": ("unboxing-pov",),
     "direction": "First-person view looking down at a table: two hands open a plain, unprinted cardboard box, fold "
                  "back plain tissue paper and lift it out, turn it so its label faces the camera and stand it on the "
                  "table. Only hands and forearms are in frame, never a face. The box and the paper carry no print.",
     "script": {"shape": "What is in the box, one thing worth noticing about it, then where to get it. Said by a "
                         "narrator, never as a customer's reaction.", "starter": "This is what arrives when you order {product}."},
     "beats": ({"beat": "open", "seconds": 7, "source": "hands",
                "shot": "first-person: hands open a plain box and fold back the tissue paper",
                "say": "what is in the box", "text": ""},
               {"beat": "reveal", "seconds": 8, "source": "hands",
                "shot": "the hands lift the product out, turn its label to the camera and stand it on the table",
                "say": "one thing worth noticing, then where to get it", "text": "the product's name"},
               _END_CARD)},
    {"id": "talk-to-camera", "name": "Talk to camera", "ad_type": "ugc", "duration": 8, "needs": "",
     "line": "One presenter, one room, eight seconds",
     "what_for": "For saying one thing plainly: a presenter in a real room says a short script, your product beside them.",
     "use": ("talking-head",),
     "direction": "",
     "script": {"shape": "One thought: what it is and why it is worth a look. The presenter explains; they never say "
                         "they bought or used it.", "starter": "Quick one about {product}."},
     "beats": ({"beat": "one take", "seconds": 8, "source": "presenter",
                "shot": "the presenter to camera in their situation, the product on the surface beside them, hands off it",
                "say": "the whole script", "text": ""},)},
    {"id": "car-talk", "name": "Car talk", "ad_type": "ugc", "duration": 8, "needs": "", "situation": "car",
     "line": "A presenter in a parked car, phone on the dash",
     "what_for": "For a casual, between-errands feel: a presenter in a parked car says a short script, your product on "
                 "the dashboard.",
     "use": ("talking-head",),
     "direction": "",
     "script": {"shape": "An offhand opener, then what it is and where to find it. The presenter explains; they never "
                         "say they bought or used it.", "starter": "Okay, quick thing about {product}."},
     "beats": ({"beat": "one take", "seconds": 8, "source": "presenter",
                "shot": "the presenter in the driver's seat of a parked car, the product standing on the dashboard",
                "say": "the whole script", "text": ""},)},
    {"id": "season-gift-clip", "name": "Seasonal gift moment", "ad_type": "voiceover", "duration": 8,
     "placement": "product_only", "needs": "", "season": "winter",
     "line": "The product among plain parcels and warm lights",
     "what_for": "For gifting seasons: your product among plain wrapped parcels while a narrator reads a short line.",
     "use": ("seasonal", "gifting"),
     "direction": "It stands on a wooden table among three parcels wrapped in plain brown paper and twine, a sprig of "
                  "greenery beside it and warm lights softly out of focus behind. The camera drifts slowly towards it and "
                  "settles on its label. No people and no hands in frame.",
     "script": {"shape": "Who it makes a good present for, then where to get it. No deadlines unless your own offer has one.",
                "starter": "A present they will actually use."},
     "beats": ({"beat": "gift scene", "seconds": 8, "source": "product",
                "shot": "a slow drift towards the product among plain parcels, settling on its label",
                "say": "who it is a good present for, then where to get it", "text": "the headline"},
               _END_CARD)},
    {"id": "founder-story", "name": "Founder story, narrated", "ad_type": "voiceover", "duration": 20, "needs": "",
     "crossfade": 5,
     "line": "Your own story, read over the product being made and packed",
     "what_for": "For why you started: your own story in your own words, read by a narrator over the product. No AI "
                 "person plays you.",
     "use": ("founder-story",),
     "direction": "Quiet shots of it where it is made and packed: on a workbench among plain tools and unprinted boxes, "
                  "then close on its label, then on a shelf beside others like it. Hands may pack in the background, never "
                  "a face. Warm, plain light.",
     "script": {"shape": "Written by you as 'we': why it exists, one thing you do differently, an invitation. Only "
                         "what is true of your business.", "starter": "We started making {product} because"},
     "beats": ({"beat": "why", "seconds": 6, "source": "product", "shot": "the product on a workbench among plain tools",
                "say": "why it exists", "text": ""},
               {"beat": "how", "seconds": 7, "source": "product", "shot": "close on the label, hands packing behind",
                "say": "one thing you do differently", "text": ""},
               {"beat": "invitation", "seconds": 7, "source": "product", "shot": "the product on a shelf beside others like it",
                "say": "an invitation, and where to get it", "text": "the brand name"},
               _END_CARD)},
    # ---- these wait for the keyframe-first pipeline (cuts between beats, real screens, words set as type)
    {"id": "talking-head-cutaways", "name": "Talking head with cutaways", "ad_type": "ugc", "duration": 20,
     "needs": "keyframe_video",
     "line": "A presenter opens and closes; the product fills the middle",
     "what_for": "For explaining it person to person without a long lip-sync take: the presenter opens and closes, "
                 "and close shots of your product carry the middle.",
     "use": ("talking-head-cutaways",),
     "direction": "",
     "captions": True,
     "script": {"shape": "A hook to camera, the facts over the product, a close to camera.", "starter": "Let me show you {product}."},
     "beats": ({"beat": "hook", "seconds": 6, "source": "presenter", "shot": "the presenter to camera, the product beside them",
                "say": "the hook", "text": "the hook in five words or fewer"},
               {"beat": "cutaway", "seconds": 7, "source": "product", "shot": "close shots of the product and its label",
                "say": "one or two approved facts (the presenter's voice carries on)", "text": "one fact, as the product page says it"},
               {"beat": "close", "seconds": 7, "source": "presenter", "shot": "back to the presenter, same room",
                "say": "who it is for and where to get it", "text": ""},
               _END_CARD)},
    {"id": "problem-solution", "name": "Problem, then solution", "ad_type": "ugc", "duration": 20,
     "needs": "keyframe_video",
     "line": "The problem to camera, the product as the answer",
     "what_for": "For a problem your product solves: a presenter names it, the product answers it, a real packshot closes.",
     "use": ("problem-solution",),
     "direction": "",
     "captions": True,
     "script": {"shape": "The problem as a question, the product as the answer, one approved fact, where to get it. "
                         "No health or results claims.", "starter": "Still putting up with"},
     "beats": ({"beat": "problem", "seconds": 6, "source": "presenter", "shot": "the presenter to camera, no product yet",
                "say": "the problem, as a question", "text": "the problem in five words or fewer"},
               {"beat": "product", "seconds": 7, "source": "hands", "shot": "hands set the product down and use it, no face",
                "say": "the product as the answer", "text": "the product's name"},
               {"beat": "fact", "seconds": 7, "source": "product", "shot": "close on the product and its label",
                "say": "one approved fact, then where to get it", "text": "the fact, as the product page says it"},
               _END_CARD)},
    {"id": "green-screen-product-page", "name": "Over the product page", "ad_type": "ugc", "duration": 15,
     "needs": "keyframe_video", "extra_inputs": ("page_screenshot",),
     "line": "A presenter in the corner, your real product page behind",
     "what_for": "For price and details that must be exact: a presenter talks over a screenshot of your real product "
                 "page, so the product, its name and its price are never generated.",
     "use": ("green-screen-product-page",),
     "direction": "",
     "script": {"shape": "Point at what is on the page: what it is, what it costs as the page says, where the button is.",
                "starter": "This is the page for {product}."},
     "beats": ({"beat": "top of the page", "seconds": 7, "source": "screen",
                "shot": "the top of your product page full frame, the presenter cut out in the lower corner",
                "say": "what it is, pointing at the page", "text": ""},
               {"beat": "details", "seconds": 8, "source": "screen",
                "shot": "the page scrolled to its photos and details, the presenter still in the corner",
                "say": "one detail from the page and where the button is", "text": ""},
               _END_CARD)},
    {"id": "show-your-app", "name": "Show your app", "ad_type": "ugc", "duration": 15, "needs": "keyframe_video",
     "fits": ("app_service",), "extra_inputs": ("app_screens",),
     "line": "A presenter introduces it, then your real screens",
     "what_for": "For apps and software: a presenter introduces it, then your real screens fill the frame. No invented "
                 "interface.",
     "use": ("show-your-app",),
     "direction": "",
     "script": {"shape": "What the app is for, then one thing you can do on the screens shown, then where to get it.",
                "starter": "This is {product}."},
     "beats": ({"beat": "intro", "seconds": 7, "source": "presenter", "shot": "the presenter to camera at a desk",
                "say": "what the app is for", "text": "the app's name"},
               {"beat": "screens", "seconds": 8, "source": "screen",
                "shot": "your screenshots full frame, one after another, as they are",
                "say": "one thing you can do in it, then where to get it", "text": ""},
               _END_CARD)},
    {"id": "street-interview", "name": "Street interview", "ad_type": "ugc", "duration": 15, "needs": "keyframe_video",
     "situation": "outdoor", "labels": ("AI presenter",),
     "line": "A question on screen, an AI presenter answers outdoors",
     "what_for": "For a quick question-and-answer feel: a question appears as text and an AI presenter answers it "
                 "outdoors as a spokesperson, labelled as AI the whole way through. Never a customer, never a passer-by.",
     "use": ("street-interview",),
     "direction": "",
     "captions": True,
     "script": {"shape": "One question you choose (shown as text), then a two-part answer: what it is, then who it is "
                         "for and where to get it. The presenter speaks for the brand, never as someone who bought it.",
                "starter": "What is {product}?"},
     "beats": ({"beat": "answer", "seconds": 7, "source": "presenter",
                "shot": "the presenter outdoors, chest up, as if stopped mid-walk; the question as a card above",
                "say": "what it is", "text": "the question, and the label AI presenter"},
               {"beat": "who it is for", "seconds": 8, "source": "presenter", "shot": "the same framing, a step closer",
                "say": "who it is for and where to get it", "text": "the label AI presenter"},
               _END_CARD)},
    {"id": "three-reasons-video", "name": "Three reasons", "ad_type": "voiceover", "duration": 20,
     "needs": "keyframe_video",
     "line": "Three reasons, numbered on screen, over the product",
     "what_for": "For a fast, skimmable case: three true reasons, each with its number on screen, over shots of your product.",
     "use": ("listicle",),
     "direction": "",
     "script": {"shape": "Three short reasons, each one sentence and each true of the product, then where to get it.",
                "starter": "Three reasons to try {product}."},
     "captions": True,
     "beats": ({"beat": "reason 1", "seconds": 6, "source": "product", "shot": "the product front on",
                "say": "the first reason", "text": "1 and the reason in four words", "tag": "1"},
               {"beat": "reason 2", "seconds": 7, "source": "product", "shot": "close on a detail",
                "say": "the second reason", "text": "2 and the reason in four words", "tag": "2"},
               {"beat": "reason 3", "seconds": 7, "source": "hands", "shot": "hands use it, no face",
                "say": "the third reason, then where to get it", "text": "3 and the reason in four words", "tag": "3"},
               _END_CARD)},
    {"id": "offer-promo", "name": "Offer, no countdown", "ad_type": "voiceover", "duration": 8,
     "needs": "keyframe_video", "placement": "product_only",
     "line": "The product, then your saved offer on the end card",
     "what_for": "For an offer you already run: the product on screen, then your saved price or offer on the end card. "
                 "No countdown clocks, no made-up deadlines.",
     "use": ("offer",),
     "direction": "",
     "script": {"shape": "What it is, then the offer exactly as it is saved on the product. No 'ends tonight' unless "
                         "the saved offer says so.", "starter": "{product}, and right now"},
     "beats": ({"beat": "product", "seconds": 8, "source": "product", "shot": "a slow push-in on the product",
                "say": "what it is, then the offer as saved", "text": ""},
               dict(_END_CARD, text="the saved price or offer as a badge (never typed fresh), the brand name and the button text"))},
    {"id": "how-to-steps", "name": "How-to in three steps", "ad_type": "voiceover", "duration": 20,
     "needs": "keyframe_video", "fits": _ALL_BUT_APPS,
     "line": "Three steps, each labelled, hands only",
     "what_for": "For teaching the use: three steps, each with its step number set on screen, shown by hands only.",
     "use": ("how-to", "hands-only-demo"),
     "direction": "",
     "script": {"shape": "Three steps in order, one sentence each, then where to get it.", "starter": "Here is how to use {product}."},
     "captions": True,
     "beats": ({"beat": "step 1", "seconds": 6, "source": "hands", "shot": "wide from above: the first step",
                "say": "the first step", "text": "Step 1 and three words", "tag": "Step 1"},
               {"beat": "step 2", "seconds": 7, "source": "hands", "shot": "the same frame: the second step",
                "say": "the second step", "text": "Step 2 and three words", "tag": "Step 2"},
               {"beat": "step 3", "seconds": 7, "source": "hands", "shot": "the same frame: the result, label to camera",
                "say": "the third step, then where to get it", "text": "Step 3 and three words", "tag": "Step 3"},
               _END_CARD)},
]

# What an ad is for: the `use` tag's vocabulary (id -> the words a picker shows).
USES = {
    "product-hero": "Product hero", "lifestyle": "Everyday scene", "detail": "Detail and finish", "gifting": "Gifting",
    "arrival": "Just arrived", "offer": "Offer", "feature-callouts": "Feature callouts", "social-proof": "Social proof",
    "testimonial-real-only": "Real reviews only", "comparison": "Comparison", "marketplace": "Marketplace listing",
    "seasonal": "Seasonal", "problem-solution": "Problem, then solution", "listicle": "List of reasons",
    "how-to": "How-to", "before-after-honest": "The switch, honestly told", "show-your-app": "Show your app",
    "talking-head": "Talking head", "talking-head-cutaways": "Talking head with cutaways", "voiceover": "Voiceover",
    "hands-only-demo": "Hands-only demo", "unboxing-pov": "Unboxing, first person",
    "green-screen-product-page": "Over the product page", "street-interview": "Street interview",
    "founder-story": "Founder story", "product-motion": "Product in motion", "trend": "Trend",
}
# The tag of every record that existed before m15 (the new ones carry theirs).
_USE = {
    "ugc": ("talking-head",), "voiceover": ("voiceover",), "image": ("product-hero",), "product_video": ("product-motion",),
    "motion": ("trend",),
    "hero": ("product-hero",), "routine": ("lifestyle",), "flatlay": ("lifestyle",), "inhand": ("lifestyle",),
    "gift": ("gifting", "seasonal"), "turn": ("product-motion",), "swap": ("how-to", "hands-only-demo"),
    "label": ("detail",), "how": ("how-to", "hands-only-demo"), "two-pm": ("lifestyle",), "iced": ("lifestyle",),
    "cost": ("comparison",), "gym": ("lifestyle",), "copy-trend": ("trend",),
    "shelf": ("lifestyle",), "closeup": ("detail",), "doorstep": ("arrival",), "table": ("lifestyle",),
    "counter": ("lifestyle",), "vanity": ("lifestyle",), "pastel": ("product-hero",), "outfit": ("lifestyle",),
    "street": ("lifestyle",), "nightstand": ("lifestyle",), "cozy": ("lifestyle",), "windowsill": ("lifestyle",),
    "desk": ("lifestyle",), "inuse": ("show-your-app", "lifestyle"),
    "clean-headline": ("product-hero",), "offer": ("offer",), "callouts": ("feature-callouts",),
    "review": ("social-proof", "testimonial-real-only"), "compare": ("comparison",), "marketplace": ("marketplace",),
}

# Where each layout needs calm space, said to the image model after the scene.
ZONES = {
    "headline": "Keep the top third of the frame calm, plain and uncluttered.",
    "offer": "Keep the top third of the frame and the area to the right of the product calm, plain and uncluttered.",
    "callouts": "Keep the top third and the left half of the frame calm, plain and uncluttered.",
    "review": "Keep the lower third of the frame calm, plain and uncluttered.",
    "compare": "Keep the top fifth and the lower half of the frame calm, plain and uncluttered.",
    "none": "",
}

# A layered static is the product alone: a person in it is where the words go (the image model has drawn a man
# holding the product for "the product centred on a clean surface"). Said after the scene and the calm zone, for
# every layered preset whose record doesn't set "people": True (none does today).
NO_PEOPLE = "No people, no hands, no faces and no body parts anywhere in the picture."


def product_only(preset_id) -> bool:
    """Is this a static with a text layer whose picture shows the product and nobody (all of them today)?"""
    p = BY_ID.get(str(preset_id or ""))
    return p is not None and bool(p.layout) and not p.extra.get("people")


def layered_scene(scene: str, layout: str, preset_id=None) -> str:
    """What the image model is asked to draw for a static with a text layer: the scene, where to leave calm space
    for the words, and that nobody is in it (worker.image_prompt; an unknown preset is treated as product-only)."""
    nobody = NO_PEOPLE if (str(preset_id or "") not in BY_ID or product_only(preset_id)) else ""
    return " ".join(x for x in ((scene or "").strip(), ZONES.get(layout, ""), nobody) if x)


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
    # m15: tags, and what a recipe needs before it can run
    category: tuple = ("any",)      # products.CATEGORIES codes it fits, or ("any",)
    use: tuple = ()                 # USES ids: what the ad is for
    needs: str = ""                 # "" = runs today; "keyframe_video" = waits for the keyframe-first pipeline
    vendor_preset: str = ""         # a vendor preset id to run instead of our recipe (none yet: see hf.vendor_presets)
    season: str = ""

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
        """The dict shape catalog.TEMPLATES has always had (templates, statics and recipes)."""
        if self.legacy:
            return self.legacy
        d = {"id": self.id, "name": self.name, "line": self.extra.get("line") or self.what_for, "tool": self.tool,
             "aspect": self.aspect, "fits": self.fits, "brief": self.recipe, "audience": "", "preview": self.preview,
             "preset": True}
        if self.layout:
            d["layout"] = self.layout
        if self.kind == "recipe":
            d.update(recipe=True, ad_type=self.ad_type, duration=self.price.get("duration") or 8,
                     situation=self.extra.get("situation") or "")
        return d

    def contract(self) -> dict:
        """The whole record as plain data (docs, MCP, tests)."""
        return {"id": self.id, "kind": self.kind, "name": self.name, "what_for": self.what_for, "format": self.format,
                "aspects": list(self.aspects), "aspect": self.aspect, "inputs": list(self.inputs),
                "recipe": self.recipe, "model": self.model, "params": dict(self.params), "beats": list(self.beats),
                "text_layers": list(self.text_layers), "layout": self.layout, "qa": list(self.qa),
                "locks": list(self.locks), "price": {"tool": self.tool, **self.price, "credits": self.price_credits()},
                "fits": self.fits if isinstance(self.fits, str) else list(self.fits),
                "tags": {"category": list(self.category), "use": list(self.use)}, "needs": self.needs,
                "vendor_preset": self.vendor_preset, "season": self.season,
                "script": dict(self.extra.get("script") or {}),
                "example": example_url(self.id), "example_video": example_video_url(self.id)}


_TOOL_FORMAT = {"ad_image": "image", "photo_to_video": "video", "motion_copy": "video", "ugc_video": "talking"}
_TOOL_TYPE = {"ad_image": "image", "photo_to_video": "product_video", "motion_copy": "motion", "ugc_video": "ugc"}
_IMAGE_ASPECTS = ("9:16", "1:1", "4:5", "16:9")       # catalog.TOOLS["ad_image"]["aspects"]
_TALK_ASPECTS = ("9:16", "1:1", "16:9")


def _category(fits) -> tuple:
    """The `category` tag from a record's `fits`."""
    if fits == "coffee":
        return ("food_drink",)
    return ("any",) if isinstance(fits, str) else tuple(fits)


def _from_type(d: dict) -> Preset:
    c = _TYPE_CONTRACT[d["id"]]
    aspects = _IMAGE_ASPECTS if d["tool"] == "ad_image" else _TALK_ASPECTS if d["tool"] == "ugc_video" else ()
    return Preset(id=d["id"], kind="type", name=d["name"], what_for=c["what_for"], format=c["format"], tool=d["tool"],
                  ad_type=d["id"], aspects=aspects, aspect=aspects[0] if aspects else "", inputs=c["inputs"],
                  recipe=c["recipe"], params=c["params"], beats=c["beats"], qa=c["qa"], locks=c["locks"],
                  price=c["price"], preview=d["preview"], legacy=d, extra={"line": d["line"]},
                  use=_USE[d["id"]])


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
                  preview=d["preview"], legacy=d if legacy else {}, extra={"line": d["line"]},
                  category=_category(d["fits"]), use=_USE[d["id"]])


def _from_layered(d: dict) -> Preset:
    return Preset(id=d["id"], kind="static", name=d["name"], what_for=d["what_for"], format="image", tool="ad_image",
                  ad_type="image", aspects=_IMAGE_ASPECTS, aspect=d["aspect"], inputs=d["inputs"], recipe=d["brief"],
                  params={"language": "English", "text": "none: set afterwards by textlayer.py"},
                  beats=({"beat": "one frame", "shot": d["line"]},), text_layers=d["text_layers"], layout=d["layout"],
                  fields=tuple(d["fields"]), qa=IMAGE_QA, price={"batch": 1}, preview=d["preview"],
                  extra={k: d[k] for k in ("line", "items_label", "items_hint", "item_max", "ai_items", "headline",
                                           "people") if k in d},
                  fits=d.get("fits", "any"), category=_category(d.get("fits", "any")),
                  use=tuple(d.get("use") or _USE.get(d["id"]) or ()), season=d.get("season") or "")


def _from_recipe(d: dict) -> Preset:
    """A fixed video format. It runs on the talking-video tool: a presenter video (ugc) or a voiceover."""
    ugc = d["ad_type"] == "ugc"
    fits = d.get("fits", "any")
    inputs = ("product_photo", "script") + (("presenter", "situation") if ugc else ("voice",)) \
        + tuple(d.get("extra_inputs") or ())
    params = {"bitrate_mode": "high", "generate_audio": True, "resolution": "720p", "duration": d["duration"]}
    for k in ("placement", "situation"):
        if d.get(k):
            params[k] = d[k]
    words = tuple(dict.fromkeys(("end_card" if b["beat"] == "end card" else "caption") for b in d["beats"] if b.get("text")))
    return Preset(id=d["id"], kind="recipe", name=d["name"], what_for=d["what_for"], format="talking", tool="ugc_video",
                  ad_type=d["ad_type"], aspects=_TALK_ASPECTS, aspect="9:16", inputs=inputs,
                  recipe=d["direction"] or "catalog.talking_prompt", params=params, beats=tuple(d["beats"]),
                  text_layers=words if d["needs"] else (),      # one take draws no words; the stitched one sets them
                  qa=(("keyframe",) + VIDEO_QA) if ugc else VIDEO_QA[:1],
                  locks=("product", "presenter_still", "voice") if ugc else ("product", "voice"),
                  price={"duration": d["duration"]}, fits=fits, preview="preset-" + d["id"],
                  extra={"line": d["line"], "script": d["script"], "direction": d["direction"],
                         "situation": d.get("situation") or "", "placement": d.get("placement") or "",
                         "labels": tuple(d.get("labels") or ()),
                         # read only by the keyframe-first pipeline (keyframe_video.py)
                         "captions": bool(d.get("captions")), "crossfade": int(d.get("crossfade") or 0),
                         "product_at": d.get("product_at") or "table",
                         "screens": tuple(d.get("extra_inputs") or ())},
                  category=_category(fits), use=tuple(d["use"]), needs=d["needs"], season=d.get("season") or "")


TYPES = [_from_type(d) for d in _TYPES]
OLD_TEMPLATES = [_from_template(d, True) for d in _TEMPLATES]
SCENES = [_from_template(d, False) for d in _SCENES]
LAYERED = [_from_layered(d) for d in _LAYERED]
STATICS_M15 = [_from_layered(d) for d in _STATICS_M15]      # seasonal statics and a few more, on the same layouts
RECIPES = [_from_recipe(d) for d in _RECIPES]              # fixed video formats
LIBRARY = TYPES + OLD_TEMPLATES + SCENES + LAYERED + STATICS_M15 + RECIPES
BY_ID = {p.id: p for p in LIBRARY}
assert len(BY_ID) == len(LIBRARY), "preset ids must be unique"
assert all(u in USES for p in LIBRARY for u in p.use) and all(p.use for p in LIBRARY), "every preset has known use tags"

def keyframe_on() -> bool:
    """KEYFRAME_VIDEO (default off): videos made from a recipe are built from one approved still, in beats, and
    stitched (keyframe_video.py). Off, nothing about any video changes."""
    return os.environ.get("KEYFRAME_VIDEO", "0").strip().lower() in ("1", "on", "true", "yes")


class _Pipelines:
    """The ways a job can be rendered: one take, always; the keyframe-first pipeline while KEYFRAME_VIDEO is on.
    Reads the switch each time it is asked, so `"keyframe_video" in PIPELINES` is the switch."""

    def _now(self) -> tuple:
        return ("single_take",) + (("keyframe_video",) if keyframe_on() else ())

    def __iter__(self):
        return iter(self._now())

    def __contains__(self, item) -> bool:
        return item in self._now()

    def __len__(self) -> int:
        return len(self._now())

    def __eq__(self, other) -> bool:
        return tuple(other) == self._now() if isinstance(other, (tuple, list, _Pipelines)) else NotImplemented

    def __repr__(self) -> str:
        return repr(self._now())


PIPELINES = _Pipelines()
EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "presets")
EXAMPLE_URL = "/static/presets"


def runnable(p: Preset) -> bool:
    """Can this preset be made today?"""
    return not p.needs or p.needs in PIPELINES


def pipeline_for(p) -> str:
    """How a video of this preset is rendered now: "keyframe_video" for any recipe while KEYFRAME_VIDEO is on, else
    "single_take" (also for anything that isn't a recipe)."""
    return "keyframe_video" if (p is not None and p.kind == "recipe" and keyframe_on()) else "single_take"


SCREEN_NEEDS = {"page_screenshot": "a screenshot of your product page", "app_screens": "your app's screenshots"}


def screens_needed(p) -> str:
    """Which of the member's own screenshots a recipe shows ("page_screenshot", "app_screens"), or ""."""
    return next((x for x in (p.extra.get("screens") or ()) if x in SCREEN_NEEDS), "") if p is not None else ""


def recipe_ready(p, product=None, n_photos: int = 0, n_screens: int = 0) -> tuple[bool, str]:
    """(can this recipe be made for this product now, why not). A recipe that shows the member's own screens needs
    them: screenshots passed with the config (`screens`), or, for an app, the product's own pictures (an app's
    photos are its screens). Nothing of a page or an app is ever generated, so without them the recipe stays
    unavailable for that product, and the reason says what to add."""
    if p is None or p.kind != "recipe":
        return False, "That isn't a video preset."
    if not runnable(p):
        return False, f"{p.name} isn't available yet."
    need = screens_needed(p)
    if not need or n_screens:
        return True, ""
    if need == "app_screens" and category(product) == "app_service" and n_photos:
        return True, ""
    return False, (f"{p.name} needs {SCREEN_NEEDS[need]}: upload it with the product's photos and pick it as the "
                   "screenshot. It is shown as it is; nothing of it is ever generated.")


def _example(preset_id, ext: str) -> str | None:
    pid = str(preset_id or "")
    if pid not in BY_ID or not re.fullmatch(r"[a-z0-9_-]+", pid):
        return None
    return f"{EXAMPLE_URL}/{pid}{ext}" if os.path.isfile(os.path.join(EXAMPLE_DIR, pid + ext)) else None


def example_url(preset_id) -> str | None:
    """The url of a preset's example image (static/presets/<id>.webp), or None while there is none."""
    return _example(preset_id, ".webp")


def example_video_url(preset_id) -> str | None:
    """The url of a video preset's example clip (static/presets/<id>.mp4), or None while there is none."""
    return _example(preset_id, ".mp4")


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
    """Every template, static and recipe as a catalog.TEMPLATES-shaped dict (catalog.TEMPLATE_BY_ID is built from
    this, so a job's template name and thumbnail work for all of them)."""
    return [p.as_template() for p in OLD_TEMPLATES + SCENES + LAYERED + STATICS_M15 + RECIPES]


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
    return coffee + own + anyp + ([p for p in LAYERED + STATICS_M15 if p.fits_product(product)] if lay else [])


def layered() -> list[Preset]:
    """Every static whose words are set by textlayer.py (the six of quality wave 2 and m15's)."""
    return LAYERED + STATICS_M15


def library_for(product=None) -> list[Preset]:
    """Everything Create's preset list offers for this product (the five ad types are its own step). The video
    recipes are not in this list: the pages that read it can't yet send a recipe (recipes_for)."""
    vids = [p for p in OLD_TEMPLATES if p.tool != "ad_image" and p.fits_product(product)]
    return statics_for(product) + vids


def recipes_for(product=None, everything: bool = False) -> list[Preset]:
    """The fixed video formats that fit this product. Members only get the ones that can run today; `everything`
    (a site admin, the docs) also lists the ones waiting for the keyframe-first pipeline."""
    return [p for p in RECIPES if (everything or runnable(p)) and (product is None or p.fits_product(product))]


def offered(product=None, everything: bool = False) -> list[Preset]:
    """Every preset a member can pick for this product (or for any, with none): statics, the older video templates
    and the recipes that run today."""
    if product is None:
        stat = [p for p in OLD_TEMPLATES + SCENES if p.tool == "ad_image"] + (layered() if text_layers_on() else [])
        vids = [p for p in OLD_TEMPLATES if p.tool != "ad_image"]
        return stat + vids + recipes_for(None, everything)
    return library_for(product) + recipes_for(product, everything)


def public(p: Preset, url_for=None) -> dict:
    """card() plus what m15 added: tags, the example's urls and, for a recipe, its length, place and script shape."""
    d = card(p, url_for)
    d.update(tags={"category": list(p.category), "use": list(p.use)}, season=p.season, needs=p.needs,
             runnable=runnable(p), example=example_url(p.id), example_video=example_video_url(p.id),
             inputs=list(p.inputs))
    if p.kind == "recipe":
        d.update(duration=p.price.get("duration"), situation=p.extra.get("situation") or "",
                 script=dict(p.extra.get("script") or {}), beats=[dict(b) for b in p.beats],
                 pipeline=pipeline_for(p), screens=screens_needed(p), captions=bool(p.extra.get("captions")))
    if p.layout:
        d["fields"] = list(p.fields)
    return d


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
                raise LayerError(f.get("error") or "Tick the box to confirm the reviews are real before making this.")
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
