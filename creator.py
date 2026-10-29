"""The writing behind Create: angle ideas and scripts for one product and ad
type. Small, structured calls on the fast model, charged at what they cost.

Every line that comes back is checked (compliance.py) and anything that breaks
a rule is dropped before the member sees it. Spoken lines have one more rule:
an AI presenter never speaks as a customer reporting results.
"""
import compliance
import copywriter
import llm
import catalog

ANGLE_TOKENS = (1800, 1200)
SCRIPT_TOKENS = (1800, 1500)
CUSTOMER_VOICE = "AI presenters can't speak as customers or claim results."

SYSTEM = ("You are a direct-response creative strategist who writes short-form video and image ads for small brands, "
          "in the style of the best performing UGC ads: specific, conversational, one idea per ad, a hook that earns "
          "the next three seconds. You only use facts from the product details and approved claims given.\n\n"
          + copywriter.RULES + "\n- Presenters are AI: they talk about the product and why it fits a moment, never as a "
          "customer reporting their own results or a testimonial.")


def product_text(b: dict) -> str:
    lines = [f"Brand: {b.get('name') or '(none)'}", f"Product: {b.get('product') or '(not given)'}"]
    for key, label in (("description", "What it is"), ("price", "Price"), ("offer", "Offer"),
                       ("audience", "Audience"), ("tone", "Brand tone"), ("about", "About the brand")):
        if b.get(key):
            lines.append(f"{label}: {b[key]}")
    lines.append(f"Approved claims (the only claims you may make): {(b.get('claims') or '').replace(chr(10), '; ') or 'none given'}")
    return "\n".join(lines)


def ok(text: str, spoken: bool = False) -> bool:
    chk = compliance.check(text)
    return chk["ok"] and not (spoken and CUSTOMER_VOICE in chk["warnings"])


def estimate_angles() -> int:
    return llm.estimate_credits(*ANGLE_TOKENS, model_id=llm.fast_model())


def estimate_script() -> int:
    return llm.estimate_credits(*SCRIPT_TOKENS, model_id=llm.fast_model())


ANGLES_SCHEMA = {"type": "object", "properties": {"angles": {"type": "array", "items": {
    "type": "object", "properties": {
        "title": {"type": "string", "description": "2-6 words"},
        "hook": {"type": "string", "description": "An opening line for the first 3 seconds"},
        "insight": {"type": "string", "description": "Why someone in the audience cares, one sentence"},
        "audience": {"type": "string", "description": "The specific slice of the audience"}},
    "required": ["title", "hook", "insight"]}}}, "required": ["angles"]}


def angles(b: dict, ad_type: str, n: int = 5, avoid=(), usage: llm.Usage | None = None) -> list[dict]:
    """n distinct angle ideas with a hook each. Raises llm.LLMError."""
    at = catalog.AD_TYPE_BY_ID.get(ad_type) or catalog.AD_TYPES[0]
    prompt = (product_text(b) + f"\n\nAd format: {at['name']} ({at['line']})\n"
              f"Give {n} distinct angles: different reasons to care, moments of use, objections answered or comparisons "
              "with what people use today. Each with a scroll-stopping hook line written for this format.")
    if avoid:
        prompt += "\nAlready used, don't repeat: " + "; ".join(list(avoid)[:12])
    out = llm.structured(system=SYSTEM, prompt=prompt, schema=ANGLES_SCHEMA, name="submit_angles",
                         max_tokens=ANGLE_TOKENS[1], usage=usage, temperature=0.9, model_id=llm.fast_model())
    res = []
    spoken = at["script"] == "spoken"
    for a in out.get("angles") or []:
        if not isinstance(a, dict):
            continue
        item = {k: " ".join(str(a.get(k) or "").split())[:240] for k in ("title", "hook", "insight", "audience")}
        item["title"] = item["title"][:80]
        if item["title"] and ok(" ".join(item.values()), spoken):
            res.append(item)
    return res[:n]


SCRIPT_SCHEMA = {
    "spoken": {"type": "object", "properties": {
        "hooks": {"type": "array", "items": {"type": "string"}, "description": "Alternative opening lines"},
        "body": {"type": "string", "description": "The rest of the spoken script, after the hook"},
        "on_screen": {"type": "array", "items": {"type": "string"}, "description": "Short on-screen captions"}},
        "required": ["hooks", "body"]},
    "image": {"type": "object", "properties": {
        "headlines": {"type": "array", "items": {"type": "string"}},
        "concept": {"type": "string", "description": "The image as a concrete scene"}},
        "required": ["headlines", "concept"]},
    "scene": {"type": "object", "properties": {
        "scenes": {"type": "array", "items": {"type": "string"}, "description": "Each a concrete scene for a short clip"}},
        "required": ["scenes"]},
}


def script(b: dict, ad_type: str, angle: dict, duration: int = 10, n: int = 3,
           usage: llm.Usage | None = None) -> dict:
    """{items, body, on_screen} for one angle. `items` are the variants the
    member picks from: hooks (spoken), headlines (image) or scenes (video).
    Raises llm.LLMError."""
    at = catalog.AD_TYPE_BY_ID.get(ad_type) or catalog.AD_TYPES[0]
    kind = at["script"]
    head = product_text(b) + (f"\n\nAngle: {angle.get('title')}\nWhy it matters: {angle.get('insight') or ''}\n"
                              f"For: {angle.get('audience') or b.get('audience') or 'the product audience'}\n"
                              f"Starting hook idea: {angle.get('hook') or ''}\n")
    if kind == "spoken":
        words = catalog.max_words(duration)
        who = "a narrator over product shots" if ad_type == "voiceover" else "an on-camera presenter, selfie-style"
        prompt = (head + f"\nWrite a {duration}-second spoken script for {who}. Give {n} alternative hooks (each under 12 "
                  f"words) and one body that follows any of them. Hook + body together must be at most {words} words, "
                  "said out loud at a natural pace. End with a simple call to action. Plain spoken English, no stage "
                  "directions, no emojis, no hashtags. Also give 2-3 short on-screen captions.")
    elif kind == "image":
        prompt = (head + f"\nWrite {n} alternative headlines (each under 8 words) for an image ad, and one concrete "
                  "image concept: the scene, the light, where the product sits, with the product's label and colors "
                  "exactly as in the photo and room for the headline.")
    else:
        prompt = (head + f"\nWrite {n} alternative scenes for a {duration}-second product clip made from one product "
                  "photo: what happens, the setting, the light and the camera move. The product's label and colors "
                  "stay exactly as in the photo. No people's faces, no text.")
    out = llm.structured(system=SYSTEM, prompt=prompt, schema=SCRIPT_SCHEMA[kind], name="submit_script",
                         max_tokens=SCRIPT_TOKENS[1], usage=usage, temperature=0.8, model_id=llm.fast_model())
    clean = (lambda x: " ".join(str(x or "").split()))
    spoken = kind == "spoken"
    if kind == "spoken":
        items, body = [clean(h)[:200] for h in out.get("hooks") or []], clean(out.get("body"))[:1200]
    elif kind == "image":
        items, body = [clean(h)[:120] for h in out.get("headlines") or []], clean(out.get("concept"))[:1500]
    else:
        items, body = [clean(s)[:900] for s in out.get("scenes") or []], ""
    if body and not ok(body, spoken):
        body = ""
    items = [i for i in items if i and ok(i, spoken)][:max(1, n)]
    on_screen = [clean(t)[:80] for t in out.get("on_screen") or [] if ok(clean(t))][:4]
    return {"items": items, "body": body, "on_screen": on_screen}
