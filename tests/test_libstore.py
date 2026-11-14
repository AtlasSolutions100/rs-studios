"""The library store (libstore.py): gallery clips kept outside the deploy bundle, on a throwaway data folder with
the fetch faked. Nothing leaves the machine.

  * the manifest: store items name a file, its size and its SHA-256; the source is this repository at one commit
  * a store item is not in the repo; until its file is on the volume it is left out of the gallery and Inspiration
  * sync: fetches what is missing, keeps only a file whose size and hash match, never raises, can be called again
  * the background pass: nothing while nothing is missing, one try, then a wait before the next
  * /lib/<name>: only a name the manifest lists, cached for a year, no way out of the folder
  * the admin page shows the count and "Fetch them now"; a bad manifest entry is left out, not trusted
  * LIB_STORE=0 switches it off

    python tests/test_libstore.py      (tests/test_app.py runs it too)
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_libstore_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_EMAIL_VERIFY="0", IMPORT_ENRICH="0", UI_V2="1")
for k in ("HOME_V2", "RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "PLANS_ONLY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
          "STRIPE_SECRET_KEY", "LIB_STORE"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import inspiration  # noqa: E402
import libstore  # noqa: E402
import public_site  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


# ================================================================ the manifest as shipped
print("the manifest as shipped")
raw = json.load(open(libstore.MANIFEST, encoding="utf-8"))
want = libstore.wanted()
stored = [it for it in raw["items"] if isinstance(it.get("store"), dict)]
check(len(want) == len(stored) >= 1 and re.fullmatch(r"[0-9a-f]{40}", raw["_store"]["commit"]),
      f"{len(want)} store item(s), all from this repository at one pinned commit")
check(all(w["url"] == f"https://raw.githubusercontent.com/AtlasSolutions100/rs-studios/{raw['_store']['commit']}/"
                      f"{raw['_store']['dir']}/{w['name']}" for w in want),
      "each is read from raw.githubusercontent.com, this repository, that commit, that folder")
check(all(re.fullmatch(r"[0-9a-f]{64}", w["sha256"]) and 0 < w["bytes"] < 600_000 for w in want),
      "each names its SHA-256 and its size, and is under 600 KB like every gallery clip")
check(not any(os.path.exists(os.path.join(ROOT, "static", "showcase", w["name"])) for w in want),
      "a store item's file is not in the repo (that is the point)")
check(all(it.get("kind") == "video" and os.path.isfile(os.path.join(ROOT, "static", "showcase", it["poster"])) for it in stored),
      "its poster is: the tile has a picture at once")
first16 = [it["file"] for it in raw["items"][:public_site.SHOWCASE_MAX]]
_home = public_site.showcase()      # no store file has arrived on this machine
check(len(_home) == public_site.SHOWCASE_MAX and not any(it["store"] for it in _home)
      and max(len(m.group()) for m in re.finditer(r"v+", "".join(it["kind"][0] for it in _home))) <= 2,
      "store clips may be among the home page's sixteen: until they arrive the grid is filled from further down "
      "and still never runs three videos in a row")

# ================================================================ nothing fetched yet
print("before the first fetch")
names = {w["name"] for w in want}
check(libstore.missing() == want and all(libstore.have(n) is None for n in names) and libstore.status()["there"] == 0,
      "nothing is on the volume, and the store says so")
shown = public_site.showcase(1000)
check(not any(s["store"] for s in shown) and len(shown) == len(raw["items"]) - len(want),
      "the gallery leaves out a store item whose file is not there yet")
c = appmod.app.test_client()
check(c.get(f"/lib/{want[0]['name']}").status_code == 404, "and its address answers 404")

# ================================================================ sync
print("sync")
real = {w["name"]: os.urandom(64) for w in want}      # stand-ins of the wrong size and hash
calls = []


def bad_fetch(url, max_bytes):
    calls.append(url)
    return real[url.rsplit("/", 1)[1]]


res = libstore.sync(fetch=bad_fetch)
check(res["fetched"] == 0 and sorted(res["failed"]) == sorted(names) and libstore.missing() == want
      and not [f for f in os.listdir(libstore.folder()) if not f.startswith(".")],
      "a file that is not the one the manifest names (size or hash) is refused and nothing is kept")


def boom(url, max_bytes):
    raise OSError("network down")


res = libstore.sync(fetch=boom)
check(res["fetched"] == 0 and len(res["failed"]) == len(want), "a fetch that fails is reported, never raised")

# Make stand-in files and point the store's idea of them at their real size and hash (the shipped manifest's
# hashes are of clips this test does not have).
good = {w["name"]: (b"clip:" + w["name"].encode()) * 50 for w in want}
fake = [dict(w, sha256=hashlib.sha256(good[w["name"]]).hexdigest(), bytes=len(good[w["name"]])) for w in want]
libstore._state["wanted"] = fake
calls.clear()
res = libstore.sync(fetch=lambda url, max_bytes: calls.append((url, max_bytes)) or good[url.rsplit("/", 1)[1]])
check(res["fetched"] == len(fake) and not res["failed"] and not libstore.missing()
      and all(open(libstore.have(w["name"]), "rb").read() == good[w["name"]] for w in fake),
      "a file with the right size and hash is kept")
check(all(u.startswith("https://raw.githubusercontent.com/AtlasSolutions100/rs-studios/") and mb == w["bytes"]
          for (u, mb), w in zip(calls, fake)), "each fetch asks that one origin, for at most the size named")
check(not [f for f in os.listdir(libstore.folder()) if f.startswith(".")], "no half-written file is left behind")
calls.clear()
res = libstore.sync(fetch=lambda url, max_bytes: calls.append(url) or b"")
check(res == {"fetched": 0, "kept": len(fake), "failed": []} and not calls, "a second sync fetches nothing")

# ================================================================ the gallery and the address
print("once the files are there")
shown = public_site.showcase(1000)
mine = [s for s in shown if s["store"]]
check(len(shown) == len(raw["items"]) and len(mine) == len(fake), "the gallery shows the store items without a restart")
w0 = fake[0]
s0 = next(s for s in mine if s["url"].startswith(f"/lib/{w0['name']}"))
check(s0["url"] == f"/lib/{w0['name']}?v={w0['sha256'][:12]}" and s0["poster_url"].startswith("/static/showcase/"),
      "a store item's clip is at /lib/<name>?v=<hash>; its poster stays in static")
check(all(s["url"] == "/static/" + s["src"] for s in shown if not s["store"]), "every other item is where it was")
r = c.get(s0["url"])
check(r.status_code == 200 and r.data == good[w0["name"]] and r.mimetype == "video/mp4"
      and "max-age=31536000" in r.headers.get("Cache-Control", ""), "the address serves the clip, cached for a year")
check(c.get("/lib/..%2F..%2Fcampaignify.db").status_code == 404 and c.get("/lib/manifest.json").status_code == 404
      and c.get("/lib/nothing.mp4").status_code == 404, "only a name the manifest lists is served")
with appmod.app.test_request_context("/app/tools"):
    cards = [x for grp in inspiration.groups(None) for x in grp["items"]]
check(any(x["video"] == s0["url"] and x["image"] == s0["poster_url"] for x in cards),
      "Inspiration shows it too, clip from the store and poster from static")
home = c.get("/").get_data(as_text=True)
check('<source src="/static/showcase/' in home and "url_for" not in home, "the home page's gallery still renders")
os.remove(libstore.have(w0["name"]))
check(libstore.have(w0["name"]) is None and len(public_site.showcase(1000)) == len(raw["items"]) - 1,
      "a store file that goes missing drops its item again")

# ================================================================ the background pass
print("the background pass")
n = [0]
libstore._fetch = lambda url, max_bytes: n.__setitem__(0, n[0] + 1) or b"wrong"
libstore._state["next_try"] = 0.0
out = libstore.tick(now=1000.0)
check(out is not None and n[0] == 1 and out["failed"] == [w0["name"]], "one file is missing, so the pass tries once")
check(libstore.tick(now=1000.0 + libstore.RETRY_S - 1) is None and n[0] == 1, "after a failed try it waits")
libstore._fetch = lambda url, max_bytes: good[url.rsplit("/", 1)[1]]
out = libstore.tick(now=1000.0 + libstore.RETRY_S + 1)
check(out["fetched"] == 1 and not libstore.missing(), "then tries again and gets it")
check(libstore.tick(now=99999.0) is None, "with nothing missing the pass does nothing")

# ================================================================ the admin page
print("the admin page")


def csrf(cl):
    with cl.session_transaction() as s:
        return s["csrf"]


a = appmod.app.test_client()
a.get("/login")
a.post("/signup", data={"csrf": csrf(a), "email": "admin@example.com", "password": "longpassword1"})
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=1")
    conn.commit()
page = a.get("/admin/library").get_data(as_text=True)
check(f"<b>{len(fake)}</b> of {len(fake)} on this server" in page and "Fetch them now" not in page,
      "the admin library page says how many store files are on the server")
os.remove(libstore.have(w0["name"]))
page = a.get("/admin/library").get_data(as_text=True)
check(f"<b>{len(fake) - 1}</b> of {len(fake)}" in page and w0["name"] in page and "Fetch them now" in page,
      "a missing one is named, with a button")
r = a.post("/admin/library", data={"csrf": csrf(a), "do": "store_sync"})
check(r.status_code == 302 and not libstore.missing(), "the button fetches it")
m = appmod.app.test_client()
m.get("/login")
m.post("/signup", data={"csrf": csrf(m), "email": "member@example.com", "password": "longpassword1"})
with db.connect() as conn:
    conn.execute("UPDATE users SET is_admin=0 WHERE email='member@example.com'")
    conn.commit()
os.remove(libstore.have(w0["name"]))
r = m.post("/admin/library", data={"csrf": csrf(m), "do": "store_sync"})
check(r.status_code in (302, 403, 404) and m.get("/admin/library").status_code != 200 and len(libstore.missing()) == 1,
      "a member cannot open the page or press the button")

# ================================================================ a manifest that is not right
print("a manifest that is not right")
bad = os.path.join(TMP, "manifest.json")
ok_sha = "a" * 64


def load(doc):
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    libstore.MANIFEST = bad
    libstore._state["key"] = None
    return [w["name"] for w in libstore.wanted()]


src = {"commit": "b" * 40, "dir": "assets_staging/showcase_540"}
items = [{"file": "fine.mp4", "store": {"sha256": ok_sha, "bytes": 10}},
         {"file": "../up.mp4", "store": {"sha256": ok_sha, "bytes": 10}},
         {"file": "sub/dir.mp4", "store": {"sha256": ok_sha, "bytes": 10}},
         {"file": "script.js", "store": {"sha256": ok_sha, "bytes": 10}},
         {"file": "nohash.mp4", "store": {"sha256": "abc", "bytes": 10}},
         {"file": "huge.mp4", "store": {"sha256": ok_sha, "bytes": libstore.MAX_FILE + 1}},
         {"file": "nosize.mp4", "store": {"sha256": ok_sha, "bytes": "10"}},
         {"file": "fine.mp4", "store": {"sha256": ok_sha, "bytes": 10}},
         {"file": "plain.mp4"}]
check(load({"_store": src, "items": items}) == ["fine.mp4"],
      "a path in the name, another kind of file, a hash that is not one, a size over the limit or not a number, "
      "and a name listed twice are all left out")
check(load({"_store": dict(src, commit="main"), "items": items}) == [], "a branch name is not a commit: nothing is wanted")
check(load({"_store": dict(src, dir="../elsewhere"), "items": items}) == [] and load({"items": items}) == [],
      "nor is a folder that climbs out, or no source at all")
load({"_store": src, "items": items})
os.environ["LIB_STORE"] = "0"
check(not libstore.missing() and libstore.have("fine.mp4") is None and libstore.sync(fetch=boom)["fetched"] == 0
      and libstore.tick() is None, "LIB_STORE=0: nothing is fetched and nothing is served")
os.environ.pop("LIB_STORE")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nlibrary store: {passed} checks passed")
