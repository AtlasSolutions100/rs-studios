"""Checks a brief before anything is generated.

Direct-sales companies (and the FTC) are strict about what product content may
say. This catches the common mistakes in the words members type; it is a guard
rail, not legal review. Income, earnings and recruiting claims are blocked for
every account. 'block' stops the generation, 'warn' lets it run with a
note shown to the member.
"""
import contextvars
import re
import unicodedata

# ---------------------------------------------------------------- shared patterns
# adpolicy.py (the launch pre-flight) matches the same claims with these exact lists, so whatever its
# cure, health-outcome, weight-loss, guaranteed-returns and before/after rules catch, generation refuses
# too (tests/test_adpolicy.py checks the two agree on every fixture).
DISEASES = (r"cancer|diabetes|depression|anxiety|arthritis|alzheimer'?s|dementia|covid(-19)?|flu|infections?|diseases?|"
            r"high blood pressure|hypertension|cholesterol|adhd|autism|insomnia|acne|eczema|psoriasis|asthma|migraines?|"
            r"ibs|crohn'?s|herpes|hiv|obesity|heart disease|stroke")
# Health words that turn "treat", "prevent" or "cure" into a health claim even in a context that is usually fine.
SYMPTOMS = (DISEASES + r"|pain|aches?|fatigue|bloating|inflammation|symptoms?|conditions?|illness(es)?|sickness|colds|common cold|"
            r"headaches?|cramps?|nausea|constipation|hair loss|balding|wounds?|injur(y|ies)|immune|disorders?|"
            r"blood sugar|a1c|weight")
CURE_CLAIM = [
    r"\b(cures?|cured|curing|treats?|treated|treating|heals?|healed|healing|prevents?|prevented|reverses?|reversed|"
    r"eliminates?|eliminated|gets? rid of|fights?|fight off|wards? off|relieves?|relief from)\b[^.!?\n]{0,40}?\b(" +
    DISEASES + r")\b",
    r"\b(cure|remedy|remedies|treatment|relief) for (\w+\s+){0,2}?(" + DISEASES + r")\b",
    r"\b(" + DISEASES + r")\b[^.!?\n]{0,30}?\b(gone|cured|healed|reversed|disappeared|vanished)\b",
]
_WORDNUM = r"(\d+(\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|a full|a whole)"
_UNITS = r"(lbs?|pounds?|kg|kilos?|inches|in\.|(dress |jean |pant |clothing )?sizes?|% body fat)"
_ARROWS = r"\s*(->|-->|=>|>|\u2192|\u27f6|\u279c|\u27a1\ufe0f?|\u21d2|\u27f9|\u00bb)\s*"
_TO = r"\s*(to|down to|->|-->|=>|>|\u2192|\u27f6|\u279c|\u27a1\ufe0f?|\u21d2|\u27f9|\u00bb)\s*"
# "size 16 to size 8": a first number bigger than the second (10 to 39 down to a smaller one)
_SMALLER = (r"size\s+([1-3]\d{to}(a\s+)?(size\s+)?\d|[2-3]\d{to}(a\s+)?(size\s+)?1\d|3\d{to}(a\s+)?(size\s+)?2\d)\b")
WEIGHT_OUTCOME = [
    r"\b(lose|lost|losing|drop|dropped|dropping|shed|shedding|burn|burned|melt|melted|cut(?!\s+\S+\s*(inches|in\.))|"
    r"down)\s+(up to\s+|over\s+|nearly\s+)?"
    + _WORDNUM + r"\s*" + _UNITS + r"(?!\s+(of|bag|pack|box|tin)s?\b)\b",
    r"\b\d+(\.\d+)?\s*(lbs?|pounds?|kg|kilos?|inches|sizes?)\s+(down\s+)?(in|within|after)\s+(just\s+)?\d+\s*(days?|weeks?|months?)\b",
    r"\b\d+(\.\d+)?\s*(lbs?|pounds?|kg|kilos?)\s+(down|gone|lighter|lost|off)\b",
    r"\b(down|lost|dropped|minus)\s+" + _WORDNUM + r"\s*" + _UNITS + r"\b",
    r"\b(melts?|melted|melting|burns?|burning|torch(es)?|blasts?|dissolves?)\s+(away\s+)?(stubborn\s+)?(belly|body|"
    r"tummy|stomach|thigh|arm)?\s*fat\b",
    r"\bfat[- ](melting|melter|burning|burner|blasting|torching|dissolving)\b",
    r"\bburns?\s+(up to\s+|over\s+)?\d+\s*(extra\s+)?(calories|kcal|cals)\b",
    r"\bflat (tummy|belly|stomach|abs)\s+(in|within|by)\b",
    r"\blose (the|your|that|those|this) (belly|gut|muffin top|love handles|baby weight|weight|pounds|fat)\b[^.!?\n]{0,30}?"
    r"\b(in|within|by)\b",
    # a smaller clothing size: "from a size 16 to a size 8", "size 14 -> size 6" ("sizes 2 to 16" is a range)
    r"\bfrom\s+(a\s+)?" + _SMALLER.format(to=_TO), r"\b" + _SMALLER.format(to=_ARROWS),
]
HEALTH_OUTCOME = [
    r"\b(lower(ed|s)?|reduce[ds]?|cut|drop(ped|s)?)\s+(your\s+|my\s+)?(blood sugar|blood pressure|cholesterol|a1c|bmi)\s+"
    r"(by\s+)?\d+",
    r"\b\d+(\.\d+)?\s*%\s+(less|fewer|lower|reduction in|drop in)\s+(pain|anxiety|stress|body fat|fat|wrinkles|acne|bloating|"
    r"cravings|joint pain|inflammation)\b",
]
_RATE = r"\d+(\.\d+)?\s*(%|percent|pct)"
_PERIOD = (r"(returns?|profits?|roi|gains|yields?|interest|payouts?|apy|(a|per|each|every) (day|week|month|year)|daily|"
           r"weekly|monthly|yearly|annually|annual)")
