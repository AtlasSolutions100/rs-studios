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

## Phase 4: example videos through Unlimited mode (web app, Chrome)
- Settings each time: Seedance 2.5, 720p, 8 s, 9:16, Unlimited mode ON, Generate showed "Unlimited" with no credit number.
  (After a page reload the Unlimited switch was OFF again and the button showed 56 credits; it was switched back on before
  every Generate.) No offer, upgrade or auto-refill dialog was used; the top "special offer" banner was closed with its X.
- Made and staged (4): ex-hair-salon, ex-plumbing, ex-bakery, ex-trainer-presenter (talking; speech not checked).
  QA on 4 frames each: no text/logos/deformations; all have sound.
- Balance did not move for these clips (5144.06 before and after), so Unlimited took no credits.
- Why only 4 of the 20: the Unlimited queue got slower through the run (first clips ~10 min, the 4th ~25 min). The 5th,
  ex-pressure-washing (job 6d2e2fb6-b629-452b-97c9-247c6fc276fb), was still "waiting" after ~60 min when the run ended; it is
  NOT staged. It should finish on its own; its URL can be read with show_generations later.
- Side trip: the Audio page's voice picker was opened in the same tab only to read the Gender/Age filter labels for Phase 3.
- Prompts ready for the next Unlimited session (same pattern, each under ~470 characters):
  - ex-flower-shop: Vertical ad for a small flower shop, no faces. A bright shop with buckets of fresh flowers, morning light. Shot 1: hands pick pink tulips and white roses from metal buckets. Shot 2: the stems are trimmed and arranged into a round bouquet. Shot 3: the bouquet is wrapped in plain brown paper and tied with twine on a wooden counter. Soft snips, rustling paper, quiet music. Warm, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-realtor-presenter: Vertical talking-to-camera video. A man of about 45 with short greying hair and a neat beard, navy jacket over a white shirt, standing on the porch of a tidy suburban house with the front door open behind him. He looks into the camera and says in a warm, confident voice: "Come take a look inside. Three bedrooms, a big kitchen, and a yard out back." Phone-camera look, natural daylight, realistic, clear voice. No text, no captions, no logos, no watermark, no house numbers.
  - ex-pizza-oven: Vertical ad for a small pizzeria, no faces. A warm restaurant kitchen at night. Shot 1: a pizza with tomato sauce and fresh mozzarella slides into a glowing wood-fired oven on a peel. Shot 2: close-up of the crust puffing and the cheese bubbling, flames behind. Shot 3: the pizza is pulled out, basil scattered on top, and sliced on a wooden board. Crackling fire, sizzling. Warm, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-pet-grooming: Vertical ad for a local pet grooming salon, no faces. A clean, bright grooming room. Shot 1: a small fluffy white dog stands calmly in a tub while warm water rinses away suds. Shot 2: a gentle dryer fluffs its fur on a grooming table. Shot 3: a brush finishes the coat and the dog looks up, clean and happy. Running water, soft dryer hum, light music. Warm, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-house-painter: Vertical ad for a house painting service, no faces. An empty living room with drop cloths on the floor, daylight through a window. Shot 1: a roller rolls a smooth stripe of sage green paint over an old beige wall. Shot 2: close-up of a brush cutting a clean line along the white trim. Shot 3: wide shot of the finished sage wall in soft light, drop cloths being folded. Roller sounds, quiet music. Realistic, calm. No text, no logos, no watermark.
  - ex-accountant-presenter: Vertical talking-to-camera video. A woman of about 40 with shoulder-length dark hair and glasses, wearing a soft blue sweater, sitting at a tidy desk with a closed laptop and a plant in a small home office. She looks into the camera and says in a calm, clear voice: "I do the monthly bookkeeping for small businesses, so you can spend your time on the work." Phone-camera look, natural light, realistic, clear voice. No text, no captions, no logos, no watermark.
  - ex-smoothie-bar: Vertical ad for a small smoothie bar, no faces. A bright counter with fresh fruit, daylight. Shot 1: strawberries, banana slices and ice drop into a blender jar in slow motion. Shot 2: the blender whirs into a smooth pink swirl. Shot 3: the smoothie is poured into a clear cup and a paper straw is added. Blender whir, ice clinking. Bright, fresh, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-handmade-soap: Vertical ad for handmade soap, no faces. A wooden workbench with dried lavender, soft window light. Shot 1: a knife slices a block of creamy lavender soap into even bars. Shot 2: the bars are stacked, flecks of lavender visible. Shot 3: one bar is wrapped in plain paper and tied with string. Gentle slicing sounds, soft music. Warm, calm, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-bike-repair: Vertical ad for a neighborhood bike repair shop, no faces. A small workshop with tools on a pegboard, daylight. Shot 1: a bicycle on a repair stand, a hand drips oil onto the chain. Shot 2: close-up of a small screwdriver adjusting the rear gears. Shot 3: the pedal is turned and the back wheel spins smoothly, chain clicking through gears. Ticking freewheel, soft music. Realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-window-cleaning: Vertical ad for a window cleaning service, no faces. A large house window from inside, garden outside, morning sun. Shot 1: a soapy applicator spreads suds across the glass. Shot 2: a squeegee pulls down in one smooth stroke, leaving crystal-clear glass. Shot 3: the clean window shows the bright green garden. Squeak of the squeegee, water drips. Realistic, satisfying. No text, no logos, no watermark.
  - ex-yoga-studio: Vertical ad for a small yoga studio, no faces. A calm studio with a wooden floor and tall windows, soft morning sun. Shot 1: a sage-green mat is unrolled across the floor. Shot 2: a row of mats with folded blankets and blocks waits in the light. Shot 3: a hand lights a candle on a low shelf, steam rising from a cup of tea. Quiet ambient music, birdsong. Peaceful, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-headphones: Vertical product ad for plain wireless headphones with no brand or markings, no faces. A tidy desk by a window, soft afternoon light. Shot 1: slow push-in on matte sand-colored headphones resting on the desk. Shot 2: a hand lifts them and turns them, showing the soft ear cushions. Shot 3: the headphones fold flat and slide into a plain fabric case. Soft music, gentle clicks. Clean, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-electrician: Vertical ad for a local electrician, no faces. A home kitchen in the evening. Shot 1: gloved hands connect wires to a new pendant light fitting on the ceiling, using a screwdriver. Shot 2: the cover is fitted and the shade straightened. Shot 3: a hand flips the wall switch and the warm pendant light glows over the kitchen island. Small tool clicks, a switch click. Warm, realistic, shallow depth of field. No text, no logos, no watermark.
  - ex-moving-crew: Vertical ad for a local moving company, no faces shown. A suburban street on a bright morning, a plain unmarked white moving truck with its ramp down. Shot 1: arms carry taped cardboard boxes up the ramp. Shot 2: inside the truck, boxes are stacked neatly and a chair wrapped in blankets is strapped in. Shot 3: the roller door slides down. Footsteps, tape, quiet music. Realistic. No text, no logos, no watermark, no license plates.
  - ex-pool-cleaning: Vertical ad for a pool cleaning service, no faces. A backyard pool on a sunny afternoon. Shot 1: a long skimmer net glides across the surface, scooping floating leaves. Shot 2: underwater view of a brush sweeping the pool floor. Shot 3: the clear blue water sparkles, sunlight dancing on the tiles. Gentle water sounds, birds. Bright, realistic. No text, no logos, no watermark.
  - ex-water-bottle: Vertical product ad for a plain stainless steel water bottle with no brand or markings, no faces. A sunny trailhead in the mountains. Shot 1: ice cubes drop into the open bottle, then cold water is poured in. Shot 2: the lid is screwed on and the bottle is set on a rock, condensation-free. Shot 3: slow push-in on the bottle with the green valley behind. Clinking ice, wind, birdsong. Fresh, realistic, shallow depth of field. No text, no logos, no watermark.

