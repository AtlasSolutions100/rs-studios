"""Auto-edit: a finished presenter (UGC) video turned into a ready-to-run ad, with no editing by the member.

The member makes a talking video as today. This pass then adds what an editor would, from what Campaignify already
knows (the script and its beats, the brand kit, the product, its approved claims and saved offer):

    hook banner      the opening line as a white card at the top, its key words highlighted in the brand colour
    captions         word by word, timed to the real speech, the spoken word lit in the brand colour
    brand pop-ins    the brand's logo chip when the brand is said
    list scene       "make ads, write the copy, plan my launch": a panel that ticks off each item as it is said
    claim cards      an approved claim as a card when the presenter says it (never a claim that isn't saved)
    product card     the product's own photo with its name when it's mentioned, or in a quiet stretch
    offer badge      the saved offer, and only the saved one, when price or offer words are said
    end card         logo, name or web address, a one-line promise, one button, the saved offer
    AI label         "AI presenter" in a corner, as the platforms ask for AI people
    sound            pops, whooshes and dings made here (no sound library, nothing to license) under each graphic

How it works:

    words = transcribe(video)                 # faster-whisper word timings (local, no network)
    words = align(words, script)              # the script's spellings on the speech's timings: a brand the
                                              # presenter says oddly ("campaign-fied") is still captioned right
    plan  = make_plan(words, ctx)             # what appears when (a plain dict; tests check it, no pictures)
    render(video, out, plan, ctx)             # Pillow frames over the video with ffmpeg, plus the sound

Nothing here calls a model or the network; nothing is invented: every word on screen is from the script, the brand
kit, the product's saved claims and offer, or the member's own CTA. Switch: AUTO_EDIT (default off), see enabled().
"""
import difflib
import math
import os
import re
import subprocess
import tempfile
import wave

from PIL import Image, ImageDraw, ImageFilter

import textlayer

BASE_W, BASE_H = 720, 1280            # design units; everything is scaled to the real frame
FPS = 30
CAP_WORDS = 3                          # words on screen at once
HOOK_MIN, HOOK_MAX = 2.4, 4.6          # seconds the hook banner stays
END_MIN = 2.2                          # the end card's shortest run
FILLERS = {"okay", "ok", "so", "look", "listen", "hey", "alright", "yo", "um", "uh", "well"}
STOP = {"a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with", "your", "my", "you", "it", "is", "are",
        "this", "that", "i", "we", "our", "be", "can", "just", "so", "then", "like", "at", "as", "from", "by", "all"}
OFFER_WORDS = {"price", "priced", "free", "trial", "off", "discount", "deal", "offer", "save", "sale", "$"}
END_WORDS = {"link", "below", "bio", "today", "now", "try", "download", "visit", "shop", "get"}


def enabled() -> bool:
    return os.environ.get("AUTO_EDIT", "0").strip().lower() in ("1", "on", "true", "yes")


# ---------------------------------------------------------------- words

def norm(token: str) -> str:
    return re.sub(r"[^a-z0-9$%]", "", token.lower())


def transcribe(path: str, model: str | None = None) -> list:
    """Word timings for the speech in a video: [{"w", "s", "e"}]. faster-whisper on the CPU, offline once the model
    is cached (AUTO_EDIT_MODEL, default base.en)."""
    from faster_whisper import WhisperModel   # imported here: only the worker that edits needs it
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "a.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
        m = WhisperModel(model or os.environ.get("AUTO_EDIT_MODEL", "base.en"), compute_type="int8")
        segs, _ = m.transcribe(wav, word_timestamps=True)
        return [{"w": x.word.strip(), "s": round(float(x.start), 3), "e": round(float(x.end), 3)}
                for seg in segs for x in seg.words if x.word.strip()]


def has_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def speech_spans(path: str, duration: float) -> list:
    """Where someone is talking: the gaps between ffmpeg's detected silences, [(start, end)]."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-vn", "-af", "silencedetect=noise=-32dB:d=0.16", "-f",
                        "null", "-"], capture_output=True, text=True)
    marks = re.findall(r"silence_(start|end): ([0-9.]+)", r.stderr)
    spans, cur = [], 0.0
    for kind, v in marks:
        v = float(v)
        if kind == "start":
            if v - cur > 0.08:
                spans.append((cur, v))
            cur = None
        else:
            cur = v
    if cur is not None and duration - cur > 0.08:
        spans.append((cur, duration))
    return spans or [(0.0, duration)]


def syllables(token: str) -> int:
    """A rough count of spoken syllables: vowel groups in a word, one per letter in an acronym ("MCP" = 3), a
    number by its digits, and a little for a symbol read out ("$", "%")."""
    t = token.strip(" ,.;:!?\"'()")
    if not t:
        return 1
    if t.isupper() and len(t) <= 5 and t.isalpha():
        return len(t)
    if any(c.isdigit() for c in t):
        return max(1, 2 * sum(c.isdigit() for c in t))
    groups = re.findall(r"[aeiouy]+", t.lower())
    n = len(groups) - (1 if t.lower().endswith("e") and len(groups) > 1 and not t.lower().endswith("le") else 0)
    return max(1, n) + t.count("$") + t.count("%")


def estimate(path: str, script: str, duration: float) -> list:
    """Word timings without a speech model. The script is cut into clauses at its punctuation and the take into
    stretches of speech at its pauses; the clauses are matched to the stretches in order so that the speaking rate
    stays as even as possible (several clauses can share a stretch, one clause can run over a pause). Within a
    match each word gets time by its length. Good to a couple of tenths of a second on a clear take;
    transcribe() is used instead when faster-whisper is installed."""
    toks = script_tokens(script)
    if not toks:
        return []
    spans = speech_spans(path, duration)
    wt = [syllables(t) + 0.6 for t in toks]
    clauses, cur = [], []
    for i, t in enumerate(toks):
        cur.append(i)
        if re.search(r"[.,!?:;]$", t):
            clauses.append(cur)
            cur = []
    if cur:
        clauses.append(cur)
    cw = [sum(wt[i] for i in c) for c in clauses]
    sd = [b - a for a, b in spans]
    total = sum(cw)
    rate = sum(sd) / total
    n, m, INF = len(clauses), len(spans), float("inf")
    best = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for i in range(n + 1):
        for k in range(m + 1):
            if best[i][k] == INF:
                continue
            moves = [(i + 1, k + j) for j in range(1, m - k + 1)] + [(i + j, k + 1) for j in range(2, n - i + 1)]
            for i2, k2 in moves:
                if i2 > n or k2 > m:
                    continue
                w, t = sum(cw[i:i2]), sum(sd[k:k2])
                c = best[i][k] + w / total * math.log(t / (w * rate)) ** 2 + 0.03 * (k2 - k - 1)
                if c < best[i2][k2]:
                    best[i2][k2], back[i2][k2] = c, (i, k)
    groups, i, k = [], n, m
    while (i, k) != (0, 0):
        pi, pk = back[i][k]
        groups.append((pi, i, pk, k))
        i, k = pi, pk
    out = []
    for i0, i1, k0, k1 in reversed(groups):
        ids = [x for c in clauses[i0:i1] for x in c]
        sub = spans[k0:k1]
        total_w, total_t = sum(wt[x] for x in ids), sum(b - a for a, b in sub)

        def at(x, sub=sub):
            for a, b in sub:
                if x <= b - a + 1e-9:
                    return a + x
                x -= b - a
            return sub[-1][1]
        acc = 0.0
        for x in ids:
            s0, acc = acc, acc + wt[x]
            out.append({"w": toks[x], "s": round(at(s0 / total_w * total_t + 1e-6), 3),
                        "e": round(at(acc / total_w * total_t), 3)})
    return out


def script_tokens(script: str) -> list:
    return [t for t in re.findall(r"\S+", script or "") if norm(t)]


def align(words: list, script: str) -> list:
    """The script's tokens (its spelling and punctuation) with timings from the speech. Matching runs on normalized
    tokens; where they differ (the model heard "campaign -fied" for "Campaignify,") the script's tokens share the
    heard tokens' time span by length. Script words with no speech under them are dropped (never captioned early);
    heard words that aren't in the script are dropped too (the caption is what the member wrote)."""
    toks = script_tokens(script)
    if not toks:
        return [dict(w) for w in words]
    heard = [w for w in words if norm(w["w"])]
    a, b = [norm(w["w"]) for w in heard], [norm(t) for t in toks]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out += [{"w": toks[j1 + k], "s": heard[i1 + k]["s"], "e": heard[i1 + k]["e"]} for k in range(i2 - i1)]
        elif op == "replace":
            s, e = heard[i1]["s"], heard[i2 - 1]["e"]
            lens = [max(1, len(norm(t))) for t in toks[j1:j2]]
            tot, acc = sum(lens), 0
            for k, t in enumerate(toks[j1:j2]):
                ts = s + (e - s) * acc / tot
                acc += lens[k]
                out.append({"w": t, "s": round(ts, 3), "e": round(s + (e - s) * acc / tot, 3)})
    return out