GUARANTEED_RETURNS = [
    r"\bguarantee(d|s)?\s+([\w$%.,+-]+\s+){0,3}?(profits?|income|gains|yields?|interest|payouts?|roi|dividends?)\b",
    r"\bguarantee(d|s)?\b[^.!?\n]{0,30}?" + _RATE + r"\s+(\w+\s+){0,2}?" + _PERIOD + r"\b",
    _RATE + r"\s+(\w+\s+){0,2}?" + _PERIOD + r"\b[^.!?\n]{0,25}?\bguarantee(d|s)?\b",
    r"\b(returns?|profits?|roi|gains|payouts?)\s+(are\s+|is\s+)?guarantee(d|s)?\b",
    r"\brisk[- ]free\s+(\S+\s+){0,2}?(investment|returns?|profits?|trading|income|gains|roi|yields?)\b",
    r"\b(can'?t|cannot|won'?t|never) lose (money|a (dime|cent))\b|\bdouble your (money|investment|crypto)\b",
    r"\bno risk\b[^.!?\n]{0,30}?\b(invest\w*|trading|returns?|profits?)\b",
]
# "before -> after", "before | after", "B4 and after", "Before: dull. After: glowing." Any run of symbols
# (arrows, emoji, bars, dashes) between the two words counts; commas and full stops don't ("before work, after").
_BA_JOIN = r"(and|&|n|'n'|vs\.?|versus|to|then|\+|[^\w\s,.;!?()'\"]{1,8})"
BEFORE_AFTER = [r"\b(before|b4)\s*-?\s*" + _BA_JOIN + r"\s*-?\s*afters?\b", r"\bbefore\s*:[^\n]{1,60}?\bafter\s*:"]
# "before and after school", "before and after each use": a time, not before/after pictures
_BA_TIME = re.compile(r"^\s*(the |your |a |an |each |every |our |their |any |all |big )*(construction|renovations?|remodels?|"
                      r"moves?|moving|move-ins?|move-outs?|work|school|class(es)?|shifts?|meals?|dinner|lunch|breakfast|"
                      r"brunch|events?|parties|party|weddings?|games?|gym|workouts?|training|practice|surgery|procedures?|"
                      r"sessions?|storms?|holidays?|trips?|travel|flights?|sunrise|sunset|hours|care|day|use|bed|bedtime|"
                      r"swim(ming)?|showers?|baths?|closing|opening|the (show|game|match|race))\b(?!\s+(photos?|pics?|"
                      r"pictures?|shots?|images?|results?|selfies?|videos?))", re.I)


