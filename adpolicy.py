"""The policy linter: the pre-flight that keeps the launch button off (roadmap M2.2, design doc section 5).

    issues = adpolicy.preflight(spec, conn=conn, workspace_id=ws_id)   # adspec.validate + lint + AI provenance
    adpolicy.blocking(issues)                                           # True: no launch

It runs on every creative's text (primary text, headline, description, spoken script, on-screen
text, Google headlines and descriptions) and on the spec (vertical, ages, special ad categories,
AI disclosure). Each finding is an adspec.Issue: level "block" or "warn", a stable rule id, a
plain message and a fix. Rules come from research B6 (FTC 16 CFR 255 and 465, Meta, TikTok,
Google and Snapchat ad policies as of 2026-10/11) and are a guard rail, not legal review:

    policy.personal_attributes       copy that implies it knows a personal trait ("Are you overweight?")
    policy.cure_claim                cures, treats or prevents a named disease
    policy.health_outcome_numeric    a number of pounds, sizes, points of blood sugar, % less pain, ...
    policy.before_after              before/after: blocked for weight loss and health, else warn (cosmetic, 18+)
    policy.implied_transformation    "day 1 vs day 30", "results after 4 weeks" for weight loss and health
    policy.appearance_inferiority    "embarrassing belly", "hate your thighs"
    policy.guaranteed_returns        guaranteed or risk-free returns, "can't lose"
    policy.finance_rates             cites a rate or return: Meta wants its standard risk text
    policy.results_claim             "3x more", "clinically proven", "#1": blocked until the member confirms proof
    policy.ai_testimonial            an AI presenter or voice speaking as a customer (hard block, 16 CFR 465)
    policy.ai_first_person           an AI presenter speaking of their own use ("I love how it feels"): warn
    policy.ai_disclosure_missing     an AI presenter or voice without the AI disclosure on the ad
    policy.ai_generated_disclosure   other AI-made photoreal media without the disclosure: warn
    policy.age_restricted            health, supplements, weight loss, cosmetics, finance, credit: 18+; crypto 25+; alcohol 21+ in the US
    policy.special_category_missing  housing, credit or employment without Meta's special ad category
    policy.special_category_maybe    copy that reads like housing, credit or employment: declare it or confirm
    policy.special_category_targeting declared category with narrowed age, gender or ZIP targeting
    policy.supplement_disclaimer     supplements without the FDA statement: warn
    policy.pharmacy                  prescriptions, telehealth, GLP-1 names: LegitScript needed
    policy.political                 elections and social issues need authorization: warn
    policy.snapchat_supplements      Snapchat pre-approves supplement ads: warn
    house.<id>                       compliance.py's rules (what generation already refuses), same level

compliance.py stays the source of the house rules; this file adds the platform rules on top.
"""
import re

import adspec
import compliance
from adspec import Issue

HEALTHY = {"health", "supplements", "weight_loss"}          # verticals with the health rules
AGE_18 = {"health", "supplements", "weight_loss", "cosmetics", "finance", "credit"}
SPECIAL = {"housing": "housing", "credit": "credit", "employment": "employment"}
TEXT_FIELDS = ("primary_text", "headline", "description", "script", "on_screen_text")

_I = re.I
CONDITIONS = (r"depression|depressed|anxiety|anxious|diabetes|diabetic|acne|eczema|psoriasis|adhd|cancer|arthritis|"
              r"insomnia|obesity|obese|overweight|hair loss|balding|addiction|chronic pain|migraines?|erectile dysfunction|"
              r"bad credit|debt|bankruptcy|disability|herpes|hiv")
DISEASES = (r"cancer|diabetes|depression|anxiety|arthritis|alzheimer'?s|dementia|covid(-19)?|flu|infections?|diseases?|"
            r"high blood pressure|hypertension|cholesterol|adhd|autism|insomnia|acne|eczema|psoriasis|asthma|migraines?|"
            r"ibs|crohn'?s|herpes|hiv|obesity|heart disease|stroke")
