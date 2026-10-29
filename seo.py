"""SEO, AEO (answer engines) and GEO (generative engines / AI assistants)
optimizer.

`parse(html)` pulls out what search and answer engines read; `audit(page)`
turns it into pass / warn / fail checks in three groups; `suggest(...)`
(the AI part, optional) writes a better title and description, an answer-first
summary, an FAQ block, keyword and topic ideas, and the facts for schema.org
JSON-LD, which `jsonld(...)` serializes itself so the output is always valid
JSON. `llms_txt(...)` drafts an llms.txt file.
"""
import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import fetcher
import llm

AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User", "Claude-SearchBot",
               "PerplexityBot", "Google-Extended", "Applebot-Extended", "CCBot"]


class _Parser(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.links = []
        self.canonical = ""
        self.lang = ""
        self.headings = []
        self.images = []
        self.jsonld = []
        self.text = []
        self.paragraphs = []
        self._stack = []
        self._in_title = False
        self._in_ld = False
        self._ld = ""
        self._h = None
        self._p = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
            if "charset" in a:
                self.meta["charset"] = a["charset"]
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "canonical" in rel:
                self.canonical = a.get("href", "")
        elif tag == "a":
            self.links.append(a.get("href", ""))
        elif tag == "img":
            self.images.append({"src": a.get("src", ""), "alt": a.get("alt"), "has_alt": "alt" in a})
        elif tag in ("h1", "h2", "h3"):
            self._h = [tag, ""]
        elif tag == "p":
            self._p = ""
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld = True
            self._ld = ""
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3") and self._h and self._h[0] == tag:
            self.headings.append((tag, " ".join(self._h[1].split())))
            self._h = None
        elif tag == "p" and self._p is not None:
            t = " ".join(self._p.split())
            if t:
                self.paragraphs.append(t)
            self._p = None
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            try:
                self.jsonld.append(json.loads(self._ld))
            except ValueError:
                self.jsonld.append({"_invalid": True})
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_ld:
            self._ld += data
            return
        if self._skip:
            return
        if self._h is not None:
            self._h[1] += data
        if self._p is not None:
            self._p += data
        self.text.append(data)


def _types(obj) -> list[str]:
    out = []
    if isinstance(obj, list):
        for o in obj:
            out += _types(o)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if isinstance(t, list):
            out += [str(x) for x in t]
        elif t:
            out.append(str(t))
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in obj:
                out += _types(obj[k])
    return out


def parse(html: str, url: str = "") -> dict:
    p = _Parser()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001 — broken HTML still gives partial results
        pass
    text = " ".join(" ".join(p.text).split())
    host = urlsplit(url).hostname if url else None
    internal = external = 0
    for href in p.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        h = urlsplit(urljoin(url or "https://x.invalid/", href)).hostname
        if host and h == host or (not host and not href.startswith("http")):
            internal += 1
        else:
            external += 1
    types = []
    for block in p.jsonld:
        types += _types(block)
    return {"url": url, "title": " ".join(unescape(p.title).split()), "description": p.meta.get("description", ""),
            "robots": p.meta.get("robots", ""), "viewport": p.meta.get("viewport", ""), "lang": p.lang,
            "canonical": p.canonical, "og": {k: v for k, v in p.meta.items() if k.startswith("og:")},
            "twitter": {k: v for k, v in p.meta.items() if k.startswith("twitter:")},
            "author": p.meta.get("author", ""), "published": p.meta.get("article:published_time", ""),
            "headings": p.headings[:80], "paragraphs": p.paragraphs[:40], "images": len(p.images),
            "images_no_alt": sum(1 for i in p.images if not i["has_alt"]),
            "jsonld_types": types, "jsonld_invalid": sum(1 for b in p.jsonld if isinstance(b, dict) and b.get("_invalid")),
            "words": len(text.split()), "text": text[:20000], "internal_links": internal, "external_links": external}


def from_text(text: str) -> dict:
    """A pasted draft (no HTML): content checks only."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    heads = [("h2", ln.lstrip("# ").strip()) for ln in lines if ln.startswith("#") or (len(ln) < 90 and ln.endswith("?"))]
    paras = [ln for ln in lines if not ln.startswith("#")]
    return {"url": "", "title": "", "description": "", "robots": "", "viewport": "", "lang": "", "canonical": "",
            "og": {}, "twitter": {}, "author": "", "published": "", "headings": heads, "paragraphs": paras[:40],
            "images": 0, "images_no_alt": 0, "jsonld_types": [], "jsonld_invalid": 0,
            "words": len(" ".join(lines).split()), "text": " ".join(lines)[:20000], "internal_links": 0,
            "external_links": 0, "pasted": True}


def robots_ai(robots_txt: str | None) -> dict:
    """{crawler: 'blocked'|'allowed'} from robots.txt (simple group parsing)."""
    if robots_txt is None:
        return {}
    groups, agents, rules = [], [], []
    for raw in robots_txt.splitlines():
        line = raw.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        k, v = [x.strip() for x in line.split(":", 1)]
        k = k.lower()
        if k == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(v.lower())
        elif k in ("allow", "disallow"):
            rules.append((k, v))
    if agents:
        groups.append((agents, rules))
    out = {}
    for bot in AI_CRAWLERS:
        g = next((r for a, r in groups if bot.lower() in a), None) or next((r for a, r in groups if "*" in a), None)
        blocked = bool(g) and any(k == "disallow" and v == "/" for k, v in g) and not any(k == "allow" and v == "/" for k, v in g)
        out[bot] = "blocked" if blocked else "allowed"
    return out


def audit(page: dict, robots_txt: str | None = None, llms_txt: str | None = None) -> dict:
    """{'seo': [...], 'aeo': [...], 'geo': [...], 'score': int} — each item (level, title, detail)."""
    seo, aeo, geo = [], [], []
    pasted = page.get("pasted")
    t, d = page["title"], page["description"]
    h1s = [h for tag, h in page["headings"] if tag == "h1"]
    h2s = [h for tag, h in page["headings"] if tag == "h2"]
    if not pasted:
        if not t:
            seo.append(("fail", "No title tag", "Add a unique <title> of about 50-60 characters with the main topic first."))
        elif len(t) > 65:
            seo.append(("warn", f"Title is {len(t)} characters", "Search results usually cut titles around 60 characters."))
        elif len(t) < 20:
            seo.append(("warn", "Title is very short", "Say what the page is and who it's for in 50-60 characters."))
        else:
            seo.append(("pass", "Title length is good", t))
        if not d:
            seo.append(("fail", "No meta description", "Add a 140-160 character summary that answers why someone should click."))
        elif not 70 <= len(d) <= 165:
            seo.append(("warn", f"Meta description is {len(d)} characters", "Aim for 140-160."))
        else:
            seo.append(("pass", "Meta description length is good", d))
        if "noindex" in page["robots"].lower():
            seo.append(("fail", "The page tells search engines not to index it", f"robots meta: {page['robots']}"))
        if not page["canonical"]:
            seo.append(("warn", "No canonical link", "Add <link rel=\"canonical\"> so duplicates point to one URL."))
        if not page["viewport"]:
            seo.append(("fail", "No mobile viewport", "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">."))
        if not page["lang"]:
            seo.append(("warn", "No language on <html>", "Add lang=\"en\" (or your language)."))
        if not page["og"].get("og:title") or not page["og"].get("og:image"):
            seo.append(("warn", "Open Graph tags missing", "og:title, og:description and og:image control how links look when shared."))
        if page["images_no_alt"]:
            seo.append(("warn", f"{page['images_no_alt']} of {page['images']} image{'s' if page['images'] != 1 else ''} "
                        f"{'has' if page['images_no_alt'] == 1 else 'have'} no alt text",
                        "Describe each image in a few words; it helps search and accessibility."))
        if page["jsonld_invalid"]:
            seo.append(("fail", "A JSON-LD block is not valid JSON", "Search engines ignore broken structured data."))
    if len(h1s) == 0:
        seo.append(("fail" if not pasted else "warn", "No H1 heading", "Use one H1 that states the page's main topic."))
    elif len(h1s) > 1:
        seo.append(("warn", f"{len(h1s)} H1 headings", "Use a single H1; make the rest H2s."))
    else:
        seo.append(("pass", "One H1", h1s[0]))
    if page["words"] < 300:
        seo.append(("warn", f"Thin content ({page['words']} words)", "Pages with more useful detail rank and get quoted more."))
    if not pasted and page["internal_links"] < 3:
        seo.append(("warn", "Few internal links", "Link to related pages on your site so crawlers and readers find them."))

    first = page["paragraphs"][0] if page["paragraphs"] else ""
    qheads = [h for h in h2s + [h for tag, h in page["headings"] if tag == "h3"] if h.endswith("?")]
    types = set(page["jsonld_types"])
    if first and len(first.split()) <= 60:
        aeo.append(("pass", "Opens with a short summary", first[:200]))
    else:
        aeo.append(("warn", "No answer-first summary",
                    "Start with 2-3 sentences (40-60 words) that answer the main question directly."))
    if qheads:
        aeo.append(("pass", f"{len(qheads)} question-style heading{'s' if len(qheads) != 1 else ''}", "; ".join(qheads[:3])))
    else:
        aeo.append(("warn", "No question headings", "Phrase some H2s as the questions people ask, then answer right below."))
    if "FAQPage" in types:
        aeo.append(("pass", "FAQ structured data", "FAQPage JSON-LD found."))
    else:
        aeo.append(("warn", "No FAQ block", "Add a short FAQ (and FAQPage JSON-LD) with real customer questions."))
    if types & {"HowTo", "Product", "Article", "BlogPosting", "Service", "LocalBusiness", "Recipe", "Event", "Course"}:
        aeo.append(("pass", "Content type is marked up", ", ".join(sorted(types))))
    else:
        aeo.append(("warn", "No content-type structured data", "Mark up what the page is (Product, Article, Service…)."))
    if re.search(r"<li|^\s*[-*•]\s", page.get("text", ""), re.M) or len(page["paragraphs"]) >= 4:
        pass

    if types & {"Organization", "LocalBusiness", "Person", "Corporation", "Brand"}:
        geo.append(("pass", "The entity behind the page is marked up", ", ".join(sorted(types))))
    else:
        geo.append(("warn", "Who is behind this page isn't clear to machines",
                    "Add Organization JSON-LD with your name, logo, url and sameAs links to your profiles."))
    if not pasted:
        if page["author"] or page["published"] or types & {"Article", "BlogPosting", "NewsArticle"}:
            geo.append(("pass", "Author or date present", page["author"] or page["published"] or "Article markup"))
        else:
            geo.append(("warn", "No author or date", "AI assistants favour content that shows who wrote it and when."))
    if page["external_links"] >= 1 or re.search(r"\b(source|according to|study|data from)\b", page.get("text", ""), re.I):
        geo.append(("pass", "Cites sources", "Links out or names sources."))
    else:
        geo.append(("warn", "No sources cited", "Back up facts and numbers with links to where they come from."))
    if re.search(r"\b\d+(\.\d+)?\s?(%|percent|minutes|hours|days|ml|mg|g|kg|servings|customers)\b", page.get("text", ""), re.I):
        geo.append(("pass", "Specific facts and numbers", "Concrete details are easier for AI answers to quote."))
    else:
        geo.append(("warn", "Few specific facts", "Add concrete specs, sizes, times and prices that an answer can quote."))
    if not pasted:
        ai = robots_ai(robots_txt)
        blocked = [b for b, v in ai.items() if v == "blocked"]
        if robots_txt is None:
            geo.append(("info", "robots.txt not found", "Without one, all crawlers are allowed by default."))
        elif blocked:
            geo.append(("warn", f"robots.txt blocks {len(blocked)} AI crawler{'s' if len(blocked) != 1 else ''}",
                        ", ".join(blocked) + ". Blocked assistants can't read or cite this site. Keep this if it's deliberate."))
        else:
            geo.append(("pass", "AI crawlers are allowed in robots.txt", ""))
        if llms_txt:
            geo.append(("pass", "llms.txt found", "A plain-language map of the site for AI tools."))
        else:
            geo.append(("info", "No llms.txt", "Optional: a short Markdown file at /llms.txt that tells AI tools what "
                                               "your site is and which pages matter. A draft is below."))
    allitems = seo + aeo + geo
    weights = {"pass": 1.0, "info": 0.8, "warn": 0.45, "fail": 0.0}
    scored = [i for i in allitems if i[0] in weights]
    score = int(round(100 * sum(weights[i[0]] for i in scored) / max(1, len(scored))))
    return {"seo": seo, "aeo": aeo, "geo": geo, "score": score}


# ---------------------------------------------------------------- AI suggestions

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Improved title tag, 50-60 characters"},
        "description": {"type": "string", "description": "Improved meta description, 140-160 characters"},
        "h1": {"type": "string"},
        "summary": {"type": "string", "description": "Answer-first summary, 40-60 words, plain and factual"},
        "faq": {"type": "array", "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string", "description": "Direct answer, 1-3 sentences"}},
            "required": ["q", "a"]}},
        "keywords": {"type": "array", "items": {"type": "object", "properties": {
            "phrase": {"type": "string"}, "intent": {"type": "string", "enum": ["informational", "commercial",
                                                                               "transactional", "navigational"]},
            "note": {"type": "string"}}, "required": ["phrase", "intent"]}},
        "topics": {"type": "array", "items": {"type": "string"}, "description": "Content ideas that would earn links and citations"},
        "entity": {"type": "object", "properties": {
            "name": {"type": "string"}, "kind": {"type": "string", "enum": ["Organization", "LocalBusiness", "Person"]},
            "description": {"type": "string"}, "same_as": {"type": "array", "items": {"type": "string"}}}},
        "page_type": {"type": "string", "enum": ["Product", "Service", "Article", "WebPage", "HowTo"]},
        "fixes": {"type": "array", "items": {"type": "string"}, "description": "The 3-6 highest-impact fixes, in order"},
    },
    "required": ["title", "description", "summary", "faq", "keywords", "fixes"],
}

SYSTEM = ("You are a technical SEO and answer-engine optimization specialist. You improve pages so they rank in "
          "search, get picked as direct answers, and get cited accurately by AI assistants. Be factual: only use "
          "facts present in the page or the brand kit. Never invent statistics, reviews, prices or awards. If a fact "
          "is missing, leave it out. No health, income or discount claims beyond what the page already says.")


def suggest_estimate() -> int:
    return llm.estimate_credits(7000, 2200)


def suggest(page: dict, report: dict, brand: dict, goal: str = "", usage: llm.Usage | None = None) -> dict:
    issues = [f"- {lvl}: {t}" for grp in ("seo", "aeo", "geo") for lvl, t, _ in report[grp] if lvl in ("warn", "fail")]
    heads = "\n".join(f"{tag}: {h}" for tag, h in page["headings"][:30])
    prompt = (f"URL: {page['url'] or '(pasted draft)'}\nCurrent title: {page['title']}\nCurrent description: "
              f"{page['description']}\nHeadings:\n{heads}\n\nBrand kit: {json.dumps(brand)}\nGoal: {goal or 'more qualified visitors'}\n"
              f"Audit findings:\n" + "\n".join(issues[:30]) + f"\n\nPage text (truncated):\n{page['text'][:9000]}\n\n"
              "Write the improvements. FAQ: 4-6 questions real customers ask, answered from the page. Keywords: 8-12 "
              "phrases with search intent. Topics: 5 content ideas. Entity: who is behind the page, from the page only.")
    return llm.structured(system=SYSTEM, prompt=prompt, schema=SUGGEST_SCHEMA, name="submit_seo", max_tokens=2200,
                          usage=usage, temperature=0.4)


def jsonld(page: dict, sug: dict | None, brand: dict) -> str:
    """schema.org JSON-LD (@graph) built from facts we have. Always valid JSON."""
    url = page.get("url") or ""
    root = f"{urlsplit(url).scheme}://{urlsplit(url).hostname}/" if url else ""
    sug = sug or {}
    ent = sug.get("entity") or {}
    name = ent.get("name") or brand.get("name") or ""
    graph = []
    if name:
        org = {"@type": ent.get("kind") or "Organization", "name": name}
        if root:
            org["url"] = root
            org["@id"] = root + "#org"
        if ent.get("description"):
            org["description"] = ent["description"]
        same = [s for s in (ent.get("same_as") or []) if isinstance(s, str) and s.startswith("https://")]
        if same:
            org["sameAs"] = same
        graph.append(org)
    ptype = sug.get("page_type") or ("Product" if brand.get("product") else "WebPage")
    page_node = {"@type": ptype, "name": sug.get("h1") or page.get("title") or brand.get("product") or name,
                 "description": sug.get("description") or page.get("description") or sug.get("summary") or ""}
    if url:
        page_node["url"] = url
    if ptype == "Product" and name:
        page_node["brand"] = {"@type": "Brand", "name": name}
    if ptype in ("WebPage", "Article") and root and name:
        page_node["publisher"] = {"@id": root + "#org"}
    graph.append({k: v for k, v in page_node.items() if v})
    faq = [f for f in (sug.get("faq") or []) if isinstance(f, dict) and f.get("q") and f.get("a")]
    if faq:
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": f["q"], "acceptedAnswer": {"@type": "Answer", "text": f["a"]}} for f in faq[:8]]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, indent=2, ensure_ascii=False)


def llms_txt(page: dict, sug: dict | None, brand: dict) -> str:
    sug = sug or {}
    name = (sug.get("entity") or {}).get("name") or brand.get("name") or page.get("title") or "Your site"
    summary = sug.get("summary") or page.get("description") or "One or two sentences on what you offer and for whom."
    url = page.get("url") or "https://example.com/"
    lines = [f"# {name}", "", f"> {summary}", "", "## Key pages", "",
             f"- [{page.get('title') or 'Home'}]({url}): {page.get('description') or 'What this page covers.'}"]
    if sug.get("faq"):
        lines += ["", "## Common questions", ""] + [f"- {f['q']} {f['a']}" for f in sug["faq"][:5]]
    return "\n".join(lines) + "\n"


def run_audit(url: str | None, text: str | None):
    """(page, report, extras) for a URL or a pasted draft. Raises fetcher.FetchError."""
    robots_txt = llms = None
    if url:
        r = fetcher.fetch(url)
        if r["status"] >= 400:
            raise fetcher.FetchError(f"The page answered with HTTP {r['status']}.")
        if "html" not in r["content_type"].lower() and "<html" not in r["text"][:2000].lower():
            raise fetcher.FetchError("That link isn't a web page.")
        page = parse(r["text"], r["url"])
        parts = urlsplit(r["url"])
        root = f"{parts.scheme}://{parts.netloc}"
        for path, slot in (("/robots.txt", "robots"), ("/llms.txt", "llms")):
            try:
                x = fetcher.fetch(root + path, accept="text/plain,*/*;q=0.5")
                ok = x["status"] == 200 and "<html" not in x["text"][:500].lower()
            except fetcher.FetchError:
                ok, x = False, None
            if slot == "robots":
                robots_txt = x["text"] if ok else None
            else:
                llms = x["text"] if ok else None
    else:
        page = from_text(text or "")
    return page, audit(page, robots_txt, llms), {"robots_found": robots_txt is not None,
                                                      "ai_crawlers": robots_ai(robots_txt), "llms_found": bool(llms)}