def before_after_time(text: str, end: int) -> bool:
    """Is the before/after that ends at `end` about time ("before and after school"), not pictures?"""
    return bool(_BA_TIME.match(text[end:end + 60]))


# First-person customer voice: an AI presenter saying it owns, uses or was changed by the product. Wider than results
# verbs: "I've been using this for a month", "I take this mug on every hike", "my skin", "changed my life", "I swear
# by it". Habitual use needs the product as the object ("this", "it", "mine", "my ...") AND a how-often word, so "I'll
# show you this" or "I use a kettle" stay fine. "We" is left to the results verbs only: a brand may say "we use".
_USE = (r"(?:take|use|drink|wear|bring|carry|keep|pack|apply|eat|grab|put|throw|toss|reach for|rely on|cook with|"
        r"sleep (?:on|with)|start (?:my|every|each))")
_OFTEN = (r"(?:every(?:day|where)?|each|daily|always|all the time|whenever|nightly|twice a|once a|"
          r"(?:first|last) thing)")
_OBJ = r"(?:this|it|these|them|mine|my)"
CUSTOMER_VOICE_RX = "|".join([
    r"\b(i|we) (lost|gained|felt|noticed)\b|\bmy results\b",
    r"\bi(?:['\u2019]ve| have) (?:been |now |only |just |never |always )*(?:using|taking|drinking|wearing|trying|"
    r"tried|used|taken|worn|owned|had (?:this|it|these|mine|my)|bought|ordered|switched|loved|relied)\b",
    r"\bi(?:['\u2019]ve| have) been on (?:this|it|these)\b",
    r"\bi (?:always |still |only |now |just |never )?" + _USE + r" " + _OBJ + r"\b[^.!?\n]{0,60}\b" + _OFTEN + r"\b",
    r"\b" + _OFTEN + r"\b[^.!?\n]{0,30}\bi (?:always |still |only |now |just )?" + _USE + r" " + _OBJ + r"\b",
    r"\bi (?:always|never|still) " + _USE + r" " + _OBJ + r"\b",
    r"\bmy (?:skin|hair|face|scalp|body|gut|sleep|energy|joints?|life|own results|experience with)\b",
    r"\bchanged my (?:life|skin|hair|mornings?|routine|mind)\b|\bi swear by\b|\bi can['\u2019]?t live without\b|"
    r"\bi(?:['\u2019]m| am) (?:obsessed|hooked|a (?:customer|convert|believer|fan))\b|"
    r"\bi (?:bought|ordered|purchased|own|switched to|started (?:using|taking|drinking|wearing)|"
    r"tried (?:this|it|these)|(?:honestly |really |absolutely )?love (?:this|it|these|mine|my))\b|"
    r"\b(?:game[- ]changer|life[- ]saver) for me\b|\bworked for me\b|\bas a (?:customer|user|mom who uses)\b",
])

DISCOUNT_MSG = ("No discounts, coupons or free shipping unless you've stated the offer: say it in your request or save "
                "it as the product's offer.")

