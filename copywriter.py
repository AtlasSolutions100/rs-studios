"""Copy studio and content optimizer: the prompts, the output shape, and the
checks every piece of generated copy goes through (platform lengths and
compliance.py). The planner calls these too.
"""
import re

import compliance
import llm
import platforms

RULES = """Rules every line must follow (they're enforced after you write, and anything that breaks them is thrown out):
- No income, earnings, pay, lifestyle or "be your own boss" claims. Sell the product, never an opportunity.
- No recruiting or business-opportunity language ("join my team", "become a rep", "ground floor").
- No health, disease, cure, treatment or weight-loss claims, and no before-and-after framing. Only use product claims
  from the approved list; if there is none, describe the product plainly without claims.
- No discounts, percent-off, coupons, promo codes or free-shipping offers unless the brief's offer says so explicitly.
- No fake testimonials or reviews; never write as a customer describing personal results ("I lost", "my results").
- No guarantees or "clinically proven" unless that exact wording is in the approved claims.
- Plain, specific language. No ALL CAPS sentences, at most one exclamation mark per variant."""

SYSTEM = ("You are Campaignify's senior direct-response copywriter. You write short, specific, platform-native "
          "marketing copy for small brands and independent sellers. You know how each ad platform displays text "
          "and you write to its lengths.\n\n" + RULES)

GOALS = {"awareness": "Awareness", "traffic": "Traffic to a page", "leads": "Leads / sign-ups",
         "sales": "Sales", "engagement": "Engagement", "installs": "App installs"}
TONES = ["Friendly", "Bold", "Calm and premium", "Playful", "Straight-talking", "Expert"]

VARIANTS_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "angle": {"type": "string", "description": "The idea behind this variant, a few words."},
                    "fields": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "text": {"type": "string"}},
                        "required": ["name", "text"]}},
                },
                "required": ["angle", "fields"],
            },
        },
    },
    "required": ["variants"],
}


def brief_text(brand: dict, brief: dict) -> str:
    lines = [f"Brand: {brand.get('name') or '(none)'}",
             f"Product: {brand.get('product') or brief.get('offer') or '(not given)'}",
             f"Approved claims (the only claims you may make): {brand.get('claims') or 'none given'}",
             f"Offer / what we're promoting: {brief.get('offer') or brand.get('product') or '(not given)'}",
             f"Audience: {brief.get('audience') or brand.get('audience') or '(not given)'}",
             f"Goal: {GOALS.get(brief.get('goal'), brief.get('goal') or 'Sales')}",
             f"Tone: {brief.get('tone') or 'Friendly'}"]
    if brief.get("link"):
        lines.append(f"Destination link: {brief['link']}")
    if brief.get("notes"):
        lines.append(f"Notes from the marketer: {brief['notes']}")
    return "\n".join(lines)


def field_spec(kind: str, platform: str) -> str:
    out = []
    for name, rec, hard in platforms.fields_for(kind, platform):
        bits = []
        if rec:
            bits.append(f"aim for {rec} characters or fewer")
        if hard and hard != rec:
            bits.append(f"never over {hard}")
        elif hard:
            bits.append("hard limit")
        out.append(f'- "{name}"' + (f" ({', '.join(bits)})" if bits else ""))
    return "\n".join(out)


def estimate(n: int, kind: str) -> int:
    """Credits quote for a copy run (worst case at the output budget)."""
    return llm.estimate_credits(1800, _max_tokens(n, kind))


def _max_tokens(n: int, kind: str) -> int:
    per = {"landing": 900, "email": 600}.get(kind, 260)
    return min(6000, 400 + per * n)


