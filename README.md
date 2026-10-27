# RS Studios

Rector & Stull's ad studio: templates that already sell, a brand kit (product
name, approved claims, product photos), and generation of image ads, photo-to-video
clips and motion-copy videos. Members pay in RS credits (1 credit = $0.01; price =
our cost x `RS_MARKUP`). Invite-only.

## Run locally

    pip install -r requirements.txt
    RS_DATA=./data HIGGSFIELD_API_KEY=key:secret python app.py      # http://localhost:8080

Owners (Koby, Travis) sign up with their one-time setup links (`owners.py` holds only the code hashes) and become admins of the Rector & Stull workspace; everyone else needs an invite. Admin: top up credits, create
invite links (join a workspace or get your own), upload reference videos for
"Copy a video's motion".

## Tests

    python tests/test_app.py        # generation API faked; 41 checks

## Deploy

Push to main. The workflow runs the tests, then sends the commit and the
settings from GitHub secrets (`HIGGSFIELD_API_KEY`, optional variable
`RS_MARKUP`) to the deploy hook on the Crowdcut server, authenticated with a
GitHub OIDC token (no SSH key). The hook writes `/opt/rs-studios`, keeps
`data/` and `settings.txt`, and runs `docker compose -p rs-studios up -d --build`.
Crowdcut's Caddy serves studio.rectorandstull.com and proxies to
`rs-studios-app:8080`. Data (SQLite + media) lives in `/opt/rs-studios/data`.

## Pieces

- `app.py` routes and pages (`web/`), `static/style.css`
- `catalog.py` tools, prices, templates (the playbook's angles)
- `compliance.py` brief checks (weight-loss, health, discount, income, recruiting)
- `worker.py` submits, polls, saves results, refunds failures
- `hf.py` the generation API client (the only file that knows the vendor)
