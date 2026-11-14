#!/usr/bin/env python3
"""Campaignify's light-model worker for the Mac mini.

It holds a long poll open on the site (/_light/next), runs each job on the local model (Ollama, on this Mac's GPU),
and posts the JSON answer back (/_light/done/<id>). The site only waits about 20 seconds for an answer and then
falls back on its own, so this worker being off never breaks anything.

Only the Python standard library. Settings come from the environment (setup.sh writes them to ~/.campaignify-light.env,
which the launchd job reads):
  CFY_LIGHT_URL      the site, default https://campaignify.ai
  LIGHT_WORKER_TOKEN the shared Bearer token (the same value as the GitHub secret of that name)
  CFY_LIGHT_MODEL    the Ollama model, default qwen3:14b
  OLLAMA_URL         default http://127.0.0.1:11434
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

SITE = os.environ.get("CFY_LIGHT_URL", "https://campaignify.ai").rstrip("/")
TOKEN = os.environ.get("LIGHT_WORKER_TOKEN", "").strip()
MODEL = os.environ.get("CFY_LIGHT_MODEL", "qwen3:14b")
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
NAME = os.environ.get("CFY_LIGHT_NAME") or socket.gethostname()
UA = "campaignify-light/1"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def site(path: str, body: dict, timeout: float = 35):
    req = urllib.request.Request(SITE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def ollama(job: dict) -> dict:
    body = {"model": MODEL, "stream": False, "think": False, "format": job.get("schema") or "json",
            "options": {"temperature": 0.4, "num_predict": int(job.get("max_tokens") or 900)},
            "messages": [{"role": "system", "content": job.get("system") or ""},
                         {"role": "user", "content": job.get("prompt") or ""}]}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    text = ((out.get("message") or {}).get("content") or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("the model did not answer with an object")
    return result


def main() -> int:
    if not TOKEN:
        log("LIGHT_WORKER_TOKEN is not set; see README.md")
        return 2
    log(f"light worker {NAME}: {SITE} -> {MODEL} at {OLLAMA}")
    pause = 1.0
    while True:
        try:
            status, job = site("/_light/next", {"model": MODEL, "by": NAME, "wait": 20})
            pause = 1.0
            if status == 204 or not job:
                continue
            started = time.monotonic()
            try:
                result, error = ollama(job), ""
            except Exception as e:  # noqa: BLE001
                result, error = None, f"{e.__class__.__name__}: {e}"[:300]
            site(f"/_light/done/{job['id']}", {"result": result, "error": error}, timeout=15)
            log(f"job {job['id']} {job.get('kind')}: {'failed ' + error if error else 'done'} "
                f"in {time.monotonic() - started:.1f}s")
        except urllib.error.HTTPError as e:
            if e.code in (401, 404):
                log(f"the site refused the worker ({e.code}): check LIGHT_WORKER_TOKEN on both sides")
                pause = 60.0
            else:
                log(f"site error {e.code}")
            time.sleep(pause)
            pause = min(pause * 2, 60.0)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"can't reach the site: {e}")
            time.sleep(pause)
            pause = min(pause * 2, 60.0)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
