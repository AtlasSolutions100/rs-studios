"""What talking videos are with KEYFRAME_VIDEO off: the jobs Create queues for a fixed set of configs and exactly
what the worker would send to the vendor for each (m15).

tests/fixtures/keyframe_off_snapshot.json is this script's output on m15-integration BEFORE the keyframe-first
pipeline was added. tests/test_keyframe_video.py recomputes it with the switch off and fails on any difference:
off has to mean "exactly what it was": the same jobs, params, prices and vendor inputs. Rewrite the fixture only
when a talking video's one-take prompt or price is meant to change:

    python tools/keyframe_off_snapshot.py > tests/fixtures/keyframe_off_snapshot.json

collect() needs a fresh, empty database (it signs up the first user and adds the first product, so every id is
1) and fakes the vendor's upload; it never reaches a network.
"""
import io
import json
import os
import sys

ROOT = os.environ.get("RS_SNAPSHOT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = "Meet the mug. It keeps drinks hot for 6 hours."
CONFIGS = {
    "ugc": {"type": "ugc", "items": [SCRIPT], "body": "Try it today.", "presenters": ["maya"], "duration": 10},
    "ugc two presenters, two shapes": {"type": "ugc", "items": [SCRIPT], "presenters": ["jordan", "walt"], "duration": 8,
                                       "aspects": ["9:16", "1:1"], "setting": "a quiet porch at dusk"},
    "ugc with a situation": {"type": "ugc", "items": [SCRIPT], "presenters": ["nia"], "situation": "desk", "duration": 15},
    "ugc holding, 1080p": {"type": "ugc", "items": [SCRIPT], "presenters": ["maya"], "duration": 8, "placement": "holding",
                           "resolution": "1080p", "voice": "upbeat_f"},
    "voiceover": {"type": "voiceover", "items": [SCRIPT], "voices": ["narrator_deep"], "duration": 10},
    "talk-to-camera": {"type": "ugc", "preset": "talk-to-camera", "items": [SCRIPT], "presenters": ["maya"]},
    "car-talk": {"type": "ugc", "preset": "car-talk", "items": [SCRIPT], "presenters": ["theo"]},
    "vo-product-shots": {"type": "voiceover", "preset": "vo-product-shots", "items": [SCRIPT], "voices": ["narrator_bright"]},
    "hands-only-demo": {"type": "voiceover", "preset": "hands-only-demo", "items": [SCRIPT]},
    "pov-unboxing": {"type": "voiceover", "preset": "pov-unboxing", "items": [SCRIPT]},
    "season-gift-clip": {"type": "voiceover", "preset": "season-gift-clip", "items": ["A present they will actually use."]},
    "founder-story at 30 s": {"type": "voiceover", "preset": "founder-story", "items": [SCRIPT], "duration": 30},
}
HIDDEN = ["talking-head-cutaways", "problem-solution", "green-screen-product-page", "show-your-app", "street-interview",
          "three-reasons-video", "offer-promo", "how-to-steps"]


def collect(appmod=None) -> dict:
    """Queue every config as the first member of a fresh database and read back each job and its vendor inputs."""
    if appmod is None:
        import app as appmod
    import db
    import hf
    import products
    import worker
    from PIL import Image
    ups = []

    def upload(data, ctype):
        ups.append(ctype)
        return f"https://files.example/snap{len(ups)}"
    real_upload, hf.upload = hf.upload, upload
    try:
        c = appmod.app.test_client()
        c.environ_base["REMOTE_ADDR"] = "10.77.0.1"
        c.environ_base["HTTP_USER_AGENT"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 Safari/605.1.15"
        c.get("/login")
        with c.session_transaction() as s:
            token = s["csrf"]
        assert c.post("/signup", data={"csrf": token, "email": "snapshot@example.com", "password": "longpassword"}).status_code == 302
        with db.connect() as conn:
            u = conn.execute("SELECT * FROM users WHERE email='snapshot@example.com'").fetchone()
            conn.execute("UPDATE users SET email_verified_at=? WHERE id=?", (db.now(), u["id"]))
            conn.execute("UPDATE workspaces SET credits=1000000 WHERE id=?", (u["workspace_id"],))
            conn.commit()
        photo = os.path.join(os.environ["RS_DATA"], "snapshot-product.png")
        b = io.BytesIO()
        Image.new("RGB", (600, 600), (90, 110, 130)).save(b, "PNG")
        open(photo, "wb").write(b.getvalue())
        with appmod.app.test_request_context("/"):
            from flask import g
            with db.connect() as conn:
                g.user = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
                g.ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (u["workspace_id"],)).fetchone()
                pid = products.save_kit(conn, g.ws, brand_name="Northwind", product_name="Trail Mug", audience="Hikers",
                                        claims="Keeps drinks hot for 6 hours")
                products.add_photo(conn, u["workspace_id"], pid, photo, "product.png", "image/png")
                conn.commit()
        with c.session_transaction() as s:
            token = s["csrf"]
        post = lambda url, body: c.post(url, json=body, headers={"X-CSRF": token, "Accept": "application/json"})  # noqa: E731
        out = {"configs": {}, "hidden": {}}
        for name, cfg in CONFIGS.items():
            cfg = dict(cfg, product=pid)
            q = post("/app/create/quote", cfg).get_json()
            r = post("/app/create/generate", dict(cfg, total=q.get("total"))).get_json()
            rows = []
            with db.connect() as conn:
                for j in conn.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY id", (r["batch"],)).fetchall():
                    rows.append({"tool": j["tool"], "template_id": j["template_id"], "brief": j["brief"],
                                 "credits": j["credits"], "cost_usd": j["cost_usd"],
                                 "params": json.loads(j["params_json"]), "inputs": worker.build_inputs(conn, j)})
                    worker._fail(conn, j, worker.CANCELED)
            out["configs"][name] = {"quote": {k: q.get(k) for k in ("count", "total", "each")},
                                    "lines": [[ln["key"], ln["label"], ln["credits"]] for ln in q.get("lines") or []],
                                    "jobs": rows}
        for pid_ in HIDDEN:
            q = post("/app/create/quote", {"type": "ugc", "preset": pid_, "product": pid, "items": [SCRIPT],
                                           "presenters": ["maya"]}).get_json()
            q2 = post("/app/create/quote", {"type": "voiceover", "preset": pid_, "product": pid, "items": [SCRIPT]}).get_json()
            out["hidden"][pid_] = [q.get("error"), q.get("count"), q2.get("error"), q2.get("count")]
        import presets
        out["recipes_for"] = [p.id for p in presets.recipes_for()]
        out["offered_recipes"] = [p.id for p in presets.offered() if p.kind == "recipe"]
        out["pipelines"] = list(presets.PIPELINES)
        return out
    finally:
        hf.upload = real_upload


if __name__ == "__main__":
    import tempfile
    sys.path.insert(0, ROOT)
    tmp = tempfile.mkdtemp(prefix="rs_kfsnap_")
    for k in ("KEYFRAME_VIDEO", "VIDEO_QA", "PRESENTER_STILLS", "PRESENTER_VOICE_REF", "IMAGE_QA", "FREE_FIX", "CLAIMS_CHECK",
              "RS_MARKUP", "RS_HD_MARKUP", "RS_TALKING_DURATIONS", "RS_TALKING_USD_PER_SECOND", "RS_VIDEO_USD_PER_SECOND",
              "RS_VIDEO_USD_PER_SECOND_1080P", "RS_CARD_REQUIRED", "UI_V2", "RS_TALKING", "RS_SERVICE_PATH"):
        os.environ.pop(k, None)
    os.environ.update(RS_DATA=tmp, RS_WORKER="0", HF_KEY="test:secret", RS_PLAN_SYNC="0", RS_NUDGE_LOOP="0",
                      RS_PUBLIC_URL="https://campaignify.test", RS_DISK_MIN_GB="0.2", ADS_READY_EMAIL="0",
                      RS_SIGNUP_CONFIRM="0")
    import logging
    logging.disable(logging.CRITICAL)
    real_stdout, sys.stdout = sys.stdout, sys.stderr          # the app logs requests to stdout: keep the JSON clean
    try:
        data = collect()
    finally:
        sys.stdout = real_stdout
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(data, indent=1, sort_keys=True))
