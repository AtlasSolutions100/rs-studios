# RS Studios

Rector & Stull's ad studio: templates that already sell, a brand kit (product
name, approved claims, product photos), and generation of image ads, photo-to-video
clips and motion-copy videos. Members pay in RS credits (1 credit = $0.01; price =
our cost x `RS_MARKUP`). Invite-only.

## Run locally

    pip install -r requirements.txt
    RS_DATA=./data HF_KEY=key:secret python app.py      # http://localhost:8080

The first account to sign up becomes the admin. Admin: top up credits, create
invite links (join a workspace or get your own), upload reference videos for
"Copy a video's motion".

## Tests

    python tests/test_app.py        # generation API faked; 41 checks

## Deploy

`docker compose up -d --build` with a `settings.txt` next to it (see
`settings.example.txt`). Caddy serves `$DOMAIN` (default studio.rectorandstull.com)
with automatic HTTPS. Data (SQLite + media) lives in `./data`.

## Pieces

- `app.py` routes and pages (`web/`), `static/style.css`
- `catalog.py` tools, prices, templates (the playbook's angles)
- `compliance.py` brief checks (weight-loss, health, discount, income, recruiting)
- `worker.py` submits, polls, saves results, refunds failures
- `hf.py` the generation API client (the only file that knows the vendor)
