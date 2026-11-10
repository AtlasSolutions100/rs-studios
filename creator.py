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
TIGHTEN_TOKENS = (1400, 900)    # one retry when a spoken script comes back too long
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


def estimate_script(with_photo: bool = True, spoken: bool = False) -> int:
    """Most a script costs: the writing call (a product photo for the writer adds
    its tokens; spoken scripts never send one) plus, for a spoken script, the one
    tightening call it may need when it comes back too long."""
    tin, tout = SCRIPT_TOKENS
    if with_photo:
        tin += PHOTO_TOKENS
    if spoken:
        tin, tout = tin + TIGHTEN_TOKENS[0], tout + TIGHTEN_TOKENS[1]
    return llm.estimate_credits(tin, tout, model_id=llm.fast_model())


def estimate_shorten() -> int:
    """Most "Shorten to fit" costs (one call)."""
    return llm.estimate_credits(*TIGHTEN_TOKENS, model_id=llm.fast_model())


def budget_text(duration: int) -> str:
    """The exact length rules the writer gets for a spoken script."""
    b = catalog.word_budget(duration)
    return (f"Length limits (hard; the video is exactly {duration} seconds and speech is about "
            f"{catalog.WORDS_PER_SECOND:g} words per second): each hook at most {b['hook']} words; the body at most "
            f"{b['body']} words; any hook + the body together at most {b['total']} words. Count every word, numbers "
            "included. Shorter is fine; longer gets cut off.")


def over_budget(hooks: list, body: str, duration: int) -> list[str]:
    """What breaks the word budget, in words the writer can act on ([] when it fits)."""
    b, out = catalog.word_budget(duration), []
    for i, h in enumerate(hooks):
        if catalog.words(h) > b["hook"]:
            out.append(f"hook {i + 1} is {catalog.words(h)} words (max {b['hook']})")
    longest = max([catalog.words(h) for h in hooks] or [0])
    if longest + catalog.words(body) > b["total"]:
        out.append(f"the longest hook + body is {longest + catalog.words(body)} words (max {b['total']}, so "
                   f"{longest + catalog.words(body) - b['total']} too many)")
    return out


TIGHTEN_SCHEMA = {"type": "object", "properties": {
    "hooks": {"type": "array", "items": {"type": "string"}, "description": "The hooks, tightened, same order"},
    "body": {"type": "string", "description": "The body, tightened"}}, "required": ["hooks", "body"]}


def tighten(b: dict, ad_type: str, hooks: list, body: str, duration: int, usage: llm.Usage | None = None,
            problems: list | None = None, keep_all: bool = False) -> tuple[list[str], str]:
    """Ask the writer once to cut a spoken script to the budget, then make sure
    it fits: whatever is still over is trimmed at sentence boundaries
    (catalog.fit_spoken). Lines that break a rule are dropped. Raises llm.LLMError
    only if the call itself fails and nothing usable is left."""
    problems = problems if problems is not None else over_budget(hooks, body, duration)
    who = "a narrator over product shots" if ad_type == "voiceover" else "an on-camera presenter, selfie-style"
    prompt = (product_text(b) + f"\n\nThis {duration}-second spoken script for {who} is too long: "
              + "; ".join(problems) + ".\n" + budget_text(duration) + "\nCut it to fit. Keep the idea, the hook "
              "style and the call to action at the end; cut filler, not facts. Don't add claims. Same number of "
              "hooks, same order.\n\nHooks:\n" + "\n".join(f"{i + 1}. {h}" for i, h in enumerate(hooks))
              + f"\n\nBody:\n{body}")
    clean = (lambda x: " ".join(str(x or "").split()))
    try:
        out = llm.structured(system=SYSTEM, prompt=prompt, schema=TIGHTEN_SCHEMA, name="submit_tightened",
                             max_tokens=TIGHTEN_TOKENS[1], usage=usage, temperature=0.4, model_id=llm.fast_model())
        new_hooks = [clean(h)[:200] for h in out.get("hooks") or []]
        new_body = clean(out.get("body"))[:1200]
        if keep_all:
            # one tightened hook per hook typed, in order; a missing or rule-breaking one stays as typed (then cut)
            hooks = [nh if nh and ok(nh, True) else h for h, nh in zip(hooks, new_hooks + [""] * len(hooks))]
        elif new_hooks:
            hooks = [h for h in new_hooks if h and ok(h, True)] or hooks
        if new_body and ok(new_body, True):
            body = new_body
    except llm.LLMError:
        if not hooks and not body:
            raise
    return catalog.fit_spoken(hooks, body, duration, keep_all=keep_all)