def sentences(words: list) -> list:
    """Word lists split at . ! ? (on the script's punctuation)."""
    out, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]) and not re.search(r"\.[a-z]{2,}$", w["w"].lower()):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def caption_groups(words: list, n: int = CAP_WORDS) -> list:
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) == n or re.search(r"[.,!?;:]$", w["w"]):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


# ---------------------------------------------------------------- plan

def _find(words: list, phrase: str, start: int = 0):
    """Index of the first word where `phrase` (normalized tokens) starts, from `start`, or None."""
    p = [norm(t) for t in phrase.split() if norm(t)]
    if not p:
        return None
    n = [norm(w["w"]) for w in words]
    for i in range(start, len(n) - len(p) + 1):
        if n[i:i + len(p)] == p:
            return i
    return None


def _clean(t: str) -> str:
    return t.strip(" ,;:")


def _bare(t: str) -> str:
    """A word for display in a graphic: no trailing sentence punctuation (captions keep only what reads well)."""
    return t.strip(" ,;:.!?")


def hook_of(words: list, beats: list | None = None) -> dict:
    """The banner: the hook beat's line, or the first sentence, without a leading filler, at most 8 words; the last
    two meaningful words are highlighted."""
    sents = sentences(words)
    first = sents[0] if sents else words[:8]
    toks = [w for w in first]
    while toks and norm(toks[0]["w"]) in FILLERS:
        toks = toks[1:]
    while toks and norm(toks[0]["w"]) in {"you", "can", "literally", "honestly"} and len(toks) > 6:
        toks = toks[1:]
    toks = toks[:8]
    text = " ".join(_bare(w["w"]) for w in toks).strip()
    if not text:
        return {}
    text = text[0].upper() + text[1:]
    parts = text.split()
    hl = " ".join(parts[-2:]) if len(parts) > 3 else ""
    head = " ".join(parts[:-2]) if hl else text
    end = min(max(first[-1]["e"] + 0.6, HOOK_MIN), HOOK_MAX)
    return {"t0": 0.0, "t1": round(end, 2), "line1": head, "highlight": hl, "hl_at": toks[-2]["s"] if hl else 0.0}


def list_scenes(words: list) -> list:
    """Spoken lists ("make ads for my product, write the copy, plan my launch"): 2 to 5 short items in one sentence,
    split by commas, each item 2 to 6 words. The title is the words before the first item when they end in a colon
    or "ask"/"just"; otherwise the scene has no title."""
    out = []
    for sent in sentences(words):
        items, cur = [], []
        for w in sent:
            cur.append(w)
            if re.search(r"[,:;]$", w["w"]) or w is sent[-1]:
                items.append(cur)
                cur = []
        short = [it for it in items if 2 <= len(it) <= 6]
        if len(short) < 2:
            continue
        # the run of consecutive short items
        best, run = [], []
        for it in items:
            if 2 <= len(it) <= 6:
                run.append(it)
            else:
                if len(run) > len(best):
                    best = run
                run = []
        if len(run) > len(best):
            best = run
        if len(best) < 2:
            continue
        if re.search(r":$", best[0][-1]["w"]):           # "Then you just ask:" introduces the list
            best = best[1:]
        if len(best) < 2:
            continue
        lead = [w for w in sent if w["s"] < best[0][0]["s"]]
        title = ""
        if lead and (re.search(r":$", lead[-1]["w"]) or norm(lead[-1]["w"]) in {"ask", "asked", "say", "type"}):
            title = " ".join(_bare(w["w"]) for w in lead if norm(w["w"]) not in {"then", "so", "and", "you", "now"})
            title = " ".join(title.split()[-5:])
        out.append({"t0": round(best[0][0]["s"] - 0.35, 2), "t1": round(best[-1][-1]["e"] + 0.9, 2), "title": title,
                    "items": [{"text": " ".join(_bare(w["w"]) for w in it), "t": it[0]["s"]} for it in best[:5]]})
    return out


LINK_WORDS = {"connect", "connects", "connected", "plug", "plugs", "plugged", "integrates", "syncs", "sync", "link",
              "links", "hooks", "pairs"}


def name_mentions(words: list, skip: set, first: bool = True) -> list:
    """Names the presenter says (a capitalized word that doesn't start a sentence, or an acronym such as "MCP"),
    first mention of each, in order: [(index, word)]. The member's own brand is in `skip`."""
    starts, i = set(), 0
    for sent in sentences(words):
        starts.add(i)
        i += len(sent)
    seen, out = set(), []
    for i, w in enumerate(words):
        b = _bare(w["w"])
        if i in starts or len(b) < 2 or not b[0].isupper() or b in ("I", "AI", "OK") or norm(b) in skip:
            continue
        if i > 0 and re.search(r":$", words[i - 1]["w"]):
            continue
        if not first or norm(b) not in seen:
            seen.add(norm(b))
            out.append((i, b))
    return out