WEIGHT = re.compile(r"\b(weight|pounds|lbs?|kilos?|kg|belly fat|body fat|fat loss|slim(ming)?|skinny|waist|inches off|"
                    r"dress sizes?|jean sizes?|calories? burn|fat[- ]burn\w*)\b", _I)

RULES = {
    "policy.personal_attributes": [
        r"\b(are you|you're|you are|if you're|if you are)\s+(still\s+|really\s+|too\s+)?(overweight|obese|fat|depressed|"
        r"anxious|diabetic|pregnant|bald|balding|single|divorced|gay|lesbian|in debt|broke|bankrupt|unemployed|disabled|"
        r"over (40|50|60)|a (christian|muslim|jew|veteran|smoker|senior))\b",
        r"\byour (" + CONDITIONS + r"|credit score|belly fat|bald spot|wrinkles|cellulite|religion|divorce|criminal record|"
        r"arrest record|medical condition)\b",
        r"\b(struggling|suffering|living|dealing|battling) with (your |their )?(" + CONDITIONS + r")\b",
        r"\bfor (people|those|men|women|anyone|moms|dads) (with|managing|suffering from|living with|battling) (\w+\s+){0,2}?(" +
        CONDITIONS + r")\b",
        r"\b(conquer|beat|overcome|fight|fix|end|ditch) your (" + CONDITIONS + r"|weight)\b",
    ],
    "policy.cure_claim": [
        r"\b(cures?|cured|curing|treats?|treated|treating|heals?|healed|healing|prevents?|prevented|reverses?|reversed|"
        r"eliminates?|eliminated|gets? rid of)\b[^.!?\n]{0,40}?\b(" + DISEASES + r")\b",
        r"\b(cure|remedy|treatment) for (\w+\s+){0,2}?(" + DISEASES + r")\b",
        r"\b(" + DISEASES + r")\b[^.!?\n]{0,30}?\b(gone|cured|healed|reversed|disappeared)\b",
    ],
    "policy.health_outcome_numeric": [
        r"\b(lose|lost|losing|drop|dropped|dropping|shed|shedding|burn|burned|melt|melted|cut)\s+(up to\s+|over\s+|nearly\s+)?"
        r"\d+(\.\d+)?\s*(lbs?|pounds?|kg|kilos?|inches|in\.|sizes?|dress sizes?|jean sizes?|% body fat)\b",
        r"\b\d+(\.\d+)?\s*(lbs?|pounds?|kg|kilos?|inches|sizes?)\s+(down\s+)?(in|within|after)\s+(just\s+)?\d+\s*(days?|weeks?|months?)\b",
        r"\b(lower(ed|s)?|reduce[ds]?|cut|drop(ped|s)?)\s+(your\s+|my\s+)?(blood sugar|blood pressure|cholesterol|a1c|bmi)\s+"
        r"(by\s+)?\d+",
        r"\b\d+(\.\d+)?\s*%\s+(less|fewer|lower|reduction in|drop in)\s+(pain|anxiety|stress|body fat|fat|wrinkles|acne|bloating|"
        r"cravings|joint pain|inflammation)\b",
    ],
    "policy.before_after": [r"\bbefore\s*-?\s*(and|&|/|vs\.?|to)\s*-?\s*after\b", r"\bbefore\s*\+\s*after\b"],
    "policy.implied_transformation": [
        r"\bday\s*1\s*(vs\.?|to|->|→)\s*day\s*\d+\b", r"\bweek\s*1\s*(vs\.?|to|->|→)\s*week\s*\d+\b",
        r"\b(my|your|her|his|the)\s+transformation\b", r"\bresults? after\s+(just\s+)?\d+\s*(days?|weeks?|months?)\b",
        r"\b\d+\s*(days?|weeks?|months?)\s+(later|in|transformation)\b",
    ],
    "policy.appearance_inferiority": [
        r"\b(ugly|embarrassing|disgusting|gross|unattractive|flabby|saggy)\s+(\w+\s+){0,1}?(skin|body|belly|teeth|hair|fat|rolls|"
        r"thighs|arms|stomach|muffin top|love handles)\b",
        r"\b(hate|ashamed of|embarrassed (by|of)|insecure about) (your|my) (body|skin|belly|thighs|arms|teeth|smile|hair|looks|"
        r"weight|stomach)\b",
    ],
    "policy.guaranteed_returns": [
        r"\bguarantee(d|s)?\s+(\w+\s+){0,2}?(returns?|profits?|income|gains|yields?|interest|payouts?)\b",
        r"\brisk[- ]free\s+(\w+\s+){0,1}?(investment|returns?|profits?|trading|income|gains)\b",
        r"\b(can'?t|cannot|won'?t|never) lose (money|a (dime|cent))\b|\bdouble your (money|investment|crypto)\b",
        r"\bno risk\b[^.!?\n]{0,30}?\b(invest\w*|trading|returns?|profits?)\b",
    ],
    "policy.finance_rates": [
        r"\b\d+(\.\d+)?\s*%\s*(apy|apr|returns?|yield|interest|roi|annual returns?|a year|per year|monthly returns?)\b",
    ],
    "policy.results_claim": [
        r"\b\d+(\.\d+)?\s*(%|x|times)\s+(more|less|faster|better|fewer|higher|lower|stronger|longer)\b",
        r"\b(clinically|scientifically|lab|dermatologist)[- ](proven|tested|shown)\b|\b(studies|research|science) (shows?|proves?)\b",
        r"(^|\s)#1\b|\bnumber one\b|\b(doctor|dermatologist)[- ]recommended\b",
        r"\b\d+\s*% of (users|customers|people|women|men|participants|moms)\b",
    ],
    "policy.ai_testimonial": [
        r"\b(i|we)\s+(\w+\s+){0,2}?(bought|tried|ordered|purchased|started (using|taking)|have been (using|taking)|"
        r"switched to|was using)\b[^.!?\n]{0,80}?\b(and|now|since|it|this|my)\b",
        r"\bi('ve| have)? (lost|dropped|gained)\b",
        r"\bmy (skin|hair|energy|sleep|pain|weight|results?|acne|joints|anxiety|digestion|life|body|wrinkles|confidence)\s+"
        r"(is|are|was|were|has|have|feels?|cleared|improved|changed|got|went)\b",
        r"\b(best (purchase|thing|product) i('ve)? (ever )?(made|bought|tried))\b",
        r"\bi was (so )?skeptical\b|\bas a (real |happy |verified )?customer\b|\bsince i started (using|taking)\b",
        r"\b(it|this)\s+(\w+\s+){0,1}?(cured|fixed|saved|changed|transformed)\s+my\b",
    ],
    "policy.ai_first_person": [
        r"\bi (love|use|swear by|can'?t live without|always reach for|never leave home without)\b",
        r"\bmy (favorite|go-to|holy grail)\b",
    ],
    "policy.special_category_maybe": [
        r"\b(apartments?|condos?|homes?|houses?|rooms?) for (rent|sale|lease)\b|\b(mortgage|realtor|real estate agent|"
        r"rent[- ]to[- ]own|first[- ]time home ?buyers?)\b",
        r"\b(personal loans?|payday loans?|car loans?|auto loans?|credit cards?|line of credit|financing available|"
        r"bad credit (ok|okay|welcome)|get approved|pre[- ]approved)\b",
        r"\b(now hiring|we'?re hiring|job openings?|apply (now|today) for (the|this|our) (job|position|role)|hiring (drivers|"
        r"nurses|staff|reps))\b",
    ],
    "policy.pharmacy": [
        r"\b(prescription|rx|online pharmacy|telehealth|telemedicine|semaglutide|tirzepatide|ozempic|wegovy|mounjaro|"
        r"zepbound|glp-?1)\b",
    ],
    "policy.political": [
        r"\b(vote|voting|voters?|elections?|ballots?|candidates?|senators?|congress(man|woman)?|governor|referendum|"
        r"political|legislation|policy reform)\b",
    ],
}
_RX = {k: [re.compile(p, _I) for p in v] for k, v in RULES.items()}

