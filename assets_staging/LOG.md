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

## Phase 2: 8 new presenters
- New: ray (M 55-64), luis (M 55-64), omar (M 35-44), ben (M 35-44), dana (F 45-54), gloria (F 55-64), kai (M 18-24, about 22), tess (F 25-34).
  Records in roster_additions.json (new_presenters), plus 3 proposed service-business places (new_situations, no pictures).
- Model: nano_banana_2_1, 1k. First still text-to-image with the still_prompt structure; every other picture of that person
  used the first still (by its job id) as the reference image.
- 64/64 staged: per presenter 4 situation stills, portrait (864x1080), 3 angle pictures.
- Retries (5): dana first still (a glass stood in the product space); portraits of ray, ben, tess, gloria (the model put other
  people or a different place behind them). Retried portraits use a stricter background line ("Nobody else is in the picture").
- QA note: gloria/sofa has a few small items on the coffee table near the product space (kept, noted in MANIFEST).
- Balance after Phase 2: 5244.56 (Phase 2 ~91.5 cr incl. the 12 cr first stills counted above).

## Phase 3: voices
- Model: seed_audio (Seed Audio 1.0) with Higgsfield preset voices; preflight 0.5 cr per sample, so all 28 were made.
- 8 new voice descriptors in roster_additions.json (new_voices).
- Voice choice: list_voices gives names only. Gender and age band were read from the web app's voice picker filter
  (Gender: Female/Male; Age: Young/Middle aged/Old) and recorded per sample (voice_samples[].vendor_tags). Each presenter got a
  different preset voice; tone (warm, brisk, gruff, ...) could not be judged by ear.
- First picks by name were partly wrong for age/gender (e.g. "Remy" is a female voice, "Celine" is an old female voice), so
  maya/priya/elena/nia/rosa/marcus etc. were re-picked from the filter lists before staging. Extra samples made and not used: ~5.
- seed_audio rate-limited batches above ~4-8 requests (HTTP 429); resubmitted in groups of 4 with pauses. One 400 "Voice not
  found" from a mistyped voice id, fixed and resubmitted.
- Staged 28 presenters/<id>/voice.mp3 (mono MP3 96k). Most lines were spoken in 3.4-5 s, so silence was trimmed and then padded
  (0.3 s lead-in, quiet tail) to reach >= 5.3 s; marcus and rosa run 7.4-7.5 s of speech (file 8.0 s).
- Pitch check (automatic, rough): the male samples Dylan (jordan), Evan (theo) and Cody (otis) measured unusually high for male
  voices; the app labels them male/young. Koby should listen to those three first.

### Phase 3 audio-reference test (Seedance 2.5, omni_reference)
- Preflight: 42 cr per 6 s 720p clip; 2 clips = 84 cr (cap for this test was 150).
- maya (portrait + Bella sample) and marcus (portrait + Reid sample). The vendor ACCEPTED the audio_references media
  (passed as the audio job id); both jobs completed with sound. Files in voice_test/ (clip + the sample it was given).
- Picture: both hold the presenter's face from the portrait, lip movement visible, no text/logos.
- Rough automatic pitch comparison (not a listening test): marcus clip median ~158 Hz vs sample ~163 Hz (close);
  maya clip ~198 Hz vs sample ~232 Hz (lower than the sample). Whether the voice really matches needs Koby's ears.
