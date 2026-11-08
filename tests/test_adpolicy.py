"""The campaign spec, its validator and the policy linter (adspec.py, adpolicy.py; roadmap M2.2).
No network, no app server: a throwaway database only for the asset provenance checks.

    python tests/test_adpolicy.py      (tests/test_app.py runs it too)

  * tests/fixtures/policy/*.json: 60+ ad copy snippets, each with the exact policy rules it must hit
    (and, where given, the house rules from compliance.py and the level)
  * compliance.check() is unchanged; compliance.findings() names the rule behind each message
  * CampaignSpec: shape check against the JSON schema, round trip, a stable hash
  * the validator: budgets, dates, location, ages, pixel for sales and leads, text limits per
    platform, landing page, aspect ratios, video-only platforms, Google responsive search ads
  * spec rules: AI disclosure, an AI actor's testimonial blocks the launch, age limits for health,
    finance, crypto and alcohol, Meta special ad categories, supplements
  * provenance from the library: presenter and voice videos turn the AI disclosure on; another
    workspace's asset tells nothing
"""
import atexit
import copy
import glob
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rs_adpolicy_")
atexit.register(shutil.rmtree, TMP, True)
os.environ.update(RS_DATA=TMP)
os.environ.pop("RS_DB", None)

import adpolicy  # noqa: E402
import adspec  # noqa: E402
import compliance  # noqa: E402
import db  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


def ids(issues, prefix=""):
    return sorted({i.rule_id for i in issues if i.rule_id.startswith(prefix)})


# ================================================================ fixtures
print("policy fixtures")
cases = []
for f in sorted(glob.glob(os.path.join(HERE, "fixtures", "policy", "*.json"))):
    with open(f) as fh:
        cases += [dict(c, file=os.path.basename(f)) for c in json.load(fh)["cases"]]
check(len(cases) >= 60 and len({c["id"] for c in cases}) == len(cases), f"{len(cases)} fixture snippets, unique ids")
for c in cases:
    iss = adpolicy.lint_text(c["text"], platform=c["platform"], vertical=c["vertical"], ai=c["ai"],
                             claims_substantiated=c.get("claims_substantiated", False))
    got = ids(iss, "policy.")
    ok = got == c["expect"]
    if "house" in c:
        ok = ok and sorted(i.rule_id[6:] for i in iss if i.rule_id.startswith("house.")) == c["house"]
    lv = {i.rule_id: i.level for i in iss}
    ok = ok and all(lv.get(k) == v for k, v in (c.get("levels") or {}).items())
    check(ok, f"{c['id']} {c['platform']}/{c['vertical']}{' AI' if c['ai'] else ''}: {c['text'][:48]!r} -> "
              f"{', '.join(got) or 'clean'}" + ("" if ok else f" (want {c['expect']}, got {got}, levels {lv})"))
hit = {r for c in cases for r in c["expect"]}
check({"policy.personal_attributes", "policy.cure_claim", "policy.health_outcome_numeric", "policy.before_after",
       "policy.implied_transformation", "policy.guaranteed_returns", "policy.ai_testimonial"} <= hit,
      "the fixtures cover the rules the design names (personal attributes, cures, numeric outcomes, before/after, "
      "transformations, guaranteed returns, AI testimonials)")
check(sum(1 for c in cases if not c["expect"]) >= 15, "and at least 15 clean snippets that must not trip anything")

# ================================================================ compliance.py stays the same
print("compliance")
samples = ["Lose 10 pounds fast", "No before/after shots", "Join my team", "Gentle cleanser", "I lost 5 lbs",
           "Clinically proven. 20% off.", "earn $500 a week", "cures cancer"]
for s in samples:
    old = compliance.check(s)
    f = compliance.findings(s)
    same = old["blocks"] == list(dict.fromkeys(m for lvl, _, m in f if lvl == "block")) and \
        old["warnings"] == list(dict.fromkeys(m for lvl, _, m in f if lvl == "warn"))
    check(same, f"compliance.findings matches check() for {s!r}")