def link_scenes(words: list, skip: set) -> list:
    """"connect it to Claude", "plugs right into Shopify": the brand joined to the named app, line drawn as it's
    said. The app is the first name within six words of the verb."""
    names = dict(name_mentions(words, skip, first=False))
    out = []
    for i, w in enumerate(words):
        if norm(w["w"]) not in LINK_WORDS:
            continue
        for j in range(i + 1, min(i + 7, len(words))):
            if j in names and not names[j].isupper():
                out.append({"t0": round(w["s"] - 0.1, 2), "t_hit": words[j]["s"],
                            "t1": round(min(words[j]["e"] + 1.7, words[min(j + 6, len(words) - 1)]["e"] + 0.3), 2),
                            "target": names[j]})
                break
        if out:
            break
    return out


def claim_hits(words: list, claims: list) -> list:
    """Approved claims the presenter says: a claim matches where at least two of its content words (or all of them,
    if it has one or two) are said within a few seconds of each other."""
    out, said = [], [(norm(w["w"]), w["s"]) for w in words]
    for c in claims or []:
        keys = [norm(t) for t in str(c).split() if norm(t) and norm(t) not in STOP]
        if not keys:
            continue
        need = min(2, len(keys))
        for i, (tok, ts) in enumerate(said):
            if tok not in keys:
                continue
            window = {t for t, s2 in said[i:i + 10] if s2 - ts <= 3.0}
            if len(set(keys) & window) >= need:
                out.append({"t": ts, "text": str(c).strip()})
                break
    return out


def make_plan(words: list, ctx: dict) -> dict:
    """What appears when. ctx: script (str), duration (s), brand {name, aliases, logo, primary, accent, url},
    product {name, photos, claims, offer, line}, cta (str), ai_label (bool, default True)."""
    brand = ctx.get("brand") or {}
    product = ctx.get("product") or {}
    dur = float(ctx.get("duration") or (words[-1]["e"] + 1.0 if words else 10.0))
    plan = {"duration": dur, "captions": caption_groups(words), "events": []}
    ev = plan["events"]

    hook = hook_of(words)
    if hook:
        ev.append(dict(hook, kind="hook"))

    # end card: from the last sentence when it names the brand, a web address or a call to action, else the tail
    sents = sentences(words)
    names = [norm(x) for x in [brand.get("name", "")] + list(brand.get("aliases") or []) if norm(x)]
    end_t = dur - END_MIN
    if sents:
        last = sents[-1]
        toks = {norm(w["w"]) for w in last}
        if toks & set(names) or toks & END_WORDS or any(re.search(r"\.(ai|com|io|co)$", w["w"].lower()) for w in last):
            end_t = min(end_t, last[0]["s"] - 0.1)
    end_t = max(end_t, min(dur - END_MIN, dur * 0.7))
    plan["end_t"] = round(end_t, 2)
    ev.append({"kind": "end", "t0": round(end_t, 2), "t1": dur})

    busy = [(hook.get("t0", 0), hook.get("t1", 0))] if hook else []

    def free(t0, t1):
        return all(t1 <= a or t0 >= b for a, b in busy)

    for sc in link_scenes(words, set(names)):
        if sc["t0"] < end_t - 1 and free(sc["t0"], sc["t1"]):
            ev.append(dict(sc, kind="link"))
            busy.append((sc["t0"], sc["t1"]))

    for sc in list_scenes(words):
        if sc["t0"] < end_t and free(sc["t0"], min(sc["t1"], end_t)):
            sc["t1"] = min(sc["t1"], end_t)
            ev.append(dict(sc, kind="list"))
            busy.append((sc["t0"], sc["t1"]))

    # brand pop-ins: the first two mentions outside the hook, scenes and end card
    pops = 0
    for w in words:
        if pops >= 2 or w["s"] >= end_t - 0.3:
            break
        if norm(w["w"]) in names and free(w["s"] - 0.1, w["s"] + 1.4):
            ev.append({"kind": "brand", "t0": round(w["s"] - 0.05, 2), "t1": round(w["s"] + 1.5, 2)})
            busy.append((w["s"] - 0.1, w["s"] + 1.5))
            pops += 1

    # other names said (an app, a partner, a tech term): a plain chip, first mention, at most two
    n_names = 0
    for i, b in name_mentions(words, set(names)):
        w = words[i]
        if n_names >= 2 or w["s"] >= end_t - 0.5:
            break
        t1 = min(w["s"] + 1.0, words[min(i + 3, len(words) - 1)]["e"])
        if free(w["s"] - 0.1, t1):
            ev.append({"kind": "name", "t0": round(w["s"] - 0.05, 2), "t1": round(t1, 2), "text": b})
            busy.append((w["s"] - 0.1, t1))
            n_names += 1

    for hit in claim_hits(words, product.get("claims")):
        t0 = hit["t"] - 0.1
        if t0 < end_t - 1 and free(t0, t0 + 2.2):
            ev.append({"kind": "claim", "t0": round(t0, 2), "t1": round(t0 + 2.2, 2), "text": hit["text"]})
            busy.append((t0, t0 + 2.2))

    if product.get("offer"):
        for w in words:
            if norm(w["w"]) not in OFFER_WORDS:
                continue
            # on the word, or just after whatever is on screen then (at most 0.8 s late)
            t0 = max([w["s"]] + [b for a, b in busy if a <= w["s"] < b])
            t1 = min(t0 + 2.0, end_t)
            if t0 - w["s"] <= 0.8 and t1 - t0 >= 1.2 and free(t0, t1):
                ev.append({"kind": "offer", "t0": round(t0, 2), "t1": round(t1, 2), "text": product["offer"]})
                busy.append((t0, t1))
                break

    # the product's photo: on its name, else in the longest quiet stretch of at least 2.5 s
    photos = [p for p in (product.get("photos") or []) if p and os.path.exists(p)]
    if photos:
        pname = product.get("name") or ""
        i = _find(words, pname) if pname and norm(pname) not in names else None
        spot = None
        if i is not None and free(words[i]["s"], words[i]["s"] + 2.4):
            spot = words[i]["s"]
        else:
            edges = sorted(busy + [(end_t, dur)])
            cur, best = 0.6, (0, 0)
            for a, b in edges:
                if a - cur > best[1] - best[0]:
                    best = (cur, a)
                cur = max(cur, b)
            if best[1] - best[0] >= 2.5:
                spot = best[0] + 0.2
        if spot is not None:
            ev.append({"kind": "product", "t0": round(spot, 2), "t1": round(min(spot + 2.6, end_t), 2),
                       "photo": photos[0], "name": product.get("name") or ""})
    ev.sort(key=lambda e: e["t0"])
    plan["ai_label"] = ctx.get("ai_label", True)
    return plan


# ---------------------------------------------------------------- drawing helpers

