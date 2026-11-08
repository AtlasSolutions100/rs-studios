"""Checks a brief before anything is generated.

Direct-sales companies (and the FTC) are strict about what product content may
say. This catches the common mistakes in the words members type; it is a guard
rail, not legal review. Income, earnings and recruiting claims are blocked for
every account. 'block' stops the generation, 'warn' lets it run with a
note shown to the member.
"""
import re
import unicodedata

RULES = [
    # (level, pattern, message)
    ("block", r"\b(los[et]|losing|drop|shed|burn|melt(s|ed|ing)?)\b[^.]{0,30}\b(weight|pounds|lbs|kilos?|fat|inches|belly)\b",
     "No weight-loss language."),
    ("block", r"\bweight[- ]?loss\b|\bslim(ming)?\b|\bskinny\b|\bfat[- ]burn", "No weight-loss language."),
    ("block", r"\bbefore\s*-?\s*(and|&|/|vs\.?|->|=>|>|\u2192|\u27f6|\u279c|\u27a1|\u21d2|\u00bb)\s*-?\s*after\b",
     "No before-and-after content.", "negatable"),
    ("block", r"\b(cure[sd]?|treat(s|ment)?|heal(s|ing)?|prevent(s|ion)?|diagnos\w*)\b",
     "No health or disease claims: use only the label's words."),
    ("block", r"\b(diabetes|cancer|anxiety|depression|adhd|arthritis|disease|blood pressure|cholesterol)\b",
     "No health or disease claims: use only the label's words."),
    ("block", r"\b\d{1,2}\s?% off\b|\bdiscount\b|\bcoupon\b|\bpromo code\b|\bfree shipping\b|\bbogo\b",
     "No discounts, coupons or free-shipping offers. Free extras only."),
    # Income, earnings and lifestyle claims: blocked for every account. Most
    # members are independent reps, and the FTC treats these as earnings claims.
    ("block", r"\b(earn(s|ed|ing)?|make|makes|making|made|bring(s|ing)? in|pull(s|ing)? in|clear(ed|ing)?)\b[^.!?]{0,30}"
              r"(\$\s?\d|\d+\s?(k|grand)\b|\b(six|seven|6|7)[- ]figures?|\bmoney\b|\bcash\b|\bincome\b|\ba living\b|\bcommissions?\b)",
     "No income or earnings claims."),
    ("block", r"\b(income|earnings|paychecks?|pay checks?|residuals?|commissions?|side hustle|six[- ]figures?|"
              r"seven[- ]figures?|[67][- ]figures?|financial(ly)? free(dom)?|time freedom|passive income|extra cash|"
              r"quit (your|my) (9[- ]?to[- ]?5|nine[- ]to[- ]five|job)|fire (your|my) boss|retire (early|young)|"
              r"replace (your|my) (income|salary|paycheck)|be your own boss|dream (car|house|home|life)|luxury lifestyle|"
              r"get paid to|work from (home|anywhere) and (earn|make|get paid))\b",
     "No income or earnings claims."),
    ("block", r"\$\s?\d[\d,.]*\s*k?\s*(a|an|per|/)\s*(hour|day|week|month|year)\b[^.]{0,40}\b(income|earn|paid|profit)",
     "No income or earnings claims."),
    ("block", r"\b(join (my|our) team|join us as an?|business opportunity|opportunity call|recruit(s|ing|ment)?|"
              r"downline|upline|sign up under me|enroll(ing)? under me|become an? (isr|rep|representative|distributor|"
              r"affiliate|ambassador|brand partner|partner)|looking for (people|partners|reps|go[- ]getters)|"
              r"ground[- ]floor|start your own business|own your own business|build (a|your) team)\b",
     "No recruiting or business-opportunity content in product creatives."),
    ("warn", r"\b(guarantee[ds]?|clinically|proven|miracle|instant results)\b",
     "Avoid promise words unless they are printed on the label."),
    ("warn", r"\b(i|we) (lost|gained|felt|noticed)\b|\bmy results\b",
     "AI presenters can't speak as customers or claim results."),
]

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


_COMPILED = [(r[0], re.compile(r[1], re.I), r[2], "negatable" in r[3:]) for r in RULES]

# A rule marked "negatable" doesn't fire on a phrase that rules the thing out
# ("no before/after imagery", "without before-and-after shots", "avoid
# before & after"). "No more ..." is not a negation: it promises a change.
_NEGATION = re.compile(r"\b(no|not|never|without|avoid(ing)?|don'?t (use|show|include)|do not (use|show|include)|"
                       r"zero|free of|instead of|rather than)\b(\s+(any|a|an|the|real|actual|explicit|obvious))*\s*$", re.I)


def _negated(text: str, start: int) -> bool:
    before = text[max(0, start - 40):start]
    return bool(_NEGATION.search(before)) and not re.search(r"\bno\s+more\s*$", before, re.I)


def _hits(rx, text: str, negatable: bool) -> bool:
    if not negatable:
        return bool(rx.search(text))
    return any(not _negated(text, m.start()) for m in rx.finditer(text))


def check(*texts: str) -> dict:
    """{'ok': bool, 'blocks': [msg], 'warnings': [msg]} for the combined text.
    The same rules run for every kind of text everywhere (headlines, concepts,
    scripts, briefs); callers check each piece the member sees on its own."""
    text = normalize(" ".join(t for t in texts if t))
    blocks, warns = [], []
    for lvl, rx, msg, negatable in _COMPILED:
        if _hits(rx, text, negatable):
            (blocks if lvl == "block" else warns).append(msg)
    dedupe = lambda xs: list(dict.fromkeys(xs))
    return {"ok": not blocks, "blocks": dedupe(blocks), "warnings": dedupe(warns)}


def findings(*texts: str) -> list:
    """[(level, rule id, message)] for every rule the combined text breaks, in RULES order.
    The same rules and wording as check(); used where a caller needs to know which rule hit."""
    text = normalize(" ".join(t for t in texts if t))
    return [(lvl, IDS[i], msg) for i, (lvl, rx, msg, negatable) in enumerate(_COMPILED) if _hits(rx, text, negatable)]


def negated(text: str, start: int) -> bool:
    """Is the match at `start` ruled out by a negation just before it ("no before/after")?"""
    return _negated(text, start)
