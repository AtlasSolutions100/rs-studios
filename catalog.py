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
import presenters
import presets

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


# ---------------------------------------------------------------- internal tools (never sold to members)
# The library's own image model (m15, library.py): a general text-to-image model that also edits from reference
# images, used by a site admin to make presenter stills ("same person, new situation" passes the first still as its
# reference) and the demo products' packshots. It is NOT in TOOLS: Create, the planner, the price pages and
# offered_tools() never see it, and only library.py queues jobs for it.
#
# Model: the vendor's "Marketing Studio Image 2.5 Sunburst" (GPT Image 2.5 behind it; `prompt`, optional
# `image_urls` to edit from, `resolution` 1k/2k/4k, `aspect_ratio`, `quality`). Chosen because the owner's paid test
# showed a GPT Image still keeps a product label exactly and holds a face from a reference. The vendor bills it by
# tokens ($0.0107 to $0.6136 an image across its settings) and publishes no per-setting table, so the dollars here
# are our estimates; the quote asks the vendor's free estimate first and the worker refuses a still whose live
# estimate is over PRICE_TOLERANCE x what was quoted. RS_LIBRARY_STILL_MODEL / RS_LIBRARY_STILL_USD override.
LIBRARY_STILL_USD = {("low", "1k"): 0.015, ("medium", "1k"): 0.055, ("high", "1k"): 0.20,
                     ("low", "2k"): 0.022, ("medium", "2k"): 0.085, ("high", "2k"): 0.33}
LIBRARY_STILL_REF_USD = 0.015        # each reference image read by the model (image input tokens)
LIBRARY_STILL_DEFAULT = ("high", "1k")
LIBRARY_STILL_ASPECTS = ("9:16", "3:4", "1:1", "2:3", "4:3", "16:9")       # the vendor's list has no 4:5
INTERNAL_TOOLS = {
    "library_still": {
        "preview": "ad_image", "name": "Library still", "kind": "image", "hidden": True, "internal": True,
        "blurb": "A still for the library: a presenter in a situation, or a demo product's packshot.",
        "model": os.environ.get("RS_LIBRARY_STILL_MODEL", "").strip() or "marketing-studio/image/sunburst",
        "usd": LIBRARY_STILL_USD, "max_photos": 4},
}
# Library clips (a presenter's voice sample) run on the talking-video model at the cheapest setting the vendor has;
# members are never offered 480p (preview.py uses the same rate for the watermarked free preview).
LIBRARY_USD_PER_SECOND_480P = float(os.environ.get("RS_VIDEO_USD_PER_SECOND_480P", "0.2056"))


def any_tool(tool: str) -> dict | None:
    """A tool's record whether it is sold (TOOLS) or internal (INTERNAL_TOOLS)."""
    return TOOLS.get(tool) or INTERNAL_TOOLS.get(tool)


def model_for(tool: str) -> str:
    """The vendor model a job of this tool is sent to (the worker)."""
    t = any_tool(tool)
    if t is None:
        raise KeyError(tool)
    return t["model"]


def library_still_usd(quality: str = "", resolution: str = "", refs: int = 0) -> float:
    """Our estimate of one library still's vendor cost at these settings, with `refs` reference images."""
    env = os.environ.get("RS_LIBRARY_STILL_USD", "").strip()
    q, r = (quality or LIBRARY_STILL_DEFAULT[0]), (resolution or LIBRARY_STILL_DEFAULT[1])
    if (q, r) not in LIBRARY_STILL_USD:
        raise KeyError(f"{q}/{r}")
    try:
        base = float(env) if env else LIBRARY_STILL_USD[(q, r)]
    except ValueError:
        base = LIBRARY_STILL_USD[(q, r)]
    return round(base + LIBRARY_STILL_REF_USD * max(0, int(refs or 0)), 4)


