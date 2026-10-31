# Campaignify

Campaignify (campaignify.ai), Rector & Stull's ad studio: templates that already sell, a brand kit (product
name, approved claims, product photos), and generation of image ads, photo-to-video
clips and motion-copy videos. Members pay in RS credits (1 credit = $0.01; price =
our cost x `RS_MARKUP`). Public and self-serve: the landing page at `/` (logged
out), open signup with free starter credits (`RS_STARTER_CREDITS`, default two
image ads), a guided first ad (`/app/start`), and credit packs bought through
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

Owners (Koby, Travis) sign up with their one-time setup links (`owners.py` holds only the code hashes) and become admins of the Rector & Stull workspace. Anyone else can sign up and gets their own workspace with the starter credits (signups are rate-limited per IP and have a honeypot). A new free account confirms its email before the starter credits or the free talking-video preview work: signup emails a signed, single-use link (`/verify`, 48 hours; `verify.py`), a banner offers Resend (once a minute, three an hour), and paid plans, pack buyers, admins and accounts made before this check are never blocked (`RS_EMAIL_VERIFY=0` switches it off; free previews are capped at `RS_FREE_PREVIEWS_PER_DAY`, default 30). Invite links from Admin join an existing workspace. Admin: top up credits, create
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
immediately, downgrades and cancellations at period end). Admins can comp a workspace onto a plan (`plan_override`).
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

## Tests

    python tests/test_app.py        # generation API and Stripe faked; also runs test_products, test_outreach, test_public_site

## Deploy

Push to main. The workflow runs the tests, then sends the commit and the
settings from GitHub secrets (`HIGGSFIELD_API_KEY`, `STRIPE_SECRET_KEY`,
`STRIPE_WEBHOOK_SECRET`; optional variables `RS_MARKUP`, `RS_STARTER_CREDITS`, `GADS_SIGNUP_LABEL`, `GADS_PURCHASE_LABEL`) to the deploy hook on the Crowdcut server, authenticated with a
GitHub OIDC token (no SSH key). The hook writes `/opt/rs-studios`, keeps
`data/` and `settings.txt`, and runs `docker compose -p rs-studios up -d --build`.
Crowdcut's Caddy serves campaignify.ai (studio.rectorandstull.com redirects there) and proxies to
`rs-studios-app:8080`. Data (SQLite + media) lives in `/opt/rs-studios/data`.

## Pieces

- `app.py` routes and pages (`web/`: `landing.html`, `privacy.html`, `terms.html` for the public site), `static/style.css`
- `billing.py` Stripe Checkout and webhook signatures (no SDK)
- `catalog.py` tools, prices, templates (the playbook's angles)
- `compliance.py` brief checks (weight-loss, health, discount, income, recruiting), run on every brief, the first-ad flow and brand-kit saves
- `worker.py` submits, polls, saves results, refunds failures
- `hf.py` the generation API client (the only file that knows the vendor)

Some prompts and templates (email and landing-page guidance, the planner's offer and lead-magnet checks, starter
flows, the setup guide's activation steps) are condensed from MIT-licensed marketing playbooks; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Brand

Name Campaignify, tagline "Create your own creatives". Logo: the lowercase wordmark with a lime spark over the i and a play cut into the g, traced to SVG (`web/_wordmark.svg` inherits the text colour; `static/img/campaignify-wordmark-light.svg` is white for dark backgrounds, `-dark.svg` is black for light ones). App icon and favicon: the lime spark on a black tile (`static/img/campaignify-mark.svg`, `campaignify-icon-*.png`). Accent lime #BDFA25 (#3F6B00 for green text on light backgrounds). Dark theme by default, light theme from the system setting or the Theme button (remembered per browser). All colours are `:root` variables in `static/style.css`. Env vars, the repo, the container and `/opt/rs-studios` keep their old names on purpose: renaming them would break the running deploy.
