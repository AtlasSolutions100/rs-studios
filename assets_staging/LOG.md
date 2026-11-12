# Asset run log (branch assets/library-2)

## Start
- Higgsfield balance at start: 5443.56 credits (plan: ultra). Hard cap for this run: 2,000 -> do not go below 3443.56.

## Phase 0: stage 15 approved example videos
- Balance before/after: 5443.56 / 5443.56 (no generation).
- 15 clips downloaded, re-encoded (H.264 720x1280, ~1 Mbit/s, AAC 80k, faststart, 8.0 s, 0.95-1.13 MB), posters 540x960 WebP.
- QA: 4 frames per clip on contact sheets; no logos, readable text or deformations seen; all have an audio track.
  Talking clips (coffee, lawn care, tote, cleaning): picture ok, speech not checked by ear.

## Phase 1: look-lock angle pictures (20 existing presenters)
- Model choice: tested nano_banana_2_1 (1k, 1.5 cr) and gpt_image_2_6 (medium, 0.25 cr) on maya and marcus. Both held identity;
  Nano Banana 2.1 held face shape closer and turned the head more, so it was used for all 60. Test cost 3.5 cr.
- Reference: each presenter's static/presenters/<id>/portrait.webp, imported by its public GitHub raw URL (media_import_url)
  instead of a presigned PUT upload.
- Angle wording was strengthened after the test ("a clear three-quarter view with the far side of the face partly hidden").
- 60/60 staged (720x1280 WebP, <=180 KB). QA on contact sheets next to the portrait: all same person, no text/logos.
  One regenerated: nia angle-profile came out three-quarter; second try with a stricter profile wording is a true profile.
  Mild notes: elena angle-right and maya angle-left are turned less than 45 degrees; jordan's profile is slightly short of 90.
- Balance before/after: 5443.56 / 5336.06 (includes 8 Phase 2 first stills submitted in parallel, 12 cr). Phase 1 itself ~95.5 cr.