class _DisplayTools(dict):
    """TOOLS for templates: also knows tools that are switched off now (an old upscale job still renders) and the
    internal ones (a library still shows in the admin's own Library like any image)."""

    def __missing__(self, key):
        return TOOLS.get(key) or INTERNAL_TOOLS.get(key) or (
            UPSCALE_TOOL if key == "video_upscale" else {"name": key, "kind": "video", "preview": "photo_to_video"})

    def __contains__(self, key):
        return key in TOOLS or key in INTERNAL_TOOLS or key == "video_upscale"


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
             resolution: str = DEFAULT_RESOLUTION, quality: str = "", refs: int = 0) -> float:
    if tool == "library_still":      # internal (INTERNAL_TOOLS): `resolution` is 1k or 2k here, never a video size
        res = resolution if resolution in ("1k", "2k") else ""
        return library_still_usd(quality, res, refs) * max(1, batch)
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


# The five ad types and every template live in presets.py now (one record per preset, with the whole contract);
# AD_TYPES and TEMPLATES are the same dicts as before, built from it.
AD_TYPES = presets.legacy_types()
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


# AI presenters: described, never real people. They live in presenters.py now (m15: the roster, with situations and
# one voice each). PRESENTERS is the eight Campaignify has always had, the same dicts as before: Create, the old Home
# and the planner list these. PRESENTER_BY_ID knows the whole roster, so a job, a saved batch or an MCP call naming
# any presenter resolves. Without an approved still (PRESENTER_STILLS) each video draws its presenter from the
# description, so two videos with the same presenter look alike but not identical.
PRESENTERS = presenters.classic_presenters()
PRESENTER_BY_ID = {p["id"]: presenters.classic(p) for p in presenters.all_presenters()}
# Each presenter's one voice (default_voice() below). Every presenter has one: tests check the map is complete.
PRESENTER_VOICE = {p["id"]: p["voice"] for p in presenters.all_presenters()}

VOICES = [
    {"id": "warm_f", "name": "Warm & friendly", "desc": "a warm, friendly female voice, natural American accent", "who": "female"},
    {"id": "upbeat_f", "name": "Upbeat", "desc": "an upbeat, energetic female voice, natural American accent", "who": "female"},
    {"id": "calm_m", "name": "Calm & assured", "desc": "a calm, assured male voice, natural American accent", "who": "male"},
    {"id": "chatty_m", "name": "Casual & chatty", "desc": "a casual, chatty male voice, natural American accent", "who": "male"},
    {"id": "narrator_deep", "name": "Narrator, deep", "desc": "a deep, smooth narrator voice", "who": "narrator"},
    {"id": "narrator_bright", "name": "Narrator, bright", "desc": "a bright, clear narrator voice", "who": "narrator"},
]
# VOICES is what a member can pick (a narrator, or another voice for a presenter). The newer presenters' own voices
# (presenters.VOICES) resolve by id as well, so a job that names one says the right descriptor.
VOICE_BY_ID = {v["id"]: v for v in VOICES + presenters.VOICES}
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
                   setting: str = "", placement: str = "", *, situation: str = "", direction: str = "",
                   still: bool = False, voice_ref: bool = False) -> str:
    """The prompt for a talking video (built by the worker from the job's params).

    The keyword arguments are m15's and all default to "as before":
      situation   one of presenters.SITUATIONS: where the presenter is, when the member typed no setting
      direction   a video preset's own shot (presets.RECIPES): what the camera sees in a voiceover, or what the
                  presenter does in a presenter video ("{product}" is filled in)
      still       the first reference image is the presenter's approved still (PRESENTER_STILLS): keep that person
      voice_ref   audio 1 is the presenter's voice sample (PRESENTER_VOICE_REF): speak in that voice"""
    product = product or "the product"
    voice = VOICE_BY_ID.get(voice_id) or VOICES[0]
    say = " ".join((script or "").split())
    direction = " ".join((direction or "").split()).replace("{product}", product)
    keep = (f"The {product} looks exactly as in the reference photos: same packaging, label, text and colors; never "
            "invent other branding.")
    if ad_type == "voiceover":
        shots = direction or (
            "Slow, simple camera moves over the product resting on a table in a real home: a push-in, a gentle "
            "pan, a close-up of the label. No people and no hands in frame."
            if placement == "product_only" else
            "Clean, well-lit close-ups and hands using it in a real home; smooth, simple camera moves.")
        return (f"Short product video of the {product}. {shots} "
                f"{keep} A narrator, never seen on camera, says in {voice['desc']}: "
                f"\"{say}\" The narration is clear and matches the pacing of the shots. No on-screen text, no subtitles, "
                "no music.")
    pr = PRESENTER_BY_ID.get(presenter_id) or PRESENTERS[0]
    sit = presenters.situation(pr["id"], situation) if situation else None
    where = setting or (sit["place"] if sit else pr["setting"])
    if direction:
        does = direction.rstrip(".")
    elif placement == "holding":
        does = f"holds the {product} up to the camera and talks"
    elif sit:
        does = (f"is {sit['pose']}, with the {product} standing on {sit['surface']} in clear view, its label facing "
                "the camera, and talks. Their hands stay off the product")
    else:
        does = (f"sits with the {product} standing on the table beside them, in clear view, label facing the camera, "
                "and talks. Their hands stay off the product")
    extra = ""
    if still:
        extra += ("The presenter is the person in the first reference image: keep their face, hair, age, build and "
                  "clothes exactly as in that image. ")
        keep = (f"The {product} looks exactly as in its own reference photos (every reference image after the first): "
                "same packaging, label, text and colors; never invent other branding.")
    if voice_ref:
        extra += ("Their voice is the voice heard in audio 1: the same timbre, pitch, pace and accent. Audio 1 is only "
                  "a sample of the voice: never repeat its words and never play it. ")
    return (f"Vertical selfie-style UGC video filmed on a phone. A {pr['look']}, in {where}, {does} directly to the "
            f"viewer in a natural, unscripted tone with {voice['desc']}. They say: "
            f"\"{say}\" Lip movements match the speech exactly. {extra}{keep} {UNPOLISHED} Natural light, slight handheld "
            "movement, no on-screen text, no subtitles, no music.")