MESSAGES = {
    "policy.personal_attributes": ("This line suggests you know something personal about the viewer (a condition, money "
                                   "trouble, age, religion). Meta rejects that most of all.",
                                   "Talk about the product, not the viewer: \"For dry skin\" instead of \"Your acne\"."),
    "policy.cure_claim": ("Says the product cures, treats or prevents a disease.", "Use only what the label says."),
    "policy.health_outcome_numeric": ("Promises a number for a health or weight result.", "Remove the number and the time frame."),
    "policy.before_after": ("Before and after content.", "Platforms reject it for weight loss and health; for cosmetic "
                                                       "changes only, with an 18+ audience."),
    "policy.implied_transformation": ("Implies a body or health transformation over time.", "Show the product in use instead."),
    "policy.appearance_inferiority": ("Makes people feel bad about how they look.", "Say what the product does, not what is wrong "
                                                                                    "with the viewer."),
    "policy.guaranteed_returns": ("Promises guaranteed or risk-free returns.", "Remove the guarantee."),
    "policy.finance_rates": ("Cites a rate or return. Meta requires its standard risk warning text on these ads.",
                             "Remove the rate, or add Meta's risk warning in Ads Manager before turning the ad on."),
    "policy.results_claim": ("Makes a results claim (a number, \"clinically proven\", \"#1\"). You need proof on file.",
                             "Remove it, or confirm you can back it up."),
    "policy.ai_testimonial": ("An AI presenter or voice speaks as a customer. AI testimonials are fake reviews under FTC "
                              "rule 16 CFR 465.", "Rewrite it as a presenter describing the product (\"This serum is "
                                                  "made for...\"), not as someone who bought it."),
    "policy.ai_first_person": ("An AI presenter talks about their own use.", "Keep the presenter describing the product."),
    "policy.ai_disclosure_missing": ("This ad has an AI presenter or AI voice and the AI disclosure is off.",
                                     "Turn on the AI disclosure. We set the platform's AI label on the ad."),
    "policy.ai_generated_disclosure": ("This ad was made with AI and looks real. Platforms ask for an AI disclosure.",
                                       "Turn on the AI disclosure."),
    "policy.age_restricted": ("This kind of product can't be shown to people that young.", "Raise the minimum age."),
    "policy.special_category_missing": ("Housing, credit and employment ads must be declared as a special ad category on Meta.",
                                        "Add the special ad category."),
    "policy.special_category_maybe": ("This reads like a housing, credit or job ad. Meta requires those to be declared.",
                                      "Declare the special ad category, or reword it if it isn't one."),
    "policy.special_category_targeting": ("Special ad categories can't narrow age, gender or ZIP codes on Meta.",
                                          "Use ages 18 to 65, all genders and a radius of at least 15 miles."),
    "policy.supplement_disclaimer": ("Supplement ads are expected to carry the FDA statement in the copy.",
                                     "Add: These statements have not been evaluated by the Food and Drug Administration."),
    "policy.pharmacy": ("Prescription drugs, online pharmacies and telehealth need LegitScript certification on Meta and "
                        "TikTok.", "Remove it, or get certified first."),
    "policy.political": ("Ads about elections, politics or social issues need the platform's authorization and a "
                         "\"Paid for by\" label.", "Remove it, or get authorized first."),
    "policy.snapchat_supplements": ("Snapchat reviews supplement ads before they run.", "Expect a review delay."),
}

