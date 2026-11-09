"""What Campaignify can make: the tools (each backed by one model), what they
cost, and the ready-made templates (the angles that sell for us).

Costs are the vendor's list prices in USD at the settings we use. Members pay
in RS credits: 1 credit = $0.01, price = cost x MARKUP, rounded up, so the
markup is the margin. Change MARKUP in settings, not here.
"""
import math
import os
import re

import liveids

MARKUP = float(os.environ.get("RS_MARKUP", "2.0"))
# 1080p carries a smaller markup of its own (owner decision D9): the vendor's
# 1080p rate is ~2.5x its 720p rate, so 2x would price it out of reach.
HD_MARKUP = float(os.environ.get("RS_HD_MARKUP", "1.4"))
USD_PER_SECOND_1080P = float(os.environ.get("RS_VIDEO_USD_PER_SECOND_1080P", "1.1372"))
DEFAULT_RESOLUTION = "720p"

TALKING_MIN_SECONDS, TALKING_MAX_SECONDS = 4, 30     # the vendor's limits for one talking video


def _talking_durations() -> list[int]:
    """The talking-video lengths offered: RS_TALKING_DURATIONS ("8,10,15,20,25,30"),
    kept inside the vendor's 4 to 30 seconds, sorted; the default if that's empty."""
    raw = os.environ.get("RS_TALKING_DURATIONS", "8,10,15,20,25,30")
    out = sorted({int(x) for x in re.findall(r"\d+", raw) if TALKING_MIN_SECONDS <= int(x) <= TALKING_MAX_SECONDS})
    return out or [8, 10, 15, 20, 25, 30]


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
        # Vendor's own estimate (Oct 29): $0.2056/s at 480p, $0.4622/s at 720p,
        # $1.1372/s at 1080p. We render 720p.
        "usd_per_second": float(os.environ.get("RS_VIDEO_USD_PER_SECOND", "0.4622")),
        "durations": [5, 10],
        "max_photos": 1,
        "resolutions": {"720p": float(os.environ.get("RS_VIDEO_USD_PER_SECOND", "0.4622")),
                        "1080p": USD_PER_SECOND_1080P},
    },
    "ugc_video": {
        "preview": "how",
        "name": "Talking video",
        "blurb": "A presenter or narrator says your script, with your product on screen. Speech is generated with the video.",
        # Reference-to-video with native audio: product photos as references, the
        # presenter, voice and spoken lines in the prompt. Same Seedance 2.5 price
        # table as image-to-video (vendor estimate, Oct 29: $0.4622/s at 720p).
        "model": "bytedance/seedance-2.5/reference-to-video",
        "kind": "video",
        "usd_per_second": float(os.environ.get("RS_TALKING_USD_PER_SECOND", "0.4622")),
        # The vendor's Seedance 2.5 takes 4 to 30 whole seconds in one generation
        # (its model catalog, checked Nov 7 2026), so a longer script is one longer
        # video, never clips stitched together: each generation draws its presenter
        # fresh, so stitched clips would cut between different-looking people.
        # RS_TALKING_DURATIONS narrows the list if the vendor ever lowers its cap.
        "durations": _talking_durations(),
        "max_photos": 4,
        "aspects": ["9:16", "1:1", "16:9"],
        "resolutions": {"720p": float(os.environ.get("RS_TALKING_USD_PER_SECOND", "0.4622")),
                        "1080p": USD_PER_SECOND_1080P},
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


# Upscaling a finished video to 1080p: off until a vendor model and its price are
# confirmed (owner decision D10). Set both to switch it on.
UPSCALE_MODEL = os.environ.get("RS_VIDEO_UPSCALE_MODEL", "").strip()
UPSCALE_USD_PER_SECOND = float(os.environ.get("RS_VIDEO_UPSCALE_USD_PER_SECOND", "0") or 0)
UPSCALE_TOOL = {"preview": "photo_to_video", "name": "Upscale to 1080p", "model": UPSCALE_MODEL, "kind": "video",
                "usd_per_second": UPSCALE_USD_PER_SECOND, "max_photos": 0, "hidden": True,
                "blurb": "Upscale a finished video to 1080p."}
if UPSCALE_MODEL and UPSCALE_USD_PER_SECOND > 0:
    TOOLS["video_upscale"] = UPSCALE_TOOL


class _DisplayTools(dict):
    """TOOLS for templates: also knows tools that are switched off now (an old upscale job still renders)."""

    def __missing__(self, key):
        return TOOLS.get(key) or (UPSCALE_TOOL if key == "video_upscale" else
                                  {"name": key, "kind": "video", "preview": "photo_to_video"})

    def __contains__(self, key):
        return key in TOOLS or key == "video_upscale"


DISPLAY_TOOLS = _DisplayTools()


def offered_tools() -> dict:
    """Tools a member (or the planner) can pick directly: not the hidden ones."""
    return {k: t for k, t in TOOLS.items() if not t.get("hidden")}


def upscale_on() -> bool:
    return "video_upscale" in TOOLS


def resolutions(tool: str) -> dict:
    """{name: vendor USD per second} for a video tool that offers a choice (720p is the default)."""
    return TOOLS.get(tool, {}).get("resolutions") or {}


def markup_for(tool: str, resolution: str = "") -> float:
    return HD_MARKUP if resolution == "1080p" or tool == "video_upscale" else MARKUP


def cost_usd(tool: str, *, batch: int = 1, duration: float = 5, ref_seconds: float = 0,
             resolution: str = DEFAULT_RESOLUTION) -> float:
    t = TOOLS[tool]
    if tool == "ad_image":
        return t["usd_each"] * max(1, batch)
    if tool in ("photo_to_video", "ugc_video"):
        rate = resolutions(tool).get(resolution or DEFAULT_RESOLUTION)
        if rate is None:
            raise KeyError(resolution)
        return rate * duration
    if tool == "video_upscale":
        return t["usd_per_second"] * duration
    if tool == "motion_copy":
        secs = math.ceil(min(t["max_seconds"], ref_seconds or t["max_seconds"]))
        return t["usd_per_second"] * secs
    raise KeyError(tool)


def price_credits(tool: str, **kw) -> int:
    return int(math.ceil(round(cost_usd(tool, **kw) * markup_for(tool, kw.get("resolution", "")) * 100, 6)))


def hd_delta(tool: str, duration: float) -> int:
    """Extra credits for 1080p over 720p at this length."""
    return price_credits(tool, duration=duration, resolution="1080p") - price_credits(tool, duration=duration)


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
AD_TYPE_BY_ID = {a["id"]: a for a in AD_TYPES}
TOOL_AD_TYPE = {"ad_image": "image", "photo_to_video": "product_video", "motion_copy": "motion", "ugc_video": "ugc"}


def job_ad_type(tool: str, params=None, default: str | None = None) -> str:
    """The ad type of a job or a planner proposal: what its params say first (a voiceover and a presenter video share
    the ugc_video tool), then the tool's own type. `params` is the params dict or its JSON."""
    if isinstance(params, (str, bytes)):
        try:
            import json
            params = json.loads(params or "{}")
        except (ValueError, TypeError):
            params = {}
    typ = (params or {}).get("ad_type") if isinstance(params, dict) else None
    if typ in AD_TYPE_BY_ID:
        return typ
    return TOOL_AD_TYPE.get(tool, default if default is not None else tool)


def ad_types() -> list[dict]:
    caps = capabilities()
    return [a for a in AD_TYPES if not a.get("talking") or caps["talking"]]


def from_prices() -> dict:
    """The cheapest version of each tool, for "from N credits" labels (Home and Create)."""
    return {"ugc_video": price_credits("ugc_video", duration=TOOLS["ugc_video"]["durations"][0]),
            "ad_image": price_credits("ad_image", batch=1),
            "photo_to_video": price_credits("photo_to_video", duration=5),
            "motion_copy": price_credits("motion_copy", ref_seconds=5)}


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
# Each presenter's default voice (default_voice() below). Every presenter has one: tests check the map is complete.
PRESENTER_VOICE = {"maya": "warm_f", "priya": "warm_f", "elena": "warm_f", "sam": "upbeat_f", "lena": "upbeat_f",
                   "jordan": "chatty_m", "theo": "chatty_m", "marcus": "calm_m"}

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


# A typed script may run up to 20% over max_words (some people talk fast); past
# that the video cuts off or rushes, so Create refuses it. Scripts the AI writes
# aim at max_words itself and are checked before the member sees them.
HARD_SLACK = 1.2
HOOK_MAX_WORDS = 12


def max_words(duration: int) -> int:
    """Roughly how many spoken words fit a clip (natural pace)."""
    return int(duration * WORDS_PER_SECOND)


def hard_words(duration: int) -> int:
    """The most words a typed script may have at this length before it's refused."""
    return int(max_words(duration) * HARD_SLACK)


def words(text: str) -> int:
    return len((text or "").split())


def seconds_for(n_words: int) -> float:
    """About how long n words take to say at the natural pace."""
    return round(n_words / WORDS_PER_SECOND, 1)


def fits_duration(n_words: int, durations=None) -> int | None:
    """The shortest offered length a script of n words fits (as typed), or None."""
    for d in durations or TOOLS["ugc_video"]["durations"]:
        if n_words <= hard_words(d):
            return d
    return None


def word_budget(duration: int) -> dict:
    """The exact limits the script writer gets for one length: the whole script
    (hook + body, said out loud), each hook, and the body after the longest hook."""
    total = max_words(duration)
    hook = max(4, min(HOOK_MAX_WORDS, round(total * 0.4)))
    return {"seconds": duration, "total": total, "hook": hook, "body": max(0, total - hook), "hard": hard_words(duration)}


_SENT = re.compile(r"[^.!?]+(?:[.!?]+[\"')\]]*|$)")


def sentences(text: str) -> list[str]:
    return [x.strip() for x in _SENT.findall(" ".join((text or "").split())) if x.strip()]


def trim_words(text: str, limit: int) -> str:
    """`text` cut to at most `limit` words, on sentence boundaries where it can:
    the last sentence (the call to action) is kept and sentences before it are
    dropped from the end; a single sentence that's still too long is cut at a
    clause (comma) or, last of all, at a word, and closed with a full stop."""
    text = " ".join((text or "").split())
    if limit <= 0:
        return ""
    if words(text) <= limit:
        return text
    sents = sentences(text)
    if len(sents) > 1:
        last, ends, cands = sents[-1], [], []
        for k in range(len(sents) - 1, -1, -1):
            for c in (sents[:k] + [last], sents[:k]):
                n = words(" ".join(c))
                if c and n <= limit:
                    (ends if c[-1] is last else cands).append((n, " ".join(c)))
        # keep the call to action when that still leaves at least half the words
        if ends and max(ends)[0] >= max(3, limit // 2):
            return max(ends)[1]
        if ends or cands:
            best = max(ends + cands)
            if best[0] >= max(3, limit // 2) or words(sents[0]) > limit:
                return best[1]
    one = sents[0] if sents else text
    parts = [p.strip() for p in one.split(",") if p.strip()]
    acc = []
    for part in parts:
        if words(", ".join(acc + [part])) > limit:
            break
        acc.append(part)
    cut = ", ".join(acc) if acc and words(", ".join(acc)) >= max(2, limit // 2) else " ".join(one.split()[:limit])
    cut = cut.rstrip(" ,;:-")
    return cut + ("" if cut[-1:] in ".!?" else ".") if cut else ""


def fit_spoken(hooks: list[str], body: str, duration: int, keep_all: bool = False) -> tuple[list[str], str]:
    """(hooks, body) where every hook + body is at most max_words(duration) words,
    with no hook over the hook limit. Hooks that are too long are dropped while
    another one fits, else cut (with keep_all, each is cut, so the number of
    hooks, and so of videos, stays the same); the body is cut to what the
    longest kept hook leaves."""
    bud = word_budget(duration)
    hooks = [" ".join(h.split()) for h in hooks if h and h.strip()]
    if keep_all:
        # a typed hook may run longer than the writer's hook limit, as long as it leaves room for a body
        keep = [trim_words(h, max(bud["hook"], int(bud["total"] * 0.6))) for h in hooks]
    else:
        keep = [h for h in hooks if words(h) <= bud["hook"]]
        if not keep and hooks:
            keep = [trim_words(min(hooks, key=words), bud["hook"])]
    room = bud["total"] - max([words(h) for h in keep] or [0])
    body = trim_words(body, room)
    return [h for h in keep if words(h + " " + body) <= bud["total"]], body


# The fidelity line every prompt that starts from the member's product photo ends with (worker.build_inputs adds it
# to photo-to-video scenes, hand-typed ones included, when the scene doesn't already say it).
KEEP_PRODUCT = "Keep the product, its label and its logo exactly as in the reference photo."

# What makes a presenter video read as filmed by a person rather than rendered: said in every presenter prompt.
UNPOLISHED = ("Unpolished, real-phone look: natural skin texture with visible pores, slight softness rather than "
              "razor-sharp focus, an ordinary lived-in room, handheld phone framing. No slow motion, no studio "
              "lighting, no beauty filter.")

# Where the product is in a presenter video. "table" (the default) keeps it out of the presenter's hands: a talking
# face and a hand on the product are the two hardest things to render, and they shouldn't share a clip. "holding" is
# the old look, still selectable (params["placement"]). A voiceover's "product_only" keeps hands out of the shots.
PLACEMENTS = ("table", "holding")
DEFAULT_PLACEMENT = "table"


def with_fidelity(scene: str) -> str:
    """A photo-to-video scene with the fidelity line at the end (once)."""
    scene = " ".join((scene or "").split())
    if KEEP_PRODUCT.lower() in scene.lower():
        return scene
    if scene and scene[-1] not in ".!?":
        scene += "."
    return (scene + " " + KEEP_PRODUCT).strip()


def talking_prompt(ad_type: str, product: str, script: str, presenter_id: str = "", voice_id: str = "",
                   setting: str = "", placement: str = "") -> str:
    """The prompt for a talking video (built by the worker from the job's params)."""
    product = product or "the product"
    voice = VOICE_BY_ID.get(voice_id) or VOICES[0]
    say = " ".join((script or "").split())
    keep = (f"The {product} looks exactly as in the reference photos: same packaging, label, text and colors; never "
            "invent other branding.")
    if ad_type == "voiceover":
        shots = ("Slow, simple camera moves over the product resting on a table in a real home: a push-in, a gentle "
                 "pan, a close-up of the label. No people and no hands in frame."
                 if placement == "product_only" else
                 "Clean, well-lit close-ups and hands using it in a real home; smooth, simple camera moves.")
        return (f"Short product video of the {product}. {shots} "
                f"{keep} A narrator, never seen on camera, says in {voice['desc']}: "
                f"\"{say}\" The narration is clear and matches the pacing of the shots. No on-screen text, no subtitles, "
                "no music.")
    pr = PRESENTER_BY_ID.get(presenter_id) or PRESENTERS[0]
    where = setting or pr["setting"]
    if placement == "holding":
        does = f"holds the {product} up to the camera and talks"
    else:
        does = (f"sits with the {product} standing on the table beside them, in clear view, label facing the camera, "
                "and talks. Their hands stay off the product")
    return (f"Vertical selfie-style UGC video filmed on a phone. A {pr['look']}, in {where}, {does} directly to the "
            f"viewer in a natural, unscripted tone with {voice['desc']}. They say: "
            f"\"{say}\" Lip movements match the speech exactly. {keep} {UNPOLISHED} Natural light, slight handheld "
            "movement, no on-screen text, no subtitles, no music.")


def default_voice(presenter_id: str) -> str:
    """The voice a presenter speaks with unless the member picks another (one map: Create, the first run and the
    planner all read it). A presenter without an entry gets the first voice of VOICES."""
    return PRESENTER_VOICE.get(presenter_id) or VOICES[0]["id"]


# ---------------------------------------------------------------- image ad text
# The image model draws the ad's text itself (there is no overlay renderer for
# images), so the prompt has to say exactly which words may appear. Only the
# member's headline, the optional subline and button text they typed, and the
# brand name are allowed; everything else in the brief is treated as a scene
# description, with any quoted words, "Callout:"-style text and sentences
# about buttons, badges or prices taken out before it reaches the model.
TEXT_LIMITS = {"headline": 120, "subline": 140, "cta": 30}
_LABELED = re.compile(r'\b(Headline|Sub-?line|Sub-?headline|Button|CTA)\s*:\s*["“]([^"”]{1,200})["”][^.!?"“]*[.!?]?', re.I)
_TEXT_LABELS = re.compile(r"\b(tag-?line|slogan|caption|callouts?|call[- ]to[- ]action|badge|sticker|banner text|"
                          r"(?:on-screen|overlay) text|text overlay|bullet points?|copy)\s*:\s*[^.!?\n]*[.!?]?", re.I)
_QUOTED = re.compile(r'["“”]([^"“”]{1,200})["“”]')
_TEXTY = re.compile(r"\b(buttons?|badges?|callouts?|stickers?|seals? of|taglines?|slogans?|captions?|bullet points?|"
                    r"feature list|price tags?|prices?|discounts?|percent|shop now|buy now|order now|call[- ]to[- ]action|"
                    r"cta|hashtags?|url|website address|star ratings?|reviews?|text overlays?|on-screen text|"
                    r"sale|limited time)\b", re.I)


def split_image_brief(brief: str) -> dict:
    """{scene, headline, subline, cta} from a stored image brief: the text the
    member chose is written in it as Headline: "...", Subline: "...",
    Button: "..."; the rest is the scene."""
    out = {"headline": "", "subline": "", "cta": ""}
    keys = {"headline": "headline", "subline": "subline", "sub-line": "subline", "subheadline": "subline",
            "sub-headline": "subline", "button": "cta", "cta": "cta"}

    def take(m):
        k = keys.get(m.group(1).lower())
        if k and not out[k]:
            out[k] = " ".join(m.group(2).split())
        return " "
    scene = _LABELED.sub(take, brief or "")
    out["scene"] = " ".join(scene.split())
    return out


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def scene_only(text: str, allowed=(), forbid=()) -> str:
    """The scene part of a brief with every source of extra on-image text taken
    out: labeled text ("Callout: ..."), quoted words that aren't allowed,
    sentences about buttons, badges, prices or captions, and any sentence that
    repeats a product claim the member didn't put on the image."""
    ok = {_norm(a) for a in allowed if a}
    text = _LABELED.sub(" ", text or "")
    text = _TEXT_LABELS.sub(" ", text)
    text = _QUOTED.sub(lambda m: m.group(0) if _norm(m.group(1)) in ok else "", text)
    bad = [_norm(f) for f in forbid if f and _norm(f) and _norm(f) not in ok]
    keep = []
    for sent in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        n = _norm(sent)
        if not n or _TEXTY.search(sent) or any(b and b in n for b in bad):
            continue
        keep.append(re.sub(r"\s+([,.!?;:])", r"\1", sent).strip(" ,;:"))
    return " ".join(k for k in keep if k)


NO_EXTRA_TEXT = ("No other text of any kind anywhere in the image: no extra slogans, taglines, claims, benefit callouts, "
                 "feature lists, bullet points, labels, badges, seals, stickers, star ratings, reviews, prices, numbers, "
                 "percentages, discounts, hashtags, URLs, watermarks or extra logos.")


def image_ad_prompt(scene: str, headline: str = "", subline: str = "", cta: str = "", brand: str = "",
                    product: str = "", forbid=(), service: bool = False) -> str:
    """The prompt the image model gets for one image ad. It names every word
    allowed on the image (headline, optional subline and button, brand name)
    and forbids all others; product claims in `forbid` (the product's approved
    and suggested claims) never enter it unless the member put that exact text
    in the headline, subline or button."""
    clean = lambda s, k: " ".join(str(s or "").split())[:TEXT_LIMITS[k]].replace('"', "'")  # noqa: E731
    headline, subline, cta = clean(headline, "headline"), clean(subline, "subline"), clean(cta, "cta")
    brand = " ".join(str(brand or "").split())[:80].replace('"', "'")
    product = " ".join(str(product or "").split())[:80] or "product"
    allowed = [x for x in (headline, subline, cta, brand) if x]
    scene = scene_only(scene, allowed, forbid) or f"Clean, premium product shot of the {product}."
    if service:   # a service business (service_path.py): the references are its logo or site photos, not packaging
        import service_path
        lines = [f"Advertising image. Scene: {scene}"] + service_path.image_lines(product)
    else:
        lines = [f"Advertising image. Scene: {scene}",
                 f"The product is the {product} exactly as in the reference photos: same shape, packaging, label, printed "
                 "text and colors. Don't redraw, change or add to its label."]
    lines.append("Text on the image: only the words listed here, each shown once and spelled exactly as written.")
    if headline:
        lines.append(f'- Headline (the main text, large and easy to read): "{headline}"')
    if subline:
        lines.append(f'- Subline (smaller, under the headline): "{subline}"')
    if cta:
        lines.append(f'- Button (one simple button): "{cta}"')
    if brand:
        lines.append(f'- Brand name (optional, small): "{brand}"')
    if not (headline or subline or cta):
        lines.append("- No headline: add no words at all.")
    lines.append("The only other text allowed is what is already in the logo." if service else
                 "The only other text allowed is what is already printed on the product's own packaging in the photos.")
    lines.append(NO_EXTRA_TEXT + (" Exactly one button, with the text above, and no other call to action." if cta else
                                  " No buttons and no call to action."))
    return "\n".join(lines)


# Templates: the angles from the playbook. {product} and {brand} are filled from
# the workspace's brand kit; members can edit the brief before generating.
# "fits": "any" works for any product you can photograph; "coffee" templates
# were written for a coffee product and assume one.
TEMPLATES = [
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

TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}


def fill(text: str, brand: dict) -> str:
    """Put the product and brand names into a template brief. With no product
    name, "the {product}" reads "the product" (never "the the product") and a
    bare {product} reads "your product"."""
    import re
    name = (brand.get("product") or "").strip()
    text = re.sub(r"\b([Tt]he|[Yy]our) \{product\}", lambda m: f"{m.group(1)} {name or 'product'}", text or "")
    return text.replace("{product}", name or "your product").replace("{brand}", (brand.get("name") or "").strip())


COFFEE_WORDS = ("coffee", "espresso", "latte", "cold brew", "cappuccino", "mocha", "decaf", "arabica", "roast")


def is_coffee(product) -> bool:
    """Coffee templates assume a coffee product: only offered when it says so."""
    if not product:
        return False
    if isinstance(product, dict) and "product" in product:   # a brand brief (core.brand())
        text = " ".join(str(product.get(k) or "") for k in ("product", "description", "tags", "about"))
    else:
        keys = product.keys() if hasattr(product, "keys") else []
        text = " ".join(str(product[k] or "") for k in ("name", "description", "tags") if k in keys)
    text = text.lower()
    return any(w in text for w in COFFEE_WORDS)


def templates_for(product=None, tool=None) -> list[dict]:
    """Templates that fit this product: the any-product ones first, coffee ones only for coffee."""
    out = [t for t in TEMPLATES if (t.get("fits") == "any" or (t.get("fits") == "coffee" and is_coffee(product)))
           and (tool is None or t["tool"] == tool)]
    return sorted(out, key=lambda t: t.get("fits") != "any")


def first_run_templates(product=None) -> list[dict]:
    """Image templates for the first ad: cheapest tool; with no product yet, only ones that fit anything."""
    return templates_for(product, "ad_image")


# The first two free image ads are two different looks, not two versions of one: the template the member picked plus
# the one that suits what they sell. Each row: the words that mark the category, then image templates in the order
# to offer them. Coffee has its own templates (is_coffee); anything else falls back to FIRST_RUN_GENERIC.
FIRST_RUN_CATEGORIES = [
    (("skincare", "skin care", "serum", "moisturi", "cleanser", "lotion", "shampoo", "conditioner", "makeup", "lipstick",
      "mascara", "soap", "deodorant", "perfume", "fragrance", "sunscreen", "supplement", "vitamin", "toothpaste"),
     ["routine", "flatlay", "hero"]),
    (("mug", "tumbler", "bottle", "flask", "backpack", "bag", "wallet", "watch", "sunglasses", "hat", "cap", "shoe",
      "sneaker", "shirt", "hoodie", "jacket", "jewel", "necklace", "bracelet", "ring", "phone case", "headphone",
      "earbud"),
     ["inhand", "hero", "flatlay"]),
    (("candle", "gift", "card", "print", "poster", "toy", "puzzle", "plush", "ornament", "decor", "vase", "blanket",
      "chocolate", "candy", "cookie"),
     ["gift", "flatlay", "hero"]),
    (("tea", "snack", "sauce", "spice", "granola", "protein", "drink", "juice", "soda", "honey", "jam", "pasta", "oil"),
     ["routine", "hero", "flatlay"]),
]
FIRST_RUN_GENERIC = ["hero", "routine", "flatlay", "inhand", "gift"]
FIRST_RUN_COFFEE = ["two-pm", "iced", "gym"]


def first_run_category(product) -> list:
    """Image template ids that suit this product, best first (product: a brand brief, a product row, or a name)."""
    import re
    if isinstance(product, str):
        product = {"product": product}
    if is_coffee(product):
        return FIRST_RUN_COFFEE + FIRST_RUN_GENERIC
    if isinstance(product, dict):
        text = " ".join(str(product.get(k) or "") for k in ("product", "name", "description", "tags"))
    else:
        keys = product.keys() if product is not None and hasattr(product, "keys") else []
        text = " ".join(str(product[k] or "") for k in ("name", "description", "tags") if k in keys)
    text = text.lower()
    for words, ids in FIRST_RUN_CATEGORIES:
        if any(re.search(r"\b" + re.escape(w), text) for w in words):
            return ids + [i for i in FIRST_RUN_GENERIC if i not in ids]
    return list(FIRST_RUN_GENERIC)


def first_run_pair(product, pick_id: str | None = None) -> list[dict]:
    """The two image templates for a first run: the member's pick (else the best fit), then a DIFFERENT one that
    suits the product's category, else a different generic one. Always two distinct image templates."""
    order = [TEMPLATE_BY_ID[i] for i in first_run_category(product)]
    pick = TEMPLATE_BY_ID.get(pick_id or "")
    first = pick if pick and pick["tool"] == "ad_image" else order[0]
    second = next(t for t in order if t["id"] != first["id"])
    return [first, second]


def template_prefill(t: dict, b: dict) -> dict:
    """A template as a starting point in Create: ad type, shape, the filled brief
    (an image template's headline becomes its first headline), and length."""
    import re
    brief = fill(t["brief"], b)
    typ = TOOL_AD_TYPE.get(t["tool"], "image")
    out = {"type": typ, "angle": {"title": t["name"], "hook": "", "insight": t["line"], "audience": t.get("audience") or ""},
           "step": "script", "template": t["id"]}
    if t["tool"] == "ad_image":
        m = re.search(r'Headline:\s*"([^"]+)"', brief)
        out["items"] = [m.group(1)] if m else [t["name"]]
        out["body"] = re.sub(r'\s*Headline:\s*"[^"]+"\.?', "", brief).strip()
        out["aspects"] = [t.get("aspect") or "9:16"]
    else:
        out["items"], out["body"] = [brief], ""
        if t.get("duration"):
            out["duration"] = t["duration"]
    return out


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
     "price_id": None},
    {"code": "pro", "name": "Pro", "usd": 79, "credits": 8500, "bonus": 600,
     "price_id": None},
    {"code": "studio", "name": "Studio", "usd": 199, "credits": 23000, "bonus": 3100,
     "price_id": None},
]
for _p in PACKS:      # the price ids are settings (liveids.py, M7.4)
    _p["price_id"] = liveids.get(f"STRIPE_PRICE_{_p['code'].upper()}")
PACK_BY_CODE = {p["code"]: p for p in PACKS}


def rates() -> dict:
    """What credits buy, for pricing pages."""
    return {"talk10": price_credits("ugc_video", duration=10),
            "talk8": price_credits("ugc_video", duration=8),
            "image": price_credits("ad_image", batch=1),
            "video5": price_credits("photo_to_video", duration=5),
            "video10": price_credits("photo_to_video", duration=10),
            "video5_1080": price_credits("photo_to_video", duration=5, resolution="1080p"),
            "talk10_1080": price_credits("ugc_video", duration=10, resolution="1080p"),
            "motion_per_s": price_credits("motion_copy", ref_seconds=1),
            "motion_max": price_credits("motion_copy", ref_seconds=TOOLS["motion_copy"]["max_seconds"])}
