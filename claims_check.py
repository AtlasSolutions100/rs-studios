"""The claims check: every factual sentence of a script or of ad copy has to trace back to something the seller
approved or their own product page says (quality wave 2). Switch: CLAIMS_CHECK, default off.

One model call per script (llm.structured on the fast model, the same wrapper and price table as every other
writing call). It gets the lines about to be made, the product's approved claims, its saved details (name,
description, price, offer) and the text of its product page (kept at import, products.import_json["text"]).
For each sentence that states a fact about the product it must quote, word for word, the source line that
supports it. What the model says is not trusted on its own: a "supported" verdict only stands when its quote
really is in the sources and shares enough words with the sentence (verify()). Everything else is flagged.

    not a fact         opinion, a question, a call to action, a scene description: never flagged
    supported          a quote from the approved claims, the saved details or the page backs it
    flagged            a fact with nothing behind it: the member edits it or removes it (Create shows each
                       flagged line with "edit or remove"); Generate refuses while one is left

Create runs it when the member leaves the script step and Generate requires a clean result for the same words
(claims_checks keeps the last result per product and text, so the same words are never checked twice).

It is FREE to members (m15): no credits are held or taken, and estimate() is 0. We absorb the model cost, about
$0.004 a script (cost_usd()); each row keeps what its call cost us. Because nobody pays per check, two daily caps
bound the spend: CLAIMS_CHECK_DAILY_MAX checks a day across the site (default 3000, about $12) and
CLAIMS_CHECK_WS_DAILY_MAX a day for one workspace (default 150). Past either, the check answers "skipped" until
midnight UTC. The switch still ships off.

When the check can't run (no key, the model is down) the script is let through and the result says "skipped":
the regex rules in compliance.py still apply, as they do today.
"""
import hashlib
import json
import os
import re

import catalog
import llm

TOKENS = (3200, 900)            # the most one check may use: page text + lines in, verdicts out
PAGE_CHARS = 6000
SYSTEM = ("You check advertising copy for a small brand before it is made into an ad. For each numbered line you "
          "split out every sentence that states a FACT about the product, its maker, its price or its results: "
          "something that could be true or false (what it contains, does, costs, weighs, how long it lasts, who "
          "uses it, numbers, comparisons, awards, guarantees). Opinions, greetings, questions, scene descriptions "
          "and calls to action are not facts. For each fact, find the source line that supports it and quote that "
          "source WORD FOR WORD. If no source supports it, say so. Never stretch a source to cover more than it "
          "says, and never use the copy itself as a source.")
SCHEMA = {"type": "object", "properties": {"sentences": {"type": "array", "items": {"type": "object", "properties": {
    "line": {"type": "integer", "description": "The number of the line the sentence is in."},
    "sentence": {"type": "string", "description": "The factual sentence, word for word from the line."},
    "supported": {"type": "boolean"},
    "source": {"type": "string", "enum": ["claim", "details", "page", "none"]},
    "quote": {"type": "string", "description": "The supporting source text, word for word; empty if none."}},
    "required": ["line", "sentence", "supported", "source", "quote"]}}}, "required": ["sentences"]}
EDIT_OR_REMOVE = "Nothing on the product backs this up. Edit it or remove it."


def on() -> bool:
    return os.environ.get("CLAIMS_CHECK", "0").strip().lower() in ("1", "on", "true", "yes")


DEFAULT_DAILY_MAX, DEFAULT_WS_DAILY_MAX = 3000, 150


def estimate() -> int:
    """What one check costs a member, in credits: nothing (m15). Our own cost is cost_usd()."""
    return 0


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def daily_max() -> int:
    return _cap("CLAIMS_CHECK_DAILY_MAX", DEFAULT_DAILY_MAX)


def ws_daily_max() -> int:
    return _cap("CLAIMS_CHECK_WS_DAILY_MAX", DEFAULT_WS_DAILY_MAX)


def checks_today(conn, ws_id: int | None = None) -> int:
    """Checks that really ran today (UTC): the whole site's, or one workspace's."""
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    q, args = "SELECT COUNT(*) FROM claims_checks WHERE created_at >= ? AND status != 'skipped'", [day]
    if ws_id is not None:
        q += " AND workspace_id=?"
        args.append(ws_id)
    return conn.execute(q, args).fetchone()[0]


def over_cap(conn, ws_id: int) -> bool:
    """Is today's safety cap used, for the site or for this workspace? Then the check is skipped (never charged)."""
    return checks_today(conn) >= daily_max() or checks_today(conn, ws_id) >= ws_daily_max()


def cost_usd() -> float:
    """A typical check's model cost: about 1,800 tokens in and 450 out on the fast model."""
    return round(llm.cost_usd(llm.fast_model(), 1800, 450), 4)


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def sources(brief: dict, page_text: str = "") -> dict:
    """What a fact may lean on: {"claim": [...], "details": [...], "page": str}."""
    claims = [c.strip(" -•\t") for c in (brief.get("claims") or "").splitlines() if c.strip(" -•\t")]
    details = [str(brief.get(k)) for k in ("product", "description", "price", "offer") if brief.get(k)]
    return {"claim": claims, "details": details, "page": " ".join(str(page_text or "").split())[:PAGE_CHARS]}


def page_text_of(product_row) -> str:
    """The product page's text kept at import ('' for a product added by hand or imported before this existed)."""
    try:
        return str((json.loads(product_row["import_json"] or "{}") or {}).get("text") or "")
    except (ValueError, TypeError, KeyError, IndexError):
        return ""