# level per rule, then per-platform overrides
LEVELS = {
    "policy.personal_attributes": ("block", {"google": "warn", "snapchat": "warn"}),
    "policy.cure_claim": ("block", {}), "policy.health_outcome_numeric": ("block", {}),
    "policy.implied_transformation": ("block", {}), "policy.appearance_inferiority": ("block", {"google": "warn"}),
    "policy.guaranteed_returns": ("block", {}), "policy.finance_rates": ("warn", {}),
    "policy.ai_testimonial": ("block", {}), "policy.ai_first_person": ("warn", {}),
    "policy.ai_disclosure_missing": ("block", {}), "policy.ai_generated_disclosure": ("warn", {}),
    "policy.age_restricted": ("block", {}), "policy.special_category_missing": ("block", {"google": "warn", "tiktok": "warn",
                                                                                         "snapchat": "warn"}),
    "policy.special_category_maybe": ("warn", {}), "policy.special_category_targeting": ("block", {}),
    "policy.supplement_disclaimer": ("warn", {}), "policy.pharmacy": ("block", {}), "policy.political": ("warn", {}),
    "policy.snapchat_supplements": ("warn", {}),
}


def level(rule_id: str, platform: str) -> str:
    base, over = LEVELS.get(rule_id, ("warn", {}))
    return over.get(platform, base)


