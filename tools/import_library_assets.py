"""Put reviewed library assets where the app serves them from (m15).

    python tools/import_library_assets.py <dir>            # writes under static/
    python tools/import_library_assets.py <dir> --dry-run  # says what it would write
    python tools/import_library_assets.py <dir> --static /some/other/static

<dir> holds files downloaded from the admin's library (the export zip unpacked, or single downloads saved under the
names list_library_assets gives), each named by its tags:

    presenter__<presenter>__<situation>.png|jpg|webp   -> static/presenters/<presenter>/<situation>.webp   (9:16)
    presenter__<presenter>__portrait.png|jpg|webp      -> static/presenters/<presenter>/portrait.webp      (4:5)
    presenter__<presenter>__angle-left.png|jpg|webp    -> static/presenters/<presenter>/angle-left.webp    (9:16; also
                                                          angle-right and angle-profile: the look-lock pictures)
    voice__<presenter>.mp4|mp3|wav|m4a                 -> static/presenters/<presenter>/voice.mp3
    preset__<preset>.png|jpg|webp                      -> static/presets/<preset>.webp      (4:5, or 9:16 for a tall one)
    preset__<preset>.mp4|mov                           -> static/presets/<preset>.mp4       (and its first frame as the
                                                          .webp when the preset has no image example)

A presenter with a still but no portrait file gets a portrait cut from their first situation's still. Pictures are
cropped to the shape, scaled down and saved as WebP of at most 200 KB; clips are re-encoded to H.264 with ffmpeg
(720 px wide at most) and a voice sample is cut to 12 seconds of mono MP3. Names that match no presenter, situation
or preset are skipped and listed, as are demo packshots (demo__*), which live in the workspace, not in static/.

No network and no model. ffmpeg is needed only for clips and voice samples.
"""
import io
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

import presenters  # noqa: E402
import presets  # noqa: E402

MAX_BYTES = 200 * 1024
SHAPES = {"9:16": (720, 1280), "4:5": (864, 1080)}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac")
VOICE_SECONDS = 12


def crop_to(im: Image.Image, shape: str, top: bool = False) -> Image.Image:
    """The picture cropped to 9:16 or 4:5 (centred; `top` keeps the top, for a portrait cut from a still) and scaled
    down to the shape's size."""
    w, h = im.size
    tw, th = SHAPES[shape]
    want = tw / th
    if w / h > want:                         # too wide: cut the sides
        nw = int(round(h * want))
        x = (w - nw) // 2
        im = im.crop((x, 0, x + nw, h))
    elif w / h < want:                       # too tall: cut top and bottom (or only the bottom)
        nh = int(round(w / want))
        y = 0 if top else (h - nh) // 2
        im = im.crop((0, y, w, y + nh))
    if im.size[0] > tw:
        im = im.resize((tw, th), Image.LANCZOS)
    return im


def shape_for(im: Image.Image) -> str:
    """4:5, or 9:16 for a picture that is clearly a tall one."""
    w, h = im.size
    return "9:16" if h / max(1, w) >= 1.5 else "4:5"


def webp_bytes(im: Image.Image, limit: int = MAX_BYTES) -> bytes:
    """WebP of at most `limit` bytes: the quality steps down first, then the size."""
    im = im.convert("RGB")
    while True:
        for q in (86, 80, 74, 68, 60, 52, 44, 36):
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=q, method=6)
            if buf.tell() <= limit:
                return buf.getvalue()
        if min(im.size) < 240:
            return buf.getvalue()
        im = im.resize((int(im.size[0] * 0.85), int(im.size[1] * 0.85)), Image.LANCZOS)


def open_image(path: str) -> Image.Image:
    import imagesafe
    imagesafe.probe(path, max_pixels=imagesafe.MAX_PIXELS, max_bytes=None)
    with Image.open(path) as im:
        return im.convert("RGB")


def _ffmpeg(args: list) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, timeout=300)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0