def voice_sample_prompt(presenter_id: str, voice_id: str, situation: str, line: str) -> str:
    """The prompt for a presenter's voice sample (library.py): them, from their still, saying one neutral line. No
    product: the clip is kept for its sound."""
    pr = PRESENTER_BY_ID.get(presenter_id) or PRESENTERS[0]
    voice = VOICE_BY_ID.get(voice_id) or VOICE_BY_ID[default_voice(pr["id"])]
    sit = presenters.situation(pr["id"], situation) if situation else None
    say = " ".join((line or "").split())
    return (f"Vertical selfie-style video filmed on a phone. A {pr['look']}, in {sit['place'] if sit else pr['setting']}, "
            f"talks directly to the viewer in a natural, unhurried tone with {voice['desc']}. They say: \"{say}\" "
            "Lip movements match the speech exactly. The presenter is the person in the reference image: keep their "
            f"face, hair, age, build and clothes exactly as in that image. {UNPOLISHED} Natural light, slight handheld "
            "movement, a quiet room with no background noise, no on-screen text, no subtitles, no music.")


# ---------------------------------------------------------------- keyframe-first video (m15, keyframe_video.py)
# One still is made first (the presenter in their place with the real product where the recipe puts it, or the
# product in its scene for a voiceover), checked, and every beat of the video is then generated from that still as
# its image reference. The prompts below are the three things sent to the vendor for one such video.
KEYFRAME_SETTINGS = ("high", "1k")           # the library still model's quality and size for a keyframe
KEYFRAME_MAX_PHOTOS = 2                      # product photos given beside the presenter's still
PRODUCT_AT = {
    "table": "It stands upright on {surface}, in clear view, its label facing the camera, sharp and fully readable. "
             "Their hands stay off it.",
    "hand": "They hold it up beside their shoulder in one hand, a simple grip around its body with every finger "
            "visible and none over the label, the label facing the camera, sharp and fully readable.",
    "shelf": "It stands on a shelf just behind their shoulder at eye level, in clear view, its label facing the "
             "camera, sharp and fully readable. Their hands stay off it.",
}
KEYFRAME_LOOK = ("Real-phone look: natural skin texture with visible pores, slight softness rather than razor-sharp "
                 "focus, ordinary mixed light from the room, no studio lighting, no beauty filter. The only readable "
                 "text anywhere is what is printed on the product itself. No captions, no watermark, no border.")