def write(brand: dict, kind: str, platform: str, brief: dict, n: int = 3, usage: llm.Usage | None = None) -> list[dict]:
    """n variants of `kind` for `platform`, each checked. Raises llm.LLMError."""
    kind = kind if kind in platforms.KINDS else "ad"
    platform = platform if platform in platforms.PLATFORMS else "meta"
    pname = platforms.PLATFORMS[platform]["name"]
    prompt = (f"Write {n} distinct variants of {platforms.KINDS[kind]['name'].lower()} for {pname}.\n"
              f"{platforms.KINDS[kind]['blurb']}\n\n{brief_text(brand, brief)}\n\n"
              f"Each variant has exactly these fields, with these names:\n{field_spec(kind, platform)}\n\n"
              "Give each variant a different angle (for example: problem/solution, a specific moment of use, "
              "a curiosity gap, a direct benefit, social context without fake reviews). "
              + ("Hashtags go in their own field, space-separated. " if kind == "captions" else "")
              + ("Write the FAQ as 3 short question-and-answer pairs. " if kind == "landing" else ""))
    data = llm.structured(system=SYSTEM, prompt=prompt, schema=VARIANTS_SCHEMA, name="submit_copy",
                          max_tokens=_max_tokens(n, kind), usage=usage)
    return [check_variant(v, kind, platform) for v in (data.get("variants") or [])[:n]]


def check_variant(v: dict, kind: str, platform: str) -> dict:
    fields = platforms.length_report([f for f in (v.get("fields") or []) if isinstance(f, dict)], kind, platform)
    text = " ".join(f["text"] for f in fields)
    c = compliance.check(text)
    return {"angle": str(v.get("angle") or "")[:120], "fields": fields, "compliance": c,
            "ok": c["ok"] and not any(f["state"] == "over" for f in fields)}


# ---------------------------------------------------------------- optimizer

CTA_RX = re.compile(r"\b(shop|buy|order|get|try|grab|start|book|sign up|subscribe|download|learn more|see|discover|"
                    r"tap|click|visit|join the waitlist|claim|reserve|watch|read|find out|apply|request|call|dm)\b", re.I)
EMOJI_RX = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def quick_checks(text: str, platform: str = "meta") -> dict:
    """Free, deterministic checks: length, hook, CTA, readability, hashtags,
    emoji, compliance. Scores 0-10."""
    p = platforms.PLATFORMS.get(platform) or platforms.PLATFORMS["meta"]
    text = (text or "").strip()
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
    words = re.findall(r"[A-Za-z0-9'$%]+", text)
    sentences = [s for s in re.split(r"[.!?]+\s|\n+", text) if s.strip()]
    syl = sum(_syllables(w) for w in words)
    wps = len(words) / max(1, len(sentences))
    ease = 206.835 - 1.015 * wps - 84.6 * (syl / max(1, len(words))) if words else 0
    hashtags = re.findall(r"#\w+", text)
    emoji = EMOJI_RX.findall(text)
    first_words = len(first.split())
    rec = p["ad_fields"][0][1] or p.get("caption") or 300
    items = []

    hook = 5
    if first_words and first_words <= 12:
        hook += 2
    elif first_words > 20:
        hook -= 2
        items.append(("warn", "The first line is long. Put the hook in the first 12 words."))
    if re.match(r"^(you|your|why|how|what|stop|this|the \d|\d)", first, re.I) or first.endswith("?"):
        hook += 2
    if re.search(r"\d", first):
        hook += 1
    if first.isupper() and len(first) > 8:
        hook -= 2
        items.append(("warn", "Avoid all-caps lines; they read as shouting and some platforms reject them."))

    cta = 8 if CTA_RX.search(text) else 3
    if cta < 5:
        items.append(("warn", "No clear call to action. Tell people the one thing to do next."))

    clarity = 5 + (2 if ease >= 60 else -1 if ease < 40 else 0) + (2 if wps <= 16 else -1 if wps > 25 else 0)
    if wps > 25:
        items.append(("warn", f"Long sentences (about {wps:.0f} words each). Shorter ones read faster on a phone."))

    fit = 8
    if len(text) > rec * 1.6:
        fit -= 3
        items.append(("warn", f"{len(text)} characters. On {p['short']}, about {rec} show before the text is cut off."))
    lo, hi = p["hashtags"]
    if len(hashtags) > max(hi, 0) + 2:
        fit -= 2
        items.append(("warn", f"{len(hashtags)} hashtags. {hi or 'None'} is plenty on {p['short']}."))
    if hi == 0 and hashtags:
        fit -= 1
        items.append(("info", f"Hashtags don't do much on {p['name']}."))
    if len(emoji) > 4:
        fit -= 1
        items.append(("info", "Lots of emoji. One or two is usually enough."))

    c = compliance.check(text)
    for m in c["blocks"]:
        items.append(("block", m))
    for m in c["warnings"]:
        items.append(("warn", m))
    clamp = lambda x: max(0, min(10, int(round(x))))
    scores = {"hook": clamp(hook), "clarity": clamp(clarity), "cta": clamp(cta), "fit": clamp(fit)}
    overall = sum(scores.values()) / 4
    if not c["ok"]:
        overall = min(overall, 3)
    return {"scores": scores, "overall": round(overall, 1), "items": items, "compliance": c,
            "stats": {"chars": len(text), "words": len(words), "reading_ease": round(ease), "hashtags": len(hashtags),
                      "emoji": len(emoji), "first_line_words": first_words}}