check(len(compliance.IDS) == len(compliance.RULES) == len(set(compliance.IDS)), "every house rule has one unique id")
# M-4: generation's own guard (compliance.check) matches the normalized text too
for probe in ("This tea cu\u200bres diab\u200betes", "L\u200bose 10 lb\u200bs in 2 weeks", "This tea cures diab\u0435tes",
              "\uff2c\uff4f\uff53\uff45 10 pounds", "\u2067Lose 10 pounds\u2069", "Before \u2192 After",
              "Melts belly fat while you sleep", "c\u0443res \u0430nxiety"):
    check(not compliance.check(probe)["ok"], f"compliance.check blocks the evasion {probe!r}")
check(compliance.normalize("caf\u00e9 \uff21\u200b\u0412") == "cafe AB" and compliance.hidden_chars("a\u200bb\u00adc") == ["\u200b", "\u00ad"]
      and compliance.hidden_chars("\U0001F468\u200d\U0001F469") == [],
      "normalize: NFKC, no format characters, no accents, look-alikes as Latin; an emoji's joiner isn't hidden text")

# ================================================================ the spec
print("spec")
LATER = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
BASE = {
    "objective": "sales", "platform": "meta", "name": "Ember candle fall", "daily_budget_minor": 2000, "currency": "USD",
    "start_at": None, "end_at": LATER, "geo": {"countries": ["US"]}, "audience": {"age_min": 25, "age_max": 54},
    "placements": "auto", "conversion": {"pixel_id": "700000001", "event": "Purchase",
                                        "landing_url": "https://glowco.test/ember"},
    "creatives": [{"kind": "video", "asset_id": None, "name": "Ember 1", "primary_text": "Soy wax, 40 hours, cedar and smoke.",
                   "headline": "The Ember candle", "description": "Ships in 2 days", "script": "", "on_screen_text": "",
                   "cta": "SHOP_NOW", "destination_url": "https://glowco.test/ember", "utm": "utm_source=meta",
                   "aspect": "9:16", "ai_presenter": False, "synthetic_voice": False}],
    "special_ad_categories": [], "ai_disclosure": False, "google": None, "vertical": "general",
    "claims_substantiated": False}


def spec(**over):
    d = copy.deepcopy(BASE)
    cre = over.pop("creative", None)
    d.update(over)
    if cre:
        d["creatives"] = [dict(d["creatives"][0], **cre)]
    s, issues = adspec.from_dict(d)
    assert s is not None, issues
    return s


s, issues = adspec.from_dict(BASE)
check(s is not None and not issues and isinstance(s.creatives[0], adspec.CreativeSpec), "a full spec loads")
check(adspec.to_dict(s) == BASE, "to_dict gives back the same JSON")
check(adspec.spec_hash(s) == adspec.spec_hash(adspec.from_dict(copy.deepcopy(BASE))[0]) and
      adspec.spec_hash(s) != adspec.spec_hash(spec(daily_budget_minor=2100)), "spec_hash is stable and changes with the spec")
check(not adspec.validate(s) and not adpolicy.lint(s), "the base spec has no issues at all")
for bad, rule, what in (
        (dict(BASE, objective="likes"), "spec.shape", "an unknown objective"),
        (dict(BASE, currency="usd"), "spec.shape", "a lower-case currency"),
        (dict(BASE, daily_budget_minor="20"), "spec.shape", "a budget as text"),
        (dict(BASE, daily_budget_minor=True), "spec.shape", "a budget as true/false"),
        (dict(BASE, placements=["feed", "billboard"]), "spec.shape", "an unknown placement"),
        (dict(BASE, bid_cap=5), "spec.unknown_field", "a field that isn't in the spec"),
        ({k: v for k, v in BASE.items() if k != "platform"}, "spec.missing", "a missing platform"),
        (dict(BASE, creatives=[dict(BASE["creatives"][0], aspect="2:3")]), "spec.shape", "an unknown aspect ratio")):
    s2, iss = adspec.from_dict(bad)
    check(s2 is None and rule in ids(iss) and all(i.level == "block" for i in iss), f"shape check refuses {what}")