def compliance_claims(b: dict) -> list:
    """The product's claims as a list (they never go into an image concept as text)."""
    return [c.strip() for c in (b.get("claims") or "").splitlines() if c.strip()]


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


# The hook types a spoken script's opening lines are written as (id: name, what it is). The writer is asked for a
# different type per hook and says which each one is, so the member picks between kinds of opening, not rewordings.
HOOK_TYPES = {
    "problem_callout": ("Problem callout", "names a specific annoyance the viewer has right now"),
    "bold_claim": ("Bold claim", "a confident statement about the product, from the approved claims only"),
    "question": ("Question", "a direct question the viewer answers in their head"),
    "pov": ("POV", "\"POV:\" or \"when you...\": drops the viewer into a moment of use"),
    "before_after": ("Then and now", "the old way of doing it against the way with the product; a routine or a "
                                     "step, never a body, health or results change"),
    "social_proof": ("Social proof", "why people are talking about it or switching; no invented numbers, reviews "
                                     "or testimonials"),
    "curiosity_gap": ("Curiosity gap", "holds back one detail so they keep watching for it"),
    "demo_first": ("Demo first", "starts on what the product is doing, then explains"),
}


def hook_type_name(type_id: str) -> str:
    return (HOOK_TYPES.get(type_id) or ("",))[0]


def hook_types_text() -> str:
    return "; ".join(f"{k} ({name}: {what})" for k, (name, what) in HOOK_TYPES.items())


def _split_hooks(raw) -> tuple[list, list]:
    """(texts, types) from the writer's hooks: objects {type, text}, or bare strings from an older reply."""
    texts, types = [], []
    for h in raw or []:
        if isinstance(h, dict):
            t, typ = " ".join(str(h.get("text") or "").split())[:200], str(h.get("type") or "")
        else:
            t, typ = " ".join(str(h or "").split())[:200], ""
        texts.append(t)
        types.append(typ if typ in HOOK_TYPES else "")
    return texts, types