def _product_line(product: str, refs: str) -> str:
    return (f"The product is the {product} from {refs}: the same shape, packaging, label, printed text and colors, "
            "unchanged. Never invent other branding and never redraw its label.")


def keyframe_prompt(ad_type: str, product: str, *, presenter_id: str = "", situation: str = "",
                    product_at: str = "table", has_still: bool = False, shot: str = "", hands: bool = False,
                    setting: str = "") -> str:
    """The prompt for a video's keyframe (the library still model, with image references).

    A presenter video: the person in their place with the product where the recipe puts it (PRODUCT_AT). With
    `has_still` the first reference image is their approved still of that situation and the person, the room and
    the clothes are kept from it; without one the person is drawn from the roster's description. The product's
    photos are the other reference images. A voiceover: the product in the scene of its first shot (`shot`), with
    two hands when the recipe is shown by hands (`hands`), never a face."""
    product = product or "the product"
    if ad_type != "ugc":
        who = ("Two hands rest on the table beside it, relaxed, every finger visible. Only hands and forearms are in "
               "frame, never a face." if hands else "No people and no hands in frame.")
        scene = " ".join((shot or "the product resting on a table in a real home, front on").split()).rstrip(".")
        return "\n".join([
            "Photo taken with an ordinary phone, the first frame of a short product video.",
            f"The shot: {scene}. {who}",
            _product_line(product, "the reference photos") + " Its label faces the camera, sharp and fully readable.",
            "An ordinary lived-in home in natural light, slight softness rather than razor-sharp focus, no studio "
            "lighting. The only readable text anywhere is what is printed on the product itself. No captions, no "
            "watermark, no border."])
    pr = presenters.record(presenter_id) or presenters.record(PRESENTERS[0]["id"])
    sit = presenters.situation(pr["id"], situation) or presenters.situation(pr["id"], presenters.default_situation(pr["id"]))
    surface = sit["surface"] if sit else "the table beside them"
    place = PRODUCT_AT.get(product_at, PRODUCT_AT["table"]).format(surface=surface)
    if has_still:
        lines = ["Image 1 shows a person in a room. Make a new photo of exactly the same person in the same room: the "
                 "same face, bone structure, skin, hair, age, build and clothes, the same place, the same light and "
                 "the same phone framing.",
                 _product_line(product, "the other reference images"), place]
    else:
        wear = f" Wearing {sit['wardrobe']}." if sit and sit["wardrobe"] else ""
        where = setting or (sit["scene"] if sit else pr["setting"])
        lines = ["Photo taken with the front camera of an ordinary phone, a little above eye level, as the first frame "
                 "of a selfie video.",
                 f"{pr['appearance']}{wear}",
                 f"The place is {where}." + (f" They are {sit['pose']}." if sit and not setting else ""),
                 _product_line(product, "the reference photos"), place]
    lines += ["They look into the lens with a relaxed, friendly expression, the mouth closed, as if about to start "
              "talking. Framed from the chest up: the whole face is clear and nothing covers it, the product least "
              "of all.",
              KEYFRAME_LOOK + " One person only.", presenters.FICTIONAL]
    return "\n".join(lines)