check(adspec.SCHEMA["properties"]["objective"]["enum"] == list(adspec.OBJECTIVES) and
      json.loads(json.dumps(adspec.SCHEMA)) == adspec.SCHEMA, "the JSON schema is plain JSON and matches the enums")

# ================================================================ the validator
print("validator")
past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
for kw, rule, lvl, what in (
        (dict(daily_budget_minor=50), "meta.budget_min", "block", "a budget under Meta's floor"),
        (dict(platform="tiktok", daily_budget_minor=1500, creative={"primary_text": "Cedar and smoke."}),
         "tiktok.budget_min", "block", "a budget under TikTok's 20.00 floor"),
        (dict(end_at=past), "spec.dates", "block", "an end date in the past"),
        (dict(start_at=LATER, end_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat()), "spec.dates", "block",
         "an end before the start"),
        (dict(start_at="next tuesday"), "spec.dates", "block", "a date that isn't a date"),
        (dict(geo={}), "spec.geo_missing", "block", "no location"),
        (dict(geo={"countries": ["USA"]}), "spec.geo_country", "block", "a 3-letter country"),
        (dict(audience={"age_min": 40, "age_max": 30}), "spec.age_range", "block", "ages the wrong way round"),
        (dict(audience={"age_min": 10}), "spec.age_range", "block", "an age under 13"),
        (dict(conversion=None), "meta.conversion_pixel", "block", "a sales campaign with no pixel"),
        (dict(conversion={"pixel_id": "1", "event": "Bought"}), "spec.conversion_event", "block", "an unknown event"),
        (dict(creative={"headline": "x" * 41}), "meta.text.headline_long", "warn", "a 41-character Meta headline (cut off)"),
        (dict(creative={"headline": "x" * 256}), "meta.text.headline_too_long", "block", "a 256-character Meta headline"),
        (dict(creative={"primary_text": "x" * 126}), "meta.text.primary_text_long", "warn", "126 characters of Meta primary text"),
        (dict(platform="tiktok", creative={"primary_text": "x" * 101}), "tiktok.text.primary_text_too_long", "block",
         "101 characters of TikTok ad text"),
        (dict(platform="tiktok", creative={"primary_text": ""}), "tiktok.text.primary_text_missing", "block", "no TikTok ad text"),
        (dict(platform="snapchat", creative={"headline": "x" * 35}), "snapchat.text.headline_too_long", "block",
         "a 35-character Snapchat headline"),
        (dict(creative={"destination_url": "http://glowco.test"}), "spec.destination_url", "block", "an http:// landing page"),
        (dict(creative={"destination_url": ""}), "spec.destination_url", "block", "no landing page"),
        (dict(platform="snapchat", creative={"aspect": "4:5"}), "snapchat.aspect", "block", "a 4:5 ad on Snapchat"),
        (dict(platform="tiktok", creative={"aspect": "1:1", "primary_text": "Cedar."}), "tiktok.aspect_not_vertical", "warn",
         "a square TikTok ad"),
        (dict(platform="tiktok", creative={"kind": "image", "primary_text": "Cedar."}), "tiktok.video_only", "block",
         "an image on TikTok"),
        (dict(creative={"aspect": "4:5"}), "meta.aspect_auto_no_vertical", "warn", "automatic placements with no 9:16 ad"),
        (dict(placements=["stories"], creative={"aspect": "4:5"}), "meta.aspect_stories", "warn", "a 4:5 ad in Stories"),
        (dict(creatives=[]), "spec.no_creatives", "block", "no ads")):
    iss = adspec.validate(spec(**kw))
    match = [i for i in iss if i.rule_id == rule]
    check(match and match[0].level == lvl and match[0].message and match[0].where is not None,
          f"validator: {what} -> {rule} ({lvl})")
check(not adspec.validate(spec(objective="traffic", conversion=None)), "a traffic campaign needs no pixel")
g = dict(type="search", keywords=["soy candles"], headlines=["Soy candles", "Cedar and smoke", "40 hour burn"],
         descriptions=["Hand poured in Austin.", "Ships in 2 days."])
