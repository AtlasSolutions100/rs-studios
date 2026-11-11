# Campaignify

Campaignify (campaignify.ai), Rector & Stull's ad studio: templates that already sell, a brand kit (product
name, approved claims, product photos), and generation of image ads, photo-to-video
clips and motion-copy videos. Members pay in RS credits (1 credit = $0.01; price =
our cost x `RS_MARKUP`). Public and self-serve: the landing page at `/` (logged
out), open signup with free starter credits (`RS_STARTER_CREDITS`, default two
image ads; sign-ups from a paid ad get `SIGNUP_CREDITS_ADS`, default 500, once per person: `adoffer.py`), a guided first ad (`/app/start`), and credit packs bought through
Stripe Checkout (`/app/billing`). Built for independent reps: income, earnings
and recruiting claims are blocked on every brief.

The public site sells the whole campaign, A to Z: `JOURNEY` in `app.py` is the
landing page's spine (plan, make, write, get found, launch, capture leads, grow,
measure) and also drives `/features` and `/llms.txt`. Landing media lives in
`static/site/` (generated loops with unbranded, invented products, plus app
screenshots taken from a seeded demo workspace, light and dark) and reuses the
template previews in `static/previews/`. Keep it truthful: no real brands, no
presenters or testimonials, no claims of posting ads or reading ad-platform results.

## Run locally

    pip install -r requirements.txt
    RS_DATA=./data HIGGSFIELD_API_KEY=key:secret python app.py      # http://localhost:8080

Owners (Koby, Travis) sign up with their one-time setup links (`owners.py` holds only the code hashes) and become admins of the Rector & Stull workspace. Anyone else can sign up and gets their own workspace with the starter credits (signups are rate-limited per IP and have a honeypot). A new free account can make image ads (and copy, pages...) with its starter or ad-offer credits straight away, up to `RS_UNVERIFIED_CREDITS` (default 250) of generations; anything that makes a video (talking/UGC, product video, motion copy, 1080p, the free video preview, a voiceover over the product) waits for a confirmed email. A form sign-up answers "Check your email" whether or not the email already has an account (SEC-8): a new account waits for the emailed link (`/verify`, 48 hours, single use), and opening it is its first login; a taken email gets "You already have an account" instead, and a sign-up still waiting gets a new link with the newest password (off by default: `RS_SIGNUP_CONFIRM=1` turns this confirm-first sign-up on; off, a new sign-up logs straight in). A password reset logs out every other browser (`users.session_version`), and past 20 wrong passwords in 15 minutes an account slows new browsers to one try every 30 s and emails the owner once an hour, while browsers that have logged in to it before carry on (`login_security.py`). Otherwise signup emails a signed, single-use link (`/verify`, 48 hours) and a 6-digit code (15 minutes, 5 tries, stored as an HMAC; `verify.py`) in the same email; starting a video opens a small "Confirm your email" box that takes the code and then carries on. A banner offers Enter code and Resend (once a minute, three an hour), and paid plans, pack buyers, admins and accounts made before this check are never blocked (`RS_EMAIL_VERIFY=0` switches it off; free previews are capped at `RS_FREE_PREVIEWS_PER_DAY`, default 30). Home for a new account is the first-ad form: "Paste your product page" (the same importer as Products, without the AI read, so it costs nothing) fills name, brand and photo, or they type the name and upload one; the first submit makes 2 image ads at once from the starter credits (two different templates: the one picked plus one that suits the product, `catalog.first_run_pair`) and Home shows them rendering with a "confirm your email to unlock video" box underneath (ad-grant accounts get the 5-second product video pre-selected, behind an inline code box). With `RS_SERVICE_PATH=1` (off by default; `service_path.py`) the form first asks "What do you sell?": "A service" takes the business website instead (business name, logo, colors, up to 3 photos and up to 5 suggested claims, read without AI), needs no product photo, and makes the 2 free ads from a site photo or a logo-and-headline template; the product import then also fills 3 photos and suggested claims, and the planner adds a service area, a Google keyword list and the "Publish the lead page", "Add the quote form" and "Turn on the follow-up flow" steps. A sign-up with no ad after 10 minutes gets "Your 2 free ads are waiting" (then once more at 24 hours) with a magic link, `/m?t=...`, that logs in and confirms the address (`nudges.py`; `FIRST_RUN_NUDGES=0` switches it off). When a member's first batch finishes they get one "Your ads are ready" email with a link to it (`ready_email.py`; `ADS_READY_EMAIL=0` switches it off). Invite links from Admin join an existing workspace. "Continue with Google" on Log in and Start free (`google_auth.py`, OpenID Connect with `requests`, no SDK) signs an existing account in by its email, or makes a self-serve account exactly as the form does (`accounts.py` holds that shared code) with the email already confirmed and `users.auth_provider = "google"`; it shows only once `GOOGLE_OAUTH_CLIENT_SECRET` is set (the public `GOOGLE_OAUTH_CLIENT_ID` defaults to `google_auth.DEFAULT_CLIENT_ID`; the authorized redirect URI in Google Cloud is `RS_PUBLIC_URL` + `/auth/google/callback`, so https://campaignify.ai/auth/google/callback). Admin: top up credits, create
invite links, see purchases, upload reference videos for "Copy a video's motion".

## Credit packs (Stripe)

Packs are one-time Prices on the Rector & Stull Stripe account (products with
metadata `brand=Campaignify`, `pack=<code>`; ids in `catalog.PACKS`): Starter
$29 = 2,900 credits, Pro $79 = 8,500, Studio $199 = 23,000. `/app/billing/checkout`
records an open purchase and starts a Checkout Session (mode=payment) with
metadata `app=campaignify, purchase_id, workspace_id, pack`. The webhook
`POST /webhooks/stripe` (event `checkout.session.completed`) checks the
signature, matches the purchase, amount and currency, and adds the credits once
(a retried event adds nothing). The Stripe account is shared with Crowdcut and
The Morning Upgrade: anything without our metadata is ignored, and we never
create Stripe customers or set `metadata.account_id` (Crowdcut's key).
Needs `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`; without them the prices
show and the buy buttons say "Checkout coming soon".

## Plans (Stripe subscriptions)

`plans.py` is the one place that decides access: `plans.has(ws, feature)`, `plans.limit(ws, "brands"|"seats"|"pages")`.
Starter $39 / Growth $99 / Scale $249 a month (recurring Prices on the shared Stripe account, ids in `plans.PLANS`)
with 2,000 / 6,000 / 18,000 credits a month; every plan includes everything below it; free accounts keep the studio,
starter credits and packs. `/app/billing/subscribe` starts a Checkout Session in subscription mode with metadata
`app=campaignify, kind=plan, workspace_id, plan` on the session and the subscription. The webhook handles
`checkout.session.completed` (links customer + subscription), `invoice.paid` (grants credits once per invoice id),
`customer.subscription.updated` (plan, status, cancel-at-period-end) and `customer.subscription.deleted` (back to free).
Credits live in two buckets: **plan credits** (each paid invoice replaces them, so unused plan credits expire at
renewal; upgrades add the difference right away) and **pack credits** (starter, packs, admin top-ups; never expire).
Spending takes plan credits first; refunds go back to the bucket they came from. "Manage or cancel plan" opens the
Stripe Customer Portal with the Campaignify configuration (`billing.PORTAL_CONFIG`: our three plans, upgrades invoiced
immediately, downgrades and cancellations at period end). Admins can comp a workspace onto a plan (`plan_override`). Free-month codes (`compcodes.py`, `/admin/codes`): an admin makes
single-use codes (CFY-XXXX-XXXX, one per label line, a plan, a number of months, a redeem-by date, CSV download,
revoke); a workspace enters one at sign-up (`/signup?code=...` prefills it) or under "Have a code?" on Billing and
gets that plan's features and monthly credits (`comp_plan` / `comp_until`) with no Stripe customer, card or
subscription. One code per workspace, not for workspaces that already pay; at `comp_until` it drops back to Free on
its own (plan credits expire, pack credits stay).
Brands (`brands` table; the workspace's brand columns mirror the active brand) and seats (Team page, owner-made invite
links) are capped per plan server-side; Scale jobs get queue priority.

## AI writing (copy studio, optimizer, later the planner and SEO)

`llm.py` talks to Anthropic's Messages API (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, default `claude-sonnet-4-6`) with an
optional Groq fallback (`GROQ_API_KEY`) for single structured calls. Structured output uses a forced tool call. Members
pay what each call actually cost: tokens x list price (`llm.PRICES`, or `LLM_USD_IN_PER_MTOK`/`LLM_USD_OUT_PER_MTOK`)
x `RS_MARKUP`, rounded up, at least 1 credit; the quote shown before running is the worst case at the token budget.
Without a key the tools say "not switched on" and nothing is charged. `copywriter.py` holds the prompts (with the
compliance rules), `platforms.py` the per-platform fields and lengths. Copy studio (`/app/copy`, Starter+) and content
optimizer (`/app/optimize`, Growth+; its quick checks are free and need no key) live in `copy_routes.py`.
Shared guards (`login_required`, `feature_required`, `owner_required`, `brand`, `charge_ai`) live in `core.py` so
blueprints never import `app.py`.

## AI campaign planner (`/app/campaigns`, Starter+)

`planner.py` is a tool-use agent (Anthropic only) over Campaignify's own tools: `get_brand_and_catalog`,
`save_angles`, `save_creative` (hooks, script, on-screen text), `propose_ad` (validates tool/template, checks the brief,
records a proposal with its credit price; never spends), `write_copy` (the copy studio), `check_compliance`, `finish`
(summary, budget split, KPIs, test plan, launch checklist). It runs in a background thread (inline when `RS_WORKER=0`),
saving each step so the page shows progress live. A run is capped at `RS_PLANNER_CAP` credits (default 150) and
charged once at the end for what it actually used (prompt caching counted at cache prices); a run that produces
nothing costs nothing. Ads are generated only when the member picks them and confirms the total
(`campaign_routes.generate` recomputes it and refuses a mismatch). Runs interrupted by a restart show as finished or
failed after 12 minutes. Revising re-plans with notes and keeps generated ads.

## SEO / AEO / GEO optimizer (`/app/seo`, Growth+)

`fetcher.py` reads member-supplied URLs safely: http(s) on ports 80/443 only, no credentials, every DNS answer must be
a public address (private, loopback, link-local, CGNAT, multicast, reserved and IPv4-mapped forms are refused), the
connection goes to the checked IP (no DNS rebinding), redirects are re-checked hop by hop, 2 MB and short timeouts.
Audits are rate-limited per workspace (30/hour). `seo.py` parses the page and scores three groups of checks (SEO,
AEO, GEO including AI crawler rules in robots.txt and llms.txt), and the optional AI pass writes a title, description,
answer-first summary, FAQ, keyword and topic ideas; `seo.jsonld` serializes schema.org JSON-LD itself so it's always
valid. The audit is free; AI suggestions are charged at cost. The planner gets an `audit_page` tool on Growth+.
Our own site's search metadata lives in `public_site.py`: a title, description, canonical URL on
https://campaignify.ai, share card (`static/img/og-campaignify.png`) and JSON-LD (Organization, WebSite, WebPage,
SoftwareApplication with Offers from `plans.py`, FAQPage where a FAQ shows) for each public page, `/sitemap.xml`,
`/robots.txt`, `/llms.txt`, and the answer-first pages (`/ai-ugc-ad-generator`, `/arcads-alternative`,
`/ai-product-ads-for-shopify`, `/ai-ad-copy-generator`, `web/solution.html`) plus `/pricing`. Everything else
(the app, admin, login/signup, files, share links) sends `X-Robots-Tag: noindex` and a noindex robots meta.