def _issue(rule_id, platform, where, lvl=None):
    msg, fix = MESSAGES[rule_id]
    return Issue(lvl or level(rule_id, platform), rule_id, msg, fix, where)


def _any(rule_id, text):
    return any(rx.search(text) for rx in _RX[rule_id])


def _before_after(text):
    for rx in _RX["policy.before_after"]:
        for m in rx.finditer(text):
            if not compliance.negated(text, m.start()):
                return True
    return False


def lint_text(text: str, *, platform: str = "meta", vertical: str = "general", ai: bool = False,
              claims_substantiated: bool = False, where: str = "", house: bool = True) -> list:
    """The policy findings for one piece of ad text."""
    text = text or ""
    if not text.strip():
        return []
    out = []
    weighty = bool(WEIGHT.search(text)) or vertical == "weight_loss"
    healthy = vertical in HEALTHY or weighty
    for rid in ("policy.personal_attributes", "policy.cure_claim", "policy.health_outcome_numeric",
                "policy.appearance_inferiority", "policy.guaranteed_returns", "policy.pharmacy", "policy.political"):
        if _any(rid, text):
            out.append(_issue(rid, platform, where))
    if _before_after(text):
        lvl = "block" if healthy or platform == "snapchat" and weighty else "warn"
        out.append(_issue("policy.before_after", platform, where, lvl))
    if healthy and _any("policy.implied_transformation", text):
        out.append(_issue("policy.implied_transformation", platform, where))
    if _any("policy.finance_rates", text) and (vertical in ("finance", "crypto", "credit") or
                                                re.search(r"\b(apy|apr|invest\w*|returns?|yield|roi|savings)\b", text, _I)):
        out.append(_issue("policy.finance_rates", platform, where))
    if _any("policy.results_claim", text):
        out.append(_issue("policy.results_claim", platform, where, "warn" if claims_substantiated else "block"))
    if ai:
        if _any("policy.ai_testimonial", text):
            out.append(_issue("policy.ai_testimonial", platform, where))
        elif _any("policy.ai_first_person", text):
            out.append(_issue("policy.ai_first_person", platform, where))
    if _any("policy.special_category_maybe", text):
        out.append(_issue("policy.special_category_maybe", platform, where))
    if house:
        for lvl, rid, msg in compliance.findings(text):
            out.append(Issue(lvl, f"house.{rid}", msg, "Reword it: Campaignify doesn't make or launch ads with this.", where))
    return out


def provenance(conn, workspace_id: int, asset_ids) -> dict:
    """{outputs.id: {"ai_presenter", "synthetic_voice", "ai_generated"}} for this workspace's assets.
    ugc_video speaks with a generated voice (and shows an AI presenter unless it is a voiceover);
    motion_copy re-creates a presenter; ad_image and photo_to_video are AI-made media."""
    import json
    ids = [int(a) for a in asset_ids or [] if str(a).isdigit()]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT o.id, j.tool, j.params_json FROM outputs o JOIN jobs j ON j.id=o.job_id "
                        f"WHERE j.workspace_id=? AND o.id IN ({marks})", (int(workspace_id), *ids)).fetchall()
    out = {}
    for r in rows:
        try:
            ad_type = (json.loads(r["params_json"] or "{}") or {}).get("ad_type") or ""
        except ValueError:
            ad_type = ""
        tool = r["tool"] or ""
        out[r["id"]] = {"ai_presenter": (tool == "ugc_video" and ad_type != "voiceover") or tool == "motion_copy",
                        "synthetic_voice": tool == "ugc_video",
                        "ai_generated": tool in ("ugc_video", "motion_copy", "ad_image", "photo_to_video", "video_upscale")}
    return out