check(not adspec.validate(spec(platform="google", objective="traffic", conversion=None, google=g, creatives=[])),
      "a Google search campaign with 3 headlines and 2 descriptions passes")
for gk, rule, what in ((dict(g, headlines=g["headlines"][:2]), "google.rsa_headlines", "2 headlines"),
                       (dict(g, descriptions=g["descriptions"] * 3), "google.rsa_descriptions", "6 descriptions"),
                       (dict(g, headlines=g["headlines"] + ["x" * 31]), "google.rsa_headline_long", "a 31-character headline"),
                       (dict(g, descriptions=["x" * 91, "ok"]), "google.rsa_description_long", "a 91-character description"),
                       (dict(g, keywords=[]), "google.keywords", "no keywords")):
    iss = adspec.validate(spec(platform="google", objective="traffic", conversion=None, google=gk, creatives=[]))
    check(rule in ids(iss), f"Google search ads: {what} -> {rule}")

# ================================================================ spec-level policy
print("spec rules")
ai_script = "I bought this candle last month and now my whole house smells amazing."
s = spec(creative={"ai_presenter": True, "script": ai_script})
iss = adpolicy.preflight(s)
check("policy.ai_testimonial" in ids(iss) and "policy.ai_disclosure_missing" in ids(iss) and adpolicy.blocking(iss),
      "an AI presenter speaking as a customer blocks the launch, and so does a missing AI disclosure")
check([i.where for i in iss if i.rule_id == "policy.ai_testimonial"] == ["creatives[0].script"], "the finding points at the script")
s = spec(ai_disclosure=True, creative={"ai_presenter": True, "script": "Meet the Ember candle. Soy wax, cedar and smoke."})
check(not adpolicy.blocking(adpolicy.preflight(s)), "the same presenter describing the product, with the disclosure on, passes")
s = spec(creative={"synthetic_voice": True, "script": "Since I started using it, my mornings feel calm."})
check({"policy.ai_testimonial", "policy.ai_disclosure_missing"} <= set(ids(adpolicy.lint(s))),
      "an AI voiceover counts as AI too")
s = spec(creative={"script": ai_script})
check("policy.ai_testimonial" not in ids(adpolicy.lint(s)), "the same words from a real person (no AI) are not an AI testimonial")
for kw, rule, what in (
        (dict(vertical="supplements", audience={"age_min": 16, "age_max": 40}), "policy.age_restricted", "supplements to 16-year-olds"),
        (dict(vertical="crypto", audience={"age_min": 21, "age_max": 40}), "policy.age_restricted", "crypto under 25"),
        (dict(vertical="alcohol", audience={"age_min": 18, "age_max": 40}), "policy.age_restricted", "alcohol to 18 in the US"),
        (dict(vertical="housing"), "policy.special_category_missing", "a housing ad on Meta without the category"),
        (dict(vertical="credit", special_ad_categories=["credit"], audience={"age_min": 25, "age_max": 65}),
         "policy.special_category_targeting", "a credit ad that narrows the age"),
        (dict(special_ad_categories=["employment"], geo={"countries": ["US"], "zips": ["75201"]}, audience={"age_min": 18}),
         "policy.special_category_targeting", "a job ad that targets ZIP codes")):
    iss = adpolicy.lint(spec(**kw))
    check(rule in ids(iss) and adpolicy.blocking(iss), f"blocks {what} ({rule})")
check(not adpolicy.lint(spec(vertical="alcohol", geo={"countries": ["GB"]}, audience={"age_min": 18, "age_max": 65})),
      "alcohol to 18+ outside the US is fine")
check(not adpolicy.lint(spec(vertical="housing", special_ad_categories=["housing"], audience={"age_min": 18, "age_max": 65})),
      "a declared housing ad with open targeting passes")
iss = adpolicy.lint(spec(vertical="supplements", platform="snapchat", creative={"aspect": "9:16"}))
check({"policy.supplement_disclaimer", "policy.snapchat_supplements"} <= set(ids(iss)) and not adpolicy.blocking(iss),
      "supplements: the FDA statement and Snapchat's review are warnings")