## Totals
- Balance: start 5443.56, end 5144.06 -> 299.50 credits spent (cap 2,000).
  Phase 1 ~95.5 (incl. 3.5 model test), Phase 2 ~103.5 (64 pictures + 5 retries + first-still batch), Phase 3 ~100.5
  (84 for the two Seedance audio-reference clips, ~16.5 for ~33 voice samples incl. re-picks), Phase 0 and 4: 0.
- Staged: 19 example videos (15 approved + 4 Unlimited), 84 angle pictures (60 existing + 24 new), 32 situation stills and
  8 portraits for 8 new presenters, 28 voice samples, 2 voice-test clips.
- Nothing purchased, no plan or auto-refill change, nothing posted or sent.

## Run 2 (Nov 12 2026): new styles + free lane
- Balance before: 5144.06. Hard cap for this run: 1,500 credits (do not go below 3644.06).
- The worktree folder /dev/shm/rs-assets was gone at the start (the branch was intact locally and on the remote at 6d8bf1d);
  it was re-created with `git worktree add` on the same branch.
- Credits lane: Seedance 2.5, 720p, 8 s, 9:16, with audio, through the MCP tools; preflight 56 credits per clip, with or without
  an image reference. Presenter clips used the roster situation still as `image_references` (mode omni_reference).