RULES = [
    # (level, pattern, message[, flags])
    ("block", r"\b(los[et]|losing|drop|shed|burn|melt(s|ed|ing)?)\b[^.]{0,30}\b(weight|pounds|lbs|kilos?|fat|inches|belly)\b|"
              + "|".join(WEIGHT_OUTCOME), "No weight-loss language."),
    ("block", r"\bweight[- ]?loss\b|\bslim(ming)?\b|\bskinny\b|\bfat[- ]burn|\bappetite suppress\w*|"
              r"\bsuppress(es)? (your )?appetite\b|\bcurbs? (your )?(appetite|cravings)\b", "No weight-loss language."),
    ("block", "|".join(BEFORE_AFTER), "No before-and-after content.", "negatable", "time"),
    ("block", r"\b(cure[sd]?|treat(s|ments?)?|heal(s|ing)?|prevent(s|ion)?|diagnos\w*|remed(y|ies))\b|" + "|".join(CURE_CLAIM + HEALTH_OUTCOME),
     "No health or disease claims: use only the label's words.", "benign"),
    ("block", r"\b(diabetes|cancer|anxiety|depression|adhd|arthritis|disease|blood pressure|cholesterol)\b",
     "No health or disease claims: use only the label's words."),
    # An offer is the member's to make, never ours to invent (Koby, Nov 11 2026): a discount, coupon or free shipping
    # passes when the member stated it, in the same terms (the "stated" flag, _Stated below); anything else is refused.
    ("block", r"\b\d{1,2}\s?% off\b|\bdiscount\b|\bcoupon\b|\bpromo code\b|\bfree shipping\b|\bbogo\b", DISCOUNT_MSG,
     "stated"),
    # Income, earnings and lifestyle claims: blocked for every account. Most
    # members are independent reps, and the FTC treats these as earnings claims.
    ("block", r"\b(earn(s|ed|ing)?|make|makes|making|made|bring(s|ing)? in|pull(s|ing)? in|clear(ed|ing)?)\b[^.!?]{0,30}"
              r"(\$\s?\d|\d+\s?(k|grand)\b|\b(six|seven|6|7)[- ]figures?|\bmoney\b|\bcash\b|\bincome\b|\ba living\b|\bcommissions?\b)",
     "No income or earnings claims."),
    ("block", r"\b(income|earnings|paychecks?|pay checks?|residuals?|commissions?|side hustle|six[- ]figures?|"
              r"seven[- ]figures?|[67][- ]figures?|financial(ly)? free(dom)?|time freedom|passive income|extra cash|"
              r"quit (your|my) (9[- ]?to[- ]?5|nine[- ]to[- ]five|job)|fire (your|my) boss|retire (early|young)|"
              r"replace (your|my) (income|salary|paycheck)|be your own boss|dream (car|house|home|life)|luxury lifestyle|"
              r"get paid to|work from (home|anywhere) and (earn|make|get paid))\b|" + "|".join(GUARANTEED_RETURNS),
     "No income or earnings claims."),
    ("block", r"\$\s?\d[\d,.]*\s*k?\s*(a|an|per|/)\s*(hour|day|week|month|year)\b[^.]{0,40}\b(income|earn|paid|profit)",
     "No income or earnings claims."),
    ("block", r"\b(join (my|our) team|join us as an?|business opportunity|opportunity call|recruit(s|ing|ment)?|"
              r"downline|upline|sign up under me|enroll(ing)? under me|become an? (isr|rep|representative|distributor|"
              r"affiliate|ambassador|brand partner|partner)|looking for (people|partners|reps|go[- ]getters)|"
              r"ground[- ]floor|start your own business|own your own business|build (a|your) team)\b",
     "No recruiting or business-opportunity content in product creatives."),
    ("warn", r"\b(guarantee[ds]?|clinically|proven|miracle|instant results)\b",
     "Avoid promise words unless they are printed on the label.", "benign"),
    ("warn", CUSTOMER_VOICE_RX, "AI presenters can't speak as customers or claim results."),
]