## Google Ads tag (`gads.py`)

The Google tag `AW-18543041154` loads on every page built on `web/base.html` (public site and app; never on
members' pages or share links). Ad cookies start denied in the EEA/UK/CH and under GPC/Do Not Track (there's no
cookie banner). Conversions fire only when their labels are set: `GADS_SIGNUP_LABEL` (sign_up, on the first page after
a new non-owner account) and `GADS_PURCHASE_LABEL` (purchase, on the billing page Stripe returns to with
`session_id`, value + currency, transaction_id = the Checkout Session id, reported once via `ad_conversions`).
A visit with UTM tags or an ad click id (gclid/gbraid/wbraid, fbclid, ttclid, rdt_cid, twclid) leaves a 90-day
first-party cookie (`cfy_ft`) that sign-up saves on `users.first_touch`.

## Ad pixels (`pixels.py`, `web/_pixels.html`) and server-side conversions (`conversions.py`)

Meta, TikTok, Reddit and X pixels load on the same pages as the Google tag once their ids are set (repo variables
`META_PIXEL_ID`, `TIKTOK_PIXEL_ID`, `REDDIT_PIXEL_ID`, `X_PIXEL_ID`, plus `X_EVENT_SIGNUP` / `X_EVENT_PURCHASE` /
`X_EVENT_VIEWCONTENT` for X's per-event ids); unset, nothing loads. Page view everywhere, ViewContent on `/pricing`,
SignUp (Meta/TikTok CompleteRegistration, Reddit SignUp, X sign-up event; id `signup-<user id>`) and Purchase (Meta
Purchase, TikTok CompletePayment, Reddit Purchase, X purchase event; value + currency, id
`purchase-<Checkout Session id>`) at the same moments as the Google conversions. They never load under GPC/DNT, in
the EEA/UK/CH (edge country or a European time zone), on admin pages, or on a page whose query has anything but
UTM tags, click ids or the checkout return. With `META_CAPI_TOKEN` / `TIKTOK_EVENTS_TOKEN` / `REDDIT_CAPI_TOKEN`
(secrets) the server also sends SignUp (at sign-up) and Purchase (Stripe webhook) with the same event ids
(`conversion_queue`, retried by the worker), so each platform counts each conversion once.

## Funnel events for HQ (`site_events.py`, `web/_beacon.html`, table `site_events`)

An append-only table in the main database (`rs.db`) that HQ (dashboard.rectorandstull.com) reads to show the
funnel by first-touch source and ad, the same way it reads Crowdcut's. One row per step; columns (a contract with
HQ's reader, don't rename): `id`, `ts` (ISO UTC), `vid` (random id from the first-party `cfy_vid` cookie; never an
email or user id), `name`, `path` (no query), `utm_source` / `utm_medium` / `utm_campaign` / `utm_content` /
`utm_term` and `click_id` + `click_type` (`gclid` / `fbclid` / `ttclid`) of the visitor's first touch (the same
`cfy_ft` capture as `gads.py`, then `users.first_touch`), `referrer_host`, `detail` (<= 120 chars, never personal)
and `workspace_id` (server-side steps only). Indexes on `ts` and `(name, ts)` (plus `vid` and `workspace_id`).

| step | written by | where |
|---|---|---|
| `lp_view` | browser, once per page load | `/`, `/features`, `/pricing`, the solution pages |
| `cta_click` | browser, `detail` = the CTA's `data-cta` (hero, nav, footer, closing, pricing-…) | links to `/signup` on those pages |
| `signup_view` | browser | `GET /signup` (not invite links) |
| `signup` | server, with `workspace_id`, once | `app.signup`: a new workspace (not invite joins or owner links); `detail` starter / ad_offer / invite |
| `first_ad` | server, once per workspace | `core.queue_job`: the workspace's first generation; `detail` = the tool |
| `checkout` | server | `/app/billing/subscribe` and `/app/billing/checkout`; `detail` `plan:<code>` / `pack:<code>` |
| `paid` | server, Stripe webhook | a pack paid (once per purchase) or a plan's first invoice (`subscription_create`, once per invoice) |

Browsers post to `POST /e` (JSON or a `text/plain` sendBeacon body): CSRF-exempt, same-origin only, browser steps
only, 2 KB max, 30 a minute per visitor and per IP (in memory), bots dropped by user agent. No IP address or email
is stored. Server-side steps copy the workspace's `signup` row (vid + first touch), so every step credits the
same source. Consent follows the pixels: under GPC / Do Not Track, in the EEA/UK/CH or with a European time zone
the beacon doesn't run and sets no cookie; those visitors' server-side steps still count, with no vid.

Test traffic is written and left out when read, as Crowdcut does: `utm_source` = `probe` (any case) or
`click_id` = `test123` (`FUNNEL_TEST_SOURCES` / `FUNNEL_TEST_CLICK_IDS`); `site_events.test_traffic_sql()` is
the condition. Visitor steps (`lp_view`, `cta_click`, `signup_view`) are meant to be counted as distinct `vid`
(`COALESCE(vid, id)`), the rest as rows.

## Ad-account setup hub (`/app/ad-accounts`, Starter+)

