"""Re-frame a finished ad for a platform's size: images with Pillow, videos
with ffmpeg. Center crop to the target aspect ratio, then scale. Results are
cached next to the original (<file>.<platform>-<format>.<ext>)."""
import os
import shutil
import subprocess

from PIL import Image


class ExportError(RuntimeError):
    pass


def target_path(src: str, platform: str, fmt: str) -> str:
    base, ext = os.path.splitext(src)
    return f"{base}.{platform}-{fmt}{ext.lower() if ext.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.mp4') else '.png'}"


def _crop_box(w, h, tw, th):
    want = tw / th
    if w / h > want:          # too wide: trim the sides
        nw = int(round(h * want))
        x = (w - nw) // 2
        return x, 0, x + nw, h
    nh = int(round(w / want))  # too tall: trim top and bottom
    y = (h - nh) // 2
    return 0, y, w, y + nh


def export_image(src: str, dst: str, tw: int, th: int) -> str:
    with Image.open(src) as im:
        im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
        im = im.crop(_crop_box(im.width, im.height, tw, th)).resize((tw, th), Image.LANCZOS)
        if dst.lower().endswith((".jpg", ".jpeg")) and im.mode == "RGBA":
            im = im.convert("RGB")
        im.save(dst, quality=92) if dst.lower().endswith((".jpg", ".jpeg")) else im.save(dst)
    return dst


def export_video(src: str, dst: str, tw: int, th: int) -> str:
    if not shutil.which("ffmpeg"):
        raise ExportError("Video export isn't available on this server.")
    vf = (f"crop='if(gt(iw/ih,{tw}/{th}),ih*{tw}/{th},iw)':'if(gt(iw/ih,{tw}/{th}),ih,iw*{th}/{tw})',"
          f"scale={tw}:{th}:flags=lanczos,setsar=1")
    tmp = dst + ".part.mp4"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                        tmp], capture_output=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise ExportError("The video couldn't be converted.")
    os.replace(tmp, dst)
    return dst


def export(src: str, kind: str, platform: str, fmt: str, tw: int, th: int) -> str:
    if not src or not os.path.exists(src):
        raise ExportError("The original file isn't on the server anymore.")
    dst = target_path(src, platform, fmt)
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    return export_video(src, dst, tw, th) if kind == "video" else export_image(src, dst, tw, th)