# Everyday senses of the health and promise words: "treat yourself", "dog treats", "lawn treatment",
# "pest prevention", "prevents leaks", "dry-cured ham", "color-treated hair", "satisfaction guaranteed".
# They are skipped only when the sentence has no health word in it ("hair treatment for hair loss" still blocks).
_BENIGN = [
    re.compile(r"\btreat(s)?\b(?=\s+(yourself|yourselves|yo ?self|someone|them|her|him|everyone|the (family|kids|team|crew)|"
               r"your(self| (family|friends|team|guests|kids|mom|dad|pup|dog|cat|skin|hair|feet|lips|taste ?buds|senses|crew|"
               r"loved ones|customers|clients)))\b)", re.I),
    re.compile(r"(\b(a|sweet|tasty|little|special|holiday|dog|cat|pet|perfect|frozen|weekend|yummy|crunchy|chewy|training|"
               r"afternoon|morning|summer|real|healthy|the|our|their|baked|homemade|party|halloween|christmas|easter|"
               r"birthday|chocolate|dessert|cold|icy|savory)\s+treats?|\btreats? (for|from|and|or|in|made|baked|bag|box|jar))\b",
               re.I),
    re.compile(r"\b(spa|hair|lawn|pest|facial|window|scalp|keratin|conditioning|water|stain|floor|wood|termite|mosquito|"
               r"weed|tree|gutter|roof|mold|deck|carpet|nail|brow|lash|body|massage|foot|leather|fabric|heat|surface|"
               r"glass|ceramic|vip|royal|red[- ]carpet)\s+treatments?\b", re.I),
    re.compile(r"\b(colou?r|heat|pressure|chemically|water|uv|kiln)[- ]treated\b|\btreated (wood|lumber|glass|leather|"
               r"cotton|fabric|water)\b", re.I),
    re.compile(r"\b(fire|fall|loss|theft|leak|mold|flood|rust|pest|termite|weed|crime|fraud|spam|ice|dam)\s+prevention\b|"
               r"\bprevent(s|ed|ing)?\s+(\w+\s+){0,2}?(leaks?|clogs?|frizz|breakage|split ends|damage|pests?|mold|rust|"
               r"stains?|spills?|slips?|tangles?|fading|shrinking|pilling|static|odou?rs?|break-?ins?|theft|scratches|"
               r"dents|freezing|burst pipes|water damage|downtime|overflows?|chafing|blisters)\b", re.I),
    re.compile(r"\b(dry|salt|sugar|smoke|smoked|house|home|hickory|honey|maple|wet|brine|cold|uv|led|gel)[- ]?cur(ed|ing|e)\b|"
               r"\bcured (meats?|ham|salami|bacon|salmon|pork|sausages?|egg yolks?|yolks?|olives|lemons|beef|chorizo|"
               r"prosciutto|duck)\b|\bcur(e|ing) time\b", re.I),
    re.compile(r"\b(satisfaction|money[- ]back|price|fit|freshness|lifetime|quality|happiness|\d+[- ](day|year|month))\s+"
               r"guarantee[ds]?\b|\bguaranteed\s+(fresh|delivery|to fit|fit|on[- ]time|by|or|for life|for \d+|"
               r"(on|for) (every|all|each))\b|\bsatisfaction\s+(is\s+)?guaranteed\b", re.I),
]
_HEALTH_NEAR = re.compile(r"\b(" + SYMPTOMS + r")\b", re.I)


def _benign(text: str, m) -> bool:
    """Is this match an everyday use of the word (see _BENIGN), in a sentence with no health word?"""
    a = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, m.end()) for c in ".!?\n") if i >= 0]
    sentence = text[a:min(ends) if ends else len(text)]
    if _HEALTH_NEAR.search(sentence) or re.search(_RATE, sentence):
        return False
    lo, hi = m.start() - a, m.end() - a
    return any(b.start() <= lo and hi <= b.end() or b.start() <= lo < b.end() or lo <= b.start() < hi
               for rx in _BENIGN for b in rx.finditer(sentence))

# A stable id per rule, in RULES order (adpolicy.py reports them as "house.<id>" in the launch
# pre-flight, so a launch never lets through what generation refuses).
IDS = ["weight_loss_phrase", "weight_loss_words", "before_after", "health_claim_words", "disease_words", "discounts",
       "income_claim", "income_words", "income_rate", "recruiting", "promise_words", "ai_customer_voice"]
assert len(IDS) == len(RULES)