def beat_prompt(ad_type: str, product: str, line: str, *, presenter_id: str = "", voice_id: str = "",
                shot: str = "", hands: bool = False, product_at: str = "table", new_place: str = "",
                voice_ref: bool = False, note: str = "") -> str:
    """The prompt for ONE beat of a keyframe-first video (reference-to-video, native speech). Image 1 is the
    approved keyframe; the product's own photos follow it.

      shot       what the camera sees in this beat (the recipe's words)
      new_place  a presenters.SITUATIONS id other than the keyframe's: the same person, somewhere new
      voice_ref  audio 1 is the presenter's voice sample (PRESENTER_VOICE_REF)
      note       a free fix's correction for this beat (an instruction about the shot, never words to say or show)"""
    product = product or "the product"
    voice = VOICE_BY_ID.get(voice_id) or VOICES[0]
    say = " ".join((line or "").split()).replace('"', "'")
    keep = (f"The {product} looks exactly as in the reference images: same packaging, label, text and colors; never "
            "invent other branding.")
    fix = ""
    if note:
        asked = " ".join(str(note).split())[:300].replace('"', "'").rstrip()
        fix = (" Correction to the last version, asked for by the advertiser: " + asked
               + ("" if asked[-1:] in ".!?" else ".") + " This is an instruction about the shot, not words to say or "
               "show.")
    shot = " ".join((shot or "").split()).rstrip(".")
    if ad_type != "ugc":
        who = "Only hands and forearms are ever in frame, never a face." if hands else "No people and no hands in frame."
        return (f"Short product video of the {product}, one shot of a longer video. Image 1 is its first frame: the "
                f"same scene, the same light and the same product where it is. {(shot[:1].upper() + shot[1:] + '. ') if shot else ''}"
                f"{who} {keep} A narrator, never seen on camera, says in {voice['desc']}: \"{say}\" The narration is "
                "clear, at an even pace, and ends when the line ends. Slow, simple camera moves, ordinary home light, no "
                f"studio lighting. No on-screen text, no subtitles, no music.{fix}")
    pr = PRESENTER_BY_ID.get(presenter_id) or PRESENTERS[0]
    sit = presenters.situation(pr["id"], new_place) if new_place else None
    if sit:
        scene = (f"Image 1 shows the presenter and the product. This shot is the same person (their face, hair, age "
                 f"and build exactly as in that image) and the same product, now in {sit['place']}: they are "
                 f"{sit['pose']}, the {product} standing on {sit['surface']}, its label facing the camera.")
    else:
        scene = ("Image 1 is its first frame: the same person (their face, hair, age, build and clothes exactly as in "
                 "that image), the same room and the same product where it is.")
    hold = "They keep holding it as in image 1, its label never covered." if product_at == "hand" else \
        "Their hands stay off the product."
    extra = ""
    if voice_ref:
        extra = ("Their voice is the voice heard in audio 1: the same timbre, pitch, pace and accent. Audio 1 is only a "
                 "sample of the voice: never repeat its words and never play it. ")
    return (f"Vertical selfie-style UGC video filmed on a phone, one shot of a longer video. {scene} They talk directly "
            f"to the viewer in a natural, unscripted tone with {voice['desc']}. They say: \"{say}\" Lip movements match "
            f"the speech exactly, and they stop talking when the line ends. {extra}{keep} {hold} {UNPOLISHED} Natural "
            "light, slight handheld movement, the framing stays the same from start to end, no on-screen text, no "
            f"subtitles, no music.{fix}")


def broll_prompt(product: str, shot: str) -> str:
    """The prompt for a silent cutaway made from the member's real product photo (image-to-video): the recipe's
    shot, a slow steady move, and the keep-the-product line."""
    product = product or "the product"
    shot = " ".join((shot or f"close shots of the {product} and its label").split()).rstrip(".")
    return with_fidelity(f"{shot[:1].upper() + shot[1:]}: the {product} on a clean surface in soft daylight, a slow, "
                         "steady camera move. No faces, no on-screen text.")


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


# A static with a text layer gets its words from textlayer.py afterwards, so its picture has to come back with none.
# The image model is an ad model and writes copy of its own when it can: it has painted invented claims into such a
# picture ("30 servings...", "Everyday carry, redefined"), which is a compliance problem and not only a looks one.
# So the text-free prompt says it plainly more than once, and the picture is checked before any layer is drawn
# (image_qa: "no extra text" always runs on these, whatever IMAGE_QA says; worker._finish_images).
TEXT_FREE = ("No words, letters, numbers, captions, logos or graphics anywhere in the picture except what is already "
             "printed on the product's own packaging in the photos. No infographic elements: no icons, arrows, callout "
             "lines, charts, panels, banners, frames, ribbons or stickers. It is a plain photograph, not a finished ad.")


