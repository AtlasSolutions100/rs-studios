"""The free hook generator on the public site (hooktool.py, /tools/ai-hook-generator), on a throwaway data folder with
the text model stubbed:

  * the page is public, in the sitemap and the footer, and its form carries the CSRF token and the hidden field
  * a run shows the model's hooks, minus any that trip a claim rule, and counts one against the day
  * a brief with a blocked claim is refused with the rule's message and never reaches the model
  * five runs a day for a visitor, HOOK_TOOL_DAILY_MAX for the site, HOOK_TOOL=0 turns the page off
  * a model error, or no model at all, is a sentence for the visitor, never a 500

    python tests/test_hook_tool.py      (tests/test_app.py runs it too)
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_hooktool_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")
for k in ("UI_V2", "HOME_V2", "HOOK_TOOL", "HOOK_TOOL_DAILY_MAX", "ANTHROPIC_API_KEY", "MCP_SERVER"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import compliance  # noqa: E402
import db  # noqa: E402
import hooktool  # noqa: E402
import llm  # noqa: E402
import public_site  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


app = appmod.app
PATH = hooktool.PATH
BAD = "Earn $5,000 a month from home"
check(not compliance.check(BAD)["ok"], "the claim rules block an income promise (the test's bad line)")

calls = []
GOOD = [{"angle": f"angle {i}", "text": f"Your coffee is cold by nine, number {i}"} for i in range(1, 8)]


def fake_structured(**kw):
    calls.append(kw)
    return {"hooks": GOOD + [{"angle": "the result", "text": BAD}, {"angle": "extra", "text": "One more plain line"}]}


llm.structured = fake_structured
llm.configured = lambda: True


def visitor(ip):
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip
    return c


def run(c, **form):
    page = c.get(PATH).get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return c.post(PATH, data=dict({"csrf": tok, "product": "A 12 oz steel travel mug", "audience": "early commuters",
                                   "platform": "short"}, **form))


print("== the page")
c = visitor("10.0.0.1")
r = c.get(PATH)
h = r.get_data(as_text=True)
check(r.status_code == 200 and "<h1>AI hook generator for ads</h1>" in h and 'name="csrf"' in h and 'name="website"' in h
      and all(v in h for v in hooktool.PLATFORMS.values()), "public, with the form, the CSRF token and the hidden field")
check("noindex" not in h and PATH in public_site.sitemap_paths()
      and any(href == PATH for _, links in public_site.footer_groups() for href, _ in links), "indexable, in the sitemap and the footer")
main = h.split("<main", 1)[1]
check(public_site.guide_path("ugc-ad-hooks") in main and 'href="/signup"' not in main.split('id="how"')[0],
      "it links the hooks guide, and asks for nothing before the hooks")

print("== a run")
r = run(c)
h = r.get_data(as_text=True)
check(r.status_code == 200 and len(calls) == 1 and "Your 8 hooks" in h and all(g["text"] in h for g in GOOD) and "One more plain line" in h,
      "the model's hooks are shown")
check(BAD not in h and "$5,000" not in h, "a hook that trips a claim rule is dropped")
check(calls[0]["model_id"] == llm.fast_model() and "A 12 oz steel travel mug" in calls[0]["prompt"]
      and hooktool.PLATFORMS["short"] in calls[0]["prompt"] and "not instructions" in calls[0]["system"],
      "it uses the fast model, and the brief goes in as data")
with db.connect() as conn:
    check(hooktool.used_today(conn) == 1, "one run is counted against the day")
check('href="/signup"' in h and 'data-cta="hook-tool"' in h, "after the hooks, one call to action")

print("== refusals")
n = len(calls)
r = run(c, product=BAD)
check(len(calls) == n and "claim our ads can&#39;t make" in r.get_data(as_text=True), "a brief with a blocked claim never reaches the model")
r = run(c, product="x")
check(len(calls) == n and "Say what you sell" in r.get_data(as_text=True), "an empty brief asks for one")
r = run(c, website="http://spam.example")
check(r.status_code == 302 and len(calls) == n, "the hidden field sends a script back to the form")
r = c.post(PATH, data={"product": "A mug"})
check(r.status_code in (400, 403) and len(calls) == n, "no CSRF token, no run")

print("== limits")
c2 = visitor("10.0.0.2")
for _ in range(hooktool.PER_VISITOR):
    check("Your 8 hooks" in run(c2).get_data(as_text=True), "a run within the visitor's daily limit")
n = len(calls)
r = run(c2)
check(len(calls) == n and f"{hooktool.PER_VISITOR} runs today" in r.get_data(as_text=True), "the sixth run today is refused, with why")
check("Your 8 hooks" in run(visitor("10.0.0.3")).get_data(as_text=True), "another visitor is not affected")
os.environ["HOOK_TOOL_DAILY_MAX"] = "3"
n = len(calls)
r = run(visitor("10.0.0.4"))
check(len(calls) == n and "today&#39;s limit" in r.get_data(as_text=True), "past the site's daily cap nobody runs it")
os.environ.pop("HOOK_TOOL_DAILY_MAX")

print("== when the model is not there")


def boom(**kw):
    raise RuntimeError("upstream")


llm.structured = boom
r = run(visitor("10.0.0.5"))
check(r.status_code == 200 and "could not finish" in r.get_data(as_text=True), "a model error is a sentence, not a 500")
llm.configured = lambda: False
r = run(visitor("10.0.0.6"))
check(r.status_code == 200 and "not available right now" in r.get_data(as_text=True), "no model configured: the page says so")
os.environ["HOOK_TOOL"] = "0"
check(visitor("10.0.0.7").get(PATH).status_code == 404 and PATH not in public_site.sitemap_paths(), "HOOK_TOOL=0: no page, not in the sitemap")
os.environ.pop("HOOK_TOOL")

print(f"\nall {passed} checks passed")