OPT_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "object", "properties": {
            "hook": {"type": "integer"}, "clarity": {"type": "integer"}, "cta": {"type": "integer"},
            "fit": {"type": "integer"}}, "required": ["hook", "clarity", "cta", "fit"]},
        "summary": {"type": "string", "description": "Two sentences: what works and the biggest fix."},
        "issues": {"type": "array", "items": {"type": "string"}},
        "rewrites": {"type": "array", "items": {"type": "object", "properties": {
            "angle": {"type": "string"}, "text": {"type": "string"}}, "required": ["angle", "text"]}},
    },
    "required": ["scores", "summary", "issues", "rewrites"],
}


def optimize_estimate() -> int:
    return llm.estimate_credits(2500, 1800)


def optimize(text: str, platform: str, goal: str = "sales", brand: dict | None = None,
             use_ai: bool = True, usage: llm.Usage | None = None) -> dict:
    """Quick checks always; plus an AI review and three rewrites when the writing
    service is on and `use_ai`."""
    platform = platform if platform in platforms.PLATFORMS else "meta"
    result = {"quick": quick_checks(text, platform), "ai": None}
    if not (use_ai and llm.configured()):
        return result
    pname = platforms.PLATFORMS[platform]["name"]
    brand = brand or {}
    prompt = (f"Review this {pname} copy for the goal '{GOALS.get(goal, goal)}'. Score 0-10 for: hook (does the first "
              "line stop the scroll), clarity, cta (one clear next step), fit (length, format and tone for the "
              f"platform). List the specific issues, then write 3 improved rewrites that fix them, each with a "
              f"different angle, sized for {pname}.\n\nApproved product claims: {brand.get('claims') or 'none given'}\n"
              f"Product: {brand.get('product') or 'unknown'}\n\nCOPY:\n{text[:6000]}")
    data = llm.structured(system=SYSTEM, prompt=prompt, schema=OPT_SCHEMA, name="submit_review",
                          max_tokens=1800, usage=usage, temperature=0.5)
    sc = data.get("scores") or {}
    rewrites = []
    for rw in (data.get("rewrites") or [])[:3]:
        t = str(rw.get("text") or "")
        rewrites.append({"angle": str(rw.get("angle") or "")[:120], "text": t, "chars": len(t),
                         "compliance": compliance.check(t)})
    result["ai"] = {"scores": {k: max(0, min(10, int(sc.get(k) or 0))) for k in ("hook", "clarity", "cta", "fit")},
                    "summary": str(data.get("summary") or "")[:600],
                    "issues": [str(i)[:300] for i in (data.get("issues") or [])[:8]],
                    "rewrites": rewrites}
    return result