def write_video(src: str, dst: str) -> bool:
    return _ffmpeg(["-i", src, "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "medium", "-crf", "26",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst]) \
        and os.path.exists(dst) and os.path.getsize(dst) > 1000


def write_voice(src: str, dst: str) -> bool:
    return _ffmpeg(["-i", src, "-vn", "-t", str(VOICE_SECONDS), "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame",
                    "-b:a", "96k", dst]) and os.path.exists(dst) and os.path.getsize(dst) > 500


def first_frame(src: str) -> Image.Image | None:
    tmp = src + ".frame.png"
    try:
        if _ffmpeg(["-ss", "0.5", "-i", src, "-frames:v", "1", tmp]) and os.path.exists(tmp):
            return open_image(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return None


def plan(folder: str) -> tuple[list, list]:
    """([(source file, kind, a, b)], [(file, why skipped)]) from the names in a folder."""
    todo, skipped = [], []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        base, ext = os.path.splitext(name)
        ext = ext.lower()
        if not os.path.isfile(path) or name == "manifest.json":
            continue
        parts = base.split("__")
        if parts[0] == "presenter" and len(parts) == 3 and ext in IMAGE_EXT:
            pid, sid = parts[1], parts[2]
            if presenters.record(pid) is None:
                skipped.append((name, f"no presenter {pid!r}"))
            elif sid in presenters.ANGLES:
                todo.append((path, "angle", pid, sid))
            elif sid != "portrait" and sid not in presenters.situation_ids(pid):
                skipped.append((name, f"{pid} has no situation {sid!r}"))
            else:
                todo.append((path, "portrait" if sid == "portrait" else "still", pid, sid))
        elif parts[0] == "voice" and len(parts) == 2 and ext in AUDIO_EXT + VIDEO_EXT:
            if presenters.record(parts[1]) is None:
                skipped.append((name, f"no presenter {parts[1]!r}"))
            else:
                todo.append((path, "voice", parts[1], ""))
        elif parts[0] == "preset" and len(parts) == 2 and ext in IMAGE_EXT + VIDEO_EXT:
            if presets.get(parts[1]) is None or not re.fullmatch(r"[a-z0-9_-]+", parts[1]):
                skipped.append((name, f"no preset {parts[1]!r}"))
            else:
                todo.append((path, "preset_video" if ext in VIDEO_EXT else "preset_image", parts[1], ""))
        elif parts[0] == "demo":
            skipped.append((name, "a demo packshot: it stays in the workspace, not in static/"))
        else:
            skipped.append((name, "the name isn't presenter__<id>__<situation>, voice__<id> or preset__<id>"))
    return todo, skipped


def run(folder: str, static: str | None = None, dry: bool = False) -> dict:
    """Import a folder. Returns {"written": [paths], "skipped": [(file, why)], "failed": [(file, why)]}."""
    static = static or os.path.join(ROOT, "static")
    todo, skipped = plan(folder)
    written, failed = [], []

    def save(data: bytes, *where) -> str:
        dst = os.path.join(static, *where)
        if not dry:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
        written.append(dst)
        return dst

    stills = {}                                         # presenter -> {situation: source path}
    portraits = set()
    for path, kind, a, b in todo:
        try:
            if kind == "still":
                save(webp_bytes(crop_to(open_image(path), "9:16")), "presenters", a, b + ".webp")
                stills.setdefault(a, {})[b] = path
            elif kind == "portrait":
                save(webp_bytes(crop_to(open_image(path), "4:5", top=True)), "presenters", a, "portrait.webp")
                portraits.add(a)
            elif kind == "angle":                       # a look-lock picture: never a portrait's source
                save(webp_bytes(crop_to(open_image(path), "9:16")), "presenters", a, b + ".webp")
            elif kind == "preset_image":
                im = open_image(path)
                save(webp_bytes(crop_to(im, shape_for(im))), "presets", a + ".webp")
            elif kind == "voice":
                dst = os.path.join(static, "presenters", a, "voice.mp3")
                if dry:
                    written.append(dst)
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if write_voice(path, dst):
                        written.append(dst)
                    else:
                        failed.append((os.path.basename(path), "ffmpeg couldn't make the voice sample"))
            elif kind == "preset_video":
                dst = os.path.join(static, "presets", a + ".mp4")
                if dry:
                    written.append(dst)
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not write_video(path, dst):
                    failed.append((os.path.basename(path), "ffmpeg couldn't re-encode the clip"))
                    continue
                written.append(dst)
                poster = os.path.join(static, "presets", a + ".webp")
                has_image = any(k == "preset_image" and x == a for _p, k, x, _b in todo)
                if not has_image and not os.path.exists(poster):
                    frame = first_frame(dst)
                    if frame is not None:
                        save(webp_bytes(crop_to(frame, shape_for(frame))), "presets", a + ".webp")
        except Exception as e:  # noqa: BLE001 — one unreadable file never stops the rest
            failed.append((os.path.basename(path), f"{type(e).__name__}: {str(e)[:120]}"))
    # a presenter with stills and no portrait: cut one from their first situation's still (or the first one given)
    for pid, got in stills.items():
        if pid in portraits or (not dry and os.path.exists(os.path.join(static, "presenters", pid, "portrait.webp"))):
            continue
        order = [s for s in presenters.situation_ids(pid) if s in got]
        try:
            save(webp_bytes(crop_to(open_image(got[order[0]]), "4:5", top=True)), "presenters", pid, "portrait.webp")
        except Exception as e:  # noqa: BLE001
            failed.append((f"{pid} portrait", f"{type(e).__name__}: {str(e)[:120]}"))
    return {"written": written, "skipped": skipped, "failed": failed}


def main(argv: list) -> int:
    argv = list(argv)
    static = None
    if "--static" in argv:
        i = argv.index("--static")
        if i + 1 >= len(argv):
            print("--static needs a folder")
            return 2
        static = argv[i + 1]
        del argv[i:i + 2]
    dry = "--dry-run" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1 or not os.path.isdir(args[0]):
        print(__doc__)
        return 2
    res = run(args[0], static, dry=dry)
    for p in res["written"]:
        print(("would write " if dry else "wrote ") + os.path.relpath(p, ROOT))
    for name, why in res["skipped"]:
        print(f"skipped {name}: {why}")
    for name, why in res["failed"]:
        print(f"FAILED {name}: {why}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
