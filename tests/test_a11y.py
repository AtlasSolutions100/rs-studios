"""Accessibility pass (roadmap M5.6, 03b_ux_copy.md A11Y-1 to A11Y-7): light-theme status colours with AA contrast,
the app's skip link, labels on every input, one h1 on first-run Home, sign-up and log-in, the button focus ring,
heading order on the ad-account hub, alt text and size on the published page's hero image. The browser check
(axe-core, zero serious violations) runs in tests/golden_path.py; these are the fast server-side checks.

    python tests/test_a11y.py      (tests/test_app.py runs it too)
"""
import io
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="rs_a11y_")
os.environ.update(RS_DATA=TMP, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_MARKUP="2.0",
                  RS_EMAIL_VERIFY="0", RS_NUDGE_LOOP="0", RS_SIGNUP_CONFIRM="0")
for k in ("RS_CARD_REQUIRED", "RS_PLAN_PAYWALL", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import db  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    passed += 1
    print("  ok ", msg)


appmod._maybe_reconcile = lambda ws_id, force=False: 0
_ip = [0]


def client():
    _ip[0] += 1
    c = appmod.app.test_client()
    c.environ_base["REMOTE_ADDR"] = f"10.9.{_ip[0] // 250}.{_ip[0] % 250 + 1}"
    c.get("/login")
    return c


def csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def page(c, path):
    return c.get(path).get_data(as_text=True)


def lum(hexc):
    h = hexc.lstrip("#")
    out = []
    for i in (0, 2, 4):
        x = int(h[i:i + 2], 16) / 255
        out.append(x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


try:
    css = open(os.path.join(ROOT, "static", "style.css")).read()

    # ================================================================ A11Y-1 light-theme status tokens
    print("== A11Y-1: status colours in the light theme")
    light_attr = re.search(r":root\[data-theme=light\]\{(.*?)\n\}", css, re.S).group(1)
    light_media = re.search(r"@media \(prefers-color-scheme: light\)\{\s*:root:not\(\[data-theme=dark\]\)\{(.*?)\n  \}", css, re.S).group(1)
    want = {"--ok": "#127A47", "--bad": "#B3261E", "--amber": "#8A5A00"}
    for name, block in (("data-theme=light", light_attr), ("prefers-color-scheme: light", light_media)):
        got = {k: (re.search(re.escape(k) + r":(#[0-9A-Fa-f]{6})", block) or [None, None])[1] for k in want}
        check(got == want, f"{name} block sets --ok, --bad and --amber to the AA values ({got})")
    for k, v in want.items():
        check(contrast(v, "#FFFFFF") >= 4.5, f"{k} {v} on white is {contrast(v, '#FFFFFF'):.2f}:1 (AA needs 4.5)")
        check(contrast(v, "#F6F7F4") >= 4.5, f"{k} {v} on the light page background is {contrast(v, '#F6F7F4'):.2f}:1")
    check("#127A47}" not in css and "{color:#8A5A00}" not in css, "the one-off light overrides are gone (the tokens do it)")
    chip = re.search(r"\.chip-inline\.video\{background:(#[0-9A-Fa-f]{6})\}", css).group(1)
    check(contrast(chip, "#FFFFFF") >= 4.5, f"the 'video' chip {chip} with white text is {contrast(chip, '#FFFFFF'):.2f}:1")

    # ================================================================ A11Y-5 focus ring on .btn
    print("== A11Y-5: button focus ring")
    check(".btn:focus-visible{outline:2px solid var(--ink);outline-offset:2px}" in css,
          ".btn:focus-visible draws a 2px ink ring (ink, not accent: the button is accent)")

    # ================================================================ pages
    print("== A11Y-4: one h1 on sign-up, log-in and the first-run Home")
    for path in ("/signup", "/login", "/forgot"):
        html = page(client(), path)
        check(len(re.findall(r"<h1\b", html)) == 1, f"{path} has exactly one h1")
    c = client()
    r = c.post("/signup", data={"csrf": csrf(c), "email": "a11y@example.com", "password": "longpassword1"})
    check(r.status_code in (302, 303), "sign up")
    home = page(c, "/app")
    check('class="fa-wrap"' in home and len(re.findall(r"<h1\b", home)) == 1 and '<h1 class="fa-title">' in home,
          "first-run Home: the panel title is the page's one h1")

    print("== A11Y-2: skip link in the app shell")
    check(re.search(r'<body>\s*<a class="skip" href="#main">Skip to content</a>', home) is not None
          and 'id="main"' in home, "the first focusable thing in the app is 'Skip to content' to #main")
    login = page(client(), "/login")
    check('class="skip"' not in login, "auth pages (no #main) don't get a dangling skip link")

    print("== A11Y-3: every visible input has a name")
    ws = db.connect().execute("SELECT workspace_id FROM users WHERE email='a11y@example.com'").fetchone()[0]
    with db.connect() as conn:   # Team, Contacts and Forms are paid tools: put the workspace on Growth
        conn.execute("UPDATE workspaces SET plan_override='growth' WHERE id=?", (ws,))
        conn.commit()

    def unlabeled(html):
        ids = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', html))
        bad = []
        for m in re.finditer(r"<(input|textarea|select)\b([^>]*)>", html):
            attrs = m.group(2)
            if re.search(r'type="(hidden|submit|button)"', attrs) or "aria-label=" in attrs or "aria-labelledby=" in attrs \
                    or 'tabindex="-1"' in attrs:
                continue
            idm = re.search(r'\bid="([^"]+)"', attrs)
            if idm and idm.group(1) in ids:
                continue
            # wrapped in a <label>: the nearest unclosed <label before it
            before = html[:m.start()]
            if before.rfind("<label") > before.rfind("</label>"):
                continue
            bad.append(m.group(0)[:90])
        return bad

    for path, field in (("/app/contacts", 'name="q"'), ("/app/contacts", 'name="tags"'), ("/app/forms", 'name="name"'),
                        ("/app/team", 'name="note"')):
        html = page(c, path)
        check(field in html and not unlabeled(html), f"{path}: {field} is there, and no input lacks a label {unlabeled(html)[:2]}")
    js = open(os.path.join(ROOT, "static", "flows.js")).read()
    check("'<label' + (tied ? ' for=\"' + id + '\"' : '')" in js, "flow editor: each field's label points at its control")

    print("== A11Y-6: heading order on the ad-account hub")
    hub = page(c, "/app/ad-accounts")
    levels = [int(x) for x in re.findall(r"<h([1-6])\b", hub)]
    skips = [(a, b) for a, b in zip(levels, levels[1:]) if b > a + 1]
    check('class="panel hubcard"' in hub and levels and levels[0] == 1 and not skips, f"/app/ad-accounts headings never skip a level ({levels[:6]})")

    print("== A11Y-7: the published page's hero image")
    from PIL import Image
    with db.connect() as conn:
        jid = conn.execute("INSERT INTO jobs (workspace_id, user_id, tool, status, credits, params_json, created_at, updated_at) "
                           "VALUES (?, (SELECT id FROM users WHERE email='a11y@example.com'), 'ad_image', 'done', 0, '{}', datetime('now'), datetime('now'))",
                           (ws,)).lastrowid
        os.makedirs(os.path.join(TMP, "media"), exist_ok=True)
        path = os.path.join(TMP, "media", "hero.png")
        Image.new("RGB", (864, 1080), (200, 120, 60)).save(path)
        oid = conn.execute("INSERT INTO outputs (job_id, kind, path) VALUES (?, 'image', ?)", (jid, path)).lastrowid
        conn.commit()
        import json
        conn.execute("INSERT INTO pages (workspace_id, slug, title, status, theme, blocks_json, created_at, updated_at, "
                     "published_at) VALUES (?, 'ember-a11y', 'Ember Candle launch', 'published', 'dark', ?, "
                     "datetime('now'), datetime('now'), datetime('now'))",
                     (ws, json.dumps([{"type": "hero", "headline": "Light it once.", "media": oid}])))
        conn.commit()
    pub = page(client(), "/p/ember-a11y")
    img = re.search(r'<div class="pp-media tall"><img [^>]*>', pub)
    check(img is not None, "the published page renders the hero image")
    tag = img.group(0)
    check('alt="Ember Candle launch"' in tag, "hero image alt is the page title, not empty")
    check('width="864"' in tag and 'height="1080"' in tag, "hero image carries its intrinsic width and height")

    print(f"\nall {passed} checks passed")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
