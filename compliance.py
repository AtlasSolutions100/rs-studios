"""Checks a brief before anything is generated.

Direct-sales companies (and the FTC) are strict about what product content may
say. This catches the common mistakes in the words members type; it is a guard
rail, not legal review. 'block' stops the generation, 'warn' lets it run with a
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
    ("block", r"\b(earn|make|made)\b[^.]{0,25}\$\s?\d|\$\s?\d[\d,]*\s*(a|per)\s*(week|month|day)\b.*\b(income|earn)",
     "No income claims."),
    ("block", r"\b(join my team|be your own boss|financial freedom|passive income)\b",
     "No recruiting content in product creatives."),
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