`adhub.py`: per platform (Meta, TikTok, YouTube/Google Ads, X, LinkedIn, Pinterest, Snapchat, Reddit) the setup steps,
tracking (pixel/tag) links, spec and policy pages, and export sizes. Every URL was checked against the platform's own
site in October 2026; deep links we couldn't confirm were left out rather than guessed (the tests assert every link is
https on an allow-listed first-party host). Step ticks are stored per workspace (`adhub_progress`). `exporter.py`
center-crops and scales a finished ad to a platform size (Pillow for images, ffmpeg for video), cached next to the
original; Library shows "Export sizes". The planner's launch checklist links each platform to its guide.

## Landing pages and smart links (Starter+; analytics Growth+)

`pages.py` + `pages_routes.py`. Pages are lists of blocks (hero, image/video from the workspace's library, text,
benefits, FAQ, call to action, embed) edited in a plain form; members never write HTML. Text is escaped by the template,
links must be http(s)/mailto/tel (`pages.safe_url`), embeds are rebuilt from YouTube/Vimeo/Loom/Calendly/Spotify URLs,
media must belong to the workspace and is served publicly only while it's on a published page
(`/p/<slug>/m/<id>`). Public pages (`/p/<slug>`) send a strict CSP with no scripts. Publishing re-runs the compliance
checks; Starter can publish 1 page, Growth/Scale unlimited (`plans.limit(ws, "pages")`). Views are counted once per
visitor IP per 30 minutes (the IP lives only in the 24-hour throttle table). Smart links (`/l/<code>`) redirect
with the link's UTM tags plus incoming UTM tags and click ids (gclid, fbclid, ttclid…), record referrer host, device,
UTMs and a country when the edge sends `CF-IPCountry`/`X-Country` (no IP stored), skip bots, and are rate-limited per
IP. Starter sees click counts; Growth+ gets 30-day charts and breakdowns.

## Forms and contacts (Growth+)