# ---------------------------------------------------------------- normalizing before matching (M-4)
# Invisible characters and look-alike letters let "cu\u200bres diab\u0435tes" slip past every rule. So the text is
# matched in a normal form: NFKC (full-width and styled letters to plain ones), format characters removed (Unicode
# category Cf: zero-width spaces and joiners, soft hyphen, bidi controls, word joiner, BOM), accents dropped, and the
# common Cyrillic and Greek look-alikes mapped to the Latin letters they imitate. Only the matching uses it.
CONFUSABLES = str.maketrans({
    # Cyrillic
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y", "\u0445": "x",
    "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d", "\u04bb": "h", "\u04cf": "l", "\u051b": "q",
    "\u051d": "w", "\u0432": "b", "\u043a": "k", "\u043c": "m", "\u043d": "h", "\u0442": "t", "\u0457": "i",
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H", "\u041e": "O",
    "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X", "\u0423": "Y", "\u0406": "I", "\u0408": "J",
    "\u0405": "S", "\u0417": "3",
    # Greek
    "\u03b1": "a", "\u03bf": "o", "\u03c1": "p", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v",
    "\u03c4": "t", "\u03c5": "u", "\u03c7": "x", "\u03b3": "y", "\u0391": "A", "\u0392": "B", "\u0395": "E",
    "\u0396": "Z", "\u0397": "H", "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O",
    "\u03a1": "P", "\u03a4": "T", "\u03a5": "Y", "\u03a7": "X",
    # Latin look-alikes outside ASCII that NFKC keeps
    "\u0261": "g", "\u0131": "i", "\u0251": "a",
})


def hidden_chars(text: str) -> list:
    """The invisible format characters (Unicode Cf) in a text, except a zero-width joiner inside an emoji."""
    out = []
    for i, ch in enumerate(text or ""):
        if unicodedata.category(ch) != "Cf":
            continue
        if ch == "\u200d" and 0 < i < len(text) - 1 and not (text[i - 1].isalnum() and text[i + 1].isalnum()):
            continue          # emoji sequences (a family, a flag) are joined with it
        out.append(ch)
    return out