SCRIPT_SCHEMA = {
    "spoken": {"type": "object", "properties": {
        "hooks": {"type": "array", "description": "Alternative opening lines, each a different hook type",
                  "items": {"type": "object", "properties": {
                      "type": {"type": "string", "enum": list(HOOK_TYPES), "description": "Which hook type this is"},
                      "text": {"type": "string", "description": "The opening line, as spoken"}},
                      "required": ["type", "text"]}},
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


# The writer never sees the product unless we send its photo, and the image and
# video models copy the product from the photo anyway: describing its packaging
# only adds a chance to get it wrong (a peach bottle written up as "deep teal
# with white type"). With the photo it may say only what it can see; without it,
# nothing about colours, materials or label design.
LOOKS_WITH_PHOTO = ("The attached image is the seller's own photo of this product. Ground the scene in it: pick a "
                    "setting, props and light that suit how it really looks. Refer to the product by name; if you "
                    "mention its packaging or colours at all, describe only what is plainly visible in the photo, "
                    "never guess, and don't restate its label text.")
LOOKS_NO_PHOTO = ("You can't see the product. Never describe its colours, packaging, bottle or box shape, materials, "
                  "label design or typography; call it by its name and say it appears exactly as in the product "
                  "photo.")
IMAGE_TEXT_RULE = ("The concept is the scene only. The only words on the finished image are the headline (plus a "
                   "subline or button only if the seller adds one), so the concept must not ask for any other text: "
                   "no slogans, taglines, callouts, claims, feature lists, badges, stickers, prices, buttons or "
                   "\"shop now\", and no quoted words.")
PHOTO_TOKENS = 1100     # a product photo sent to the writer, downscaled to at most 768 px


def photo_for_writer(path: str) -> tuple[bytes, str] | None:
    """(JPEG bytes, media type) of a product photo, small enough for the
    writer's vision input; None if it can't be read."""
    try:
        import io
        import imagesafe
        im = imagesafe.thumbnail(path, (768, 768), mode="RGB")   # draft decode, shrink, then convert (H-1)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 — no photo is fine; the writer is told it can't see the product
        return None


def script(b: dict, ad_type: str, angle: dict, duration: int = 10, n: int = 3,
           usage: llm.Usage | None = None, photo: tuple[bytes, str] | None = None) -> dict:
    """{items, body, on_screen} for one angle. `items` are the variants the
    member picks from: hooks (spoken), headlines (image) or scenes (video).
    `photo` is the selected product photo (bytes, media type): the writer
    sees it when the vision model is on. Raises llm.LLMError."""
    at = catalog.AD_TYPE_BY_ID.get(ad_type) or catalog.AD_TYPES[0]
    kind = at["script"]
    sees = bool(photo) and llm.anthropic_ready()
    looks = LOOKS_WITH_PHOTO if sees else LOOKS_NO_PHOTO
    head = product_text(b) + (f"\n\nAngle: {angle.get('title')}\nWhy it matters: {angle.get('insight') or ''}\n"
                              f"For: {angle.get('audience') or b.get('audience') or 'the product audience'}\n"
                              f"Starting hook idea: {angle.get('hook') or ''}\n")
    if kind == "spoken":
        bud = catalog.word_budget(duration)
        who = "a narrator over product shots" if ad_type == "voiceover" else "an on-camera presenter, selfie-style"
        prompt = (head + f"\nWrite a {duration}-second spoken script for {who}. Give {n} alternative hooks and one "
                  f"body that follows any of them. Make each hook a different hook type and say which it is. The "
                  f"types: {hook_types_text()}. Every type still follows the rules: no customer voice, no results. "
                  f"Hook + body together must be at most {bud['total']} words, said "
                  "out loud at a natural pace.\n" + budget_text(duration) + "\nEnd with a simple call to action. "
                  "Plain spoken English, no stage directions, no emojis, no hashtags. Also give 2-3 short on-screen "
                  "captions.")
    elif kind == "image":
        prompt = (head + f"\nWrite {n} alternative headlines (each under 8 words) for an image ad, and one concrete "
                  "image concept: the scene, the light, where the product sits, with the product exactly as in the "
                  "photo and room for the headline.\n" + IMAGE_TEXT_RULE + "\n" + looks)
    else:
        prompt = (head + f"\nWrite {n} alternative scenes for a {duration}-second product clip made from one product "
                  "photo: what happens, the setting, the light and the camera move. The product stays exactly as in "
                  "the photo. No people's faces, no text.\n" + looks)
    out = llm.structured(system=SYSTEM, prompt=prompt, schema=SCRIPT_SCHEMA[kind], name="submit_script",
                         max_tokens=SCRIPT_TOKENS[1], usage=usage, temperature=0.8, model_id=llm.fast_model(),
                         images=[photo] if sees and kind != "spoken" else None)
    clean = (lambda x: " ".join(str(x or "").split()))
    spoken = kind == "spoken"
    typed = {}
    if kind == "spoken":
        items, types = _split_hooks(out.get("hooks"))
        typed, first_types = dict(zip(items, types)), types
        body = clean(out.get("body"))[:1200]
    elif kind == "image":
        items = [clean(h)[:120] for h in out.get("headlines") or []]
        body = catalog.scene_only(clean(out.get("concept"))[:1500], forbid=compliance_claims(b))
    else:
        items, body = [clean(s)[:900] for s in out.get("scenes") or []], ""
    if body and not ok(body, spoken):
        body = ""
    kept = [i for i, x in enumerate(items) if x and ok(x, spoken)][:max(1, n)]
    if spoken:
        first_types = [first_types[i] for i in kept]
    items = [items[i] for i in kept]
    before = len(items)
    if spoken and (items or body) and over_budget(items, body, duration):
        # Too long for the length: one tightening call with the overage, then a
        # sentence-boundary trim, so a written script never trips the length check.
        items, body = tighten(b, ad_type, items, body, duration, usage)
    elif spoken:
        items, body = catalog.fit_spoken(items, body, duration)
    on_screen = [clean(t)[:80] for t in out.get("on_screen") or [] if ok(clean(t))][:4]
    res = {"items": items, "body": body, "on_screen": on_screen}
    if spoken:
        # one type per hook, in order: by position when tightening kept every hook, else by its exact words
        ids = first_types if len(items) == before else [typed.get(i, "") for i in items]
        res["hook_types"] = [{"id": t, "name": hook_type_name(t)} for t in ids]
    return res


def shorten(b: dict, ad_type: str, hooks: list, body: str, duration: int, usage: llm.Usage | None = None) -> dict:
    """"Shorten to fit" for a script the member typed: {items, body, changed}.
    Hooks that already fit with the body are kept as they are when only the
    body needs cutting; every line that comes back fits `duration`."""
    hooks = [" ".join(str(h).split())[:200] for h in hooks if str(h).strip()]
    body = " ".join(str(body or "").split())[:2000]
    bud = catalog.word_budget(duration)
    problems = []
    if any(catalog.words(h + " " + body) > bud["total"] for h in hooks) or (not hooks and catalog.words(body) > bud["total"]):
        problems = over_budget(hooks, body, duration) or [f"it is {catalog.words(body)} words (max {bud['total']})"]
    if not problems:
        return {"items": hooks, "body": body, "changed": False}
    new_hooks, new_body = tighten(b, ad_type, hooks, body, duration, usage, problems, keep_all=True)
    return {"items": new_hooks, "body": new_body, "changed": True}