`forms.py` + `forms_routes.py`. Fields: short text, email, phone, long text, dropdown, checkbox, each optionally
"show only if" an earlier dropdown/checkbox has a value. Public pages run no scripts, so the logic is CSS (`:has()`)
and re-enforced server-side (hidden fields aren't required and their values are dropped). Forms work as a page block
or a hosted link (`/f/<code>`); submissions need a signed timestamp (not faster than 2 s, not older than a day), pass a
honeypot and per-IP limits (6 per form per 10 minutes, 30 an hour). Each submission upserts a contact by email (tags
from the form) and records engagement events: the form, the page, and the smart link when the visitor arrived through
one (`/l/` links to our own pages append `cfy_l=<code>`). Webhooks go to public https URLs only (fetcher checks, pinned
IP, no redirects), signed `X-Campaignify-Signature: sha256=<hmac of body>`; the result is stored on the submission.
There is no mailer in the app, so email notifications are not sent (the form editor says so). Contacts: search, tag
filter, notes, CSV export (formula-safe), delete (also wipes their answers).

## Lead pipeline (`pipeline.py`, `/app/leads`, Growth+ with Forms)

A light CRM over the contacts: where each lead stands, what has happened with them, and a way to write to one person.

- **Stages.** The six flow stage keys are the pipeline (`flows.STAGES`: lead, engaged, qualified, customer, repeat,
  cold), so a flow's "Move to a stage" step and the board always agree. The built-in names are New lead, Contacted,
  Qualified, Won, Delivered and Lost. A workspace renames any of them and adds up to four of its own (`x1`..`x4`,
  placed after Qualified) under Rename stages; names are kept in `workspaces.pipeline_json`. Removing an own stage
  moves its people back to Qualified. Flows can't move a contact to an own stage yet.
- **Board** (`/app/leads`): one column per stage in order, Lost in a list underneath, newest activity first, 60 cards
  a column with a link to the full list. Each card has a plain select and a Move button (no drag and drop, so it works
  by keyboard and on a phone).
- **History.** Every move writes a `contact_events` row of kind `stage` ("New lead to Contacted, by Koby"). The contact
  page shows one timeline, newest first: forms, pages, smart links, ads, opens, clicks, text replies, stage moves, and
  every email and text sent, waiting or not sent, with who wrote it (a follow-up by name, or the member).
- **Write to one lead.** Email and text panels on the contact page. A message is a `messages` row with no flow
  (`flow_id` NULL) and goes out through the same senders as flows (`messaging.send_queued`, `sms.send_queued`), so
  the same rules hold: the workspace's sending setup, the contact's consent, unsubscribes and STOP, quiet hours for
  texts, the claims check with the member's own words as the stated offer, and the daily caps. The price is on the
  button (an email is 1 credit; a text is the per-part price). A direct message that can't go is marked not sent with
  the reason; there is no flow to pause.

Tests: `tests/test_pipeline.py`.

## Flows (`/app/flows`, Starter+; email Growth+, texts Scale)

The follow-up after the ad: a trigger (form submitted, tag added, contact added, community joined, a date) and steps
(email, text, wait, if/else, exit when, tag, stage, task, notify, webhook, community invite). Built in a vertical step
editor, from a template, or drafted by AI from a sentence (charged at cost, saved off for review). `flows.py` holds the
data model, validation, timeline and templates; `flows_engine.py` enrols contacts and walks runs from the worker loop
(leases, versions, auto-pause on errors). Until a provider is connected, email and text steps become tasks in
`/app/tasks` with the copy ready to send.

**Email (F1, Resend).** Set `RESEND_API_KEY`, `RESEND_WEBHOOK_SECRET` and `RS_MAIL_DOMAIN` (deploy.yml passes them).
In Resend: verify `mail.campaignify.ai` (SPF, DKIM, DMARC and the bounce MX records it shows) and add a webhook to
`https://campaignify.ai/webhooks/resend` for email.sent/delivered/bounced/complained/opened/clicked. Mail goes out as
"Brand via Campaignify" <ws-ID@mail.campaignify.ai> with Reply-To the workspace's address. Each workspace sets its
sender name, reply-to and postal address and accepts the Messaging Policy in Settings → Sending before anything sends.
Every email gets the postal address, a one-click unsubscribe link and `List-Unsubscribe` headers; links are tracked
through `/t/<token>`, opens through a pixel. Consent comes from form checkboxes (stored with time, wording hash and
IP hash), CSV import (`email_consent` column) or a manual setting. Caps: 300/day Growth, 2,000/day Scale, 50/day in a
sender's first week, 2 a day per person; complaints over 0.3% or bounces over 5% pause the flow. 1 credit per email.

**Texts (F3, Twilio, Scale).** Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_PRIMARY_PROFILE_SID`
(Campaignify's approved ISV primary customer profile). Each workspace registers in Settings → Sending → Text messaging:
toll-free verification (recommended under ~1,000 texts a month) or A2P 10DLC (business profile → brand → campaign). The
app buys the number, makes a Messaging Service with inbound/status webhooks at `/webhooks/twilio/sms` and
`/webhooks/twilio/status` (signature-checked), and polls the registration every 30 minutes. Texts only go to people
with recorded text consent, 9:00-20:00 in their time zone, at most 1 a day and 4 a week; the first one carries the
STOP/HELP line; STOP/START/HELP replies are handled. Prices: segment and number at the markup, carrier and
registration fees at cost, charged before each step.

## Community and courses (Scale)

`community_routes.py`. Workspace side `/app/community`: settings (name, address, optional open join link), spaces,
courses (modules -> lessons with text plus a library video/image or a YouTube/Vimeo/Loom embed, draft/published),
members (one-time invite links, admin/member roles, remove) and events (time zone, length, link). Member side
`/c/<slug>`: members have their own logins (`c_members`, not workspace seats; session key `cm`), post and comment in
spaces (rate-limited; authors and admins delete, admins pin), take courses with saved progress, and download events
as .ics. Workspace users of the owning workspace are members automatically (owner/admin). Member pages are escaped,
run no scripts and send the strict CSP; everything goes offline if the plan drops below Scale.

## Image ad text and share links

The image model draws an image ad's text itself, so `catalog.image_ad_prompt` (built by the worker for every image
job, whichever flow made it) lists the only words allowed on the image: the member's headline, the optional subline
and button text they typed in Create, and the brand name. Everything else in the brief is treated as scene: quoted
words, "Callout:"-style text, sentences about buttons, badges or prices, and any sentence repeating one of the
product's claims (approved or only suggested) are taken out before it reaches the model. "Write it for me" sees the
selected product photo (vision) when Anthropic is on, and otherwise is told not to describe colours or packaging.

Any finished output can get a private share link (`/s/<token>`, `share_routes.py`) from the ad's Details: a bare,
noindex page with just the file, the product's name and a "Made with Campaignify" link; the file is served through
the token so it works logged out. Stop sharing (or deleting the ad) makes the link 404; sharing again makes a new token.

## Presets, the text layer, the claims check, image QA and the free fix (quality wave 2)

- **Presets** (`presets.py`): one record per thing Create can make, each with the same contract: format, shapes,
  what the member supplies, the prompt recipe, the model and its settings, the beats, the text layers, the QA
  checks and the price. The 5 ad types and the 14 older templates are built from it and are word for word what they
  were (`tests/fixtures/presets_snapshot.json`, made by `tools/presets_snapshot.py` before the move). New: 14 scene
  statics so every product category has its own, and 6 statics with a text layer. Create lists the presets that fit
  the product with a one-line "what it's for" and a thumbnail (`tools/preset_thumbs.py` draws placeholders).
- **Text and logo layer** (`textlayer.py`, `TEXT_LAYERS`, default on): for the layered statics the image model makes a
  picture with no words, then Pillow sets the headline, subhead, price or offer badge, button, feature callouts,
  review card, comparison and the brand's logo in the brand kit's colours and font (Brand page > Look). Safe areas per
  shape, auto-fit, and a 4.5:1 contrast check with a panel when the picture is too busy. A badge can only show the
  product's saved price or offer; a review card needs the member's word that the review is real and is never
  written by AI. Fonts: Poppins and DM Serif Display (OFL, `static/fonts/ads`). Since m15 these pictures are asked
  for as "a product photograph with no text on it" with nobody in them (`catalog.TEXT_FREE`, `presets.NO_PEOPLE`):
  the image model is an ad model and had painted its own invented copy, and once a man holding the product, into
  them. Words never go over a face: `textlayer.locate_people` (a local skin-tone heuristic, no model) treats a face
  like the product, parts that can move are moved off it, and a picture whose words can't be moved off one is made
  again once, silently.
- **Claims check** (`claims_check.py`, `CLAIMS_CHECK`, default off): one fast-model call per script maps every factual
  sentence to an approved claim, the product's saved details or its page text (kept at import). A "supported" verdict
  only stands when its quote really is in the source. Create shows flagged lines with "edit or remove" and Generate
  refuses while one is left. Free to the member since m15: we pay about $0.004 of model cost a script, results are
  cached, and `CLAIMS_CHECK_DAILY_MAX` / `CLAIMS_CHECK_WS_DAILY_MAX` cap a day's checks (below).
- **Image QA** (`image_qa.py`, `IMAGE_QA`, default off): a vision check of each finished image against the product
  photo (product present, label matches and is legible, no extra or garbled text, no malformed hands or faces). A
  failed picture is made again once, silently; one that fails twice is refunded automatically ("We couldn't get this
  one right — credits returned."). Every picture gets an `asset_qa` row; the admin page shows first-pass rates per
  preset. Our cost: about $0.004 a picture, $0.111 more for a silent retry; `IMAGE_QA_DAILY_MAX` caps it.
  One part is not behind the switch (m15): the text-free picture of a static with a text layer is always checked,
  before any layer is drawn, for words the image model painted in and for a person in it
  (`image_qa.LAYERED_CHECKS`), with the same silent retry and refund on the second failure. Invented claims in a
  picture are a compliance problem, so that check doesn't wait for the switch; it costs us $0.004 a layered picture.
- **Free fix** (`free_fix.py`, `FREE_FIX`, default off): one free "fix it" per delivered image with the member's note,
  `FREE_FIX_MONTHLY_CAP` a workspace a month (default 10; `workspaces.free_fix_cap` overrides it for one workspace).
  Our cost: $0.1071 a fix. No price changes.

## m15 library: our own presets, the presenter roster and their example assets

**Presets** (`presets.py`). 67 records: the 5 ad types and 62 ad presets. New in m15, each with the same full
contract plus a `category` tag (the product categories it fits) and a `use` tag (`presets.USES`: offer, social-proof,
problem-solution, unboxing-pov, hands-only-demo, talking-head-cutaways, green-screen-product-page, comparison,
marketplace, seasonal, founder-story, how-to, before-after-honest, testimonial-real-only ...); the older 39 got tags
and nothing else (`tests/fixtures/presets_m15_snapshot.json` holds their prompts, prices and cards from before):

- 13 statics on the text layer (`STATICS_M15`): eight seasonal ones (winter gifting, fresh start, spring, summer,
  autumn, back to routine, a gift for them, seasonal offer), problem-then-answer, three reasons, how it works, the
  honest switch ("the old way, the new way": never for food, supplements or skincare, and the house rule against
  before-and-after wording still applies to every line) and an app-screen hero.
- 15 fixed video formats (`RECIPES`), each with explicit beats of 6 to 8 seconds (shot, what is said, on-screen
  text, where its first frame comes from) and an end card. Seven run today as one take through the talking-video
  path (`needs` is ""): voiceover over product shots, hands-only demo, POV unboxing, talk to camera, car talk,
  seasonal gift moment, founder story (narrated; no AI person plays the founder). Eight need the keyframe-first
  pipeline that renders beat by beat and stitches (`needs: "keyframe_video"`): talking head with cutaways,
  problem-solution, over the product page, show your app, street interview (labelled "AI presenter" throughout),
  three reasons, offer with no countdown, how-to in three steps. Until that pipeline is in `presets.PIPELINES` they
  are hidden from members (`presets.offered`, `recipes_for`) and refused by Create. The stitcher is not built here.
- House rules every recipe is written to: no invented reviews (the review card only takes a review the member pastes
  and attests to), no countdowns or made-up urgency, no before/after health or body claims, no real people or
  lookalikes, and an AI presenter never speaks as a customer.
- `presets.example_url(id)` / `example_video_url(id)` give `static/presets/<id>.webp` / `.mp4` once imported, else
  None. Create and Studio list the new statics; the video recipes are offered through `presets.recipes_for()` and
  MCP (the pages can't send a video preset yet).
- The vendor's own presets: its API can run some of them by id (`POST marketing-studio/image` with
  `enhance_prompt=true` and a `preset_id`, 10% dearer), but the ids are per account, change, and must be read from
  its catalog at run time (`hf.vendor_presets()`, a free call, shown on the admin library page). None is referenced
  by a record yet (`Preset.vendor_preset` is the slot).

**Presenters** (`presenters.py`). 20 fictional, AI-generated presenters: the eight Campaignify always had (same ids,
looks, settings and voices, so old jobs resolve to the same words) and twelve new ones, varied in age band and
gender, each with 3 or 4 situations out of kitchen, car, outdoor, desk, sofa, bathroom-mirror, gym and shop-floor
(a wardrobe per situation) and one voice. `presenters.roster()` / `get(id)` are what the front end reads: `id, name,
age_band, gender, look, voice {id, label}, portrait, voice_sample, situations [{id, label, still}]`, with a url only
when the file exists under `static/presenters/<id>/` (`portrait.webp`, `<situation>.webp`, `voice.mp3`).
`catalog.PRESENTERS` stays the eight (old Create, the planner); `catalog.PRESENTER_BY_ID` knows all twenty.

**Switches** (all optional, in `deploy.yml`'s sync list):

| Switch | Default | What it does |
|---|---|---|
| `PRESENTER_STILLS` | off | A presenter video whose presenter + situation has an approved still passes it to the video model as the first reference image and says to keep that person. Off: exactly what was sent before. No extra vendor cost. |
| `PRESENTER_VOICE_REF` | off | A presenter with a `voice.mp3` gets it sent as `audio_urls[0]` with "speak in that voice" (not when the member picked another voice). Needs the paid test below first. |
| `CLAIMS_CHECK` | off | Unchanged, but free to members now. |
| `CLAIMS_CHECK_DAILY_MAX` / `CLAIMS_CHECK_WS_DAILY_MAX` | 3000 / 150 | Checks a day for the site / one workspace; past either the check is "skipped" (about $12 a day at most). |
| `RS_LIBRARY_STILL_MODEL` | `marketing-studio/image/sunburst` | The library's own image model (internal, `catalog.INTERNAL_TOOLS`; never in `TOOLS`). |
| `RS_LIBRARY_STILL_USD` | our table | One still's vendor cost, once it is known (default table: $0.20 high/1k, $0.055 medium/1k, $0.33 high/2k; +$0.015 per reference image). |

Voice reference, what is known: the vendor's Seedance 2.5 reference-to-video takes `audio_urls` (its API reference
lists the field with no limits or meaning; other hosts of the model say MP3 or WAV, a few seconds each, alongside at
least one image). Whether it holds a voice's timbre for generated speech is documented nowhere. The paid test before
switching `PRESENTER_VOICE_REF` on: one presenter, their still and a 6-second voice sample, two 8-second videos with
different scripts (about $7.40), plus the same two without the sample; check that the voice matches the sample in
both, that it differs from the control, and that the sample's own words are never spoken or played.

**Library assets** (`library.py`, `/admin/library`, admin-only MCP tools). A site admin makes presenter stills, voice
samples, the demo brand's packshots and preset examples as ordinary jobs in their own workspace, price first:
`quote_library_assets(kind, ids, options)` returns total credits and our estimate of the vendor's bill in USD
(the vendor's free estimate when it answers, else our table); `generate_library_assets(..., confirm_total_credits)`
starts nothing unless that total matches; `list_library_assets(kind)` lists what was made with the tags and a download
link per file. The three tools are hidden from non-admins in `tools/list` and refused server-side; the page is 403 for
them. Kinds: `demo_products`, `presenter_stills` (`"maya"`, `"maya:kitchen"`; a presenter's first situation is text to
image and the others are made from it as the same person: they wait for it, and are refunded if it fails),
`presenter_voices`, `preset_examples`, `preset_videos`. Every job carries `params.library = {kind, presenter,
situation, preset, demo}`. Options that make a run cheaper, on the page and in the tools (the quote follows them, in
credits and vendor dollars): `video_seconds` for `preset_videos` (default: the preset's own length; kept to 4 to 30
whole seconds for a presenter or voiceover preset, which is what the vendor's model takes, and to 5 or 10 for a
product-video preset; a spoken example then says a shorter line that fits), `video_resolution` (`480p`, `720p` the
default, `1080p`; 480p is $0.2056 a second at the vendor and for library jobs only, like the free preview: members
are never offered it) and `voice_seconds` for `presenter_voices` (4 to 8, default 6). Talk to camera at 4 seconds and
480p is 165 credits ($0.82) instead of 740 ($3.70). A presenter's still or voice sample carries no product (never the
workspace's active one, `params.no_product`); a preset's example carries its demo product, and so does a packshot once
that product exists. `list_library_assets` and the page give every file its `output_id` (what
`get_asset_download_url` takes), its export `filename` and `in_export` (the one the zip takes for its tag), and the
page downloads each file under that name (`/admin/library/file/<output id>`), so files can be fetched one by one. The
demo brand is fictional (`library.DEMO`: Dapplewood mug, candle, moisturizer, coffee,
tote, greens, planner app); its products are set up in the admin's workspace from their packshots on first use (the
workspace needs room for one more brand). Approve or reject on the batch page, download the export zip from the page
(one file per tag, named `presenter__<id>__<situation>.png`, `preset__<id>.png|.mp4`, `voice__<id>.mp4`), then:

    python tools/import_library_assets.py <folder>     # WebP of at most 200 KB (9:16 or 4:5), mp4, voice.mp3 under static/

Vendor cost per preset at the default settings (720p, one version) and what a member pays; none costs us more than
its price (the markup is 2x):

| Presets | Vendor cost | Member price |
|---|---|---|
| Every image preset (42: templates, scene statics, text-layer statics, the m15 statics) | $0.1071 | 22 credits |
| Slow turn, The swap, Label read (5 s) | $2.31 | 463 |
| How to make it (10 s) | $4.62 | 925 |
| Copy a trending video (per 5 s of reference) | $0.50 | 100 |
| Talk to camera, Car talk, Seasonal gift moment (8 s); Offer, no countdown (8 s, later) | $3.70 | 740 |
| Voiceover over product shots, Hands-only demo, POV unboxing (15 s); Over the product page, Show your app, Street interview (15 s, later) | $6.93 | 1,387 |
| Founder story (20 s); Talking head with cutaways, Problem-solution, Three reasons, How-to in three steps (20 s, later) | $9.24 | 1,849 |

What we absorb with no price at all: the claims check ($0.004 a script), and, as before, an IMAGE_QA check and silent
retry and a free fix. The stitched recipes will add one or two keyframes each (about $0.11 to $0.20) inside the same
price. Library runs, at our table: all 70 presenter stills 2,950 credits (about $14.75), the 7 demo packshots 280
(about $1.40), all 42 image examples 924 ($4.50), six video examples 6,381 ($31.89), 20 voice samples 4,940 ($24.67).
What the first paid runs changed (70 stills, 7 packshots, 42 examples; `tests/test_m15_library_fixes.py`): the
text-free prompt and the always-on check above; a "same person, somewhere new" still the vendor's content filter
refuses is tried once more with a softer prompt (`presenters.still_prompt(soft=True)`: the person isn't described
again, the clothes lose their adjectives, the place is one phrase) before it is given up, and the vendor's own reason
is kept on the job (`params.vendor_reason`) and shown on `/admin/library` and in `list_library_assets`, never to
members; the bathroom-mirror situation has a modest wardrobe (crew-neck tee, closed robe or zipped hoodie) and says
so in its stills; and every example's demo product and words come from one brief (`library._EXAMPLES`), so an
example can't carry another product's copy (`then-now` is the mug throughout).
Tests: `tests/test_m15_library.py`, `tests/test_m15_library_options.py`, `tests/test_m15_library_fixes.py`.

## Keyframe-first video (`keyframe_video.py`, `stitch.py`, `videotext.py`; `KEYFRAME_VIDEO`, default off)

A talking video made from a recipe used to be one long unbroken take. With `KEYFRAME_VIDEO=1` it is built from one
approved still, in beats, and stitched. Off, nothing about any video changes: the jobs Create queues and what the
worker sends the vendor are compared with a snapshot taken before the pipeline existed
(`tests/fixtures/keyframe_off_snapshot.json`, `tools/keyframe_off_snapshot.py`). On, `"keyframe_video"` is in
`presets.PIPELINES`, the eight recipes that needed it are offered, and every video made from a recipe takes this
path (a plain presenter video or voiceover with no recipe stays one take). A job remembers how it is made
(`params.pipeline`), so one queued while the switch was on finishes this way after it is switched off.

1. **Keyframe.** One still from references, by the library's image model (`marketing-studio/image/sunburst`,
   high / 1k): the presenter's approved situation still (`static/presenters/<id>/<situation>.webp`; without one the
   person is drawn from the roster's description) and up to two product photos, the product where the recipe puts
   it (on the situation's surface by default, in hand with `placement: holding`, or on a shelf). A voiceover's
   keyframe is the product in the scene of its first shot. It is kept on the job (`video_parts`, `/admin/library/keyframe/<job>`).
2. **Keyframe check.** The image check in keyframe mode (product there, label right, no extra text, hands and face
   fine, one clear face), whatever `IMAGE_QA` says. One silent retry; a second failure ends the job before any video
   is paid for, with "We couldn't get this one right — credits returned." and a full refund.
3. **Beats.** Every spoken beat is one Seedance reference-to-video request with the keyframe as its first image
   reference (and the presenter's `voice.mp3` as `audio_urls[0]` behind `PRESENTER_VOICE_REF`), its own line of the
   script and the realism and fidelity lines. The script is cut into one line a beat on sentence ends
   (`split_script`); the beats are 4 to 8 whole seconds and add up to the length sold (`fit_beats`: another length
   than the recipe's own scales them, joins ones under 4 s and cuts ones over 8 s in two). All beats go out in one
   pass, each with its own idempotency key. A beat that fails is made again once; after that the whole job is
   refunded. Never a partial video. A beat with a `place` is "the same person, a new scene" from the same keyframe.
4. **Cutaways.** In a presenter recipe a beat that shows the product or hands is still spoken by the presenter, and
   silent b-roll is laid over part of its picture while their voice carries on: an image-to-video clip from the real
   product photo while the seconds allow it, else a slow push-in on the packshot itself (free; the label can't
   change). Recipes that show the member's screens (over the product page, show your app) put their real
   screenshots full frame with the presenter small in the bottom-left corner; nothing of a page is generated.
   Without a screenshot those recipes are refused for that product with the reason.
5. **Stitch** (ffmpeg): every beat at one size and rate with its sound levelled (`loudnorm`), hard cuts (a recipe
   can ask for a crossfade of 4 to 6 frames: founder story), captions, step tags and the "AI presenter" label as
   real type inside the safe area, then a 2-second end card from the real packshot and the brand kit (the product's
   name, its saved price or offer as a badge where the recipe shows one, a button), with a quiet tail. The server
   needs ffmpeg and ffprobe: without them a job is refused when it starts, before anything is bought (`/admin/library`
   says whether they are there).
6. **Frame check** (`VIDEO_QA`, default off): three frames of each beat against the product photo and the keyframe's
   face; a beat with two bad frames is made again once, then the job is refunded. `VIDEO_QA_DAILY_MAX` (default
   3000 checks a day, about $15) is counted apart from `IMAGE_QA_DAILY_MAX`.

**Free fix.** With `FREE_FIX` on, a keyframe-first video's fix re-renders only the beat the member points at, from the
kept keyframe, and stitches again with the other beats as they were: it costs us one beat ($2.77 to $3.70), not the
video. `FREE_FIX_VIDEO_MONTHLY_CAP` (default 2 a workspace a month) bounds it. Beats are kept for 14 days for this
while `FREE_FIX` is on, else removed at delivery; the keyframe always stays. The cards that exist only offer the
image fix; a page that asks "which part?" reads `free_fix.video_offer()` and posts `beat` (1-based) with `note` to
`/app/jobs/<id>/fix`.

**What the front end passes.** `screens`: photo ids of the member's screenshots (uploaded with the product's photos)
for the two screen recipes (an app's own photos are used when none is passed); `captions`: true or false to
override the recipe's choice; `cta`: the end card's button text (else "Shop now", or "Try it free" for an app);
`placement: holding`. MCP: `screen_photo_ids`, `captions`.

**Costs.** Member prices are unchanged: the per-duration talking-video price. Vendor cost is the beats' seconds plus
generated cutaway seconds at $0.4622 (720p), the keyframe ($0.20 + $0.015 a reference), the keyframe check and,
counted here with `VIDEO_QA` on, the frame checks ($0.004 to $0.005 each). `keyframe_video.cost_table()` gives this
table and a test compares it with the lines below:

| Recipe | Length | Beats | Cutaway | Vendor cost | Member price (credits) | Margin | If the keyframe and a beat are made twice |
|---|---|---|---|---|---|---|---|
| Voiceover over product shots | 15 s | 2 | none | $7.18 | 1,387 | 48% | $11.10, 20% **under 30%** |
| Hands-only demo | 15 s | 2 | none | $7.18 | 1,387 | 48% | $11.10, 20% **under 30%** |
| POV unboxing | 15 s | 2 | none | $7.18 | 1,387 | 48% | $11.10, 20% **under 30%** |
| Talk to camera | 8 s | 1 | none | $3.95 | 740 | 47% | $7.88, -6% **negative** |
| Car talk | 8 s | 1 | none | $3.95 | 740 | 47% | $7.88, -6% **negative** |
| Seasonal gift moment | 8 s | 1 | none | $3.93 | 740 | 47% | $7.85, -6% **negative** |
| Founder story, narrated | 20 s | 3 | none | $9.51 | 1,849 | 49% | $12.96, 29.9% **under 30%** |
| Talking head with cutaways | 20 s | 3 | 5 s generated | $11.84 | 1,849 | 36% | $15.30, 17% **under 30%** |
| Problem, then solution | 20 s | 3 | 5 s generated + 1 packshot | $11.84 | 1,849 | 36% | $15.30, 17% **under 30%** |
| Over the product page | 15 s | 2 | none | $7.20 | 1,387 | 48% | $11.13, 20% **under 30%** |
| Show your app | 15 s | 2 | none | $7.20 | 1,387 | 48% | $11.13, 20% **under 30%** |
| Street interview | 15 s | 2 | none | $7.20 | 1,387 | 48% | $11.13, 20% **under 30%** |
| Three reasons | 20 s | 3 | none | $9.51 | 1,849 | 49% | $12.96, 29.9% **under 30%** |
| Offer, no countdown | 8 s | 1 | none | $3.93 | 740 | 47% | $7.85, -6% **negative** |
| How-to in three steps | 20 s | 3 | none | $9.51 | 1,849 | 49% | $12.96, 29.9% **under 30%** |

Flags and caps. As planned no recipe is negative or under 30% at 720p. Generated cutaway seconds are capped so the
plan keeps 30% (about 0.4 x the seconds sold: one 5-second cutaway in a 20-second video, none in a shorter one;
further cutaways are the packshot). The worst case is flagged: when the frame check sends a beat back and the
keyframe was also made twice, an 8-second recipe costs about 6% more than it sold for and the others fall to 17 to
30%. One job's video seconds, second tries included, never pass 1.05 x the markup x the seconds sold (2.1 x at 720p,
so one beat can always be made again; 1.47 x at 1080p): past that a failing beat is refunded instead. At 1080p every
recipe is at 27% as planned (the 1080p markup is 1.4, and one take is at 29% today): flagged; its cutaways are never
generated, and only a 20-second video has the seconds to make a beat again. A beat the vendor fails or refuses is
not billed, so only a failed frame check costs us a second beat.

**The paid test before switching it on** (`/admin/library`, kind `keyframe_tests`, or `quote_library_assets` /
`generate_library_assets`): one recipe, one presenter, 480p, four-second beats, quoted first. It runs with
`KEYFRAME_VIDEO` off. Needs the demo mug's packshot (and uses Maya's kitchen still when it exists). Run
`kind: keyframe_tests, ids: ["talking-head-cutaways"], options: {"presenter": "maya"}`: three 4-second beats with
the packshot laid over the middle one, the keyframe and its check: 541 credits ($2.70 at the vendor). It shows the
keyframe, lip-sync on three lines, the cut to the product and back under one voice, the stitch and the end card.
With `"video_seconds": 6` (one beat's length here, 4 to 8) the cutaway is a generated clip as well: 993 credits
($4.96). Smaller: `talk-to-camera` (one beat, 212 credits, $1.06) or `street-interview` (two beats, 373 credits,
$1.86). Then look at `/admin/library?show=keyframe_tests` (the video and "Its keyframe").

Not there yet: a narrator's voice is only described to each beat, so a voiceover recipe's beats may not sound like
one person (presenters have `PRESENTER_VOICE_REF`); the recipes' free-form on-screen text slots ("the hook in five
words") are covered by captions, not written. Deleting a workspace deletes each part's request at the vendor too
(`wsdelete.py`). Tests: `tests/test_keyframe_video.py` (the stitching
checks need ffmpeg; `tests.yml` installs it).

## Feature and industry pages, and the free hook generator (Nov 10 2026)

Nine more answer-first pages in `public_pages.SOLUTIONS`, under the same rules as the rest (600+ words, three steps,
3 to 5 FAQs, every number from `facts()`, nothing the product does not do): image ads, product videos, AI presenters,
Facebook ads, TikTok ads, SaaS, mobile apps, lead generation and real estate. The footer groups every page under
Features, Industries, Compare and Guides (`public_site.footer_groups`, from `FEATURE_PAGES` and `INDUSTRY_PAGES`).

`/tools/ai-hook-generator` (`hooktool.py`, `web/tool_hooks.html`) is a free public tool: what you sell, who it is for,
eight hooks back. It is the one place the public site spends on the text model with no member behind it, so it is
capped three ways: `HOOK_TOOL=0` turns it off, each visitor gets 5 runs a day, and the whole site gets
`HOOK_TOOL_DAILY_MAX` runs a day (default 300). Briefs and hooks pass the same claim rules as the studio.
`tests/test_hook_tool.py` covers it with the model stubbed.

## The neutral look, Studio and onboarding (`ui_v2.py`, `studio_v2.py`, `onboarding.py`; `UI_V2`, default off)

**The home page is live in it (`HOME_V2`, Nov 10 2026).** The public home page renders in the new look for everyone
whatever `UI_V2` says: its route calls `ui_v2.force()` while `HOME_V2` is on (deploy.yml sends `1` unless the repo
variable `HOME_V2` is `0`; with no value, as in tests, it is off). `/?ui=v1` shows the old page. The hero
(`web/v2/landing.html`, `static/ui_v2_home.css`) gathers 18 tiles (our own preset examples, presenter stills and five
drawn cards) into one point, then shows the promise and the product-link form on a black stage with small stars that
drift with the mouse and an occasional shooting star; under it two rows of features drift past (what the studio makes,
and every shipped feature in `plans.FEATURES`), then "The link goes in once. This is what comes back out.": five
rows (image ads, presenter videos, copy and a plan, competitor research, lead pages and follow-up) beside a list that
marks the row in view, each naming the plan its tools come with. Counts and prices come from the catalog and
`rates()`. Every other page is unchanged.

One switch, off by default. Off, nothing changes. `UI_V2=1` (a repo variable, in `deploy.yml`'s sync list) turns it
on for everyone. Before that, a site admin can try it alone on the live site: open any page with `?ui=v2` (a signed
cookie keeps it on in that browser for 180 days, logged out too, so the landing page and sign-up can be looked at;
`?ui=v1` removes it), or use the switch under Settings > Team (kept on the account, `users.ui_v2`). A member's
`?ui=v2` and a cookie typed by hand do nothing.

- **Look** (`static/ui_v2.css`, loaded after `style.css` only while on; `base.html` adds `data-ui="v2"`): the same
  tokens `style.css` already uses, redefined, so every page picks it up. Near-black or near-white surfaces, four
  greys, 1px hairlines, 8 to 12px corners, Inter only, no gradients or glows. Buttons are ink on paper; the lime of
  the wordmark stays for small marks (what is selected, "today" on the card timeline, focus in dark). Light for the
  site, log in, sign-up and onboarding; dark by default in the app, light when the browser prefers it, and the
  member's own toggle wins everywhere.
- **Studio** (`/app`, `web/v2/studio.html`, `static/studio_v2.js`): the canvas of the latest ads and one composer.
  Modes: presenter video, voiceover, product video, image ad; "More" is a searchable gallery of every ad type and
  preset (text-layer presets ask for their extra words in the composer). Product chip with "Add product from URL"
  (the first run's free importer), presenter picker (the 8 presenters), a versions stepper for images, settings for
  length, shape and quality. **Send only asks for the price** (`/app/studio/quote`) and opens the confirm box; its
  button posts the same composer contents plus the shown total to `/app/studio/generate`. Both hand Create's own
  config to `create_routes.quote_for` / `generate_for`, so the rules, the claims check, the email-confirm step and
  the "total changed" refusal (409) are Create's. Results are the usual cards (Download, approve, More like this,
  the free fix when `FREE_FIX` is on). Create stays at `/app/create` as "Advanced"; `/app?classic=1` is the old Home.
- **Ask** (the composer's first tab, `studio_v2.ask`, `/app/studio/ask`): the member types what they want in a
  sentence ("launch the candle for the holidays, a few image ads and one short video"). The fast text model, given
  the product's facts, its approved claims and the presenter roster, proposes up to `ASK_MAX_PARTS` (5) things
  Studio already makes, with the words for each. Every part is priced by Create's own quote and the confirm box
  opens on the set: each part can be unticked and its words edited, and the box re-prices in place
  (`/app/studio/quote` with `plan`). The button posts the ticked parts plus the shown total to
  `/app/studio/generate`, which starts them as one batch named after the ask. What comes back from the browser is
  read through `ask_item`, so a part can only ever set its kind, words, image count, presenter and place. Words
  that trip a claim rule are never proposed (the part is named as left out), a part Create can't price is left out
  with Create's reason, and an ask that itself asks for a blocked claim never reaches the model. Asking costs the
  member nothing; it is capped at `ASK_DAILY` (40) per workspace per day (the `meta` table) and hidden when no text
  model is configured. A returning member's Studio opens on Ask; a first run still opens on its two free image
  ads. `tests/test_studio_ask.py` covers it with the model stubbed.
- **Brand** (`/app/studio/brand`): one input, "Drop your website" (the same free importer), then the brand's Look
  (colours, font, logo), saved through the existing brand form.
- **Onboarding** (`/app/welcome`, `/app/welcome/profile`): what do you want to make (stored as codes in
  `users.onboarding_json`), then name, company and website. The first thing they picked that their credits cover is
  the composer's first mode; the website is already in the import boxes. Then the card step when one is due, with a
  timeline built from `card_on_file.terms()`: $0 today, no trial, no charge date, and the real cheapest plan and
  pack as the only later charges. Prices, trial terms and Stripe objects are untouched.

Not there yet: presenter voice previews (there are no audio samples in the repo; making them costs generation), and
presenter portraits show only where the stills exist (`presenter_stills.py`).

## MCP server for Claude (`mcpserver.py`, `mcp_tools.py`, `/docs/mcp`)

`MCP_SERVER=on` (default off) opens a remote MCP server at `/mcp` (Streamable HTTP, one JSON answer per POST,
no sessions). Auth: personal API keys made on `/app/mcp` (Settings > API and Claude; SHA-256 at rest, shown
once, read or read+write, revocable) or OAuth 2.1 (discovery under `/.well-known/`, dynamic client
registration, PKCE S256, rotating refresh tokens) so a Claude.ai custom connector can sign in. Tools call the
app's own routes (`ToolContext.call` runs the route in a fresh request context as the token's user), so plan
features, credits, compliance and the email-confirmation rule are the web app's. Images and videos: `quote_ad_batch`
first; `generate_*` needs `confirm_total_credits` equal to that quote (Create's own 409 check). Tables:
`mcp_tokens`, `mcp_clients`, `mcp_grants`, `mcp_codes`. `mcpserver.py` is the same file in Crowdcut.
Tests: `tests/test_mcp.py`.

## Tests

    python tests/test_app.py        # generation API and Stripe faked; also runs test_products, test_outreach, test_public_site, test_pixels, test_site_events, test_google_auth, ...

Golden-path eval (separate, slower, about 40 seconds; needs Playwright for Python with Chromium):

    python tests/golden_path.py                  # both paths; --path product|service, --json/--markdown out, --shots dir, --headed

It runs the north star's two paths in a real browser against a local server with every provider faked (as
test_app.py fakes them): the product path (sign up, confirm the email, paste the product page, 2 free ads, pick a
plan, plan the campaign, creative batch, copy, landing page, export) and the service path (website, leads plan,
Google Search copy, quote form, page with the form, follow-up flow, a visitor's lead in Contacts). Per step it records
wall clock and user actions (clicks, fields typed, addresses typed), asserts that every button that spends credits
shows its price before it's pressed and never charges more, and that no step leaves the app except the known handoffs
(Stripe, the ad platforms' payment pages). `KNOWN_GAPS` holds failures the roadmap already tracks (today UX-13: the
service path can't make its first ads without a product photo); they print as "known gap" and don't fail the run, so
the output is the baseline. Connect and launch print "not built" until M2/M3. Exit code 1 on any other failure.

## Deploy

Push to main. The workflow runs the tests, then sends the commit and the
settings from GitHub secrets (`HIGGSFIELD_API_KEY`, `STRIPE_SECRET_KEY`,
`STRIPE_WEBHOOK_SECRET`, `GOOGLE_OAUTH_CLIENT_SECRET`; optional variables `RS_MARKUP`, `RS_STARTER_CREDITS`, `GADS_SIGNUP_LABEL`, `GADS_PURCHASE_LABEL`, `GOOGLE_OAUTH_CLIENT_ID`) to the deploy hook on the Crowdcut server, authenticated with a
GitHub OIDC token (no SSH key). The hook writes `/opt/rs-studios`, keeps
`data/` and `settings.txt`, and runs `docker compose -p rs-studios up -d --build`.
Crowdcut's Caddy serves campaignify.ai (studio.rectorandstull.com redirects there) and proxies to
`rs-studios-app:8080`. Data (SQLite + media) lives in `/opt/rs-studios/data`.

Two containers run from the one image (roadmap M7.2): `rs-studios-app` (gunicorn, requests only) and
`rs-studios-worker` (`python worker_main.py`: generation jobs, flows and sends, conversions, alerts, backups,
launches, insights, the plan sync, nudges, and the planning and research runs the web queues in `task_queue`).
One worker holds the `worker` lease at a time. Its logs: `docker logs rs-studios-worker`. The web's `/health`
answers ok only once the worker of the same image (`BUILD_ID`) beats, so the deploy hook's check after the switch
rolls back a release whose worker doesn't come up. `RS_WORKER_PROCESS=off` in settings.txt puts the loop back in
the app container as a thread (the worker container then idles).

## Health and alerts (`alerts.py`, `opslog.py`)

`/health` reads and writes the database and checks the worker loop's heartbeat (last beat under
`RS_HEALTH_WORKER_MAX_AGE`, 180 s); either failing answers 503. The loop beats each pass, between jobs, and every
10 s while one long vendor call runs (up to `RS_HEALTH_BUSY_MAX_S`, 900 s), so a slow vendor doesn't fail it. A
process goes by its own worker thread's beats, and reports "starting" until its first one, so right after a deploy
switch only a dead worker thread or the database can fail it (the deploy hook doesn't roll back for a slow vendor). Disk (under 10 GB is "low") and the vendor's last
probe (refreshed every 5 minutes by the worker, never fetched by `/health` itself unless `?deep=1`) are reported but
don't fail it. Operator alerts go by email through Resend to `OPS_ALERT_EMAIL`, at most once per alert per hour:
worker down 5 minutes, a job pending or running over 20 minutes, over 10 webhook signature failures in an hour,
over 20% of jobs failing in 15 minutes (or the vendor key rejected / out of credit), disk under 10 GB, the last
backup over 26 hours old (meta `backup_last_ok_at`, quiet until the backup job writes it; `alerts.backup_ok`), and a
refund or dispute on one of our Stripe payments (watched from the webhook's request and response, so the handler
is unchanged; billing code may also call `alerts.payment_reversed`). Thresholds are settings (`settings.example.txt`).
Logs from the `campaignify` logger are JSON lines with `request_id` and `ws`, plus one line per request with the
route pattern (never the real path or query), status and time; every response carries `X-Request-Id`.
Tests: `tests/test_alerts.py`.

## Backups and the restore drill (`backup.py`, `tools/restore.sh`)

Every night (after `BACKUP_HOUR_UTC`, default 8, which is 2 or 3 am Central) the worker loop starts a backup in its
own thread:

1. A consistent snapshot of `rs.db` with SQLite's online backup API (what `sqlite3 rs.db ".backup FILE"` does),
   gzipped to `data/backups/rs-<utc>.db.gz`. The newest 3 stay on the server (`BACKUP_KEEP_LOCAL`).
2. The snapshot is uploaded to Cloudflare R2 through rclone's crypt remote, so file contents and names are
   encrypted on the server before they leave it. Snapshots older than 30 days are deleted from the bucket
   (`BACKUP_KEEP_DAYS`; the newest is never deleted).
3. `data/uploads`, `data/media`, `data/presenters` and the session key are synced. A file deleted on the server is
   moved to `media-trash/<date>/` in the bucket and kept 30 days.
4. A good run calls `alerts.backup_ok`; the `backup_late` alert fires when the last good one is over 26 hours old,
   and `backup_failed` fires when a run fails. Admin > System > Backups shows the last good backup's time and size,
   the snapshot count and the last error.

Settings (GitHub secrets, synced by deploy.yml): `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET`, `BACKUP_CRYPT_PASSWORD`, `BACKUP_CRYPT_SALT`. Without all six nothing changes: nothing is uploaded, the
app logs one warning and the System card says backups are off. Keep a copy of the two crypt values outside GitHub
(a password manager): without them the backups can't be decrypted. Layout in the bucket:
`<R2_BUCKET>/campaignify/{db,media,media-trash,keys}` (names encrypted). Kill switch: `BACKUPS=off`; database only:
`BACKUP_MEDIA=off`. By hand, in the container: `python backup.py` (or `--status`).

**Restore drill (quarterly: first week of January, April, July and October).** On the server or a dev box with
rclone and the app's requirements (the app image has both):

    docker exec rs-studios-app tools/restore.sh --media /tmp/restore-drill
    # or on a dev box: export the six settings, then
    tools/restore.sh --media /path/to/scratch

It downloads the newest snapshot (or `--snapshot NAME`), unpacks it to `<scratch>/data/rs.db`, restores the session
key and, with `--media`, the media folders, then runs `tools/restore_smoke.py`: `PRAGMA integrity_check`, the core
tables and their row counts, the app booting on the restored folder with no keys and no worker and answering
`/health` and `/login` with 200, and how many of the newest ad files are present. It never touches the live data
folder and refuses a scratch folder that isn't empty. To run the app on the restored copy:
`RS_DATA=<scratch>/data RS_WORKER=0 python app.py` (or mount it as `data/` in a scratch container). Write the date,
the snapshot name, how long it took and the smoke output in the ops log, then delete the scratch folder (it holds
customer data). A real restore is the same steps, then stop the app, move `data/` aside and put the restored folder
in its place.

Tests: `tests/test_backup.py` (rclone replaced by `tests/fake_rclone.sh`, no network).

## Storage quotas, body limits and the disk guard (`storage.py`)

Each workspace keeps up to its plan's storage on our disk: Free 200 MB, Starter 2 GB, Growth 10 GB, Scale 50 GB
(its product photos, Creator Pool files and finished ads; ads it deleted don't count). Uploads are checked before
anything is saved (against the whole request's size, so a 20-photo upload that doesn't fit keeps none) and again
with the real size; over quota answers 507 "Your storage is full: X of Y used on the Z plan" with what to do. A
finished ad that would go over keeps only the vendor's link instead of a local copy. `RS_STORAGE_QUOTAS=0` turns
quotas off. Body limits per route, from Content-Length before any parsing and on the stream: upload routes 155 MB,
the contacts CSV 5 MB, webhooks 256 KB, everything else 1 MB (413). Under `RS_DISK_MIN_GB` (5) GB free, uploads,
saved ads and exports are refused with 507 and the operator is alerted (`alerts.py`). Once a day (about 09:00 UTC)
the worker removes temp leftovers (`*.part.mp4`, `*.wm.mp4` over an hour old) and platform exports not used for
`RS_EXPORT_KEEP_DAYS` (7) days (they're made again on the next download); with `RS_STORAGE_PURGE_DELETED=1` (off
by default) it also removes the files of ads deleted over 30 days ago. Tests: `tests/test_storage.py`.

## Credit holds for AI runs (`credits.py`)

Every AI run takes the most it can cost out of the balance before the provider is called: the planner its cap,
competitor research its quote, copy, optimizer, landing page copy, flow drafts, SEO suggestions, product import and
Create's angles, scripts and shorten their quotes, a paid setup guide reply its estimate. Too little left: refused
(402 for the JSON endpoints, a message on the forms) and nothing is called. When the run ends it keeps what it used
(never more than the hold) and gives the rest back, pack credits first. Two runs at once can't spend the same
credits. The ledger shows each step: "held up to N", "used X, Y back", "nothing used, N back" (`ledger.hold_id`
ties them to the `credit_holds` row). A run whose thread dies gives its hold back when it's marked failed; any hold
older than 30 minutes is given back by the worker. Generation jobs already take their exact price when queued and
are refunded on failure. `CREDIT_HOLDS=off` goes back to charging after the run. Tests: `tests/test_credit_holds.py`.

## Pieces

- `app.py` routes and pages (`web/`: `landing.html`, `privacy.html`, `terms.html` for the public site), `static/style.css`
- `accounts.py` self-serve account creation (the sign-up form and Google share it); `google_auth.py` Continue with Google (OIDC with `requests`, no SDK)
- `billing.py` Stripe Checkout and webhook signatures (no SDK)
- `catalog.py` tools, prices, templates (the playbook's angles)
- `presets.py` the preset library; `presenters.py` the presenter roster; `library.py` the admin's library assets
- `compliance.py` brief checks (weight-loss, health, discount, income, recruiting), run on every brief, the first-ad flow and brand-kit saves
  - **Discounts are the member's to state, never ours to invent** (Nov 11 2026). A discount, coupon, promo code or
    free shipping passes when the member stated it, on the offer's own terms ("20% off" passes when "20% off" was
    stated; "30% off" does not). Stated means: in the strings they sent with the current request (their own
    headline, their ask, their brief), saved as a product's offer or price, passed by the caller as `stated=` (a
    saved email or text being sent; the page a product import read), or declared for a background run with
    `compliance.state()` (the planner). Anything else, which in practice is a model making an offer up, is refused
    with `compliance.DISCOUNT_MSG`. Every other rule ignores `stated`. `tests/test_stated_offers.py`.
- `worker.py` submits, polls, saves results, refunds failures
- `hf.py` the generation API client (the only file that knows the vendor)

Some prompts and templates (email and landing-page guidance, the planner's offer and lead-magnet checks, starter
flows, the setup guide's activation steps) are condensed from MIT-licensed marketing playbooks; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Brand

Name Campaignify, tagline "Create your own creatives". Logo: the lowercase wordmark with a lime spark over the i and a play cut into the g, traced to SVG (`web/_wordmark.svg` inherits the text colour; `static/img/campaignify-wordmark-light.svg` is white for dark backgrounds, `-dark.svg` is black for light ones). App icon and favicon: the lime spark on a black tile (`static/img/campaignify-mark.svg`, `campaignify-icon-*.png`). Accent lime #BDFA25 (#3F6B00 for green text on light backgrounds). Dark theme by default, light theme from the system setting or the Theme button (remembered per browser). All colours are `:root` variables in `static/style.css`. Env vars, the repo, the container and `/opt/rs-studios` keep their old names on purpose: renaming them would break the running deploy.
