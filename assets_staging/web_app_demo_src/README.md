# The Web app demo example (ex2-web-app-demo)

How `showcase/ex2-web-app-demo.mp4` was made (Nov 12 2026). It is the Web app demo preset's own format, put
together by the app's stitcher (`stitch.render_beat`, `videotext`, the split layout), not by a model.

- Hook beat (4 s): `showcase/ex2-saas-hook-hallway.mp4` (Seedance 2.5 through Higgsfield, 56 credits, run 2).
- Demo beat (6 s): `demo-beat.mp4`, the same invented presenter (a frame of the hook as the image reference),
  Seedance 2.5, 720p, 8 s, 56 credits, job a0fd1180-2baf-4ada-922d-d91f87def5cf. Line: "You plan the whole week
  on one board, and tick things off as they're done."
- The screen: `planner-recording.webm`, a 12-second recording of `planner.html`, a made-up web app (Dapplewood
  Planner) written for this example and recorded in a headless browser. It plays at about twice its speed.
- End card: `end.png` (the made-up app's own screen) on the brand colour, by `videotext.end_card`.
- `assemble_example.py` is the script that put it together; captions are the app's own type.

Invented person, made-up brand, no real product, no logos.
