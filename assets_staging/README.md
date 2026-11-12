# assets_staging (branch `assets/library-2`)

Generated assets waiting to be wired into the app. Nothing here is served yet; the app code that uses them is a separate step.
Every person in these files is an invented, AI-generated adult. No real brands, logos, readable text, reviews or results.

## What is here

| Folder / file | What | Count |
|---|---|---|
| `showcase/<name>.mp4` + `.webp` | Example ad videos (H.264 720x1280, ~1 Mbit/s, AAC 80k, 8 s, faststart) and posters (WebP 540x960 from ~1 s) | 19 (15 approved earlier + 4 new) |
| `presenters/<id>/angle-left.webp`, `angle-right.webp`, `angle-profile.webp` | "Look-lock" angle pictures, 720x1280 WebP | 20 existing + 8 new presenters |
| `presenters/<new id>/portrait.webp`, `<situation>.webp` | Portrait (864x1080) and 4 situation stills (720x1280) for the 8 new presenters | 8 x 5 |
| `presenters/<id>/voice.mp3` | 5-8 s voice sample, mono MP3 96k, saying `presenters.voice_line(id)` | 28 |
| `voice_test/` | Seedance 2.5 omni-reference test: portrait + voice sample as audio reference, for maya and marcus | 2 clips + 2 samples |
| `roster_additions.json` | `new_presenters` (8 full records, same fields as `_ROSTER`), `new_voices` (8 descriptors like `VOICES`), `new_situations` (3 proposals like `SITUATIONS`, no pictures), `voice_samples` (which Higgsfield voice each presenter's sample used) |
| `MANIFEST.json` | One entry per file: path, kind, presenter/situation/angle, title/caption/alt, model, prompt, job_id, source_url, credits, qa, notes |
| `LOG.md` | Credit balance per phase, model choices, retries, anything skipped |

## How it was made

- Pictures: Higgsfield `nano_banana_2_1` (1k), always with a reference image. Existing presenters: their approved
  `static/presenters/<id>/portrait.webp`. New presenters: their first situation still (text-to-image, built with the
  `presenters.still_prompt` structure) was the reference for all their other pictures.
- Voices: Higgsfield `seed_audio` preset voices; gender/age band taken from the web app's voice filter.
- Example videos: Seedance 2.5 at 720p, 8 s, 9:16. Phase 0 clips were made earlier and approved; Phase 4 clips were made in the
  web app's Unlimited mode (no credits).
- QA: pictures checked on contact sheets next to their reference; videos checked on 4 frames each and for an audio track.
  Speech in talking clips and all voices have NOT been checked by ear.

## To wire in (for the app agent)

- New presenters: copy the records from `roster_additions.json` into `presenters._ROSTER` (and the new voice descriptors
  into `VOICES`), then move `presenters/<id>/*` to `static/presenters/<id>/`.
- Angle pictures and `voice.mp3` drop straight into `static/presenters/<id>/` (same names).
- `new_situations` need stills before they can be offered.
