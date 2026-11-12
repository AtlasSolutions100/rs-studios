"""Putting a keyframe-first video together with ffmpeg (m15; keyframe_video.py decides what goes where).

Everything here is local: no model, no network. A video is built in two encodes at most:

    render_beat()   one beat in the video's standard form: the frame size and rate of the whole video, its sound
                    levelled (loudnorm) and resampled, cut to its planned length. In the same pass, anything laid
                    over its picture while its own sound carries on:
                      cutaways   silent b-roll (a generated clip, or a slow push-in on the real packshot) over
                                 part of the beat: the presenter keeps talking underneath, so the voice is one take
                      screen     the member's screenshots full frame with the presenter small in a corner
                                 (over the product page, show your app): nothing of the page is ever generated
                      overlays   full-frame transparent PNGs shown for a time window: captions, a step's tag,
                                 the "AI presenter" label
    still_clip()    a clip from one picture (the end card), with silence as its sound
    concat()        the beats and the end card in order: hard cuts, or a crossfade of a few frames at every join

Also: probe() (what a file is), frames() (pictures out of a clip for the frame check) and silence detection for the
tests. Every function raises StitchError with ffmpeg's last words when it fails; the caller refunds.
"""
import json
import os
import shutil
import subprocess

FPS = 24                            # used when a clip doesn't say
AUDIO_RATE = 48000
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
INTER_CRF, FINAL_CRF = 14, 20        # beats are kept near-lossless; the one delivered file is encoded once more
PIP_WIDTH = 0.36                     # the presenter's share of the frame's width over a screenshot
SPLIT_TOP = 0.07                     # a screen across the top starts this far down (what a platform covers)
SPLIT_MAX = 0.5                      # and the band it sits in is never more than this share of the frame's height
SPLIT_WIDE = 1.15                    # a screen at least this much wider than tall counts as wide
SPLIT_FACE = 0.08                    # how far down the presenter's own frame the cut for the lower part starts
BACKDROP = "0x111111"


class StitchError(RuntimeError):
    """ffmpeg couldn't do it; the message is for our log, the member gets the usual refund text."""


def have() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(args: list, timeout: int = 900):
    if not shutil.which("ffmpeg"):
        raise StitchError("ffmpeg isn't installed")
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *[str(a) for a in args]], capture_output=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise StitchError("ffmpeg took too long") from e
    if r.returncode != 0:
        raise StitchError((r.stderr or b"").decode(errors="ignore").strip()[-400:] or f"ffmpeg exit {r.returncode}")


def probe(path: str) -> dict:
    """{"duration", "width", "height", "fps", "audio", "audio_seconds"} of a media file. Raises StitchError."""
    if not shutil.which("ffprobe"):
        raise StitchError("ffprobe isn't installed")
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,width,height,r_frame_rate,duration", "-of", "json", path],
                           capture_output=True, timeout=60)
        d = json.loads(r.stdout or b"{}")
    except (subprocess.TimeoutExpired, ValueError) as e:
        raise StitchError("the clip couldn't be read") from e
    out = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "audio": False, "audio_seconds": 0.0}
    for s in d.get("streams") or []:
        if s.get("codec_type") == "video" and not out["width"]:
            out["width"], out["height"] = int(s.get("width") or 0), int(s.get("height") or 0)
            num, _, den = str(s.get("r_frame_rate") or "0/1").partition("/")
            try:
                out["fps"] = round(float(num) / float(den or 1), 3)
            except (ValueError, ZeroDivisionError):
                out["fps"] = 0.0
        elif s.get("codec_type") == "audio":
            out["audio"] = True
            try:
                out["audio_seconds"] = float(s.get("duration") or 0)
            except ValueError:
                pass
    try:
        out["duration"] = float((d.get("format") or {}).get("duration") or 0)
    except ValueError:
        pass
    if r.returncode != 0 or (not out["width"] and not out["audio"]):
        raise StitchError("the clip couldn't be read")
    return out


