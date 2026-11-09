"""The policy linter: the pre-flight that keeps the launch button off (roadmap M2.2, design doc section 5).

    issues = adpolicy.preflight(spec, conn=conn, workspace_id=ws_id)   # adspec.validate + lint + AI provenance
    adpolicy.blocking(issues)                                           # True: no launch

It runs on every creative's text (primary text, headline, description, spoken script, on-screen
text, Google headlines and descriptions) and on the spec (vertical, ages, special ad categories,
AI disclosure). Google keywords, the campaign and creative names, landing pages and UTM strings get
the subject rules (prescription drugs, politics, housing/credit/jobs); negative keywords only the
invisible-character check, since they keep the ad away from a search. Each finding is an
adspec.Issue: level "block" or "warn", a stable rule id, a plain message and a fix. Rules come
from research B6 (FTC 16 CFR 255 and 465, Meta, TikTok, Google and Snapchat ad policies as of
2026-10/11) and are a guard rail, not legal review:

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
    policy.caps_punctuation          Snapchat and TikTok: words in capitals or repeated !! / ?? (their creative
                                     reviews reject both): warn
    policy.emoji                     TikTok ad text with emoji (TikTok's ad text doesn't take them): block
    policy.hidden_characters         invisible characters (zero-width, bidi controls, soft hyphen) in launch text
    policy.vertical_mismatch         the product or the copy says supplements, weight loss, crypto, housing, ... while the
                                     spec says "general" (block) or another vertical (warn); the rules of both run (L-17)
    house.<id>                       compliance.py's rules (what generation already refuses), same level

Every text is matched in compliance.normalize's form (NFKC, no format characters, look-alike
letters as Latin, M-4), so "cu<zero-width>res" and a Cyrillic "е" don't slip past a rule; drug
names are also matched with digits read as letters and spaces removed ("0zempic", "sema glutide").

compliance.py stays the source of the house rules; this file adds the platform rules on top. The cure,
health-outcome, weight-loss, guaranteed-return and before/after patterns live in compliance.py and are
shared, so generation (compliance.check) refuses whatever the launch pre-flight blocks for those.
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
DISEASES = compliance.DISEASES
WEIGHT = re.compile(r"\b(weight|pounds|lbs?|kilos?|kg|belly fat|body fat|fat loss|slim(ming)?|skinny|waist|inches off|"
                    r"dress sizes?|jean sizes?|calories? burn|fat[- ]burn\w*)\b", _I)
# A short question that names a trait: "Overweight?", "Still overweight at 40?", "Anxious all the time?",
# "Tired of being overweight?", "Losing your hair?", "Over 45 and tired?". The trait opens the sentence.
_TRAIT = (CONDITIONS + r"|pregnant|in debt|losing (your )?hair|thinning hair|going bald|gaining weight|can'?t sleep|"
          r"can'?t lose (the )?weight")
_ARROW = r"(vs\.?|versus|to|then|->|[^\w\s,.;!?()'\"]{1,8})"
DRUG_NAMES = (r"semaglutide|tirzepatide|retatrutide|liraglutide|ozempic|wegovy|mounjaro|zepbound|rybelsus|saxenda|"
              r"phentermine|contrave|qsymia|metformin|sildenafil|tadalafil|viagra|cialis|finasteride|tretinoin|isotretinoin|"
              r"accutane")

RULES = {
    "policy.personal_attributes": [
        r"(^|[.!?:]\s*|\n)(still\s+|feeling\s+)?(" + CONDITIONS + r"|fat|depressed|anxious|diabetic|bald|"
        r"in debt|broke|unemployed|over (40|50|60))\s*\?",
        r"(^|[.!?:]\s*|\n)(still |feeling |getting |always |so |too |(tired|sick) of being |got |have |having |with |"
        r"struggling with |suffering from |dealing with )?(" + _TRAIT + r")\b(?!\s+to\b)[^.!?\n]{0,25}\?",
        r"(^|[.!?:]\s*|\n)over [3-7]\d\s*(\?|(and|&)\s+[^.!?\n]{0,25}\?)",
        r"\b(tired|sick) of being (" + CONDITIONS + r"|fat|bald|broke|single|in debt)\b",
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
    "policy.cure_claim": compliance.CURE_CLAIM,
    "policy.health_outcome_numeric": compliance.WEIGHT_OUTCOME + compliance.HEALTH_OUTCOME,
    "policy.implied_transformation": [
        r"\bday\s*1\s*" + _ARROW + r"\s*day\s*\d+\b", r"\bweek\s*1\s*" + _ARROW + r"\s*week\s*\d+\b",
        r"\b(my|your|her|his|the)\s+transformation\b", r"\bresults? after\s+(just\s+)?\d+\s*(days?|weeks?|months?)\b",
        r"\b\d+\s*(days?|weeks?|months?)\s+(later|in|transformation)\b",
    ],
    "policy.appearance_inferiority": [
        r"\b(ugly|embarrassing|disgusting|gross|unattractive|flabby|saggy)\s+(\w+\s+){0,1}?(skin|body|belly|teeth|hair|fat|rolls|"
        r"thighs|arms|stomach|muffin top|love handles)\b",
        r"\b(hate|ashamed of|embarrassed (by|of)|insecure about) (your|my) (body|skin|belly|thighs|arms|teeth|smile|hair|looks|"
        r"weight|stomach)\b",
    ],
    "policy.guaranteed_returns": compliance.GUARANTEED_RETURNS,
    "policy.finance_rates": [
        r"\b\d+(\.\d+)?\s*(%|percent)\s*(apy|apr|returns?|yield|interest|roi|annual returns?|a year|per year|monthly returns?|"
        r"(a|per) month|monthly|weekly|daily)\b",
    ],
    "policy.results_claim": [
        r"\b\d+(\.\d+)?\s*(%|x|times)\s+(more|less|faster|better|fewer|higher|lower|stronger|longer)\b",
        r"\b(clinically|scientifically|lab|dermatologist)[- ](proven|tested|shown)\b|\b(studies|research|science) (shows?|proves?)\b",
        r"(?<![\w&])#\s?1\b|(?<!step )(?<!tip )(?<!rule )(?<!size )(?<!day )(?<!item )(?<!track )(?<!page )"
        r"\bnumber[\s-]*(one|1)\b(?!\s+(rule|tip|step|question|goal|priority|fan|reason|thing|job)s?\b)|"
        r"\bno\.?\s*1\b|\bn\s*[\u00b0\u00ba]\s*1\b|\bnumero uno\b|"
        r"\branked\s+((#\s?|number\s+|no\.?\s*)(1|one)\b|first\s+(by|among|on)\b)|\b(doctor|dermatologist)[- ]recommended\b",
        r"\b\d+\s*% of (users|customers|people|women|men|participants|moms)\b",
    ],
    "policy.ai_testimonial": [
        r"\b(i|we)\s+(\w+\s+){0,2}?(bought|tried|ordered|purchased|started (using|taking)|have been (using|taking)|"
        r"switched to|was using)\b[^.!?\n]{0,80}?\b(and|now|since|it|this|my)\b",
        r"\bi('ve| have)? (lost|dropped|gained)\b",
        r"\bi('ve| have)\s+(been\s+)?(used|using|tried|trying|taken|taking|bought|ordered|worn|wearing|switched)\b",
        r"\b(best|only|favorite|first)\s+(\w+\s+){0,2}?i('ve| have)\s+(ever\s+)?(used|tried|bought|owned|had)\b",
        r"\bmy (face|skin|hair|energy|sleep|pain|weight|acne|joints|body|wrinkles|complexion|pores|gut|bloating|lips|lashes|"
        r"teeth|smile|nails|scalp|belly|stomach|mood|focus)\s*([?!:.,-]|" + _ARROW + r")+\s*(glowing|glows|clear|cleared|"
        r"clearer|gone|better|perfect|smooth|smoother|softer|brighter|younger|tighter|firmer|plump(er)?|hydrated|radiant|"
        r"dewy|calmer|stronger|thicker|fuller|healthier|lighter|slimmer|flawless|amazing|transformed|fixed|healed)\b",
        r"\bmy (skin|hair|energy|sleep|pain|weight|results?|acne|joints|anxiety|digestion|life|body|wrinkles|confidence|face|"
        r"complexion|pores|lips|lashes|teeth|smile|nails|scalp|belly|stomach|mood|focus)\s+(is|are|was|were|has|have|had|"
        r"feels?|felt|looks?|looked|never|hasn'?t|haven'?t|seems?|stays?|stayed|became|turned|cleared|improved|changed|got|"
        r"went)\b",
        r"\b(best (purchase|thing|product) i('ve)? (ever )?(made|bought|tried))\b",
        r"\bi was (so )?skeptical\b|\bas a (real |happy |verified )?customer\b|"
        r"\b(ever )?since i (started|switched|tried|began|got|added)\b",
        r"(^|[.!?:]\s*)me (after|before|now|then|today|vs)\b|\bme after (just )?(\d+|a|one|two|three|four|six) "
        r"(days?|weeks?|months?)\b",
        r"\b(it|this)\s+(\w+\s+){0,1}?(cured|fixed|saved|changed|transformed)\s+my\b",
    ],
    "policy.ai_first_person": [
        r"\bi (love|use|swear by|can'?t live without|always reach for|never leave home without)\b",
        r"\bmy (favorite|go-to|holy grail)\b|\bi'?m (so |totally |honestly )?obsessed\b",
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
        r"\b(prescription(?!s?\s+(glasses|sunglasses|lenses|eyewear|frames|goggles))|rx|online pharmacy|"
        r"telehealth|telemedicine|glp[\s-]*(1|one)|" + DRUG_NAMES + r")\b",
        r"\b(tirz|sema|compounded\s+(sema\w*|tirz\w*|glp\w*|weight[- ]loss (shots?|meds?|medications?)|meds?|"
        r"medications?|drugs?|injections?|shots?)|weight[- ]loss (shots?|injections?|jabs?))\b",
    ],
    "policy.political": [
        r"\b(vote|voting|voters?|elections?|ballots?|candidates?|senators?|congress(man|woman)?|governor|referendum|"
        r"political|legislation|policy reform)\b",
    ],
}
_RX = {k: [re.compile(p, _I) for p in v] for k, v in RULES.items()}
# Drug names written to dodge the rule: digits for letters ("0zempic") or spaced out ("sema glutide").
# Only the long, distinctive names: short ones turn up inside ordinary words once the spaces go ("social issues").
DRUGS = re.compile(r"(semaglutide|tirzepatide|retatrutide|liraglutide|ozempic|wegovy|mounjaro|zepbound|rybelsus|saxenda|"
                   r"phentermine)", _I)
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i"})


def _drug_collapsed(text: str) -> bool:
    return bool(DRUGS.search(re.sub(r"[^a-z]", "", text.lower().translate(LEET))))


def _drug_dodged(text: str) -> bool:
    if any(rx.search(text.lower().translate(LEET)) for rx in _RX["policy.pharmacy"]):      # "comp0unded s3ma"
        return True
    return _drug_collapsed(text)

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
    "policy.caps_punctuation": ("Words in capitals or repeated punctuation (!! or ??) often get the ad rejected in review.",
                                "Write it in normal case, with one ! or ? at most."),
    "policy.emoji": ("TikTok's ad text doesn't take emoji.", "Remove the emoji. The video can still show them."),
    "policy.hidden_characters": ("This text has invisible characters (zero-width spaces, direction marks or soft "
                                 "hyphens). They can hide words from ad review.", "Retype the text without them."),
    "policy.vertical_mismatch": ("What this campaign sells doesn't match its product type. The age and category rules "
                                 "depend on it.", "Set the product type to match what the ad sells."),
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
    "policy.snapchat_supplements": ("warn", {}), "policy.hidden_characters": ("block", {}),
    "policy.caps_punctuation": ("warn", {}), "policy.emoji": ("block", {}),
    "policy.vertical_mismatch": ("block", {}),
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
    return compliance.before_after(text)


# Rules that only one platform's review applies (how the text is written there): platform -> rule ids.
PLATFORM_RULES = {"snapchat": ("policy.caps_punctuation",), "tiktok": ("policy.caps_punctuation",)}
EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\uFE0F\u200D\u2B50\u2B55\u231A"
                   "\u231B\u23E9-\u23FA]")


def caps_or_punctuation(text: str) -> bool:
    """Two or more words of 4+ letters all in capitals (and at least half the words), or !! / ?? / ?! runs."""
    if re.search(r"[!?]{2,}", text):
        return True
    words = re.findall(r"[^\W\d_]{4,}", text)
    caps = [w for w in words if w.isupper()]
    return len(caps) >= 2 and len(caps) * 2 >= len(words)


# Rules about what is advertised (not how it is said): the only ones that apply to keywords, names and URLs.
SUBJECT_RULES = ("policy.pharmacy", "policy.political", "policy.special_category_maybe")


def lint_text(text: str, *, platform: str = "meta", vertical: str = "general", ai: bool = False,
              claims_substantiated: bool = False, where: str = "", house: bool = True, only=None) -> list:
    """The policy findings for one piece of ad text. `only`: a set of rule ids to run (the subject rules for
    keywords, names and URLs); None runs them all."""
    raw = text or ""
    text = compliance.normalize(raw)          # NFKC, no invisible characters, look-alikes as Latin (M-4)
    if not text.strip():
        return []
    run = (lambda rid: True) if only is None else (lambda rid: rid in only)
    out = []
    if compliance.hidden_chars(raw):
        out.append(_issue("policy.hidden_characters", platform, where))
    weighty = bool(WEIGHT.search(text)) or vertical == "weight_loss"
    healthy = vertical in HEALTHY or weighty
    for rid in ("policy.personal_attributes", "policy.cure_claim", "policy.health_outcome_numeric",
                "policy.appearance_inferiority", "policy.guaranteed_returns", "policy.pharmacy", "policy.political"):
        if run(rid) and (_any(rid, text) or rid == "policy.pharmacy" and _drug_dodged(text)):
            out.append(_issue(rid, platform, where))
    if run("policy.before_after") and _before_after(text):
        lvl = "block" if healthy or platform == "snapchat" and weighty else "warn"
        out.append(_issue("policy.before_after", platform, where, lvl))
    if run("policy.implied_transformation") and healthy and _any("policy.implied_transformation", text):
        out.append(_issue("policy.implied_transformation", platform, where))
    if run("policy.finance_rates") and _any("policy.finance_rates", text) and (
            vertical in ("finance", "crypto", "credit") or
            re.search(r"\b(apy|apr|invest\w*|returns?|yield|roi|savings|profits?|trading|guarantee[ds]?)\b", text, _I)):
        out.append(_issue("policy.finance_rates", platform, where))
    if run("policy.results_claim") and _any("policy.results_claim", text):
        out.append(_issue("policy.results_claim", platform, where, "warn" if claims_substantiated else "block"))
    if ai and run("policy.ai_testimonial"):
        if _any("policy.ai_testimonial", text):
            out.append(_issue("policy.ai_testimonial", platform, where))
        elif _any("policy.ai_first_person", text):
            out.append(_issue("policy.ai_first_person", platform, where))
    if run("policy.special_category_maybe") and _any("policy.special_category_maybe", text):
        out.append(_issue("policy.special_category_maybe", platform, where))
    rules = PLATFORM_RULES.get(platform, ())
    if "policy.caps_punctuation" in rules and run("policy.caps_punctuation") and caps_or_punctuation(text):
        out.append(_issue("policy.caps_punctuation", platform, where))
    if house and only is None:
        for lvl, rid, msg in compliance.findings(text):
            out.append(Issue(lvl, f"house.{rid}", msg, "Reword it: Campaignify doesn't make or launch ads with this.", where))
    return out


def url_words(url: str) -> str:
    """A landing page address or UTM string as words: percent-decoded, separators as spaces
    ("https://x.test/ozempic-alternative?utm_campaign=vote_yes" -> "... ozempic alternative ... vote yes")."""
    from urllib.parse import unquote_plus
    t = unquote_plus(unquote_plus(url or ""))
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", " ", t, flags=_I)
    t = re.sub(r"\butm_(source|medium|campaign|term|content|id)\b", " ", t, flags=_I)
    return re.sub(r"[/_\-.=&?#+:;,|~]+", " ", t).strip()


def _keyword_text(k) -> str:
    if isinstance(k, dict):
        return str(k.get("text") or k.get("keyword") or "")
    return str(k or "")


# ---------------------------------------------------------------- the vertical, derived (L-17)
# What the campaign sells, read from the product (category, tags, claims) and from every text in the spec. The
# declared spec.vertical defaults to "general", so a supplement or weight-loss ad left on "general" would skip the
# age, special-category and supplement rules. The strongest signal wins, in this order.
VERTICAL_ORDER = ("weight_loss", "health", "supplements", "crypto", "finance", "credit", "housing", "employment",
                  "alcohol", "cosmetics")
VERTICAL_LABELS = {"general": "General", "weight_loss": "Weight loss", "health": "Health", "supplements": "Supplements",
                   "cosmetics": "Cosmetics", "finance": "Finance", "crypto": "Crypto", "alcohol": "Alcohol",
                   "housing": "Housing", "credit": "Credit", "employment": "Employment"}
VERTICAL_SIGNALS = {
    "weight_loss": r"\b(weight[- ]?loss|lose weight|losing weight|fat[- ]?burn\w*|slimming|diet pills?|appetite suppress\w*|"
                   r"belly fat|keto pills?|skinny (tea|coffee|shots?|patch(es)?)|glp[\s-]*1|semaglutide|tirzepatide|ozempic|"
                   r"wegovy|mounjaro|zepbound|phentermine|weight management)\b",
    "health": r"\b(telehealth|telemedicine|online pharmacy|pharmacy|prescriptions?(?! (glasses|sunglasses|lenses|eyewear|"
              r"frames|goggles))|rx|pain relief|cbd|hormone therapy|testosterone|trt|mental health|therapists?|"
              r"hearing aids?|medical (weight loss|clinic|treatment|care)|sildenafil|tadalafil|viagra|cialis|finasteride|"
              r"tretinoin|metformin)\b",
    "supplements": r"\b(dietary supplements?|supplements?|multivitamins?|vitamin (gummies|supplements?|capsules|tablets|"
                   r"pills|d3|b12)|probiotics?|prebiotics?|collagen (peptides|powder|supplements?|gummies|drink)|creatine|"
                   r"protein powder|pre[- ]workout|ashwagandha|nootropics?|electrolyte (powder|mix|drink)s?|melatonin|"
                   r"fish oil|omega[- ]?3s?|greens powder|softgels?)\b",
    "crypto": r"\b(crypto(currency|currencies)?|bitcoin|btc|ethereum|altcoins?|nfts?|defi|token (sale|presale)|"
              r"memecoins?|web3|dogecoin|staking rewards)\b",
    "finance": r"\b(investment (account|platform|app|returns?|portfolio|opportunit(y|ies))|investing (app|platform)|"
               r"investors?|stock market|etfs?|index funds?|(day|forex|options|stock) trading|trading (bot|platform|"
               r"signals|app)|brokerage|forex|apy|high[- ]yield savings|savings account|retirement (account|savings)|"
               r"roth ira|401\(?k\)?|wealth management|financial advisors?|hedge funds?)\b",
    "credit": r"\b(personal loans?|payday loans?|car loans?|auto loans?|student loans?|loan (approval|offers?)|"
              r"line of credit|credit repair|credit score|credit card (offers?|rewards|approval|application)|"
              r"apply for (a |our )?credit card|buy now,? pay later|debt (consolidation|relief|settlement)|cash advance|"
              r"pre[- ]?approved|bad credit)\b",
    "housing": r"\b((apartments?|condos?|homes?|houses?|rooms?|townhomes?|lofts?) for (rent|sale|lease)|mortgages?|"
               r"realtors?|real estate (agents?|listings?)|rent[- ]to[- ]own|first[- ]time home ?buyers?|"
               r"move[- ]in specials?|now leasing)\b",
    "employment": r"\b(now hiring|we'?re hiring|we are hiring|job openings?|careers? at|apply (now|today) for (the|this|"
                  r"our) (job|position|role)|hiring (drivers|nurses|staff|reps|baristas|cooks|servers|techs|technicians)|"
                  r"positions? available)\b",
    "alcohol": r"\b(whiskey|whisky|vodka|tequila|bourbon|mezcal|liquor|hard seltzers?|craft beers?|brewery|breweries|"
               r"winery|wineries|wine (tasting|club|bar|shop|list)|bottles? of wine|\d+(\.\d+)?\s*% abv|abv)\b",
    "cosmetics": r"\b(botox|lip fillers?|dermal fillers?|laser hair removal|tattoo removal|chemical peels?|microneedling|"
                 r"liposuction|tummy tuck|breast augmentation|cosmetic (surgery|procedures?)|med ?spa)\b",
}
_VRX = {k: re.compile(v, _I) for k, v in VERTICAL_SIGNALS.items()}
# products.CATEGORIES -> vertical (the others are "general" unless their words say otherwise)
CATEGORY_VERTICAL = {"supplement": "supplements", "beauty": "cosmetics"}


def _signals(text: str) -> dict:
    """{vertical: the words that showed it} for one normalized text."""
    out = {}
    for v, rx in _VRX.items():
        m = rx.search(text)
        if m:
            out[v] = m.group(0)
    return out


def derive_vertical(spec, prov: dict = None) -> tuple:
    """(vertical, why) from the product behind the creatives (prov[asset_id]["product"], see provenance) and every
    text in the spec. ("general", "") when nothing points anywhere. `why` is a short plain phrase for the message."""
    hits = {}
    for c in spec.creatives:
        prod = ((prov or {}).get(c.asset_id) or {}).get("product") or {}
        cat = CATEGORY_VERTICAL.get(prod.get("category") or "")
        if cat:
            hits.setdefault(cat, f"the product category is {prod.get('category_name') or VERTICAL_LABELS[cat]}")
        ptext = compliance.normalize(" ".join(str(prod.get(k) or "") for k in ("tags", "name", "description", "claims")))
        for v, words in _signals(ptext).items():
            hits.setdefault(v, f"the product says \"{words}\"")
    texts = [spec.name]
    for c in spec.creatives:
        texts += [getattr(c, f) or "" for f in TEXT_FIELDS] + [c.name, url_words(c.destination_url), url_words(c.utm)]
    g = spec.google or {}
    texts += list(g.get("headlines") or []) + list(g.get("descriptions") or []) + list(g.get("long_headlines") or [])
    texts += [_keyword_text(k) for k in g.get("keywords") or []]
    alltext = compliance.normalize(" ".join(t for t in texts if t))
    for v, words in _signals(alltext).items():
        hits.setdefault(v, f"the ad says \"{words}\"")
    if _drug_collapsed(alltext):                                  # "sema glutide", "0zempic"
        hits.setdefault("weight_loss", "the ad names a prescription weight-loss drug")
    for v in VERTICAL_ORDER:
        if v in hits:
            return v, hits[v]
    return "general", ""


def _text_vertical(declared: str, derived: str) -> str:
    """The vertical the text rules run with: the declared one, unless the derived one has the health rules."""
    if declared in HEALTHY:
        return declared
    if derived in HEALTHY or declared == "general":
        return derived
    return declared


def provenance(conn, workspace_id: int, asset_ids) -> dict:
    """{outputs.id: {"ai_presenter", "synthetic_voice", "ai_generated", "product"}} for this workspace's assets.
    ugc_video speaks with a generated voice (and shows an AI presenter unless it is a voiceover);
    motion_copy re-creates a presenter; ad_image and photo_to_video are AI-made media. "product" is the
    product the asset was made for ({"category", "category_name", "tags", "name", "description", "claims"},
    or {}): derive_vertical reads it."""
    import json
    ids = [int(a) for a in asset_ids or [] if str(a).isdigit()]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT o.id, j.tool, j.params_json, j.product_id FROM outputs o JOIN jobs j ON j.id=o.job_id "
                        f"WHERE j.workspace_id=? AND o.id IN ({marks})", (int(workspace_id), *ids)).fetchall()
    prods = {}
    pids = sorted({r["product_id"] for r in rows if r["product_id"]})
    if pids:
        import products as _products
        for p in conn.execute(f"SELECT * FROM products WHERE workspace_id=? AND id IN ({','.join('?' * len(pids))})",
                              (int(workspace_id), *pids)).fetchall():
            cat = _products.category_of(p)
            prods[p["id"]] = {"category": cat, "category_name": _products.CATEGORY_NAMES.get(cat, ""),
                              **{k: p[k] or "" for k in ("tags", "name", "description", "claims")}}
    out = {}
    for r in rows:
        try:
            ad_type = (json.loads(r["params_json"] or "{}") or {}).get("ad_type") or ""
        except ValueError:
            ad_type = ""
        tool = r["tool"] or ""
        out[r["id"]] = {"ai_presenter": (tool == "ugc_video" and ad_type != "voiceover") or tool == "motion_copy",
                        "synthetic_voice": tool == "ugc_video",
                        "ai_generated": tool in ("ugc_video", "motion_copy", "ad_image", "photo_to_video", "video_upscale"),
                        "product": prods.get(r["product_id"], {})}
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


def _age_need(v: str, geo: dict) -> int:
    return 25 if v == "crypto" else 21 if v == "alcohol" and "US" in (geo.get("countries") or []) else \
        18 if v in AGE_18 or v == "alcohol" else 0


def lint(spec, prov: dict = None) -> list:
    """Policy findings for a whole CampaignSpec (texts, keywords, names, URLs and spec-level rules)."""
    p, declared, out = spec.platform, spec.vertical, []
    derived, why = derive_vertical(spec, prov)
    v = _text_vertical(declared, derived)
    verticals = {declared, derived} - {"general"}           # the spec rules run for both
    if derived != "general" and derived != declared and not (derived in HEALTHY and declared in HEALTHY):
        lbl = VERTICAL_LABELS
        out.append(Issue("block" if declared == "general" else "warn", "policy.vertical_mismatch",
                         f"This looks like a {lbl[derived].lower()} ad ({why}), but the product type is set to "
                         f"{lbl.get(declared, declared)}. The age and category rules depend on it.",
                         f"Set the product type to {lbl[derived]}.", "vertical"))
    sub = set(SUBJECT_RULES)
    if spec.name:
        out += lint_text(spec.name, platform=p, vertical=v, where="name", only=sub)
    for i, c in enumerate(spec.creatives):
        ai = bool(c.ai_presenter or c.synthetic_voice)
        for fld in TEXT_FIELDS:
            out += lint_text(getattr(c, fld), platform=p, vertical=v, ai=ai, where=f"creatives[{i}].{fld}",
                             claims_substantiated=spec.claims_substantiated)
        out += lint_text(c.name, platform=p, vertical=v, where=f"creatives[{i}].name", only=sub)
        for fld in ("destination_url", "utm"):
            raw = getattr(c, fld) or ""
            out += lint_text(url_words(raw), platform=p, vertical=v, where=f"creatives[{i}].{fld}", only=sub)
            if compliance.hidden_chars(raw) and not any(i2.where == f"creatives[{i}].{fld}" and
                                                        i2.rule_id == "policy.hidden_characters" for i2 in out):
                out.append(_issue("policy.hidden_characters", p, f"creatives[{i}].{fld}"))
        if p == "tiktok" and EMOJI.search(c.primary_text or ""):
            out.append(_issue("policy.emoji", p, f"creatives[{i}].primary_text"))
        if ai and not spec.ai_disclosure:
            out.append(_issue("policy.ai_disclosure_missing", p, f"creatives[{i}]"))
        elif (prov or {}).get(c.asset_id, {}).get("ai_generated") and not spec.ai_disclosure:
            out.append(_issue("policy.ai_generated_disclosure", p, f"creatives[{i}]"))
    g = spec.google or {}
    for key in ("headlines", "descriptions", "long_headlines"):
        for i, t in enumerate(g.get(key) or []):
            out += lint_text(t, platform=p, vertical=v, where=f"google.{key}[{i}]",
                             claims_substantiated=spec.claims_substantiated)
    if g.get("business_name"):
        out += lint_text(g["business_name"], platform=p, vertical=v, where="google.business_name", only=sub)
    # Keywords: Google refuses drug and political terms (and US housing, credit and job terms need its
    # restrictions). Negative keywords only keep the ad away from a search, so they get the invisible-text check.
    for i, k in enumerate(g.get("keywords") or []):
        out += lint_text(_keyword_text(k), platform=p, vertical=v, where=f"google.keywords[{i}]", only=sub)
    for i, k in enumerate(g.get("negative_keywords") or []):
        out += lint_text(_keyword_text(k), platform=p, vertical=v, where=f"google.negative_keywords[{i}]", only=set())

    aud, geo = spec.audience or {}, spec.geo or {}
    age_min = aud.get("age_min", 18) if isinstance(aud.get("age_min", 18), int) else 18
    need = max([_age_need(x, geo) for x in verticals] or [0])
    if need and age_min < need:
        out.append(Issue("block", "policy.age_restricted", MESSAGES["policy.age_restricted"][0],
                         f"Set the minimum age to {need} or more.", "audience.age_min"))
    if p == "meta":
        cats = set(spec.special_ad_categories or [])
        if any(x in SPECIAL and SPECIAL[x] not in cats for x in verticals):
            out.append(_issue("policy.special_category_missing", p, "special_ad_categories"))
        if cats & {"housing", "credit", "employment"}:
            genders = aud.get("genders") or ["all"]
            narrow = age_min > 18 or (aud.get("age_max", 65) < 65) or genders not in (["all"], []) or geo.get("zips") \
                or any((c or {}).get("radius_km", 25) < 24 for c in geo.get("cities") or [])
            if narrow:
                out.append(_issue("policy.special_category_targeting", p, "audience"))
    if "supplements" in verticals:
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
