"""Structured logs (roadmap M1.7): one JSON object per line on stderr for the
"campaignify" logger, with the request id and workspace id when there is a
request, plus one line per request (method, route pattern, status, time).

No PII and no tokens: the request line logs the route's pattern
(`/u/<token>`), never the real path or query, and no IP or email. Every
response carries `X-Request-Id` (the caller's, when it sends a sane one).
RS_LOG_JSON=0 keeps the old plain logs; RS_ACCESS_LOG=0 drops the request lines.
"""
import json
import logging
import os
import re
import sys
import time
import uuid

RID_RX = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
SKIP_PREFIXES = ("/static/", "/health")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
               "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        try:
            from flask import g, has_request_context
            if has_request_context():
                out["request_id"] = g.get("request_id")
                ws = g.get("ws")
                if ws is not None:
                    out["ws"] = ws["id"]
        except Exception:  # noqa: BLE001
            pass
        for k in ("method", "route", "status", "ms", "user"):
            v = getattr(record, k, None)
            if v is not None:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(out, default=str)


def setup():
    """JSON lines for the app's logger (idempotent)."""
    if os.environ.get("RS_LOG_JSON", "1") == "0":
        return
    lg = logging.getLogger("campaignify")
    if any(getattr(h, "_rs_json", False) for h in lg.handlers):
        return
    h = logging.StreamHandler(sys.stderr)
    h._rs_json = True
    h.setFormatter(JsonFormatter())
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False


def install(app):
    from flask import g, request
    setup()
    log = logging.getLogger("campaignify")
    access = os.environ.get("RS_ACCESS_LOG", "1") != "0" and os.environ.get("RS_LOG_JSON", "1") != "0"

    @app.before_request
    def _request_id():
        sent = request.headers.get("X-Request-Id") or ""
        g.request_id = sent if RID_RX.match(sent) else uuid.uuid4().hex[:16]
        g.request_t0 = time.monotonic()

    @app.after_request
    def _request_line(resp):
        rid = g.get("request_id")
        if rid:
            resp.headers.setdefault("X-Request-Id", rid)
        if access and not request.path.startswith(SKIP_PREFIXES):
            ms = round((time.monotonic() - g.get("request_t0", time.monotonic())) * 1000)
            user = g.get("user")
            log.info("request", extra={"method": request.method,
                                       "route": request.url_rule.rule if request.url_rule else "(no route)",
                                       "status": resp.status_code, "ms": ms,
                                       "user": user["id"] if user is not None else None})
        return resp
