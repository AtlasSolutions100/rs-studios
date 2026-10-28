"""Checks a brief before anything is generated.

Direct-sales companies (and the FTC) are strict about what product content may
say. This catches the common mistakes in the words members type; it is a guard
rail, not legal review. Income, earnings and recruiting claims are blocked for
every account. 'block' stops the generation, 'warn' lets it run with a
note shown to the member.
"""
import re

RULES = [
    # (level, pattern, message)
    ("block", r"\b(los[et]|losing|drop|shed|burn)\b[^.]{0,30}\b(weight|pounds|lbs|kilos?|fat|inches|belly)\b",
     "No weight-loss language."),
    ("block", r"\bweight[- ]?loss\b|\bslim(ming)?\b|\bskinny\b|\bfat[- ]burn", "No weight-loss language."),
    ("block", r"\bbefore\s*(and|&|/)\s*after\b", "No before-and-after content."),
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

_COMPILED = [(lvl, re.compile(p, re.I), msg) for lvl, p, msg in RULES]


def check(*texts: str) -> dict:
    """{'ok': bool, 'blocks': [msg], 'warnings': [msg]} for the combined text."""
    text = " ".join(t for t in texts if t)
    blocks, warns = [], []
    for lvl, rx, msg in _COMPILED:
        if rx.search(text):
            (blocks if lvl == "block" else warns).append(msg)
    dedupe = lambda xs: list(dict.fromkeys(xs))
    return {"ok": not blocks, "blocks": dedupe(blocks), "warnings": dedupe(warns)}
