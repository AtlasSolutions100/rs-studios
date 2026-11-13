"""The auto-edit (motion_edit.py): a finished presenter video turned into a ready-to-run ad.

  * words: the script's spelling on the speech's timing (a brand the presenter said oddly is still captioned right),
    sentences and caption groups of at most three words split at punctuation
  * timing without a speech model: syllable counts, and clauses matched to stretches of speech on a real tone track
  * the plan, from a real take's word timings: the hook banner (no leading filler, last two words highlighted), the
    brand chip, a name chip, the connection scene ("connect it to Claude"), the spoken list as a checklist, the
    offer badge only with a saved offer, approved claims only, the end card on the last line that names the brand;
    nothing overlaps, and every word drawn comes from the script, the brand kit, the product or the CTA
  * "say it as": the video model gets the spelling, captions keep the name
  * the switch: AUTO_EDIT off (the default) adds nothing to a batch; on, presenter videos are marked and the worker
    edits only those (never a voice sample, keyframe video or preview); a failed edit keeps the take
  * the render (real ffmpeg on a tiny clip made here): same length, video and sound, the raw take kept, sound mixed

No network. The render checks need ffmpeg; without it they are skipped and the suite says so.

    python tests/test_motion_edit.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_me_")
os.environ.pop("AUTO_EDIT", None)

import motion_edit as m  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    passed += 1


SCRIPT = ("Okay, you can literally turn Claude into a marketing genius. There's this tool called Campaignify. It has an "
          "MCP, so you connect it to Claude in like two minutes. Then you just ask: make ads for my product, write the "
          "copy, plan my launch. It shows you the price first, then builds it all. Campaignify dot A I.")
# what a speech model heard on the real take (the brand came out as "campaign -fied" and "Campaignfied .ai")
HEARD = [("Okay,", 0.0), ("you", .56), ("can", .64), ("literally", .78), ("turn", 1.22), ("Claude", 1.76),
         ("into", 2.04), ("a", 2.72), ("marketing", 2.86), ("genius.", 3.24), ("There's", 4.44), ("this", 4.54),
         ("tool", 4.64), ("called", 4.94), ("campaign", 5.28), ("-fied.", 5.64), ("It", 6.58), ("has", 6.72),
         ("an", 6.86), ("MCP,", 7.0), ("so", 7.46), ("you", 8.08), ("connect", 8.18), ("it", 8.42), ("to", 8.6),
         ("Claude", 8.7), ("in", 8.9), ("like", 9.14), ("two", 9.28), ("minutes,", 9.52), ("then", 9.8),
         ("you", 10.42), ("just", 10.5), ("ask,", 10.68), ("make", 11.26), ("ads", 11.68), ("for", 11.92),
         ("my", 12.18), ("product,", 12.28), ("write", 13.06), ("the", 13.14), ("copy,", 13.34), ("plan", 14.08),
         ("my", 14.24), ("launch.", 14.44), ("It", 15.26), ("shows", 15.4), ("you", 15.6), ("the", 15.78),
         ("price", 15.88), ("first,", 16.06), ("then", 16.78), ("builds", 16.92), ("it", 17.12), ("all.", 17.36),
         ("Campaignfied", 18.76), (".ai", 19.24)]
WORDS = [{"w": w, "s": s, "e": round((HEARD[i + 1][1] if i + 1 < len(HEARD) else s + 0.6) - 0.04, 3)}
         for i, (w, s) in enumerate(HEARD)]
BRAND = {"name": "Campaignify", "logo": "", "primary": "#0E0E10", "accent": "#C8FF3D", "url": "campaignify.ai"}
CTX = {"script": SCRIPT, "duration": 20.06, "brand": BRAND,
       "product": {"name": "Campaignify", "line": "", "offer": "Try it free for 3 days", "claims": []},
       "cta": "Try it free"}

# ---------------------------------------------------------------- words
al = m.align(WORDS, SCRIPT)
toks = [w["w"] for w in al]
check(toks == m.script_tokens(SCRIPT), "aligned words are exactly the script's words")
camp = [w for w in al if w["w"].startswith("Campaignify")]
check(len(camp) == 2 and abs(camp[0]["s"] - 5.28) < 0.01, "the misheard brand takes the script's spelling and time")
check(all(a["s"] <= b["s"] for a, b in zip(al, al[1:])), "timings never go backwards")
check(all(w["e"] > w["s"] for w in al), "every word has length")
sents = m.sentences(al)
check(len(sents) == 6 and sents[0][-1]["w"] == "genius.", "six sentences, split at the script's full stops")
groups = m.caption_groups(al)
check(all(len(g) <= 3 for g in groups), "captions show at most three words")
check(all(not any(x["w"][-1] in ",.!?:" for x in g[:-1]) for g in groups), "a caption never runs past punctuation")
check(m.align(WORDS, "") == WORDS, "no script: the heard words as they are")

# ---------------------------------------------------------------- timing without a speech model
check(m.syllables("MCP") == 3 and m.syllables("genius.") >= 2 and m.syllables("a") == 1, "syllable counts")
check(m.syllables("$29") >= 3, "numbers and symbols are counted as said")

HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
if HAVE_FF:
    # three bursts of tone with gaps: "One two three. Four five. Six seven eight nine."
    tone = os.path.join(TMP, "tone.wav")
    expr = "if(between(t,0.2,1.6)+between(t,2.2,3.0)+between(t,3.6,5.6),0.5*sin(2*PI*220*t),0)"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=16000:d=6", tone],
                   check=True)
    spans = m.speech_spans(tone, 6.0)
    check(len(spans) == 3 and abs(spans[1][0] - 2.2) < 0.1, f"stretches of speech found between pauses: {spans}")
    est = m.estimate(tone, "One two three. Four five. Six seven eight nine.", 6.0)
    check([w["w"] for w in est][3] == "Four" and abs(est[3]["s"] - 2.2) < 0.15, "a clause starts where speech does")
    check(abs(est[5]["s"] - 3.6) < 0.15 and est[-1]["e"] <= 5.7, "the last clause fills the last stretch")
else:
    print("ffmpeg not found: timing and render checks skipped")

# ---------------------------------------------------------------- the plan
plan = m.make_plan(al, CTX)
ev = {e["kind"]: e for e in plan["events"]}
check(ev["hook"]["line1"] == "Turn Claude into a" and ev["hook"]["highlight"] == "marketing genius",
      "hook: the first line without the filler, last two words highlighted, no full stop")
check(ev["hook"]["t1"] <= m.HOOK_MAX, "the hook leaves in time")
check(abs(ev["brand"]["t0"] - 5.23) < 0.1, "the brand chip pops on the brand name")
check(ev.get("name", {}).get("text") == "MCP", "a name the presenter says gets a chip")
check(ev["link"]["target"] == "Claude" and abs(ev["link"]["t_hit"] - 8.7) < 0.01,
      "'connect it to Claude': the line lands on 'Claude'")
check([i["text"] for i in ev["list"]["items"]] == ["make ads for my product", "write the copy", "plan my launch"],
      "the spoken list becomes a checklist, item by item")
check(ev["list"]["title"] == "just ask", "the words before the colon title the list")
check(ev["offer"]["text"] == "Try it free for 3 days", "the saved offer shows when price is mentioned")
check(17.6 < plan["end_t"] < 18.8, f"the end card comes in on the last line that names the brand ({plan['end_t']})")
spans = sorted((e["t0"], e["t1"]) for e in plan["events"] if e["kind"] != "end")
check(all(b[0] >= a[1] - 0.25 for a, b in zip(spans, spans[1:])), "graphics don't pile on each other")
check(all(e["t0"] < plan["end_t"] for e in plan["events"] if e["kind"] != "end"), "nothing starts under the end card")

no_offer = m.make_plan(al, dict(CTX, product=dict(CTX["product"], offer="")))
check(not any(e["kind"] == "offer" for e in no_offer["events"]), "no saved offer, no offer badge")
claimed = m.make_plan(al, dict(CTX, product=dict(CTX["product"], claims=["Write the copy for every platform"])))
check(any(e["kind"] == "claim" and e["text"] == "Write the copy for every platform" for e in claimed["events"])
      or any(e["kind"] == "list" for e in claimed["events"]), "an approved claim said aloud can show as a card")
check(not m.claim_hits(al, ["Doubles your sales overnight"]), "a claim that isn't said is never shown")

# every word drawn comes from what the member gave
allowed = " ".join([SCRIPT, BRAND["name"], BRAND["url"], CTX["cta"], CTX["product"]["offer"], "Connected",
                    "AI presenter"]).lower()
for e in plan["events"]:
    for k in ("line1", "highlight", "title", "text", "target"):
        for word in str(e.get(k) or "").lower().split():
            check(word.strip(".,:") in allowed, f"'{word}' on screen comes from the member's own words")
    for it in e.get("items") or []:
        check(it["text"].lower() in SCRIPT.lower(), "checklist items are the script's own words")

# ---------------------------------------------------------------- "say it as"
check(m.spoken("Try Campaignify today. campaignify.ai", "Campaignify", "Cam-pain-ih-fy")
      == "Try Cam-pain-ih-fy today. Cam-pain-ih-fy.ai", "the spelling replaces the written name for the video model")
check(m.spoken("Campaignify-style", "Campaignify", "X") == "Campaignify-style", "only the whole name")
check(m.spoken("Hi", "", "X") == "Hi" and m.spoken("Hi", "Brand", "") == "Hi", "nothing set, nothing changes")
check(m._site("https://www.Campaignify.ai/pricing?x=1") == "campaignify.ai" and m._site("not a url") == "",
      "the end card shows the bare web address")

# ---------------------------------------------------------------- the switch
check(not m.enabled(), "AUTO_EDIT is off by default")
os.environ["AUTO_EDIT"] = "1"
check(m.enabled(), "AUTO_EDIT=1 turns it on")
os.environ.pop("AUTO_EDIT")


class Row(dict):
    def keys(self):
        return list(super().keys())


def job(**p):
    return Row(id=7, tool=p.pop("tool", "ugc_video"), params_json=json.dumps(p), brief=SCRIPT, workspace_id=1,
               brand_id=1, product_id=1)


check(m.wanted(job(auto_edit=True)), "a presenter video made with it on is edited")
check(not m.wanted(job()), "made with it off: never edited later")
check(not m.wanted(job(auto_edit=True, voice_sample=True)), "never a voice sample")
check(not m.wanted(job(auto_edit=True, pipeline="keyframe_video")), "never a keyframe video")
check(not m.wanted(job(auto_edit=True, preview=True)), "never a free preview")
check(not m.wanted(job(auto_edit=True, tool="ad_image")), "never an image")

import worker  # noqa: E402
check(worker._auto_edit(None, job(), "/nope.mp4") is False, "the worker skips jobs without the mark, cheaply")

# the job's context: everything from the member's saved brand and product
db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
db.executescript("""CREATE TABLE brands (id INTEGER, workspace_id INTEGER, brand_name TEXT, website TEXT, say_as TEXT,
                    kit_primary TEXT, kit_accent TEXT, kit_font TEXT, kit_logo TEXT);
                    CREATE TABLE products (id INTEGER, workspace_id INTEGER, name TEXT, offer TEXT, claims TEXT,
                    description TEXT, category TEXT);
                    CREATE TABLE photos (id INTEGER, workspace_id INTEGER, path TEXT);""")
db.execute("INSERT INTO brands VALUES (1,1,'Campaignify','https://campaignify.ai/','Cam-pain-ih-fy','#0e0e10',"
           "'#c8ff3d','sans','')")
db.execute("INSERT INTO products VALUES (1,1,'Campaignify','Try it free for 3 days','Ads in minutes\n- Plans "
           "included','Ad creative from your product. More words follow here.','')")
ctx = m.context_for_job(db, job(auto_edit=True, cta="Start free"))
check(ctx["brand"]["url"] == "campaignify.ai" and ctx["brand"]["aliases"] == ["Cam-pain-ih-fy"], "brand kit read")
check(ctx["brand"]["accent"].lower() == "#c8ff3d", "the brand's own accent colour")
check(ctx["product"]["claims"] == ["Ads in minutes", "Plans included"], "approved claims as a list")
check(ctx["product"]["line"] == "Ad creative from your product." and ctx["cta"] == "Start free",
      "a short line from the description and the job's button text")
other = m.context_for_job(db, Row(job(auto_edit=True), workspace_id=2))
check(other["brand"]["name"] == "" and other["product"]["name"] == "", "another workspace's brand is never read")

# ---------------------------------------------------------------- render
if HAVE_FF:
    src = os.path.join(TMP, "take.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=180x320:r=24:d=4",
                    "-f", "lavfi", "-i", "sine=f=300:d=4", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", src], check=True)
    short = "Okay, turn your phone into a studio. Try Campaignify."
    words = [{"w": w, "s": round(0.3 + i * 0.38, 2), "e": round(0.62 + i * 0.38, 2)}
             for i, w in enumerate(m.script_tokens(short))]
    out = os.path.join(TMP, "edit.mp4")
    p = m.auto_edit(src, out, dict(CTX, script=short, duration=None), words=words)
    info = m.probe(out)
    check(info.get("audio") and info.get("width") == 180, "the edit has video and sound at the take's size")
    check(abs(info["duration"] - 4.0) < 0.25, "same length as the take")
    check(any(e["kind"] == "hook" for e in p["events"]) and any(e["kind"] == "end" for e in p["events"]),
          "a short take still gets a hook and an end card")
    stills = m.render(src, os.path.join(TMP, "prev"), p, CTX, preview_at=[0.8, 3.6])
    check(len(stills) == 2 and all(os.path.getsize(s) > 1000 for s in stills), "preview stills")
    wav = os.path.join(TMP, "sfx.wav")
    m.sfx(p, wav)
    check(m.probe(wav).get("audio"), "the sound effects track is made with ffmpeg alone")

    # the worker's path: the take is edited in place and the raw take kept; a failure keeps the take
    take = os.path.join(TMP, "campaignify-7-1.mp4")
    shutil.copy(src, take)
    orig = m.auto_edit
    m.auto_edit = lambda a, b, c, words=None: orig(a, b, dict(c, script=short), words=words)
    check(m.edit_job_output(db, job(auto_edit=True), take), "edited in place")
    check(os.path.exists(os.path.join(TMP, "campaignify-7-1-raw.mp4")), "the raw take is kept beside it")
    m.auto_edit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    keep = os.path.join(TMP, "campaignify-8-1.mp4")
    shutil.copy(src, keep)
    size = os.path.getsize(keep)
    check(m.edit_job_output(db, job(auto_edit=True), keep) is False and os.path.getsize(keep) == size,
          "a failed edit leaves the take exactly as it was")
    check(not os.path.exists(keep + ".edit.mp4"), "and no half-made file")
    m.auto_edit = orig

shutil.rmtree(TMP, ignore_errors=True)
print(f"{passed} auto-edit checks passed")