- New manifest fields on run-2 video entries: `style`, `script`, `transcript` (faster-whisper base.en, int8, vad_filter),
  `served` (path, sha256, bytes of the 540x960 encode). Served encodes all used crf 29 (all under 600,000 bytes).
- QA: 5-frame contact sheet per clip plus zoomed crops of products, screens and phones; transcript checked against the line.

### Credits-lane clips (56 credits per job)
| Clip | Jobs | Credits | Result |
|---|---|---|---|
| ex2-tryon-denim-jacket | 1 | 56 | kept |
| ex2-tryon-midi-dress | 1 | 56 | kept |
| ex2-tryon-sneakers | 2 | 112 | DROPPED: garbled lettering stamped on the shoe tongue, both takes |
| ex2-unboxing-pov-bottle | 1 | 56 | kept |
| ex2-unboxing-headphones (ben) | 2 | 112 | DROPPED: take 1 had a logo-like embossed mark on the box and headphones close to a real product's design; take 2 had red lettering on the hinges and again a real-product look |
| ex2-app-in-hand-workout (kai) | 2 | 112 | DROPPED: phone screen showed small garbled labels, a status-bar clock and tab icons, both takes |
| ex2-app-in-hand-budget (dana) | 2 | 112 | kept (redo); take 1 had a garbled heading and status-bar digits |
| ex2-saas-hook-hallway | 1 | 56 | kept |
| ex2-saas-hook-desk (tess) | 1 | 56 | kept |
| ex2-saas-screen-over-shoulder | 2 | 112 | DROPPED: take 1 showed a browser bar with a browser-like icon and garbled labels; take 2 showed a chart with numbers |
| ex2-showcase-planter | 1 | 56 | kept |
| ex2-showcase-steel-bottle | 1 | 56 | kept |
| ex2-tryon-wool-coat (extra) | 1 | 56 | kept. Line: "It's a camel coat, it falls past the hip, and it has two deep pockets." |
| ex2-unboxing-mug (extra, omar) | 2 | 112 | kept (redo); take 1 had a small logo-like emblem on the t-shirt. Line: "Just arrived. A speckled mug, packed in plain paper." |
| ex2-showcase-cutting-board (extra) | 1 | 56 | kept, no speech |
| ex2-unboxing-pov-throw (extra) | 1 | 56 | kept, no speech |
- Total credits lane: 22 jobs x 56 = 1,232 credits. Balance after: 3912.06 (checked with `balance`).
- Lesson: phone/laptop screens and small product surfaces (shoe tongues, hinges, box lids) are where Seedance adds text
  or logos; "no status bar, no clock" plus a "plain illustration" screen description worked once (budget) and failed once (workout).

### Free lane (web app, Unlimited mode)
- ex-pressure-washing (run-1 job 6d2e2fb6) had finished: staged.
FREE_LANE_PLACEHOLDER