def even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def standard(first_clip: str, fallback=(720, 1280)) -> tuple:
    """((width, height), fps) every part of a video is brought to: the first beat's own, with even sides."""
    try:
        p = probe(first_clip)
    except StitchError:
        p = {"width": 0, "height": 0, "fps": 0}
    w, h = (p["width"], p["height"]) if p["width"] and p["height"] else fallback
    fps = p["fps"] if 10 <= (p["fps"] or 0) <= 60 else FPS
    return (even(w), even(h)), (int(fps) if float(fps).is_integer() else fps)


def _cover(w: int, h: int, fps) -> str:
    return (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,"
            "format=yuv420p")


def _image_size(path: str) -> tuple:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def _vcodec(crf: int, fps) -> list:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", str(fps)]


def _acodec() -> list:
    return ["-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2"]


def pip_box(size: tuple, aspect_safe_bottom: float = 0.06) -> tuple:
    """(x, y, w, h) of the presenter's small picture over a screenshot: bottom left, above what a platform covers."""
    W, H = size
    w = even(W * PIP_WIDTH)
    h = even(w * H / W)
    return even(W * 0.05), even(H * (1 - aspect_safe_bottom) - h), w, h


def video_size(path: str, fmt: str = "") -> tuple | None:
    """(width, height) of a video as it is shown (a clip filmed sideways and marked as turned counts turned), read
    as the container `fmt` when one is given. None when it can't be read."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", *(["-f", fmt] if fmt else []), "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
                            "-of", "json", path], capture_output=True, timeout=60)
        st = (json.loads(r.stdout or b"{}").get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        turn = (st.get("tags") or {}).get("rotate") or next(
            (d.get("rotation") for d in st.get("side_data_list") or [] if d.get("rotation") is not None), 0)
        if not w or not h:
            return None
        return (h, w) if abs(int(float(turn or 0))) % 180 == 90 else (w, h)
    except (subprocess.TimeoutExpired, ValueError, TypeError, OSError):
        return None


def _screen_sizes(screen: dict) -> list:
    """(width, height) of each thing a screen beat shows: its recording, or each of its pictures that is there."""
    if screen.get("video"):
        if not os.path.exists(screen["video"]):
            return []
        size = tuple(screen["size"]) if screen.get("size") else video_size(screen["video"], screen.get("format") or "")
        return [size] if size and size[0] and size[1] else []
    out = []
    for p in screen.get("images") or []:
        if p and os.path.exists(p):
            try:
                out.append(_image_size(p))
            except Exception:  # noqa: BLE001 — a picture Pillow can't open is reported by the render itself
                pass
    return out


def screen_layout(screen: dict | None, size: tuple) -> str:
    """How a screen beat is laid out: "split" (the screen across the top, the presenter filling the frame below)
    when the recipe asks for it, the frame is upright and everything shown is wide; else "pip" (the screen full
    frame, the presenter small in a corner). A tall phone screen across the top would be a postage stamp."""
    if not screen or screen.get("layout") != "split" or size[1] <= size[0]:
        return "pip"
    sizes = _screen_sizes(screen)
    return "split" if sizes and all(w >= h * SPLIT_WIDE for w, h in sizes) else "pip"


def split_box(size: tuple, source: tuple) -> tuple:
    """(x, y, w, h, band) for a wide screen across the top of an upright frame: the screen whole, as wide as the
    frame allows inside a small margin, and `band`, the height it takes with its margins (the presenter starts
    there). The band is capped at SPLIT_MAX of the frame, so a squarer screen is shown narrower instead."""
    W, H = size
    sw, sh = source
    m = even(W * 0.03)
    top = even(H * SPLIT_TOP)
    w = W - 2 * m
    h = w * sh / max(1, sw)
    most = H * SPLIT_MAX - top - m
    if h > most:
        h, w = most, most * sw / max(1, sh)
    w, h = even(w), even(h)
    return even((W - w) / 2), top, w, h, even(top + h + m)


def render_beat(src: str, dst: str, *, size: tuple, fps, seconds: float | None = None, cutaways=(), screen=None,
                overlays=(), loudnorm: bool = True, safe_bottom: float = 0.06) -> dict:
    """`src` (one rendered beat) as `dst` in the video's standard form, with what is laid over its picture.

    cutaways  [{"path", "start", "seconds", "kind": "video" | "still"}]: shown full frame from `start` for `seconds`
              while the beat's own sound carries on; a "still" is a slow push-in on that picture
    screen    {"images": [paths]}: the pictures fill the frame one after another (a tall one scrolls), the beat's own
              picture is small in the bottom-left corner. Or {"video": path, "format": its container, "start":
              seconds into it, "speed": 1 to 2}: the member's screen recording plays instead, silent. With
              "layout": "split" and wide sources (screen_layout) the screen runs across the top and the beat's own
              picture fills the frame below it.
    overlays  [{"path": full-frame RGBA png, "start", "end"}]
    Returns probe(dst). Raises StitchError."""
    W, H = size
    info = probe(src)
    dur = float(seconds or info["duration"] or 0)
    if dur <= 0:
        raise StitchError("the beat has no length")
    args, n = ["-i", src], 1
    fc = [f"[0:v]{_cover(W, H, fps)},tpad=stop_mode=clone:stop_duration=3[v0]"]
    last = "v0"
    if screen and (screen.get("images") or screen.get("video")):
        split = screen_layout(screen, size) == "split"
        sizes = _screen_sizes(screen)
        if not sizes:
            raise StitchError("the screen recording isn't there" if screen.get("video") else "the screenshot isn't there")
        # where the screen goes: the whole frame, or (split) the box every source fits across the top
        box = min((split_box(size, s) for s in sizes), key=lambda b: b[3]) if split else (0, 0, W, H, 0)
        bx, by, bw, bh, band = box
        fit = (f"scale={bw}:{bh}:force_original_aspect_ratio=decrease:flags=lanczos,"
               f"pad={bw}:{bh}:(ow-iw)/2:(oh-ih)/2:color={BACKDROP},setsar=1,fps={fps},format=yuv420p")
        if screen.get("video"):
            speed = min(4.0, max(1.0, float(screen.get("speed") or 1)))
            args += (["-f", screen["format"]] if screen.get("format") else []) + \
                ["-ss", f"{max(0.0, float(screen.get('start') or 0)):.3f}", "-t", f"{dur * speed + 0.5:.3f}",
                 "-i", screen["video"]]
            fc.append(f"[{n}:v]setpts=(PTS-STARTPTS)/{speed:.3f},{fit},trim=duration={dur:.3f},setpts=PTS-STARTPTS,"
                      "tpad=stop_mode=clone:stop_duration=30[bg]")
            n += 1
        else:
            imgs = [p for p in screen["images"] if p and os.path.exists(p)]
            each = dur / len(imgs)
            parts = []
            for path in imgs:
                iw, ih = _image_size(path)
                args += ["-loop", "1", "-framerate", str(fps), "-t", f"{each + 0.5:.3f}", "-i", path]
                tall = (not split) and ih * W / iw > H * 1.02
                if tall:        # a page longer than the frame: fit its width and scroll down it over its time
                    chain = (f"scale={W}:-2:flags=lanczos,crop={W}:{H}:0:'min(ih-{H}\\,(ih-{H})*t/{each:.3f})',setsar=1,"
                             f"fps={fps},format=yuv420p")
                else:           # it fits: whole, centred, on a dark backdrop
                    chain = fit
                fc.append(f"[{n}:v]{chain},trim=duration={each:.3f},setpts=PTS-STARTPTS[s{n}]")
                parts.append(f"[s{n}]")
                n += 1
            fc.append(f"{''.join(parts)}concat=n={len(parts)}:v=1:a=0,tpad=stop_mode=clone:stop_duration=3[bg]")
        if split:
            # the presenter fills the frame under the band: cut from their own frame a little below its top, so
            # the face stays in; the screen sits above on the backdrop with a thin white edge, like the corner one
            low = H - band
            off = f"min(ih-{low}\\,ih*{SPLIT_FACE})"
            fc.append(f"[{last}]crop={W}:{low}:0:'{off}',pad={W}:{H}:0:{band}:color={BACKDROP}[low]")
            fc.append(f"[bg]pad={bw + 4}:{bh + 4}:2:2:color=white[scr]")
            fc.append(f"[low][scr]overlay={bx - 2}:{by - 2}[vs]")
        else:
            x, y, pw, ph = pip_box(size, safe_bottom)
            fc.append(f"[{last}]scale={pw - 8}:{ph - 8}:flags=lanczos,pad={pw}:{ph}:4:4:color=white[pip]")
            fc.append(f"[bg][pip]overlay={x}:{y}[vs]")
        last = "vs"
    for i, c in enumerate(cutaways or ()):
        start, secs = max(0.0, float(c["start"])), float(c["seconds"])
        if secs <= 0 or start >= dur or not os.path.exists(c["path"]):
            continue
        secs = min(secs, dur - start)
        if c.get("kind") == "still":
            frames_n = max(2, int(round(secs * float(fps))))
            args += ["-i", c["path"]]
            # a slow push-in: drawn at twice the size so the zoom moves in sub-pixel steps
            chain = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase:flags=lanczos,crop={W * 2}:{H * 2},"
                     f"zoompan=z='1+0.07*on/{frames_n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames_n}:"
                     f"s={W}x{H}:fps={fps},setsar=1,format=yuv420p")
        else:
            args += ["-i", c["path"]]
            chain = f"{_cover(W, H, fps)},trim=duration={secs:.3f}"
        fc.append(f"[{n}:v]{chain},setpts=PTS-STARTPTS+{start:.3f}/TB[c{i}]")
        fc.append(f"[{last}][c{i}]overlay=0:0:enable='between(t,{start:.3f},{start + secs:.3f})':eof_action=pass[vc{i}]")
        last = f"vc{i}"
        n += 1
    for i, o in enumerate(overlays or ()):
        if not o.get("path") or not os.path.exists(o["path"]):
            continue
        a, b = max(0.0, float(o.get("start") or 0)), min(dur, float(o.get("end") or dur))
        if b <= a:
            continue
        args += ["-i", o["path"]]
        fc.append(f"[{n}:v]scale={W}:{H}:flags=lanczos,format=rgba[o{i}]")
        fc.append(f"[{last}][o{i}]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'[vo{i}]")
        last = f"vo{i}"
        n += 1
    norm = f",{LOUDNORM}" if loudnorm else ""
    if info["audio"]:
        fc.append(f"[0:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo{norm},"
                  f"aresample={AUDIO_RATE},apad=pad_dur=3[a]")
    else:
        args += ["-f", "lavfi", "-t", f"{dur + 1:.3f}", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
        fc.append(f"[{n}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a]")
        n += 1
    _run(args + ["-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "[a]", "-t", f"{dur:.3f}",
                 *_vcodec(INTER_CRF, fps), *_acodec(), "-movflags", "+faststart", dst])
    return probe(dst)


def still_clip(image: str, dst: str, *, size: tuple, fps, seconds: float) -> dict:
    """A clip that shows one picture for `seconds`, with silence as its sound (the end card)."""
    W, H = size
    _run(["-loop", "1", "-framerate", str(fps), "-t", f"{seconds:.3f}", "-i", image,
          "-f", "lavfi", "-t", f"{seconds:.3f}", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo",
          "-filter_complex", f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},"
                             f"setsar=1,format=yuv420p[v]",
          "-map", "[v]", "-map", "1:a", "-t", f"{seconds:.3f}", *_vcodec(INTER_CRF, fps), *_acodec(),
          "-movflags", "+faststart", dst])
    return probe(dst)


def concat(parts: list, dst: str, *, fps, crossfade_frames: int = 0, tail_fade: float = 0.0) -> dict:
    """The parts in order as one file. Hard cuts by default; `crossfade_frames` (4 to 6) dissolves every join for
    that many frames, picture and sound. `tail_fade` fades the sound of the part before the last one out over that
    many seconds, so the voice doesn't stop dead into the end card. Every part must be in the standard form
    (render_beat, still_clip). Returns probe(dst)."""
    parts = [p for p in parts if p and os.path.exists(p)]
    if not parts:
        raise StitchError("nothing to put together")
    durs = [probe(p)["duration"] for p in parts]
    args = []
    for p in parts:
        args += ["-i", p]
    fc, n = [], len(parts)
    alab = []
    for i in range(n):
        fade = ""
        if tail_fade > 0 and n > 1 and i == n - 2 and durs[i] > tail_fade:
            fade = f",afade=t=out:st={durs[i] - tail_fade:.3f}:d={tail_fade:.3f}"
        fc.append(f"[{i}:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo{fade}[a{i}]")
        alab.append(f"a{i}")
    xd = max(0, int(crossfade_frames or 0)) / float(fps)
    if n == 1:
        fc.append("[0:v]null[v]")
        fc.append(f"[{alab[0]}]anull[a]")
    elif xd <= 0 or min(durs) <= xd * 2:
        fc.append("".join(f"[{i}:v][{alab[i]}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
    else:
        v, a, at = "0:v", alab[0], durs[0]
        for i in range(1, n):
            fc.append(f"[{v}][{i}:v]xfade=transition=fade:duration={xd:.4f}:offset={at - xd:.4f}[xv{i}]")
            fc.append(f"[{a}][{alab[i]}]acrossfade=d={xd:.4f}:c1=tri:c2=tri[xa{i}]")
            v, a, at = f"xv{i}", f"xa{i}", at + durs[i] - xd
        fc.append(f"[{v}]null[v]")
        fc.append(f"[{a}]anull[a]")
    _run(args + ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", *_vcodec(FINAL_CRF, fps), *_acodec(),
                 "-movflags", "+faststart", dst])
    return probe(dst)


def frames(path: str, folder: str, n: int = 3, stem: str = "frame") -> list:
    """`n` pictures out of a clip, spread over it (15%, 50%, 85% for three), as JPEG files in `folder`: [(seconds,
    path)]. Ones ffmpeg couldn't cut are left out."""
    try:
        dur = probe(path)["duration"]
    except StitchError:
        return []
    os.makedirs(folder, exist_ok=True)
    out = []
    for i in range(n):
        t = dur * (0.15 + 0.70 * (i / (n - 1) if n > 1 else 0.5))
        dst = os.path.join(folder, f"{stem}-{i + 1}.jpg")
        try:
            _run(["-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-q:v", "3", dst], timeout=60)
        except StitchError:
            continue
        if os.path.exists(dst) and os.path.getsize(dst) > 200:
            out.append((round(t, 2), dst))
    return out


def loudness(path: str, start: float = 0.0, seconds: float | None = None) -> float | None:
    """Mean volume in dB of (part of) a file's sound, or None when it has none (tests; the admin's diagnostics)."""
    if not shutil.which("ffmpeg"):
        return None
    cut = ["-ss", f"{start:.3f}"] + (["-t", f"{seconds:.3f}"] if seconds else [])
    r = subprocess.run(["ffmpeg", "-v", "info", "-nostdin", *cut, "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, timeout=120)
    for line in (r.stderr or b"").decode(errors="ignore").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0])
            except ValueError:
                return None
    return None
