"""The outputs and prices of the 5 ad types and 14 templates, as one JSON object (quality wave 2).

tests/fixtures/presets_snapshot.json is this script's output on main BEFORE the preset library existed;
tests/test_quality_wave2.py recomputes it and fails on any difference, so moving the ad types and templates onto
presets.py can't change what a member gets or pays. Only rewrite the fixture when a price or a prompt is meant
to change:

    python tools/presets_snapshot.py > tests/fixtures/presets_snapshot.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("RS_MARKUP", "RS_HD_MARKUP", "RS_VIDEO_USD_PER_SECOND", "RS_TALKING_USD_PER_SECOND", "RS_TALKING_DURATIONS",
           "RS_VIDEO_USD_PER_SECOND_1080P", "RS_VIDEO_UPSCALE_MODEL", "RS_TALKING", "RS_STARTER_CREDITS"):
    os.environ.pop(_k, None)

BRAND = {"name": "Northwind", "product": "Trail Mug", "claims": "Keeps drinks hot for 6 hours", "audience": "Hikers"}
LEGACY_TEMPLATES = ["hero", "routine", "flatlay", "inhand", "gift", "turn", "swap", "label", "how", "two-pm", "iced",
                    "cost", "gym", "copy-trend"]


def snapshot() -> dict:
    import catalog
    tpls = [catalog.TEMPLATE_BY_ID[i] for i in LEGACY_TEMPLATES]
    out = {"ad_types": catalog.AD_TYPES, "tool_ad_type": catalog.TOOL_AD_TYPE,
           "templates": [t for t in catalog.TEMPLATES if t["id"] in LEGACY_TEMPLATES],
           "template_order": [t["id"] for t in catalog.TEMPLATES][:len(LEGACY_TEMPLATES)],
           "models": {k: t["model"] for k, t in catalog.TOOLS.items()},
           "from_prices": catalog.from_prices(), "rates": catalog.rates(),
           "prices": {"ad_image": {str(n): catalog.price_credits("ad_image", batch=n) for n in (1, 2, 3, 4)},
                      "photo_to_video": {f"{d}:{r}": catalog.price_credits("photo_to_video", duration=d, resolution=r)
                                         for d in (5, 10) for r in ("720p", "1080p")},
                      "ugc_video": {f"{d}:{r}": catalog.price_credits("ugc_video", duration=d, resolution=r)
                                    for d in catalog.TOOLS["ugc_video"]["durations"] for r in ("720p", "1080p")},
                      "motion_copy": {str(s): catalog.price_credits("motion_copy", ref_seconds=s) for s in (1, 5, 15)}},
           "prefill": {t["id"]: catalog.template_prefill(t, BRAND) for t in tpls},
           "generic_ids": [t["id"] for t in catalog.templates_for({"name": "Trail Mug"})
                           if t["id"] in LEGACY_TEMPLATES],
           "coffee_ids": [t["id"] for t in catalog.templates_for({"name": "Instant coffee"})
                          if t["id"] in LEGACY_TEMPLATES],
           "first_run_pair": {k: [t["id"] for t in catalog.first_run_pair(k)] for k in
                              ("Trail Mug", "Instant coffee", "Face serum", "Scented candle", "Green tea", "Widget")},
           "prompts": {}}
    for t in tpls:
        brief = catalog.fill(t["brief"], BRAND)
        if t["tool"] == "ad_image":
            parts = catalog.split_image_brief(brief)
            out["prompts"][t["id"]] = catalog.image_ad_prompt(parts["scene"], parts["headline"], parts["subline"],
                                                              parts["cta"], brand="Northwind", product="Trail Mug",
                                                              forbid=["Keeps drinks hot for 6 hours"])
        elif t["tool"] == "photo_to_video":
            out["prompts"][t["id"]] = catalog.with_fidelity(brief)
        else:
            out["prompts"][t["id"]] = brief
    out["prompts"]["ugc"] = catalog.talking_prompt("ugc", "Trail Mug", "Meet the mug. Try it today.", "maya", "warm_f")
    out["prompts"]["ugc_holding"] = catalog.talking_prompt("ugc", "Trail Mug", "Meet the mug.", "theo", "", "", "holding")
    out["prompts"]["voiceover"] = catalog.talking_prompt("voiceover", "Trail Mug", "Meet the mug.", "", "narrator_deep")
    return out


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=1, sort_keys=True))