def apply_provenance(spec, conn, workspace_id: int):
    """Set each creative's ai_presenter / synthetic_voice from the library (they can only be turned on, never
    off), and spec.ai_disclosure when any is on. Returns {asset_id: provenance}."""
    prov = provenance(conn, workspace_id, [c.asset_id for c in spec.creatives if c.asset_id])
    for c in spec.creatives:
        p = prov.get(c.asset_id)
        if p:
            c.ai_presenter = c.ai_presenter or p["ai_presenter"]
            c.synthetic_voice = c.synthetic_voice or p["synthetic_voice"]
    if any(c.ai_presenter or c.synthetic_voice for c in spec.creatives):
        spec.ai_disclosure = True
    return prov


def lint(spec, prov: dict = None) -> list:
    """Policy findings for a whole CampaignSpec (texts and spec-level rules)."""
    p, v, out = spec.platform, spec.vertical, []
    for i, c in enumerate(spec.creatives):
        ai = bool(c.ai_presenter or c.synthetic_voice)
        for fld in TEXT_FIELDS:
            out += lint_text(getattr(c, fld), platform=p, vertical=v, ai=ai, where=f"creatives[{i}].{fld}",
                             claims_substantiated=spec.claims_substantiated)
        if ai and not spec.ai_disclosure:
            out.append(_issue("policy.ai_disclosure_missing", p, f"creatives[{i}]"))
        elif (prov or {}).get(c.asset_id, {}).get("ai_generated") and not spec.ai_disclosure:
            out.append(_issue("policy.ai_generated_disclosure", p, f"creatives[{i}]"))
    g = spec.google or {}
    for key in ("headlines", "descriptions"):
        for i, t in enumerate(g.get(key) or []):
            out += lint_text(t, platform=p, vertical=v, where=f"google.{key}[{i}]",
                             claims_substantiated=spec.claims_substantiated)

    aud, geo = spec.audience or {}, spec.geo or {}
    age_min = aud.get("age_min", 18) if isinstance(aud.get("age_min", 18), int) else 18
    need = 25 if v == "crypto" else 21 if v == "alcohol" and "US" in (geo.get("countries") or []) else \
        18 if v in AGE_18 or v == "alcohol" else 0
    if need and age_min < need:
        out.append(Issue("block", "policy.age_restricted", MESSAGES["policy.age_restricted"][0],
                         f"Set the minimum age to {need} or more.", "audience.age_min"))
    if p == "meta":
        cats = set(spec.special_ad_categories or [])
        if v in SPECIAL and SPECIAL[v] not in cats:
            out.append(_issue("policy.special_category_missing", p, "special_ad_categories"))
        if cats & {"housing", "credit", "employment"}:
            genders = aud.get("genders") or ["all"]
            narrow = age_min > 18 or (aud.get("age_max", 65) < 65) or genders not in (["all"], []) or geo.get("zips") \
                or any((c or {}).get("radius_km", 25) < 24 for c in geo.get("cities") or [])
            if narrow:
                out.append(_issue("policy.special_category_targeting", p, "audience"))
    if v == "supplements":
        alltext = " ".join(getattr(c, f) or "" for c in spec.creatives for f in TEXT_FIELDS)
        if not re.search(r"not been evaluated by the (food and drug administration|fda)", alltext, _I):
            out.append(_issue("policy.supplement_disclaimer", p, "creatives"))
        if p == "snapchat":
            out.append(_issue("policy.snapchat_supplements", p, "vertical"))
    # one finding per rule per field is enough
    seen, uniq = set(), []
    for it in out:
        k = (it.rule_id, it.where)
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq


def preflight(spec, conn=None, workspace_id: int = None) -> list:
    """Everything the launch page shows: platform constraints (adspec.validate) and the policy
    findings, with AI provenance read from the library when a connection is given."""
    prov = apply_provenance(spec, conn, workspace_id) if conn is not None and workspace_id else {}
    return adspec.validate(spec) + lint(spec, prov)


def blocking(issues) -> bool:
    return any(i.level == "block" for i in issues)