def text_hash(lines: list, src: dict) -> str:
    blob = json.dumps([[" ".join(str(x).split()) for x in lines], src["claim"], src["details"],
                       hashlib.sha256(src["page"].encode()).hexdigest()], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def verify(sentence: str, quote: str, source: str, src: dict) -> bool:
    """Does the model's quote really back the sentence? The quote must be in the named source (word for word,
    ignoring case and punctuation) and share most of the sentence's longer words or all of its numbers."""
    q, s = _norm(quote), _norm(sentence)
    if len(q) < 2 or not s:
        return False
    pool = {"claim": " | ".join(_norm(c) for c in src["claim"]), "details": " | ".join(_norm(d) for d in src["details"]),
            "page": _norm(src["page"])}
    hay = pool.get(source) or ""
    if q not in hay and not any(q in h for h in pool.values()):
        return False
    nums = re.findall(r"\d+(?:\.\d+)?", sentence)
    if any(n not in re.findall(r"\d+(?:\.\d+)?", quote) for n in nums):
        return False                      # every number said must be in the source quote
    words = [w for w in s.split() if len(w) > 3 and w not in _FILLER and not w.isdigit()]
    if not words:
        return True
    return sum(1 for w in words if w in q) / len(words) >= 0.5


# Words that carry no claim of their own ("It costs $24", "This one comes with a lid"): not counted in the overlap.
_FILLER = {"costs", "cost", "price", "priced", "only", "just", "this", "that", "with", "from", "your", "yours", "they",
           "them", "comes", "come", "have", "does", "each", "every", "there", "here", "also", "really", "about"}


def check(lines: list, brief: dict, page_text: str = "", usage: llm.Usage | None = None) -> dict:
    """{"status": "ok" | "flagged", "flags": [{"line", "sentence", "why"}], "facts": n, "supported": n}.
    `lines` are the texts about to be made (one per hook + body, headline, callout...). Raises llm.LLMError."""
    lines = [" ".join(str(x or "").split()) for x in lines]
    src = sources(brief, page_text)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines) if t)
    if not numbered:
        return {"status": "ok", "flags": [], "facts": 0, "supported": 0}
    prompt = ("SOURCES\nApproved claims:\n" + ("\n".join("- " + c for c in src["claim"]) or "(none)")
              + "\n\nSaved product details:\n" + ("\n".join("- " + d for d in src["details"]) or "(none)")
              + "\n\nProduct page text:\n" + (src["page"] or "(not available)")
              + "\n\nCOPY TO CHECK (numbered lines)\n" + numbered
              + "\n\nReturn every factual sentence in the copy with its source quote.")
    out = llm.structured(system=SYSTEM, prompt=prompt, schema=SCHEMA, name="report_claims", max_tokens=TOKENS[1],
                         usage=usage, temperature=0, model_id=llm.fast_model())
    flags, facts, good = [], 0, 0
    for s in out.get("sentences") or []:
        if not isinstance(s, dict):
            continue
        sent = " ".join(str(s.get("sentence") or "").split())[:300]
        try:
            n = int(s.get("line") or 0)
        except (TypeError, ValueError):
            n = 0
        if not sent or not (1 <= n <= len(lines)) or _norm(sent) not in _norm(lines[n - 1]):
            # a sentence that isn't in the copy can't be shown for editing: find its line, else skip it
            n = next((i + 1 for i, t in enumerate(lines) if sent and _norm(sent) in _norm(t)), 0)
            if not n:
                continue
        facts += 1
        if s.get("supported") is True and verify(sent, str(s.get("quote") or ""), str(s.get("source") or ""), src):
            good += 1
        else:
            flags.append({"line": n - 1, "sentence": sent, "why": EDIT_OR_REMOVE})
    return {"status": "flagged" if flags else "ok", "flags": flags, "facts": facts, "supported": good}


# ---------------------------------------------------------------- the last result per product and text

def cached(conn, ws_id: int, product_id: int, h: str) -> dict | None:
    r = conn.execute("SELECT status, result_json FROM claims_checks WHERE workspace_id=? AND product_id=? AND "
                     "text_hash=? ORDER BY id DESC LIMIT 1", (ws_id, product_id, h)).fetchone()
    if not r:
        return None
    try:
        return dict(json.loads(r["result_json"] or "{}"), status=r["status"])
    except ValueError:
        return None


def save(conn, ws_id: int, product_id: int, h: str, result: dict, usd: float = 0.0):
    import db
    conn.execute("INSERT INTO claims_checks (workspace_id, product_id, text_hash, status, result_json, usd, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (ws_id, product_id, h, result["status"], json.dumps(result), usd, db.now()))


def lines_of(d: dict) -> list:
    """The texts of a Create config that state things: each hook with the body (spoken), or each headline plus
    the subline, button and the words of its text layer (image). Scenes for a video clip describe a picture and
    are not checked."""
    typ = catalog.AD_TYPE_BY_ID.get(d.get("type") or "") or {}
    items = [" ".join(str(x).split())[:900] for x in (d.get("items") or []) if str(x).strip()][:5]
    body = " ".join(str(d.get("body") or "").split())[:2000]
    if typ.get("script") == "spoken":
        return [(i + " " + body).strip() for i in items] or ([body] if body else [])
    if typ.get("script") != "image":
        return []
    import presets
    pre = presets.get(d.get("preset"))
    if pre is not None and pre.layout == "review":
        items = []                 # a customer's own words, attested by the member: not the brand's claim to source
    if pre is not None and pre.extra.get("headline") is False:
        items = []
    out = list(items)
    for k in ("subline", "cta"):
        if str(d.get(k) or "").strip():
            out.append(" ".join(str(d[k]).split())[:200])
    lay = d.get("layers") if isinstance(d.get("layers"), dict) else {}
    if pre is not None and pre.layout:
        for k in ("callouts", "left", "right"):
            out += [" ".join(str(x).split())[:80] for x in (lay.get(k) or []) if str(x).strip()]
    return out
