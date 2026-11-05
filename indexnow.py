"""Ping IndexNow (Bing, Yandex, Seznam, Naver and others share it) with every URL
in the live sitemap, so new and changed public pages are crawled soon after a
deploy. Run by deploy.yml after the health check:

    python indexnow.py            (uses RS_SITE_URL, default https://campaignify.ai)

The key is public by design (public_site.INDEXNOW_KEY, served at /<key>.txt);
nothing secret is sent. A failed ping never fails the deploy.
"""
import json
import re
import sys
import urllib.error
import urllib.request

import public_site


def sitemap_urls(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "campaignify-indexnow/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def ping(urls: list[str]) -> tuple[int, str]:
    body = json.dumps(public_site.indexnow_body(urls)).encode()
    req = urllib.request.Request(public_site.INDEXNOW_ENDPOINT, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": "campaignify-indexnow/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def main() -> int:
    site = public_site.SITE_URL
    key = _get(f"{site}/{public_site.INDEXNOW_KEY}.txt").strip()
    if key != public_site.INDEXNOW_KEY:
        print("IndexNow: the live key file doesn't match; not pinging")
        return 1
    urls = sitemap_urls(_get(f"{site}/sitemap.xml"))
    code, text = ping(urls)
    # 200 OK and 202 Accepted are both success (202: key validation pending).
    print(f"IndexNow: sent {len(urls)} URLs, HTTP {code} {text}".strip())
    return 0 if code in (200, 202) else 1


if __name__ == "__main__":
    sys.exit(main())
