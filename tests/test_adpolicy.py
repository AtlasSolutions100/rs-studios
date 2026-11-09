"""The campaign spec, its validator and the policy linter (adspec.py, adpolicy.py; roadmap M2.2).
No network, no app server: a throwaway database only for the asset provenance checks.

    python tests/test_adpolicy.py      (tests/test_app.py runs it too)

  * tests/fixtures/policy/*.json: 200+ ad copy snippets, each with the exact policy rules it must hit
    (and, where given, the house rules from compliance.py and the level); probes.json holds the
    widened L-16 probes, clean.json 50+ ordinary lines that must stay clean everywhere
  * compliance.check() agrees with the linter where they overlap (cures, health outcomes, weight loss,
    guaranteed returns, before/after); compliance.findings() names the rule behind each message
  * the vertical is derived from the product and the copy (L-17); keywords, names, URLs and UTM
    strings are linted for drugs and politics (L-18)
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
probes = [c for c in cases if c["file"] == "probes.json"]
check(len(probes) >= 60 and all(c["levels"] for c in probes), f"{len(probes)} L-16 probes, each with the level it must hit")
clean = [c for c in cases if c["file"] == "clean.json"]
check(len(clean) >= 30 and all(not c["expect"] and c["house"] == [] for c in clean),
      f"{len(clean)} ordinary ad lines (coffee, skincare, local services, apparel) expected clean")
bad = [c["id"] for c in clean if not compliance.check(c["text"])["ok"] or compliance.check(c["text"])["warnings"]]
check(not bad, f"compliance.check passes every clean line with no warning {bad or ''}")
# compliance.check (what generation refuses) agrees with the linter where they overlap
OVERLAP = ("policy.cure_claim", "policy.health_outcome_numeric", "policy.guaranteed_returns", "policy.before_after")
miss, extra = [], []
for c in cases:
    iss = adpolicy.lint_text(c["text"], platform=c["platform"], vertical=c["vertical"], ai=c["ai"])
    pol = {i.rule_id for i in iss}
    chk = compliance.check(c["text"])
    if pol & set(OVERLAP) and chk["ok"]:
        miss.append(c["id"])
    house_ba = "No before-and-after content." in chk["blocks"]
    if house_ba != ("policy.before_after" in pol):
        extra.append(c["id"])
check(not miss, f"every cure, health-outcome, weight-loss or guaranteed-return line the linter flags, generation blocks too {miss or ''}")
check(not extra, f"the before/after rule fires in both or in neither {extra or ''}")
for t in ("Before and after school care, 7 am to 6 pm.", "Wipe the mat before and after each use.",
          "Stretch before, after and during your run."):
    check(not compliance.check(t)["blocks"] and not adpolicy.lint_text(t), f"a time, not pictures: {t!r}")
check(not compliance.check("Our before and after treatment photos")["ok"], "...but 'before and after treatment photos' blocks")
for t in ("Hair treatment for hair loss.", "A natural treatment for fatigue.", "Treats acne overnight.",
          "Prevents colds all winter."):
    check(not compliance.check(t)["ok"], f"an everyday word with a health word still blocks: {t!r}")

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
g = dict(type="search", final_url="https://embercandle.example/", keywords=["soy candles"], headlines=["Soy candles", "Cedar and smoke", "40 hour burn"],
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

# ---------------------------------------------------------------- the vertical, derived (L-17)
print("derived vertical")
for kw, want, rules, what in (
        (dict(creative={"primary_text": "Our collagen powder mixes into coffee."}), "supplements",
         {"policy.vertical_mismatch", "policy.supplement_disclaimer"}, "a supplement on 'general'"),
        (dict(audience={"age_min": 16, "age_max": 40}, creative={"primary_text": "A weight loss tea for busy mornings."}),
         "weight_loss", {"policy.vertical_mismatch", "policy.age_restricted"}, "weight loss to 16-year-olds on 'general'"),
        (dict(audience={"age_min": 21, "age_max": 40}, creative={"primary_text": "Buy bitcoin in 2 taps."}), "crypto",
         {"policy.vertical_mismatch", "policy.age_restricted"}, "crypto to 21-year-olds on 'general'"),
        (dict(audience={"age_min": 18, "age_max": 65}, creative={"primary_text": "Apartments for rent near downtown."}),
         "housing", {"policy.vertical_mismatch", "policy.special_category_missing"}, "a housing ad on 'general'"),
        (dict(creative={"destination_url": "https://shop.test/sema-glutide-program"}), "weight_loss",
         {"policy.vertical_mismatch", "policy.pharmacy"}, "a spaced-out drug name in the landing page")):
    s = spec(**kw)
    iss = adpolicy.lint(s)
    mm = [i for i in iss if i.rule_id == "policy.vertical_mismatch"]
    check(adpolicy.derive_vertical(s)[0] == want and rules <= set(ids(iss)) and mm and mm[0].level == "block"
          and adpolicy.blocking(iss), f"derives {want}: {what} -> {', '.join(sorted(rules))}")
check(adpolicy.derive_vertical(spec()) == ("general", "") and not adpolicy.lint(spec()), "the candle stays general")
s = spec(vertical="cosmetics", creative={"primary_text": "A weight loss tea."})
iss = adpolicy.lint(s)
check([i.level for i in iss if i.rule_id == "policy.vertical_mismatch"] == ["warn"],
      "a declared vertical that isn't general gets a warning when the copy says something else")
s = spec(vertical="supplements", creative={"primary_text": "Our collagen powder mixes into coffee. These statements have "
                                                          "not been evaluated by the Food and Drug Administration."})
check(not adpolicy.lint(s), "a declared supplement ad with the FDA statement has nothing to fix")
mm = [i for i in adpolicy.lint(spec(creative={"primary_text": "Buy bitcoin in 2 taps."})) if i.rule_id == "policy.vertical_mismatch"]
check(mm and "bitcoin" in mm[0].message and "Crypto" in mm[0].fix and "—" not in mm[0].message,
      "the mismatch says what it saw and what to pick")

# ---------------------------------------------------------------- keywords, names, URLs (L-18)
print("keywords, names and URLs")
gk = dict(g, keywords=["ozempic near me", "vote yes on prop 5", "soy candles"], negative_keywords=["ozempic", "free\u200bcandles"])
iss = adpolicy.lint(spec(platform="google", objective="traffic", conversion=None, creatives=[], google=gk))
w = {(i.rule_id, i.where): i.level for i in iss}
check(w.get(("policy.pharmacy", "google.keywords[0]")) == "block" and w.get(("policy.political", "google.keywords[1]")) == "warn"
      and not any(k[1] == "google.keywords[2]" for k in w), "keywords: a drug blocks, an election warns, 'soy candles' is clean")
check(not any(k[1] == "google.negative_keywords[0]" for k in w) and
      w.get(("policy.hidden_characters", "google.negative_keywords[1]")) == "block",
      "negative keywords only exclude searches: a drug name there is fine, hidden characters are not")
iss = adpolicy.lint(spec(platform="google", objective="traffic", conversion=None, creatives=[],
                         google=dict(g, keywords=[{"text": "0zempic", "match": "exact"}])))
check(("policy.pharmacy", "google.keywords[0]") in {(i.rule_id, i.where) for i in iss}, "a keyword given as {text, match} too")
for cre, rule, where, what in (
        ({"name": "Ozempic promo v2"}, "policy.pharmacy", "creatives[0].name", "the creative's name"),
        ({"destination_url": "https://shop.test/%6Fzempic-alternative"}, "policy.pharmacy", "creatives[0].destination_url",
         "a percent-encoded drug name in the landing page"),
        ({"utm": "utm_source=meta&utm_campaign=vote_yes_prop_5"}, "policy.political", "creatives[0].utm", "an election in the UTM"),
        ({"destination_url": "https://glowco.test/ember\u200b"}, "policy.hidden_characters", "creatives[0].destination_url",
         "an invisible character in the URL")):
    check(rule in {i.rule_id for i in adpolicy.lint(spec(creative=cre)) if i.where == where}, f"{what} -> {rule}")
check([i.where for i in adpolicy.lint(spec(name="Elections 2026 push")) if i.rule_id == "policy.political"] == ["name"],
      "the campaign name is linted too")
check(not adpolicy.lint(spec(creative={"name": "Number 1 candle, before and after", "utm": "utm_campaign=fall_2026"})),
      "names and URLs get the subject rules only (no claim rules on a name the viewer never sees)")

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
    check(prov[o_ugc] == {"ai_presenter": True, "synthetic_voice": True, "ai_generated": True, "product": {}}, "a UGC video: presenter and voice")
    check(prov[o_vo]["synthetic_voice"] and not prov[o_vo]["ai_presenter"], "a voiceover: AI voice, no presenter")
    check(prov[o_motion]["ai_presenter"] and not prov[o_motion]["synthetic_voice"], "copy-a-motion: an AI presenter")
    check(prov[o_img] == {"ai_presenter": False, "synthetic_voice": False, "ai_generated": True, "product": {}}, "an image ad: AI-made, no person")
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
    # the product behind an asset sets the vertical (L-17)
    def product(ws, cat, name, tags="", claims=""):
        return conn.execute("INSERT INTO products (workspace_id, brand_id, name, tags, claims, category, created_at, "
                            "updated_at) VALUES (?,?,?,?,?,?,?,?)", (ws, 1, name, tags, claims, cat, now, now)).lastrowid
    p_sup, p_beauty, p_coffee, p_other = (product(wa, "supplement", "Daily greens"), product(wa, "beauty", "Glow serum"),
                                          product(wa, "food_drink", "House blend", tags="coffee, beans"),
                                          product(wb, "supplement", "Not yours"))
    p_guess = product(wa, "", "Night cream", tags="skincare, cream")

    def asset(ws, pid):
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, params_json, status, created_at, updated_at, "
                           "product_id) VALUES (?,?,?,?,?,?,?,?)", (ws, 1, "ad_image", "{}", "done", now, now, pid)).lastrowid
        return conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?,?,?)", (jid, "image", "/x")).lastrowid
    a_sup, a_beauty, a_coffee, a_guess = asset(wa, p_sup), asset(wa, p_beauty), asset(wa, p_coffee), asset(wa, p_guess)
    a_cross = asset(wa, p_other)
    conn.commit()
    for aid, want, what in ((a_sup, "supplements", "a supplement product"), (a_beauty, "cosmetics", "a beauty product"),
                            (a_guess, "cosmetics", "a product with no category, guessed from its tags"),
                            (a_coffee, "general", "a coffee"), (a_cross, "general", "another workspace's product")):
        s = spec(ai_disclosure=True, creative={"asset_id": aid, "kind": "image"})
        iss = adpolicy.preflight(s, conn=conn, workspace_id=wa)
        mm = "policy.vertical_mismatch" in ids(iss)
        check(adpolicy.derive_vertical(s, adpolicy.provenance(conn, wa, [aid]))[0] == want and mm == (want != "general"),
              f"{what} -> {want}" + (" (blocks on 'general')" if mm else ""))
    s = spec(vertical="cosmetics", ai_disclosure=True, creative={"asset_id": a_beauty, "kind": "image"})
    check("policy.vertical_mismatch" not in ids(adpolicy.preflight(s, conn=conn, workspace_id=wa)),
          "the beauty product declared as cosmetics passes")
    s = spec(creative={"asset_id": o_img, "kind": "image"})
    iss = adpolicy.preflight(s, conn=conn, workspace_id=wa)
    check("policy.ai_generated_disclosure" in ids(iss) and not adpolicy.blocking(iss),
          "an AI-made image without the disclosure gets a warning, not a block")

print(f"\nall {passed} checks passed")