def image_ad_prompt(scene: str, headline: str = "", subline: str = "", cta: str = "", brand: str = "",
                    product: str = "", forbid=(), service: bool = False, text_free: bool = False) -> str:
    """The prompt the image model gets for one image ad. It names every word
    allowed on the image (headline, optional subline and button, brand name)
    and forbids all others; product claims in `forbid` (the product's approved
    and suggested claims) never enter it unless the member put that exact text
    in the headline, subline or button. `text_free`: a static with a text layer
    (presets.py), whose picture carries no words at all (TEXT_FREE above)."""
    clean = lambda s, k: " ".join(str(s or "").split())[:TEXT_LIMITS[k]].replace('"', "'")  # noqa: E731
    headline, subline, cta = clean(headline, "headline"), clean(subline, "subline"), clean(cta, "cta")
    brand = " ".join(str(brand or "").split())[:80].replace('"', "'")
    if text_free:
        headline = subline = cta = brand = ""
    product = " ".join(str(product or "").split())[:80] or "product"
    allowed = [x for x in (headline, subline, cta, brand) if x]
    scene = scene_only(scene, allowed, forbid) or f"Clean, premium product shot of the {product}."
    opener = "Product photograph with no text on it. " if text_free else "Advertising image. "
    if service:   # a service business (service_path.py): the references are its logo or site photos, not packaging
        import service_path
        lines = [f"{opener}Scene: {scene}"] + service_path.image_lines(product)
    else:
        lines = [f"{opener}Scene: {scene}",
                 f"The product is the {product} exactly as in the reference photos: same shape, packaging, label, printed "
                 "text and colors. Don't redraw, change or add to its label."]
    lines.append("Text on the image: none at all." if text_free else
                 "Text on the image: only the words listed here, each shown once and spelled exactly as written.")
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
    if text_free:
        lines.append(TEXT_FREE.replace("printed on the product's own packaging in the photos", "in the logo") if service
                     else TEXT_FREE)
    lines.append("The only other text allowed is what is already in the logo." if service else
                 "The only other text allowed is what is already printed on the product's own packaging in the photos.")
    lines.append(NO_EXTRA_TEXT + (" Exactly one button, with the text above, and no other call to action." if cta else
                                  " No buttons and no call to action."))
    return "\n".join(lines)


# Templates: the angles from the playbook. {product} and {brand} are filled from
# the workspace's brand kit; members can edit the brief before generating.
# "fits": "any" works for any product you can photograph; "coffee" templates
# were written for a coffee product and assume one.
TEMPLATES = presets.legacy_templates()      # the 14 older templates, in their order (Home, the first run, the planner)

# Lookups by id also know the newer presets (scene statics for every category and the statics with a text layer),
# so "?template=<id>", a job's template name and its thumbnail work for all of them.
TEMPLATE_BY_ID = {t["id"]: t for t in presets.template_dicts()}


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
    if t.get("recipe"):
        # A video preset (presets.RECIPES): the member writes the words; the preset owns the shot, the place and
        # the length. Its brief is a direction for the camera, never a script.
        out.update(type=t.get("ad_type") or typ, items=[], body="", preset=t["id"], duration=t.get("duration") or 8,
                   aspects=[t.get("aspect") or "9:16"])
        if t.get("situation"):
            out["situation"] = t["situation"]
        return out
    if t.get("layout"):
        # A static with a text layer (presets.py): the member writes the words; the brief is only the scene.
        # The marketplace image has no words, so its one line is just a label.
        out["items"] = [t["name"]] if t["layout"] == "none" else []
        out["body"], out["aspects"], out["layout"] = brief, [t.get("aspect") or "4:5"], t["layout"]
    elif t["tool"] == "ad_image":
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
