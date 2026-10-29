# Campaignify

Campaignify (campaignify.ai), Rector & Stull's ad studio: templates that already sell, a brand kit (product
name, approved claims, product photos), and generation of image ads, photo-to-video
clips and motion-copy videos. Members pay in RS credits (1 credit = $0.01; price =
our cost x `RS_MARKUP`). Public and self-serve: the landing page at `/` (logged
out), open signup with free starter credits (`RS_STARTER_CREDITS`, default two
image ads), a guided first ad (`/app/start`), and credit packs bought through
Stripe Checkout (`/app/billing`). Built for independent reps: income, earnings
and recruiting claims are blocked on every brief.

## Run locally

    pip install -r requirements.txt
    RS_DATA=./data HIGGSFIELD_API_KEY=key:secret python app.py      # http://localhost:8080

Owners (Koby, Travis) sign up with their one-time setup links (`owners.py` holds only the code hashes) and become admins of the Rector & Stull workspace. Anyone else can sign up and gets their own workspace with the starter credits (signups are rate-limited per IP and have a honeypot; there is no mail sending, so no email verification). Invite links from Admin join an existing workspace. Admin: top up credits, create
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
Our own site serves `/robots.txt`, `/sitemap.xml`, `/llms.txt`, canonical links and Organization JSON-LD.

## Tests

    python tests/test_app.py        # generation API and Stripe faked; 122 checks

## Deploy

Push to main. The workflow runs the tests, then sends the commit and the
settings from GitHub secrets (`HIGGSFIELD_API_KEY`, `STRIPE_SECRET_KEY`,
`STRIPE_WEBHOOK_SECRET`; optional variables `RS_MARKUP`, `RS_STARTER_CREDITS`) to the deploy hook on the Crowdcut server, authenticated with a
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

## Brand

Name Campaignify, tagline "Create your own creatives". Logo: the lowercase wordmark with a lime spark over the i and a play cut into the g, traced to SVG (`web/_wordmark.svg` inherits the text colour; `static/img/campaignify-wordmark-light.svg` is white for dark backgrounds, `-dark.svg` is black for light ones). App icon and favicon: the lime spark on a black tile (`static/img/campaignify-mark.svg`, `campaignify-icon-*.png`). Accent lime #BDFA25 (#3F6B00 for green text on light backgrounds). Dark theme by default, light theme from the system setting or the Theme button (remembered per browser). All colours are `:root` variables in `static/style.css`. Env vars, the repo, the container and `/opt/rs-studios` keep their old names on purpose: renaming them would break the running deploy.