def _rgb(h, default):
    return textlayer.hex_rgb(h, default) if h else default


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease_out(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def ease_back(x):
    x = clamp(x)
    c1 = 1.70158
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def prog(t, t0, d=0.35):
    return clamp((t - t0) / d)


class Painter:
    def __init__(self, size, ctx):
        self.W, self.H = size
        self.k = self.W / BASE_W
        brand = ctx.get("brand") or {}
        self.accent = _rgb(brand.get("accent"), (200, 255, 61))
        self.primary = _rgb(brand.get("primary"), (14, 14, 16))
        self.ink = (14, 14, 16)
        self.on_accent = textlayer.on_color(self.accent)
        self.brand = brand
        self.product = ctx.get("product") or {}
        self.cta = ctx.get("cta") or "Learn more"
        self.logo = None
        if brand.get("logo") and os.path.exists(brand["logo"]):
            try:
                self.logo = Image.open(brand["logo"]).convert("RGBA")
            except OSError:
                self.logo = None
        self._cache = {}

    def u(self, v):
        return int(round(v * self.k))

    def font(self, px, bold=True):
        key = (px, bold)
        if key not in self._cache:
            self._cache[key] = textlayer._font("sans", "bold" if bold else "regular", self.u(px))
        return self._cache[key]

    def rr(self, img, box, r, fill, outline=None, width=0):
        ImageDraw.Draw(img).rounded_rectangle([int(v) for v in box], int(r), fill=fill, outline=outline,
                                              width=int(width))

    def shadow(self, size, box, r, blur=16, alpha=110):
        s = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(s).rounded_rectangle([int(v) for v in box], int(r), fill=(0, 0, 0, alpha))
        return s.filter(ImageFilter.GaussianBlur(blur))

    @staticmethod
    def paste(base, layer, cx, cy, scale=1.0, alpha=1.0):
        if scale <= 0.02 or alpha <= 0.02:
            return
        w, h = layer.size
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        lay = layer if (nw, nh) == (w, h) else layer.resize((nw, nh), Image.LANCZOS)
        if alpha < 1:
            lay = lay.copy()
            lay.putalpha(lay.getchannel("A").point(lambda v: int(v * alpha)))
        base.alpha_composite(lay, (int(cx - nw / 2), int(cy - nh / 2)))

    def star(self, d, cx, cy, r, fill):
        pts = []
        for i in range(8):
            ang = -math.pi / 2 + i * math.pi / 4
            rad = r if i % 2 == 0 else r * 0.3
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        d.polygon(pts, fill=fill)

    def text_w(self, s, f):
        return ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength(s, font=f)

    # ---- layers (built once, cached by key)
    def chip(self, name=None, logo=True):
        key = ("chip", name, logo)
        if key in self._cache:
            return self._cache[key]
        dot = name is not None and not logo
        name = name if name is not None else (self.brand.get("name") or "")
        logo = logo and self.logo is not None
        f = self.font(40)
        ic = self.u(56) if logo else (self.u(22) if dot else 0)
        pad = self.u(22)
        w = int(self.text_w(name, f) + pad * 2 + (ic + self.u(14) if ic else 0))
        h = self.u(78)
        m = self.u(20)
        lay = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
        lay.alpha_composite(self.shadow(lay.size, (m, m + 4, m + w, m + h + 4), h // 2, self.u(10), 90))
        self.rr(lay, (m, m, m + w, m + h), h // 2, self.primary, outline=self.accent, width=self.u(3))
        x = m + pad
        if logo:
            lg = self.logo.copy()
            lg.thumbnail((ic, ic), Image.LANCZOS)
            lay.alpha_composite(lg, (x, m + (h - lg.size[1]) // 2))
            x += ic + self.u(14)
        elif dot:
            ImageDraw.Draw(lay).ellipse((x, m + h / 2 - ic / 2, x + ic, m + h / 2 + ic / 2), fill=self.accent)
            x += ic + self.u(14)
        ImageDraw.Draw(lay).text((x, m + h // 2), name, font=f, fill=textlayer.on_color(self.primary), anchor="lm")
        self._cache[key] = lay
        return lay

    def node(self, name, logo):
        """A rounded tile for the connection scene: the logo (or a dot) over the name."""
        key = ("node", name, logo)
        if key in self._cache:
            return self._cache[key]
        f = self.font(28)
        w = max(self.u(170), int(self.text_w(name, f) + self.u(40)))
        h = self.u(170)
        lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        self.rr(lay, (0, 0, w, h), self.u(30), (255, 255, 255))
        if logo and self.logo is not None:
            lg = self.logo.copy()
            lg.thumbnail((self.u(80), self.u(80)), Image.LANCZOS)
            lay.alpha_composite(lg, ((w - lg.size[0]) // 2, self.u(20)))
        else:
            r = self.u(30)
            ImageDraw.Draw(lay).ellipse((w / 2 - r, self.u(60) - r, w / 2 + r, self.u(60) + r), fill=self.ink)
        ImageDraw.Draw(lay).text((w / 2, self.u(135)), name, font=f, fill=self.ink, anchor="mm")
        self._cache[key] = lay
        return lay


def _layers_for(painter: Painter, plan: dict) -> dict:
    """Pictures that don't change frame to frame (product photo cards)."""
    out = {}
    for e in plan["events"]:
        if e["kind"] == "product" and e["photo"] not in out:
            P = painter
            try:
                ph = Image.open(e["photo"]).convert("RGBA")
            except OSError:
                continue
            side = P.u(300)
            ph.thumbnail((side, side), Image.LANCZOS)
            pad, m = P.u(16), P.u(20)
            name = e.get("name") or ""
            f = P.font(28)
            th = P.u(52) if name else 0
            w, h = max(ph.size[0], int(P.text_w(name, f)) + P.u(20)) + 2 * pad, ph.size[1] + 2 * pad + th
            lay = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
            lay.alpha_composite(P.shadow(lay.size, (m, m + 6, m + w, m + h + 6), P.u(28), P.u(14), 120))
            P.rr(lay, (m, m, m + w, m + h), P.u(28), (255, 255, 255))
            mask = Image.new("L", ph.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, ph.size[0], ph.size[1]), P.u(18), fill=255)
            lay.paste(ph, (m + (w - ph.size[0]) // 2, m + pad), mask)
            if name:
                ImageDraw.Draw(lay).text((m + w / 2, m + pad + ph.size[1] + th / 2 + P.u(4)), name, font=f,
                                         fill=P.ink, anchor="mm")
            out[e["photo"]] = lay
    return out


# ---------------------------------------------------------------- frames

def _caption_y(P, plan, t):
    for e in plan["events"]:
        if e["kind"] in ("list", "product", "claim", "offer", "link") and e["t0"] - 0.1 <= t < e["t1"] + 0.2:
            return P.u(640)
        if e["kind"] in ("brand", "name") and e["t0"] <= t < e["t1"]:
            return P.u(760)
    return P.u(1000)


def draw_captions(img, P, plan, t):
    if t >= plan["end_t"] - 0.05:
        return
    g = None
    caps = plan["captions"]
    for i, grp in enumerate(caps):
        nxt = caps[i + 1][0]["s"] if i + 1 < len(caps) else grp[-1]["e"] + 0.6
        if grp[0]["s"] - 0.06 <= t < min(nxt, grp[-1]["e"] + 0.6):
            g = grp
            break
    if not g:
        return
    d = ImageDraw.Draw(img)
    toks = [(_bare(w["w"]) + ("?" if w["w"].endswith("?") else "")).upper() for w in g]
    size = 50
    f = P.font(size)
    sp = P.u(14)
    while size > 30:
        widths = [P.text_w(s, f) for s in toks]
        total = sum(widths) + sp * (len(toks) - 1)
        if total <= P.W - P.u(70):
            break
        size -= 4
        f = P.font(size)
    widths = [P.text_w(s, f) for s in toks]
    total = sum(widths) + sp * (len(toks) - 1)
    x = (P.W - total) / 2
    p = ease_back(prog(t, g[0]["s"] - 0.06, 0.18))
    y = _caption_y(P, plan, t) + (1 - p) * P.u(18)
    for w, s, wd in zip(g, toks, widths):
        on = w["s"] - 0.03 <= t < w["e"] + 0.08
        d.text((x, y), s, font=f, fill=P.accent if on else (255, 255, 255), anchor="lm",
               stroke_width=P.u(7), stroke_fill=(0, 0, 0))
        x += wd + sp


def draw_hook(img, P, e, t):
    if t > e["t1"] + 0.3:
        return
    p = ease_back(prog(t, e["t0"] + 0.05, 0.35))
    q = 1 - ease_out(prog(t, e["t1"] - 0.3, 0.3))
    s = min(p, q)
    f1, f2 = P.font(38), P.font(50)
    l1, hl = e["line1"], e["highlight"]
    w = int(max(P.text_w(l1, f1), P.text_w(hl, f2) if hl else 0) + P.u(70))
    w = min(w, P.W - P.u(40))
    h = P.u(170) if hl else P.u(100)
    lay = Image.new("RGBA", (w + P.u(20), h + P.u(20)), (0, 0, 0, 0))
    P.rr(lay, (P.u(10), P.u(10), P.u(10) + w, P.u(10) + h), P.u(26), (255, 255, 255))
    d = ImageDraw.Draw(lay)
    cx = P.u(10) + w / 2
    if hl:
        d.text((cx, P.u(62)), l1, font=f1, fill=P.ink, anchor="mm")
        tw = P.text_w(hl, f2)
        grow = ease_out(prog(t, e["hl_at"], 0.4))
        P.rr(lay, (cx - tw / 2 - P.u(12), P.u(94), cx - tw / 2 - P.u(12) + (tw + P.u(24)) * grow, P.u(156)), P.u(14),
             P.accent)
        d.text((cx, P.u(125)), hl, font=f2, fill=P.on_accent if grow > 0.5 else P.ink, anchor="mm")
    else:
        d.text((cx, P.u(60)), l1, font=f1, fill=P.ink, anchor="mm")
    P.paste(img, lay, P.W / 2, P.u(190), max(0.0, s), clamp(s))
    bt = t - e["hl_at"] - 0.35
    if hl and 0 <= bt < 0.9:
        d2 = ImageDraw.Draw(img)
        a = 1 - bt / 0.9
        for i in range(9):
            ang = i * 2 * math.pi / 9 + 0.3
            r = P.u(60) + bt * P.u(260)
            P.star(d2, P.W / 2 + r * math.cos(ang), P.u(230) + r * math.sin(ang) * 0.7, P.u(16) * a + P.u(4),
                   P.accent + (int(255 * a),))


def draw_brand(img, P, e, t):
    p = ease_back(prog(t, e["t0"], 0.32))
    q = 1 - ease_out(prog(t, e["t1"] - 0.25, 0.25))
    P.paste(img, P.chip(), P.W / 2, P.u(880), 1.05 * min(p, q), q)
    bt = t - e["t0"]
    if bt < 0.8:
        d = ImageDraw.Draw(img)
        a = clamp(1 - bt / 0.8)
        for i in range(7):
            ang = i * 2 * math.pi / 7
            r = P.u(150) + bt * P.u(170)
            P.star(d, P.W / 2 + r * math.cos(ang), P.u(880) + r * math.sin(ang) * 0.5, P.u(12) * a + P.u(3),
                   P.accent + (int(255 * a),))


def draw_name(img, P, e, t):
    p = ease_back(prog(t, e["t0"], 0.3))
    q = 1 - ease_out(prog(t, e["t1"] - 0.25, 0.25))
    P.paste(img, P.chip(e["text"], logo=False), P.W / 2, P.u(880), min(p, q), q)


def draw_link(img, P, e, t):
    p = ease_out(prog(t, e["t0"], 0.35))
    q = 1 - ease_out(prog(t, e["t1"] - 0.3, 0.3))
    a = min(p, q)
    if a <= 0.01:
        return
    cy = P.u(880)
    lay = Image.new("RGBA", (P.W, P.u(360)), (0, 0, 0, 0))
    oy = P.u(180) - cy                                  # layer y = frame y + oy
    left = P.node(P.brand.get("name") or "", True)
    right = P.node(e["target"], False)
    lx, rx = P.u(150), P.W - P.u(150)
    hit = t >= e["t_hit"]
    d = ImageDraw.Draw(lay)
    grow = ease_out(clamp((t - e["t0"] - 0.25) / max(0.3, e["t_hit"] - e["t0"] - 0.25)))
    x0, x1 = lx + left.size[0] / 2 + P.u(8), rx - right.size[0] / 2 - P.u(8)
    xe = x0 + (x1 - x0) * grow
    col = P.accent if hit else (255, 255, 255)
    step = P.u(22)
    x = x0
    while x < xe:
        d.line((x, cy + oy, min(x + step * 0.55, xe), cy + oy), fill=col + (255,), width=P.u(7))
        x += step
    if hit:
        bp = ease_back(prog(t, e["t_hit"], 0.3))
        bx, by, r = (x0 + x1) / 2, cy + oy, P.u(30) * bp
        d.ellipse((bx - r, by - r, bx + r, by + r), fill=P.accent)
        if bp > 0.6:
            d.line((bx - P.u(12), by, bx - P.u(3), by + P.u(10), bx + P.u(13), by - P.u(10)), fill=P.on_accent,
                   width=P.u(6))
        f = P.font(26)
        tw = P.text_w("Connected", f)
        P.rr(lay, (bx - tw / 2 - P.u(18), by + P.u(100), bx + tw / 2 + P.u(18), by + P.u(146)), P.u(23), P.accent)
        d.text((bx, by + P.u(123)), "Connected", font=f, fill=P.on_accent, anchor="mm")
    ls = ease_back(prog(t, e["t0"], 0.35))
    rs = ease_back(prog(t, e["t0"] + 0.15, 0.35))
    P.paste(lay, left, lx, cy + oy, ls)
    P.paste(lay, right, rx, cy + oy, rs)
    if a < 1:
        lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
    img.alpha_composite(lay, (0, -oy))


def draw_list(img, P, e, t):
    p = ease_out(prog(t, e["t0"], 0.4))
    q = 1 - ease_out(prog(t, e["t1"] - 0.3, 0.3))
    a = min(p, q)
    if a <= 0.01:
        return
    n = len(e["items"])
    row = P.u(92)
    head = P.u(76) if e.get("title") else P.u(24)
    w, h = P.W - P.u(68), head + n * row + P.u(20)
    m = P.u(30)
    lay = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
    lay.alpha_composite(P.shadow(lay.size, (m, m + 8, m + w, m + h + 8), P.u(34), P.u(18), 140))
    P.rr(lay, (m, m, m + w, m + h), P.u(34), (22, 22, 26, 238), outline=(60, 60, 68), width=P.u(2))
    d = ImageDraw.Draw(lay)
    if e.get("title"):
        if P.logo:
            lg = P.logo.copy()
            lg.thumbnail((P.u(36), P.u(36)), Image.LANCZOS)
            lay.alpha_composite(lg, (m + P.u(24), m + P.u(20)))
        d.text((m + P.u(24) + (P.u(48) if P.logo else 0), m + P.u(38)), e["title"][0].upper() + e["title"][1:],
               font=P.font(26), fill=(235, 235, 240), anchor="lm")
    for i, it in enumerate(e["items"]):
        pi = ease_back(prog(t, it["t"] - 0.05, 0.3))
        if pi <= 0.01:
            continue
        y0 = m + head + i * row
        off = (1 - pi) * P.u(60)
        P.rr(lay, (m + P.u(20) + off, y0, m + w - P.u(20) + off, y0 + row - P.u(14)), P.u(22), (40, 40, 46))
        done = t >= it["t"] + 0.45
        cx, cy = m + P.u(58) + off, y0 + (row - P.u(14)) / 2
        r = P.u(20)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=P.accent if done else (70, 70, 78))
        if done:
            d.line((cx - P.u(9), cy, cx - P.u(2), cy + P.u(8), cx + P.u(10), cy - P.u(8)), fill=P.on_accent,
                   width=P.u(5))
        f = P.font(28)
        txt = it["text"][0].upper() + it["text"][1:]
        while P.text_w(txt, f) > w - P.u(140) and len(txt) > 4:
            txt = txt[:-2].rstrip() + "…"
        d.text((cx + P.u(36), cy), txt, font=f, fill=(255, 255, 255), anchor="lm")
    if a < 1:
        lay.putalpha(lay.getchannel("A").point(lambda v: int(v * a)))
    img.alpha_composite(lay, (P.W // 2 - lay.size[0] // 2, int(P.u(760) + (1 - p) * P.u(120))))


def draw_card(img, P, e, t, text, label=""):
    p = ease_back(prog(t, e["t0"], 0.32))
    q = 1 - ease_out(prog(t, e["t1"] - 0.25, 0.25))
    s = min(p, q)
    f = P.font(34)
    lines = textlayer.wrap(text, f, P.W - P.u(180))[:3]
    lh = P.u(46)
    w = int(max(P.text_w(ln, f) for ln in lines) + P.u(80))
    h = len(lines) * lh + P.u(56) + (P.u(34) if label else 0)
    m = P.u(20)
    lay = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
    lay.alpha_composite(P.shadow(lay.size, (m, m + 6, m + w, m + h + 6), P.u(26), P.u(12), 110))
    P.rr(lay, (m, m, m + w, m + h), P.u(26), (255, 255, 255))
    P.rr(lay, (m, m, m + P.u(12), m + h), P.u(6), P.accent)
    d = ImageDraw.Draw(lay)
    y = m + P.u(28)
    if label:
        d.text((m + P.u(40), y + P.u(8)), label.upper(), font=P.font(20), fill=(110, 110, 118), anchor="lm")
        y += P.u(34)
    for ln in lines:
        d.text((m + P.u(40), y + lh / 2), ln, font=f, fill=P.ink, anchor="lm")
        y += lh
    P.paste(img, lay, P.W / 2, P.u(900), s, clamp(q))


def draw_offer(img, P, e, t):
    p = ease_back(prog(t, e["t0"], 0.32))
    q = 1 - ease_out(prog(t, e["t1"] - 0.25, 0.25))
    f = P.font(36)
    txt = e["text"]
    w, h = int(P.text_w(txt, f) + P.u(70)), P.u(86)
    lay = Image.new("RGBA", (w + P.u(30), h + P.u(30)), (0, 0, 0, 0))
    P.rr(lay, (P.u(15), P.u(15), P.u(15) + w, P.u(15) + h), h // 2, P.accent)
    ImageDraw.Draw(lay).text((P.u(15) + w / 2, P.u(15) + h / 2), txt, font=f, fill=P.on_accent, anchor="mm")
    wob = 1 + 0.03 * math.sin(t * 9)
    P.paste(img, lay, P.W / 2, P.u(900), min(p, q) * wob, q)


def draw_product(img, P, e, t, layers):
    lay = layers.get(e["photo"])
    if lay is None:
        return
    p = ease_back(prog(t, e["t0"], 0.35))
    q = 1 - ease_out(prog(t, e["t1"] - 0.3, 0.3))
    tilt = math.sin(t * 2) * 1.5
    rot = lay.rotate(tilt, resample=Image.BICUBIC, expand=True)
    P.paste(img, rot, P.W / 2, P.u(900), min(p, q), q)


def draw_end(img, P, e, t):
    t0 = e["t0"]
    p = ease_out(prog(t, t0, 0.35))
    img.alpha_composite(Image.new("RGBA", img.size, P.primary[:3] + (int(232 * p),)))
    d = ImageDraw.Draw(img)
    for i in range(36):
        x = ((i * 397) % 1000) / 1000 * P.W
        y = ((i * 613) % 1000) / 1000 * P.H
        r = P.u(1 + (i % 3))
        tw = 0.5 + 0.5 * math.sin(t * 3 + i)
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, int(120 * p * tw)))
    y = P.u(470)
    if P.logo:
        lg = P.logo.copy()
        lg.thumbnail((P.u(220), P.u(220)), Image.LANCZOS)
        P.paste(img, lg, P.W / 2, y, ease_back(prog(t, t0 + 0.1, 0.45)))
        y += P.u(200)
    on = textlayer.on_color(P.primary)
    name = P.brand.get("url") or P.brand.get("name") or ""
    if name:
        f = P.font(64)
        size = 64
        while P.text_w(name, f) > P.W - P.u(80) and size > 34:
            size -= 4
            f = P.font(size)
        lay = Image.new("RGBA", (P.W, P.u(110)), (0, 0, 0, 0))
        ImageDraw.Draw(lay).text((P.W / 2, P.u(55)), name, font=f, fill=on, anchor="mm")
        wp = ease_back(prog(t, t0 + 0.3, 0.4))
        P.paste(img, lay, P.W / 2, y, wp, clamp(wp))
        y += P.u(90)
    line = P.product.get("line") or ""
    if line:
        sp = ease_out(prog(t, t0 + 0.55, 0.4))
        for ln in textlayer.wrap(line, P.font(28, bold=False), P.W - P.u(120))[:2]:
            d.text((P.W / 2, y + (1 - sp) * P.u(20)), ln, font=P.font(28, bold=False), fill=on + (int(235 * sp),),
                   anchor="mm")
            y += P.u(40)
    y += P.u(60)
    offer = P.product.get("offer") or ""
    cta = P.cta
    if offer and norm(offer).startswith(norm(cta)) and len(offer) <= 26:
        cta, offer = offer, ""                   # "Try it free" + "Try it free for 3 days": one button says it all
    bf = P.font(34)
    bw, bh = int(P.text_w(cta, bf) + P.u(90)), P.u(92)
    btn = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    P.rr(btn, (0, 0, bw, bh), bh // 2, P.accent)
    ImageDraw.Draw(btn).text((bw / 2, bh / 2), cta, font=bf, fill=P.on_accent, anchor="mm")
    P.paste(img, btn, P.W / 2, y, ease_back(prog(t, t0 + 0.8, 0.4)) * (1 + 0.03 * math.sin(t * 7)))
    if offer:
        op = ease_out(prog(t, t0 + 1.0, 0.4))
        d.text((P.W / 2, y + P.u(90)), offer, font=P.font(26), fill=on + (int(230 * op),), anchor="mm")


def draw_label(img, P, plan, t):
    if not plan.get("ai_label") or t >= plan["end_t"]:
        return
    f = P.font(16)
    txt = "AI presenter"
    w = int(P.text_w(txt, f) + P.u(26))
    P.rr(img, (P.W - w - P.u(20), P.H - P.u(52), P.W - P.u(20), P.H - P.u(22)), P.u(15), (0, 0, 0, 130))
    ImageDraw.Draw(img).text((P.W - P.u(20) - w / 2, P.H - P.u(37)), txt, font=f, fill=(235, 235, 235),
                             anchor="mm")


def frame(P, plan, t, layers):
    img = Image.new("RGBA", (P.W, P.H), (0, 0, 0, 0))
    for e in plan["events"]:
        if not (e["t0"] - 0.05 <= t < e["t1"] + 0.35) or (e["kind"] != "end" and t >= plan["end_t"]):
            continue
        k = e["kind"]
        if k == "hook":
            draw_hook(img, P, e, t)
        elif k == "brand":
            draw_brand(img, P, e, t)
        elif k == "list":
            draw_list(img, P, e, t)
        elif k == "name":
            draw_name(img, P, e, t)
        elif k == "link":
            draw_link(img, P, e, t)
        elif k == "claim":
            draw_card(img, P, e, t, e["text"])
        elif k == "offer":
            draw_offer(img, P, e, t)
        elif k == "product":
            draw_product(img, P, e, t, layers)
    draw_captions(img, P, plan, t)
    draw_label(img, P, plan, t)
    end = next((e for e in plan["events"] if e["kind"] == "end"), None)
    if end and t >= end["t0"]:
        draw_end(img, P, end, t)
    return img


# ---------------------------------------------------------------- sound

SR = 48000


def _pop(f0=900, f1=320, d=0.12):
    k = (f1 - f0) / (2 * d)
    return f"aevalsrc='0.9*sin(2*PI*({f0}*t+{k:.1f}*t*t))*min(1,t/0.002)*exp(-t*{3 / d:.1f})':s={SR}:d={d}"


def _ding(f=1320, d=0.6):
    return (f"aevalsrc='0.5*(sin(2*PI*{f}*t)+0.5*sin(2*PI*{f * 2.01:.1f}*t)+0.25*sin(2*PI*{f * 3}*t))"
            f"*min(1,t/0.003)*exp(-t/0.18)':s={SR}:d={d}")


def _whoosh(d=0.35):
    return f"anoisesrc=c=pink:r={SR}:a=0.6:d={d},lowpass=f=2400,highpass=f=300,afade=t=in:d={d / 2:.3f}," \
           f"afade=t=out:st={d / 2:.3f}:d={d / 2:.3f}"


def sounds(plan: dict) -> list:
    """(source, start seconds, gain) for each sound under the graphics: sine pops, filtered-noise whooshes and
    bell dings, all made by ffmpeg here (no sound files, nothing to license). Quiet: the voice stays on top."""
    G, out = 0.2, []
    for e in plan["events"]:
        k = e["kind"]
        if k == "hook":
            out.append((_whoosh(0.3), e["t0"], G))
            if e.get("highlight"):
                out.append((_ding(1760, 0.5), e["hl_at"] + 0.35, G * 0.8))
        elif k == "brand":
            out += [(_pop(1000, 350, 0.14), e["t0"], G * 1.2), (_ding(1568, 0.5), e["t0"] + 0.1, G * 0.6)]
        elif k == "name":
            out.append((_pop(900, 400, 0.12), e["t0"], G))
        elif k == "link":
            out += [(_whoosh(0.3), e["t0"], G * 0.8), (_ding(1568, 0.6), e["t_hit"], G * 0.9)]
        elif k == "list":
            out.append((_whoosh(0.35), e["t0"], G))
            for it in e["items"]:
                out += [(_pop(700, 500, 0.08), it["t"], G * 1.1), (_pop(1400, 1800, 0.06), it["t"] + 0.45, G * 0.7)]
        elif k in ("claim", "product", "offer"):
            out.append((_pop(), e["t0"], G))
        elif k == "end":
            out += [(_whoosh(0.5), e["t0"], G * 1.2), (_ding(880, 0.9), e["t0"] + 0.3, G * 0.8),
                    (_ding(1320, 0.9), e["t0"] + 0.4, G * 0.6), (_pop(), e["t0"] + 0.8, G)]
    return [s for s in out if s[1] < plan["duration"]]


def sfx(plan: dict, path: str):
    """The sound track (stereo WAV, as long as the video) from sounds()."""
    snd = sounds(plan)
    dur = f"{plan['duration'] + 0.3:.3f}"
    if not snd:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo", "-t", dur,
                        path], check=True)
        return
    cmd, chains = ["ffmpeg", "-v", "error", "-y"], []
    for i, (src, t, g) in enumerate(snd):
        cmd += ["-f", "lavfi", "-i", src]
        ms = int(max(0.0, t) * 1000)
        chains.append(f"[{i}:a]volume={g:.3f},adelay={ms}|{ms},aformat=channel_layouts=stereo[s{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[s{i}]" for i in range(len(snd))) + \
        f"amix=inputs={len(snd)}:normalize=0:duration=longest,apad,atrim=0:{dur},alimiter=limit=0.95[a]"
    subprocess.run(cmd + ["-filter_complex", graph, "-map", "[a]", "-ar", str(SR), path], check=True,
                   capture_output=True)


# ---------------------------------------------------------------- render

def probe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                          "-of", "default=nw=1", path], capture_output=True, text=True, check=True).stdout
    info = {"audio": "codec_type=audio" in out}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in ("width", "height") and k not in info:
            info[k] = int(v)
        elif k == "duration":
            info["duration"] = float(v)
    return info


def render(src: str, out: str, plan: dict, ctx: dict, width: int | None = None, preview_at: list | None = None):
    """Draw the plan over `src` into `out` (H.264 + AAC, the source's shape, at most 1080 wide). With preview_at,
    write JPEG stills at those seconds instead (out is then a folder) and return their paths."""
    info = probe(src)
    sw, sh = info.get("width", 720), info.get("height", 1280)
    W = width or min(1080, sw)
    W -= W % 2
    H = int(round(W * sh / sw))
    H -= H % 2
    P = Painter((W, H), ctx)
    layers = _layers_for(P, plan)
    if preview_at:
        os.makedirs(out, exist_ok=True)
        paths = []
        for t in preview_at:
            bg = os.path.join(out, f"bg_{t:05.2f}.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", src, "-frames:v", "1",
                            "-vf", f"scale={W}:{H}", bg], check=True)
            img = Image.open(bg).convert("RGBA")
            img.alpha_composite(frame(P, plan, t, layers))
            p = os.path.join(out, f"prev_{t:05.2f}.jpg")
            img.convert("RGB").save(p, quality=85)
            paths.append(p)
        return paths
    dur = plan["duration"]
    with tempfile.TemporaryDirectory() as td:
        snd = os.path.join(td, "sfx.wav")
        sfx(plan, snd)
        audio = "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]" if info["audio"] else "[1:a]anull[a]"
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", src, "-i", snd,
               "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
               "-filter_complex", f"[0:v]scale={W}:{H},fps={FPS}[b];[b][2:v]overlay=0:0:format=auto[v];{audio}",
               "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
        pr = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            for i in range(int(dur * FPS)):
                pr.stdin.write(frame(P, plan, i / FPS, layers).tobytes())
        finally:
            pr.stdin.close()
            pr.wait()
        if pr.returncode != 0:
            raise RuntimeError("ffmpeg failed while rendering the auto-edit")
    return out


def auto_edit(src: str, out: str, ctx: dict, words: list | None = None) -> dict:
    """The whole pass: words (or the given ones), aligned to ctx["script"], a plan, the render. Returns the plan."""
    info = probe(src)
    ctx = dict(ctx, duration=ctx.get("duration") or info.get("duration"))
    if words is None and not has_whisper() and ctx.get("script"):
        aligned = estimate(src, ctx["script"], ctx["duration"])
    else:
        heard = words if words is not None else transcribe(src)
        aligned = align(heard, ctx.get("script") or "") if ctx.get("script") else heard
    plan = make_plan(aligned, ctx)
    render(src, out, plan, ctx)
    return plan


# ---------------------------------------------------------------- jobs

def spoken(script: str, name: str, say_as: str) -> str:
    """The script as the video model should say it: the brand's "say it as" spelling ("Cam-pain-ih-fy") in place of
    its written name. Captions still use the written script (align() matches the two)."""
    name, say_as = (name or "").strip(), (say_as or "").strip()
    if not name or not say_as or not script:
        return script or ""
    return re.sub(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", say_as, script, flags=re.I)


def _site(url: str) -> str:
    u = re.sub(r"^https?://", "", (url or "").strip(), flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I).split("/")[0].split("?")[0]
    return u.lower() if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", u, re.I) else ""


def _first_sentence(text: str, limit: int = 64) -> str:
    s = re.split(r"(?<=[.!?])\s", (text or "").strip())[0].strip()
    return s if 8 <= len(s) <= limit else ""


def context_for_job(conn, job) -> dict:
    """What the edit draws from, all already saved by the member: the script (the job's brief), the brand kit
    (name, logo, colours, website, "say it as"), the product (name, its photos in this job, approved claims, saved
    offer, a short line from its description) and the job's button text."""
    import json as _json
    import textlayer as _tl
    import videotext
    p = _json.loads(job["params_json"] or "{}")
    keys = job.keys()
    brand = conn.execute("SELECT * FROM brands WHERE id=? AND workspace_id=?", (job["brand_id"], job["workspace_id"])) \
        .fetchone() if "brand_id" in keys and job["brand_id"] else None
    prod = conn.execute("SELECT * FROM products WHERE id=? AND workspace_id=?",
                        (job["product_id"], job["workspace_id"])).fetchone() \
        if "product_id" in keys and job["product_id"] else None
    get = (lambda r, k: (r[k] if r is not None and k in r.keys() else None) or "")
    kit = _tl.kit_of(brand)
    photos = []
    for pid in p.get("photo_ids") or []:
        r = conn.execute("SELECT path FROM photos WHERE id=? AND workspace_id=?", (pid, job["workspace_id"])).fetchone()
        if r and r["path"] and os.path.exists(r["path"]):
            photos.append(r["path"])
    import products
    name = get(brand, "brand_name")
    return {
        "script": job["brief"] or "",
        "brand": {"name": name, "aliases": [get(brand, "say_as")] if get(brand, "say_as") else [],
                  "logo": kit.get("logo") or "", "primary": kit.get("primary"), "accent": kit.get("accent"),
                  "url": _site(get(brand, "website"))},
        "product": {"name": get(prod, "name"), "photos": photos[:1], "claims": products.claims_list(get(prod, "claims")),
                    "offer": get(prod, "offer")[:40], "line": _first_sentence(get(prod, "description"))},
        "cta": (p.get("cta") or videotext.cta_for(get(prod, "category")))[:30],
        "ai_label": True,
    }


def wanted(job) -> bool:
    """Does this finished job get the edit? A presenter video the member made with the auto-edit on (the job's
    params say so, set when the batch was made while AUTO_EDIT was on); never a voice sample or a keyframe video
    (that one has its own captions and end card), never a free preview."""
    import json as _json
    if job["tool"] != "ugc_video":
        return False
    try:
        p = _json.loads(job["params_json"] or "{}")
    except (ValueError, TypeError):
        return False
    return bool(p.get("auto_edit")) and not p.get("voice_sample") and not p.get("pipeline") and not p.get("preview")


def edit_job_output(conn, job, path: str) -> bool:
    """Edit a finished presenter video in place. The untouched take is kept next to it as <name>-raw.mp4.
    False (and the take left as it was) if anything goes wrong: the member always gets their video."""
    import logging
    import shutil
    log = logging.getLogger("campaignify.motion_edit")
    if not shutil.which("ffmpeg") or not os.path.exists(path):
        return False
    tmp = path + ".edit.mp4"
    try:
        plan = auto_edit(path, tmp, context_for_job(conn, job))
        if not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
            raise RuntimeError("empty output")
        raw = re.sub(r"(\.\w+)$", r"-raw\1", path)
        os.replace(path, raw)
        os.replace(tmp, path)
        log.info("auto-edit job %s: %s", job["id"], ", ".join(e["kind"] for e in plan["events"]))
        return True
    except Exception as e:  # noqa: BLE001 — never lose the take over a graphic
        log.warning("auto-edit failed for job %s: %s", job["id"], e)
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
