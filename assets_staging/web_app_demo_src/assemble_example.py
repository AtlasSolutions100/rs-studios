"""Put the Web app demo example together with the app's own stitcher, the way keyframe_video.assemble does:
a hook beat (the presenter, captions low), a demo beat (the made-up app's screen recording across the top, the
presenter below, captions low), the end card (the app's own screenshot on the brand colour), hard cuts.
Args: hook clip, demo clip, screen recording, a screenshot for the end card, hook line, demo line, out file."""
import os
import sys

sys.path.insert(0, "/dev/shm/rs-presets")
import recordings  # noqa: E402
import stitch  # noqa: E402
import textlayer  # noqa: E402
import videotext  # noqa: E402

hook, demo, rec, shot, hook_line, demo_line, out = sys.argv[1:8]
hook_secs, demo_secs = float(sys.argv[8]), float(sys.argv[9])
work = os.path.join(os.path.dirname(out), "work")
os.makedirs(work, exist_ok=True)
aspect = "9:16"
kit = dict(textlayer.DEFAULT_KIT, primary="#1F4D3A", accent="#F5B301", brand="Dapplewood")
size, fps = stitch.standard(hook)
safe_bottom = textlayer.SAFE[aspect][3]
info = recordings.inspect(rec)
speed, shown = recordings.window(info["seconds"], demo_secs)
screen = {"video": rec, "format": info["format"], "start": 0, "speed": speed, "layout": "split",
          "size": (info["width"], info["height"])}
print("frame", size, fps, "recording", info, "speed", speed, "layout", stitch.screen_layout(screen, size))
clips = []
for i, (src, secs, line, scr) in enumerate(((hook, hook_secs, hook_line, None), (demo, demo_secs, demo_line, screen))):
    overlays = []
    high = scr is not None and stitch.screen_layout(scr, size) == "pip"
    said = float(sys.argv[10 + i]) if len(sys.argv) > 10 + i else secs      # the words are over by then
    for j, ch in enumerate(videotext.caption_chunks(line, min(secs, said))):
        p = os.path.join(work, f"cap-{i}-{j}.png")
        videotext.caption_png(ch["text"], size, p, kit, aspect, high=high)
        overlays.append({"path": p, "start": ch["start"], "end": ch["end"]})
    dst = os.path.join(work, f"beat-{i}.mp4")
    stitch.render_beat(src, dst, size=size, fps=fps, seconds=secs, screen=scr, overlays=overlays, safe_bottom=safe_bottom)
    clips.append(dst)
card = os.path.join(work, "end-card.png")
videotext.end_card(size, card, packshot=shot, kit=kit, aspect=aspect, headline="Dapplewood Planner", subline="Dapplewood",
                   badge="", cta=videotext.cta_for("app_service"))
end = os.path.join(work, "end-card.mp4")
stitch.still_clip(card, end, size=size, fps=fps, seconds=2.0)
clips.append(end)
res = stitch.concat(clips, out, fps=fps, crossfade_frames=0, tail_fade=0.25)
print("made", out, res)