def normalize(text: str) -> str:
    """The text as the rules match it: NFKC, no format characters, no accents, look-alikes as Latin."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    t = t.translate(CONFUSABLES)
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", t)


_COMPILED = [(r[0], re.compile(r[1], re.I), r[2], set(r[3:])) for r in RULES]

# A rule marked "negatable" doesn't fire on a phrase that rules the thing out
# ("no before/after imagery", "without before-and-after shots", "avoid
# before & after"). "No more ..." is not a negation: it promises a change.
_NEGATION = re.compile(r"\b(no|not|never|without|avoid(ing)?|don'?t (use|show|include)|do not (use|show|include)|"
                       r"zero|free of|instead of|rather than)\b(\s+(any|a|an|the|real|actual|explicit|obvious))*\s*$", re.I)


def _negated(text: str, start: int) -> bool:
    before = text[max(0, start - 40):start]
    return bool(_NEGATION.search(before)) and not re.search(r"\bno\s+more\s*$", before, re.I)


# ---------------------------------------------------------------- what the member has stated
# A rule marked "stated" doesn't fire on words the member gave us themselves. "20% off" in a headline is fine when
# the member typed it, asked for it, or saved it as the product's offer; it is refused when the model made it up.
# What counts as stated, for one check:
#   * `stated=` from the caller (a saved message being sent, the product page an import read);
#   * state() for work done for a member outside a request (a planner run);
#   * inside a web request: every string the member sent with it (form fields, query, JSON body), and the offers
#     and prices saved on their workspace's products.
# The match is on the offer's own terms: "20% off" passes when "20% off" (or "20 percent off") was stated, "30% off"
# does not. Built only when a "stated" rule matches, once per request.
_STATED = contextvars.ContextVar("compliance_stated", default=())


def state(*texts):
    """For work done for a member outside a request: these texts are what they stated. Returns a token for unstate()."""
    return _STATED.set(_STATED.get() + tuple(str(t) for t in texts if t))


def unstate(token) -> None:
    _STATED.reset(token)


def _squash(s: str) -> str:
    t = re.sub(r"\s+", " ", normalize(s).lower())
    return t.replace(" percent", "%").replace(" per cent", "%").replace(" %", "%")


def _request_strings() -> list:
    """What the member sent with the current web request, and the offers and prices saved on their products."""
    try:
        from flask import g, has_request_context, request
    except ImportError:
        return []
    if not has_request_context():
        return []
    cached = g.get("_stated_strings")
    if cached is not None:
        return cached
    out = []

    def walk(v, depth=0):
        if isinstance(v, str):
            out.append(v[:6000])
        elif isinstance(v, dict) and depth < 6:
            for x in list(v.values())[:300]:
                walk(x, depth + 1)
        elif isinstance(v, (list, tuple)) and depth < 6:
            for x in v[:300]:
                walk(x, depth + 1)

    try:
        walk(list(request.values.values()))
        walk(request.get_json(silent=True))
    except Exception:  # noqa: BLE001  (a body we can't read states nothing)
        pass
    ws = g.get("ws")
    if ws is not None:
        try:
            import db
            with db.connect() as conn:
                for r in conn.execute("SELECT offer, price FROM products WHERE workspace_id=? AND archived=0", (ws["id"],)):
                    out += [r[0] or "", r[1] or ""]
        except Exception:  # noqa: BLE001
            pass
    g._stated_strings = out
    return out


class _Stated:
    def __init__(self, extra=None):
        self.extra, self._blob = extra, None

    def has(self, words: str) -> bool:
        if self._blob is None:
            extra = [self.extra] if isinstance(self.extra, str) else list(self.extra or [])
            self._blob = _squash(" \n ".join(str(x) for x in [*extra, *_STATED.get(), *_request_strings()] if x))
        return _squash(words) in self._blob


def _hits(rx, text: str, flags, stated=None) -> bool:
    if not flags:
        return bool(rx.search(text))
    for m in rx.finditer(text):
        if "stated" in flags and stated is not None and stated.has(m.group(0)):
            continue
        if "negatable" in flags and _negated(text, m.start()):
            continue
        if "time" in flags and before_after_time(text, m.end()):
            continue
        if "benign" in flags and _benign(text, m):
            continue
        return True
    return False


def before_after(text: str) -> bool:
    """Before/after content in an already normalized text (the house rule and adpolicy share it)."""
    return _hits(_COMPILED[IDS.index("before_after")][1], text, {"negatable", "time"})


def check(*texts: str, stated=None) -> dict:
    """{'ok': bool, 'blocks': [msg], 'warnings': [msg]} for the combined text.
    The same rules run for every kind of text everywhere (headlines, concepts,
    scripts, briefs); callers check each piece the member sees on its own.
    `stated`: text (or texts) the member gave that an offer in `texts` may repeat (see "what the member has stated")."""
    text = normalize(" ".join(t for t in texts if t))
    blocks, warns, said = [], [], _Stated(stated)
    for lvl, rx, msg, flags in _COMPILED:
        if _hits(rx, text, flags, said):
            (blocks if lvl == "block" else warns).append(msg)
    dedupe = lambda xs: list(dict.fromkeys(xs))
    return {"ok": not blocks, "blocks": dedupe(blocks), "warnings": dedupe(warns)}


def findings(*texts: str, stated=None) -> list:
    """[(level, rule id, message)] for every rule the combined text breaks, in RULES order.
    The same rules and wording as check(); used where a caller needs to know which rule hit."""
    text = normalize(" ".join(t for t in texts if t))
    said = _Stated(stated)
    return [(lvl, IDS[i], msg) for i, (lvl, rx, msg, flags) in enumerate(_COMPILED) if _hits(rx, text, flags, said)]


def negated(text: str, start: int) -> bool:
    """Is the match at `start` ruled out by a negation just before it ("no before/after")?"""
    return _negated(text, start)
