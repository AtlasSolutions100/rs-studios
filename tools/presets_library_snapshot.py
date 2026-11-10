"""Every preset that existed before the m15 library, as one JSON object: its record, the prompt the worker would
send for it and what it costs (m15).

tests/fixtures/presets_m15_snapshot.json is this script's output on m15-integration BEFORE the m15 library was
added (39 records: 5 ad types, 14 templates, 14 scene statics, 6 statics with a text layer, and the talking
prompt of each of the 8 presenters). tests/test_m15_library.py recomputes it and fails on any difference, so
adding presets, presenters and tags can't change what an existing preset sends to the vendor or what a member
pays for it. Only rewrite the fixture when a price or a prompt is meant to change:

    python tools/presets_library_snapshot.py > tests/fixtures/presets_m15_snapshot.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("RS_MARKUP", "RS_HD_MARKUP", "RS_VIDEO_USD_PER_SECOND", "RS_TALKING_USD_PER_SECOND", "RS_TALKING_DURATIONS",
           "RS_VIDEO_USD_PER_SECOND_1080P", "RS_VIDEO_UPSCALE_MODEL", "RS_TALKING", "RS_STARTER_CREDITS", "TEXT_LAYERS",
           "PRESENTER_STILLS", "PRESENTER_VOICE_REF"):
    os.environ.pop(_k, None)

BRAND = {"name": "Northwind", "product": "Trail Mug", "claims": "Keeps drinks hot for 6 hours", "audience": "Hikers"}
# The records as of m15-integration, before the m15 library (in presets.LIBRARY order).
BASE_IDS = ["ugc", "voiceover", "image", "product_video", "motion",
            "hero", "routine", "flatlay", "inhand", "gift", "turn", "swap", "label", "how", "two-pm", "iced", "cost",
            "gym", "copy-trend",
            "shelf", "closeup", "doorstep", "table", "counter", "vanity", "pastel", "outfit", "street", "nightstand",
            "cozy", "windowsill", "desk", "inuse",
            "clean-headline", "offer", "callouts", "review", "compare", "marketplace"]
BASE_PRESENTERS = ["maya", "jordan", "priya", "sam", "elena", "marcus", "lena", "theo"]
SCRIPT = "Meet the mug. It keeps drinks hot for 6 hours. Try it today."


def _prompt(p, catalog, presets) -> str:
    """What the worker sends for this preset with the BRAND above (worker.image_prompt / build_inputs)."""
    if p.kind == "type":
        if p.id in ("ugc", "voiceover"):
            return catalog.talking_prompt(p.id, "Trail Mug", SCRIPT, "maya" if p.id == "ugc" else "",
                                          "warm_f" if p.id == "ugc" else "narrator_deep")
        return p.recipe
    brief = catalog.fill(p.recipe, BRAND)
    if p.tool == "ad_image":
        parts = catalog.split_image_brief(brief)
        if p.layout:
            scene = (parts["scene"] + " " + presets.ZONES.get(p.layout, "")).strip()
            return catalog.image_ad_prompt(scene, product="Trail Mug", forbid=["Keeps drinks hot for 6 hours"])
        return catalog.image_ad_prompt(parts["scene"], parts["headline"], parts["subline"], parts["cta"],
                                       brand="Northwind", product="Trail Mug", forbid=["Keeps drinks hot for 6 hours"])
    if p.tool == "photo_to_video":
        return catalog.with_fidelity(brief)
    return brief


def snapshot() -> dict:
    import catalog
    import presets
    out = {"order": [p.id for p in presets.LIBRARY if p.id in BASE_IDS], "presets": {}, "presenters": {},
           "voices": [dict(v) for v in catalog.VOICES], "catalog_presenters": [dict(p) for p in catalog.PRESENTERS],
           "tools": {k: {"model": t["model"], "kind": t["kind"]} for k, t in catalog.TOOLS.items()}}
    for pid in BASE_IDS:
        p = presets.get(pid)
        out["presets"][pid] = {
            "kind": p.kind, "name": p.name, "what_for": p.what_for, "format": p.format, "tool": p.tool,
            "ad_type": p.ad_type, "aspects": list(p.aspects), "aspect": p.aspect, "inputs": list(p.inputs),
            "recipe": p.recipe, "model": p.model, "params": dict(p.params), "beats": [dict(b) for b in p.beats],
            "text_layers": list(p.text_layers), "layout": p.layout, "fields": [dict(f) for f in p.fields],
            "qa": list(p.qa), "locks": list(p.locks), "price": dict(p.price), "credits": p.price_credits(),
            "usd": round(p.cost_usd(), 6), "fits": p.fits if isinstance(p.fits, str) else list(p.fits),
            "preview": p.preview, "prompt": _prompt(p, catalog, presets),
            "template": p.as_template() if p.kind != "type" else None,
            "card": presets.card(p) if p.kind != "type" else None}
    for who in BASE_PRESENTERS:
        out["presenters"][who] = {
            "voice": catalog.default_voice(who),
            "table": catalog.talking_prompt("ugc", "Trail Mug", SCRIPT, who, catalog.default_voice(who)),
            "holding": catalog.talking_prompt("ugc", "Trail Mug", SCRIPT, who, catalog.default_voice(who), "", "holding"),
            "setting": catalog.talking_prompt("ugc", "Trail Mug", SCRIPT, who, "", "a quiet porch at dusk")}
    out["talking_prices"] = {f"{d}:{r}": catalog.price_credits("ugc_video", duration=d, resolution=r)
                             for d in catalog.TOOLS["ugc_video"]["durations"] for r in ("720p", "1080p")}
    return out


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=1, sort_keys=True))