iss = adpolicy.lint(spec(vertical="supplements", creative={
    "description": "These statements have not been evaluated by the Food and Drug Administration."}))
check("policy.supplement_disclaimer" not in ids(iss), "with the FDA statement in the copy, no warning")
iss = adpolicy.lint(spec(platform="google", objective="traffic", conversion=None, creatives=[],
                         google=dict(g, headlines=g["headlines"] + ["Cures insomnia fast"])))
check("policy.cure_claim" in ids(iss) and any(i.where == "google.headlines[3]" for i in iss),
      "Google headlines are linted too")
check(all(i.message and i.fix for i in adpolicy.lint(spec(creative={"primary_text": "Are you overweight? Lose 10 lbs in "
                                                                              "2 weeks. Clinically proven."}))),
      "every finding has a plain message and a fix")
check(not any("—" in m or "–" in m for m, f in adpolicy.MESSAGES.values()) and
      not any("—" in f or "–" in f for m, f in adpolicy.MESSAGES.values()), "no em or en dashes in the messages")

# ================================================================ provenance from the library
print("provenance")
db.init()
with db.connect() as conn:
    now = db.now()
    wa = conn.execute("INSERT INTO workspaces (name, created_at) VALUES ('A', ?)", (now,)).lastrowid
    wb = conn.execute("INSERT INTO workspaces (name, created_at) VALUES ('B', ?)", (now,)).lastrowid

    def output(ws, tool, ad_type):
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, params_json, status, created_at, updated_at) "
                           "VALUES (?,?,?,?,?,?,?)", (ws, 1, tool, json.dumps({"ad_type": ad_type}), "done", now, now)).lastrowid
        return conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?,?,?)", (jid, "video", "/x")).lastrowid
    o_ugc, o_vo, o_img, o_motion = (output(wa, "ugc_video", "ugc"), output(wa, "ugc_video", "voiceover"),
                                    output(wa, "ad_image", "image"), output(wa, "motion_copy", "motion"))
    o_other = output(wb, "ugc_video", "ugc")
    conn.commit()
    prov = adpolicy.provenance(conn, wa, [o_ugc, o_vo, o_img, o_motion, o_other])
    check(prov[o_ugc] == {"ai_presenter": True, "synthetic_voice": True, "ai_generated": True}, "a UGC video: presenter and voice")
    check(prov[o_vo]["synthetic_voice"] and not prov[o_vo]["ai_presenter"], "a voiceover: AI voice, no presenter")
    check(prov[o_motion]["ai_presenter"] and not prov[o_motion]["synthetic_voice"], "copy-a-motion: an AI presenter")
    check(prov[o_img] == {"ai_presenter": False, "synthetic_voice": False, "ai_generated": True}, "an image ad: AI-made, no person")
    check(o_other not in prov, "another workspace's asset tells nothing")
    s = spec(creative={"asset_id": o_ugc, "script": ai_script})
    check(s.ai_disclosure is False, "(the spec came in with the disclosure off)")
    iss = adpolicy.preflight(s, conn=conn, workspace_id=wa)
    check(s.creatives[0].ai_presenter and s.ai_disclosure, "a presenter video from the library turns the AI disclosure on")
    check("policy.ai_testimonial" in ids(iss) and "policy.ai_disclosure_missing" not in ids(iss) and adpolicy.blocking(iss),
          "...and its customer-style script still blocks the launch")
    s = spec(creative={"asset_id": o_other, "script": ai_script})
    check("policy.ai_testimonial" not in ids(adpolicy.preflight(s, conn=conn, workspace_id=wa)) and not s.ai_disclosure,
          "an asset id from another workspace is ignored (no provenance leak)")
    s = spec(creative={"asset_id": o_img, "kind": "image"})
    iss = adpolicy.preflight(s, conn=conn, workspace_id=wa)
    check("policy.ai_generated_disclosure" in ids(iss) and not adpolicy.blocking(iss),
          "an AI-made image without the disclosure gets a warning, not a block")

print(f"\nall {passed} checks passed")
