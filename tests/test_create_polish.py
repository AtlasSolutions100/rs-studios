"""Create polish (roadmap M5.5: UX-3, UX-11, UX-12, COPY-3, UX-17, UX-8), on a throwaway database:

  * UX-3: every image template has its own preview still (and its hover video), all different, all 432x768
  * COPY-3: the credits pill says "1 credit", not "1 credits"
  * UX-8: the pill and the phone chip carry hooks the Create review writes the quoted balance into
  * UX-12: a failed ad from a batch leads with "Edit and retry"; "Try again" with the same brief is the dark button,
    priced in credits; a failure on our side (401/402/403) leads with "Try again"
  * UX-11: the hub's export rail and the page editor's media picker label each thumbnail with the hook's first
    3 words and the shape, and the hub says "Pick an ad" above the row
  * UX-17: a draft page has "Save draft" and "Publish"; a live page has one "Save and update live page" button and
    says that saving changes the live page

    python tests/test_create_polish.py      (tests/test_app.py runs it too)
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_createpolish_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                  RS_SIGNUP_CONFIRM="0", RS_DISK_MIN_GB="0.2", RS_PUBLIC_URL="https://campaignify.test")

import adhub  # noqa: E402
import app as appmod  # noqa: E402
import catalog  # noqa: E402
import db  # noqa: E402
import worker  # noqa: E402
from PIL import Image  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.12.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def signup(email, plan="starter"):
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": email, "password": "longpassword"})
    assert r.status_code == 302, r.status_code
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE workspaces SET plan_override=? WHERE id=?", (plan, u["workspace_id"]))
        conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
        conn.commit()
    return c, u


def text(fragment):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


try:
    print("== UX-3: one real preview per image template")
    image_tpls = [t for t in catalog.TEMPLATES if t["tool"] == "ad_image"]
    any_tpls = [t for t in image_tpls if t.get("fits") == "any"]
    check(len(any_tpls) >= 5, "the 5 templates for any product are image templates")
    pv = [t["preview"] for t in image_tpls]
    check(len(pv) == len(set(pv)), "no two image templates share a preview")
    others = {t["preview"] for t in catalog.TEMPLATES if t["tool"] != "ad_image"}
    check(not (set(pv) & others), "no image template borrows a video template's preview")
    for t in image_tpls:
        jpg = os.path.join(ROOT, "static", "previews", t["preview"] + ".jpg")
        mp4 = os.path.join(ROOT, "static", "previews", t["preview"] + ".mp4")
        ok = os.path.exists(jpg) and os.path.exists(mp4)
        if ok:
            with Image.open(jpg) as im:
                ok = im.size == (432, 768) and os.path.getsize(jpg) < 80_000
        check(ok, f"{t['name']}: a 432x768 still under 80 KB and a hover video")

    c, u = signup("owner@example.com")
    ws = u["workspace_id"]

    print("== COPY-3 and UX-8: one balance, the right word")
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET credits=1, plan_credits=0 WHERE id=?", (ws,))
        conn.commit()
    html = c.get("/app/library").get_data(as_text=True)
    pill = html.split('class="pill"', 1)[1].split("</a>", 1)[0]
    check("1 credit<" in pill and "1 credits" not in html, "the pill says 1 credit")
    check("data-bal-pill" in pill and "data-bal-chip" in html and "data-bal-n" in html,
          "the pill and the phone chip can be updated in place")
    js = open(os.path.join(ROOT, "static", "app.js")).read()
    create_html = open(os.path.join(ROOT, "web", "create.html")).read()
    check("window.cfyBalance = function" in js and "window.cfyBalance(b)" in create_html,
          "the Create quote writes its balance into the pill (no second number on screen)")
    with db.connect() as conn:
        conn.execute("UPDATE workspaces SET credits=4090 WHERE id=?", (ws,))
        conn.commit()
    html = c.get("/app/library").get_data(as_text=True)
    check("4,090 credits<" in html.split('class="pill"', 1)[1].split("</a>", 1)[0], "thousands separator in the pill")

    print("== UX-12: a failed card leads with Edit and retry")
    t0 = db.now()
    img = os.path.join(TMP, "ad.png")
    Image.new("RGB", (864, 1080), (200, 120, 40)).save(img)
    with db.connect() as conn:
        bid = conn.execute("INSERT INTO batches (workspace_id, user_id, ad_type, title, config_json, credits, created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (ws, u["id"], "image", "Mugs", "{}", 43, t0)).lastrowid
        fj = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, error, credits, "
                          "batch_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (ws, u["id"], "ad_image", "Mug on a desk", json.dumps({"batch": 1, "aspect": "4:5"}), "failed",
                           worker.failure_text("Blocked by the content filter. Try a different brief or photo."), 43, bid,
                           t0, t0)).lastrowid
        oj = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, error, credits, "
                          "batch_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (ws, u["id"], "ad_image", "Mug two", json.dumps({"batch": 1}), "failed",
                           "401: vendor key", 43, bid, t0, t0)).lastrowid
        dj = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, brief, params_json, status, credits, batch_id, "
                          "created_at, updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (ws, u["id"], "ad_image", "b", json.dumps({"hook": "Your afternoon cup in 30 seconds",
                                                                     "aspect": "9:16"}), "done", 43, bid, t0, t0, t0)).lastrowid
        d2 = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, template_id, brief, params_json, status, credits, "
                          "created_at, updated_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (ws, u["id"], "ad_image", "flatlay", "b", json.dumps({"aspect": "4:5"}), "done", 43, t0, t0,
                           t0)).lastrowid
        o1 = conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (dj, "image", "", img)).lastrowid
        o2 = conn.execute("INSERT INTO outputs (job_id, kind, url, path) VALUES (?,?,?,?)", (d2, "image", "", img)).lastrowid
        conn.commit()
    html = c.get("/app/library?status=failed").get_data(as_text=True)
    card = html.split(f'data-job="{fj}"', 1)[1].split("</article>", 1)[0]
    acts = card.split('class="row cardacts"', 1)[1]
    first_btn = re.search(r'class="btn sm[^"]*"[^>]*>([^<]+)<', acts)
    check(first_btn and first_btn.group(1) == "Edit and retry" and 'class="btn sm"' in acts.split("Edit and retry")[0][-80:],
          "a content-filter failure: Edit and retry is the first, accent button")
    price = catalog.price_credits("ad_image", batch=1)
    check(f'class="btn sm dark" title="Same brief and photo">Try again · {price:,} credit' in acts,
          "Try again with the same brief is the dark button, priced in credits")
    card2 = html.split(f'data-job="{oj}"', 1)[1].split("</article>", 1)[0]
    acts2 = card2.split('class="row cardacts"', 1)[1]
    check(acts2.index("Try again") < acts2.index("Edit and retry") and 'class="btn sm" title="Same brief' in acts2,
          "a failure on our side leads with Try again")

    print("== UX-11: labelled export thumbnails")
    cap = adhub.thumb_caption({"params_json": json.dumps({"hook": "Your afternoon cup in 30 seconds", "aspect": "9:16"}),
                               "template_id": None, "tool": "ad_image"})
    check(cap == {"words": "Your afternoon cup…", "shape": "9:16", "text": "Your afternoon cup… · 9:16"},
          "a hook's first 3 words and the shape")
    check(adhub.thumb_caption({"params_json": json.dumps({"hook": "Light it once. Smell it all week."}), "template_id": None,
                               "tool": "ad_image"})["words"] == "Light it once…", "no stray punctuation before the ellipsis")
    with db.connect() as conn:
        row = conn.execute("SELECT o.id, o.kind, j.template_id, j.tool, j.params_json FROM outputs o JOIN jobs j "
                           "ON j.id=o.job_id WHERE o.id=?", (o2,)).fetchone()
    check(adhub.thumb_caption(row)["text"] == "Flat lay · 4:5", "no hook: the template's name and the shape")
    check(adhub.thumb_caption({"params_json": "not json", "template_id": None, "tool": "nope"})["words"] == "Ad",
          "bad params still give a label")
    html = c.get("/app/ad-accounts/meta").get_data(as_text=True)
    check(html.index("Pick an ad to export it") < html.index('class="exgrid"'), "the instruction sits above the row")
    grid = html.split('class="exgrid"', 1)[1].split("</div>", 1)[0]
    check("Your afternoon cup…" in text(grid) and "Flat lay 4:5" in text(grid) and "9:16" in grid,
          "each export thumbnail has a caption")

    print("== a made ad is shown whole, in its own shape")
    html = c.get("/app/library").get_data(as_text=True)
    wide = html.split(f'data-job="{d2}"', 1)[1].split("</article>", 1)[0]
    tall = html.split(f'data-job="{dj}"', 1)[1].split("</article>", 1)[0]
    check('class="thumb tall r-4x5"' in wide and '<img class="out"' in wide, "a 4:5 ad gets a 4:5 box and is not cropped")
    check('class="thumb tall"' in tall and "r-9x16" not in tall and '<img class="out"' in tall, "a 9:16 ad keeps the tall box")
    css = open(os.path.join(ROOT, "static", "style.css")).read()
    check(".thumb img.out{object-fit:contain}" in css and ".litem .thumb.r-4x5{aspect-ratio:4/5}" in css,
          "the stylesheet shows the whole picture")

    print("== UX-17: Save vs the live page")
    with db.connect() as conn:
        pid = conn.execute("INSERT INTO pages (workspace_id, slug, title, status, blocks_json, created_at, updated_at) "
                           "VALUES (?,?,?,?,?,?,?)", (ws, "mugs-page", "Mugs", "draft",
                                                      json.dumps([{"type": "media", "media": None, "caption": ""}]),
                                                      t0, t0)).lastrowid
        conn.commit()
    html = c.get(f"/app/pages/{pid}").get_data(as_text=True)
    bar = html.split('class="go savebar"', 1)[1].split("</form>", 1)[0]
    check(">Save draft<" in bar and ">Publish<" in bar and "Update live page" not in bar,
          "a draft: Save draft and Publish")
    check("Save keeps it private; Publish puts it live." in text(bar), "the draft bar says what each does")
    pick = html.split('class="medpick"', 1)[1].split("</div>", 1)[0]
    check("Your afternoon cup…" in text(pick) and "Flat lay 4:5" in text(pick), "the media picker labels each thumbnail")
    r = c.post(f"/app/pages/{pid}", data={"csrf": csrf(c), "action": "save", "title": "Mugs"}, follow_redirects=True)
    check("Saved as a draft. Only you can see it." in r.get_data(as_text=True), "saving a draft says it stays private")
    with db.connect() as conn:
        conn.execute("UPDATE pages SET status='published', published_at=? WHERE id=?", (t0, pid))
        conn.commit()
    html = c.get(f"/app/pages/{pid}").get_data(as_text=True)
    bar = html.split('class="go savebar"', 1)[1].split("</form>", 1)[0]
    check(">Save and update live page<" in bar and ">Save<" not in bar and ">Save draft<" not in bar and ">Unpublish<" in bar,
          "a live page: one save button that names the live page, and Unpublish")
    check("Saving changes the live page right away." in text(bar), "the live bar says saving goes live")
    r = c.post(f"/app/pages/{pid}", data={"csrf": csrf(c), "action": "save", "title": "Mugs"}, follow_redirects=True)
    check("Saved. The live page is updated." in r.get_data(as_text=True), "saving a live page says it is updated")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} checks passed")
