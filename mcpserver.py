"""Remote MCP server: the app's tools for Claude and other MCP clients.

Shared core (the same file runs in Crowdcut and Campaignify; mcp_tools.py in
each repo holds that app's tools and its Adapter).

Transport: MCP Streamable HTTP at POST /mcp (spec 2025-11-25, also answers
2025-06-18 and 2025-03-26 clients). Every POST gets one JSON answer
(application/json), which the spec allows instead of an SSE stream; the server
keeps no session, so there is no Mcp-Session-Id, and GET/DELETE /mcp answer 405. A browser opening /mcp (a GET
that asks for HTML and not an event stream) gets the app's "use it in Claude" page instead (Adapter.web_page), so
the address people paste into Claude also explains itself.

Why not the official `mcp` Python SDK: it is an ASGI (Starlette/anyio) server
and these apps are Flask under sync gunicorn workers. Mounting it would need an
ASGI bridge, an async event loop per worker and a dozen new dependencies, and
its tools would still have to step back into Flask to reach the app's own
route functions. The protocol surface used here (initialize, ping,
tools/list, tools/call, prompts/list, prompts/get, notifications) is small and is implemented below.

Auth, two ways, both ending in the same Bearer token check:
  * Personal API keys made in the app's settings page: random 32 bytes,
    stored as a SHA-256 hash (the key is shown once), tied to one user and one
    workspace, read or read+write, revocable.
  * OAuth 2.1 for Claude.ai custom connectors and other clients that sign in:
    discovery (RFC 9728 protected resource metadata + RFC 8414 server
    metadata), dynamic client registration (RFC 7591), authorization code with
    PKCE S256 only, refresh tokens rotated on every use (a reused refresh token
    or code ends the whole connection), revocation (RFC 7009). The consent page
    uses the app's own login session and asks which workspace to connect.

Every tool runs as the token's user in the token's workspace, and almost every
tool calls the same Flask route the web app calls (internal request, see
ToolContext.call), so plan limits, credits, roles, the past-due pause and
every other check stay in one place. A user removed from the workspace loses
the key's access at once (membership is checked on every request).

Switch: MCP_SERVER=on (default off). Off, /mcp, the OAuth endpoints and the
key page answer 404 and nothing else changes.
"""
import base64
import collections
import contextlib
import functools
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from flask import (Blueprint, Response, current_app, g, jsonify, redirect, render_template, request, session)
from werkzeug.exceptions import HTTPException

log = logging.getLogger("mcp")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s mcp %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST = PROTOCOL_VERSIONS[0]
SCOPES = ("read", "write")
ACCESS_TTL = 3600                 # OAuth access token: 1 hour
REFRESH_TTL = 30 * 86400          # OAuth refresh token: 30 days, rotated on use
CODE_TTL = 600                    # authorization code: 10 minutes, single use
FILE_LINK_TTL = 3600              # signed download links: 1 hour
TOOLS_PAGE = 50
MAX_KEYS_PER_USER = 10
RATE = {"call": (120, 60), "write": (30, 60), "register": (20, 3600), "token": (60, 60), "authfail": (30, 60)}
CLAUDE_ORIGINS = ("https://claude.ai", "https://claude.com")


def enabled() -> bool:
    return (os.environ.get("MCP_SERVER") or "off").strip().lower() in ("1", "on", "true", "yes")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _later(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def token_hash(tok: str) -> str:
    """Keys and tokens are 32 random bytes: a plain SHA-256 is the right store
    (nothing to brute-force, and a lookup by hash needs no per-row salt)."""
    return hashlib.sha256(tok.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def ensure_tables(conn):
    conn.executescript("""
CREATE TABLE IF NOT EXISTS mcp_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,              -- key | access | refresh
    token_hash   TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,              -- first characters, to tell keys apart in the list
    name         TEXT,
    user_id      INTEGER NOT NULL,
    account_id   INTEGER NOT NULL,
    scope        TEXT NOT NULL,
    grant_id     INTEGER,
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_acct ON mcp_tokens(account_id, kind);
CREATE TABLE IF NOT EXISTS mcp_clients (
    client_id     TEXT PRIMARY KEY,
    secret_hash   TEXT,
    name          TEXT,
    redirect_uris TEXT NOT NULL,
    auth_method   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_grants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    scope      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS mcp_codes (
    code_hash      TEXT PRIMARY KEY,
    grant_id       INTEGER NOT NULL,
    client_id      TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    used_at        TEXT
);
""")
    conn.commit()


# ---------------------------------------------------------------- rate limits

class Limiter:
    """Sliding-window counts, per process (each gunicorn worker keeps its own,
    so the real ceiling is the limit times the worker count)."""

    def __init__(self):
        self.hits = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self.hits) > 20000:          # forget idle keys
                for k in [k for k, v in self.hits.items() if not v][:5000]:
                    self.hits.pop(k, None)
            return True


limiter = Limiter()


def _limited(kind: str, key) -> bool:
    n, w = RATE[kind]
    return not limiter.allow(f"{kind}:{key}", n, w)


def _ip() -> str:
    return request.remote_addr or "?"


# ---------------------------------------------------------------- tools

class ToolError(Exception):
    """A refusal or failure the model should read (returned as isError)."""


class Tool:
    def __init__(self, name, title, description, schema, fn, scope="read", destructive=False, idempotent=False,
                 open_world=False, visible=None):
        self.name, self.title, self.description, self.schema, self.fn = name, title, description, schema, fn
        self.scope, self.visible = scope, visible
        self.annotations = {"title": title, "readOnlyHint": scope == "read", "openWorldHint": bool(open_world)}
        if scope != "read":
            self.annotations.update(destructiveHint=bool(destructive), idempotentHint=bool(idempotent))

    def describe(self, ctx):
        schema = self.schema(ctx) if callable(self.schema) else self.schema
        return {"name": self.name, "title": self.title, "description": self.description,
                "inputSchema": schema, "annotations": self.annotations}


def validate(schema: dict, value, path="arguments") -> str | None:
    """The JSON Schema subset the tool schemas use. Returns the first problem."""
    t = schema.get("type")
    if "const" in schema and value != schema["const"]:
        return f"{path} must be {json.dumps(schema['const'])}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {', '.join(json.dumps(x) for x in schema['enum'])}"
    if t == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object"
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                return f"{path}.{k} is required"
        for k, v in value.items():
            if k not in props:
                if schema.get("additionalProperties", True) is False:
                    return f"{path}.{k} is not a known argument"
                continue
            err = validate(props[k], v, f"{path}.{k}")
            if err:
                return err
    elif t == "string":
        if not isinstance(value, str):
            return f"{path} must be a string"
        if len(value) < schema.get("minLength", 0):
            return f"{path} is too short"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} is longer than {schema['maxLength']} characters"
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{path} must be a whole number"
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{path} must be a number"
    elif t == "boolean":
        if not isinstance(value, bool):
            return f"{path} must be true or false"
    elif t == "array":
        if not isinstance(value, list):
            return f"{path} must be a list"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} has more than {schema['maxItems']} items"
        if len(value) < schema.get("minItems", 0):
            return f"{path} needs at least {schema['minItems']} item(s)"
        for i, v in enumerate(value):
            err = validate(schema.get("items", {}), v, f"{path}[{i}]")
            if err:
                return err
    if t in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be at most {schema['maximum']}"
    return None


def page_args(args, default=20, most=100):
    """(offset, limit) from a tool's cursor/limit arguments."""
    limit = max(1, min(most, int(args.get("limit") or default)))
    cur = args.get("cursor") or ""
    try:
        offset = int(base64.urlsafe_b64decode(cur.encode() + b"==").decode()) if cur else 0
    except (ValueError, UnicodeDecodeError):
        raise ToolError("cursor is not one this server gave out. Leave it out to start from the first page.")
    return max(0, offset), limit


def paginate(items, args, default=20, most=100, key="items"):
    """One page of a list plus next_cursor (absent on the last page)."""
    offset, limit = page_args(args, default, most)
    chunk = items[offset:offset + limit]
    out = {key: chunk, "count": len(chunk), "total": len(items)}
    if offset + limit < len(items):
        out["next_cursor"] = base64.urlsafe_b64encode(str(offset + limit).encode()).decode().rstrip("=")
    return out


PAGE_PROPS = {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Items per page (default 20)."},
              "cursor": {"type": "string", "maxLength": 40, "description": "next_cursor from the previous page."}}


class CallResult:
    def __init__(self, status, data, text, location, flashes, description):
        self.status, self.data, self.text, self.location = status, data, text, location
        self.flashes, self.description = flashes, description

    @property
    def ok(self):
        return self.status < 400

    def message(self):
        """The refusal in the app's own words."""
        if isinstance(self.data, dict):
            for k in ("error", "message"):
                if self.data.get(k):
                    return str(self.data[k])
        if self.flashes:
            return " ".join(self.flashes)
        if self.description:
            return str(self.description)
        return f"The app answered {self.status}."


class ToolContext:
    def __init__(self, adapter, token_row, role, base_url):
        self.adapter = adapter
        self.user_id = token_row["user_id"]
        self.account_id = token_row["account_id"]
        self.scopes = set((token_row["scope"] or "read").split())
        self.prefix = token_row["prefix"]
        self.role = role
        self.base_url = base_url.rstrip("/")

    @contextlib.contextmanager
    def as_user(self, path="/", **kw):
        """A fresh app and request context holding this token's user and
        workspace in flask.g, set by adapter.enter the way the app's own
        before_request sets them from a login cookie. Nothing of the outer
        request (cookies, headers) is passed in. Yields a DB connection."""
        app = current_app._get_current_object()
        kw.setdefault("method", "GET")
        kw.update(base_url=self.base_url + "/", headers={"Accept": "application/json"})
        with app.app_context(), app.test_request_context(path, **kw):
            with self.adapter.connect() as conn:
                self.adapter.enter(conn, self.user_id, self.account_id)
                yield conn

    def call(self, method, path, json_body=None, query=None, form=None) -> CallResult:
        """Run one of the app's own routes as this token's user: the route's
        decorators (login, role, plan feature) and its body run exactly as for
        the web app; only the app-wide before_request hooks are replaced by
        adapter.enter (see as_user)."""
        app = current_app._get_current_object()
        kw = {"method": method}
        if json_body is not None:
            kw["json"] = json_body
        if form is not None:
            kw["data"] = form
        if query:
            kw["query_string"] = {k: v for k, v in query.items() if v is not None}
        with self.as_user(path, **kw):
            desc = None
            try:
                if request.routing_exception is not None:
                    raise request.routing_exception
                rv = app.view_functions[request.url_rule.endpoint](**(request.view_args or {}))
                resp = app.make_response(rv)
            except HTTPException as e:
                desc = e.description
                resp = e.get_response()
            flashes = [m for _, m in session.get("_flashes", [])] if "_flashes" in session else []
            data = resp.get_json(silent=True) if resp.is_json else None
            text = "" if data is not None or resp.direct_passthrough else resp.get_data(as_text=True)[:500000]
            return CallResult(resp.status_code, data, text, resp.headers.get("Location"), flashes, desc)

    def must(self, method, path, **kw) -> CallResult:
        r = self.call(method, path, **kw)
        if not r.ok:
            raise ToolError(self.adapter.refusal(r))
        return r

    def file_link(self, kind: str, obj_id: int) -> dict:
        from itsdangerous import URLSafeTimedSerializer
        s = URLSafeTimedSerializer(current_app.secret_key, salt="mcp-file")
        tok = s.dumps({"k": kind, "i": int(obj_id), "u": self.user_id, "a": self.account_id})
        return {"url": f"{self.base_url}/mcp/file/{tok}", "expires_in_seconds": FILE_LINK_TTL}

    def ensure_write(self):
        if "write" not in self.scopes:
            raise ToolError("This connection is read-only. Make a key with read and write access in Settings to do this.")


class Prompt:
    """A prompt template (MCP prompts/list and prompts/get): clients show these as slash commands. `arguments` is
    a list of (name, description, required); `fn(ctx, args)` returns the text of the one user message."""

    def __init__(self, name, title, description, arguments, fn, visible=None):
        self.name, self.title, self.description, self.arguments, self.fn = name, title, description, arguments, fn
        self.visible = visible   # optional fn(ctx) -> bool; hidden prompts are neither listed nor served

    def shown(self, ctx):
        return self.visible is None or bool(self.visible(ctx))

    def describe(self):
        return {"name": self.name, "title": self.title, "description": self.description,
                "arguments": [{"name": n, "description": d, "required": bool(r)} for n, d, r in self.arguments]}


class Adapter:
    """What the core needs from the app. mcp_tools.py subclasses it."""
    name = "app"
    title = "App"
    version = "1"
    token_prefix = "app"
    instructions = ""
    docs_path = "/docs/mcp"
    settings_path = "/app/settings/mcp"
    consent_template = "mcp_consent.html"
    settings_template = "mcp_settings.html"
    # (path on this site, MIME type, "WxH"): the app's logo for MCP clients (serverInfo.icons, MCP 2025-11-25). Claude
    # shows it on the connector; square PNGs on a solid background read best at small sizes.
    icons: list = []
    tools: list = []
    prompts: list = []           # Prompt objects (slash commands in clients that show MCP prompts)
    # A function returning the human page for GET /mcp in a browser (see wants_page), or None for a plain 405.
    web_page = None

    def connect(self):
        raise NotImplementedError

    def base_url(self) -> str:
        return request.host_url.rstrip("/")

    def web_user(self):
        """{"id", "email"} of the browser's logged-in user, or None."""
        raise NotImplementedError

    def web_account(self):
        """{"id", "name", "role"} of the browser's current workspace, or None."""
        raise NotImplementedError

    def login_url(self, next_path: str) -> str:
        return "/login?" + urlencode({"next": next_path})

    def workspaces(self, conn, user_id) -> list:
        raise NotImplementedError

    def member_role(self, conn, user_id, account_id):
        """The user's role in the workspace right now, or None if they left it."""
        raise NotImplementedError

    def enter(self, conn, user_id, account_id):
        """Set flask.g the way the app's before_request does for a logged-in user."""
        raise NotImplementedError

    def csrf_value(self) -> str:
        raise NotImplementedError

    def csrf_ok(self) -> bool:
        raise NotImplementedError

    def serve_file(self, kind, obj_id):
        """Response for a signed download link (g is already set by enter)."""
        raise NotImplementedError

    def user_email(self, conn, user_id) -> str:
        r = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        return r["email"] if r else ""

    def refusal(self, r: CallResult) -> str:
        return r.message()


# ---------------------------------------------------------------- blueprint

def blueprint(adapter: Adapter) -> Blueprint:
    bp = Blueprint("mcp", __name__)
    tools = {t.name: t for t in adapter.tools}

    def on(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            if not enabled():
                return jsonify({"error": "not_found"}), 404
            return fn(*a, **kw)
        return wrap

    def resource_url():
        return adapter.base_url() + "/mcp"

    def prm_url():
        return adapter.base_url() + "/.well-known/oauth-protected-resource/mcp"

    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate, MCP-Protocol-Version"
        return resp

    def origin_ok() -> bool:
        """DNS-rebinding guard from the MCP spec: no Origin (server-to-server
        clients), our own site, Claude, a local tool, or MCP_ALLOWED_ORIGINS."""
        o = (request.headers.get("Origin") or "").rstrip("/")
        if not o or o == "null":
            return True
        host = (urlsplit(o).hostname or "").lower()
        allowed = {adapter.base_url().rstrip("/"), *CLAUDE_ORIGINS,
                   *[x.strip().rstrip("/") for x in (os.environ.get("MCP_ALLOWED_ORIGINS") or "").split(",") if x.strip()]}
        return o in allowed or host in ("localhost", "127.0.0.1", "::1")

    def unauthorized(desc="Sign in, or send an API key as a Bearer token.", code=401, error="invalid_token"):
        resp = jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": desc}})
        resp.status_code = code
        hdr = f'Bearer resource_metadata="{prm_url()}", scope="read write"'
        if request.headers.get("Authorization"):
            hdr += f', error="{error}", error_description="{desc}"'
        resp.headers["WWW-Authenticate"] = hdr
        return cors(resp)

    def authenticate(conn):
        h = request.headers.get("Authorization") or ""
        if h[:7].lower() != "bearer ":
            return None, None
        tok = h[7:].strip()
        if not tok or len(tok) > 300:
            return None, None
        row = conn.execute("SELECT * FROM mcp_tokens WHERE token_hash=?", (token_hash(tok),)).fetchone()
        if not row or row["kind"] not in ("key", "access") or row["revoked_at"]:
            return None, None
        if row["expires_at"] and row["expires_at"] < _now():
            return None, None
        if row["grant_id"]:
            gr = conn.execute("SELECT revoked_at FROM mcp_grants WHERE id=?", (row["grant_id"],)).fetchone()
            if not gr or gr["revoked_at"]:
                return None, None
        role = adapter.member_role(conn, row["user_id"], row["account_id"])
        if not role:
            return None, None
        now = _now()
        if not row["last_used_at"] or row["last_used_at"][:16] != now[:16]:
            conn.execute("UPDATE mcp_tokens SET last_used_at=? WHERE id=?", (now, row["id"]))
            if row["grant_id"]:
                conn.execute("UPDATE mcp_grants SET last_used_at=? WHERE id=?", (now, row["grant_id"]))
            conn.commit()
        return row, role

    # ---- the MCP endpoint

    @bp.route("/mcp", methods=["POST", "GET", "DELETE", "OPTIONS"])
    def mcp_endpoint():
        if request.method == "GET" and adapter.web_page and wants_page():
            return adapter.web_page()        # the human page, whether the server is on or not
        return mcp_protocol()

    @on
    def mcp_protocol():
        if request.method == "OPTIONS":
            return cors(Response(status=204))
        if not origin_ok():
            return cors(Response("Origin not allowed", status=403))
        if request.method in ("GET", "DELETE"):
            # No server-initiated stream and no sessions: both are optional in the spec.
            resp = Response("Use POST: this server answers each request with one JSON response.", status=405)
            resp.headers["Allow"] = "POST, OPTIONS"
            return cors(resp)
        pv = request.headers.get("MCP-Protocol-Version")
        if pv and pv not in PROTOCOL_VERSIONS:
            return cors(Response(f"Unsupported MCP-Protocol-Version {pv[:20]}", status=400))
        with adapter.connect() as conn:
            row, role = authenticate(conn)
        if not row:
            if request.headers.get("Authorization") and _limited("authfail", _ip()):
                return cors(Response("Too many failed attempts", status=429, headers={"Retry-After": "60"}))
            return unauthorized("Missing, expired or revoked token." if request.headers.get("Authorization")
                                else "Sign in, or send an API key as a Bearer token.")
        if _limited("call", row["id"]):
            return cors(Response(json.dumps({"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32000, "message": "Rate limit: 120 requests a minute."}}),
                                 status=429, mimetype="application/json", headers={"Retry-After": "30"}))
        ctx = ToolContext(adapter, row, role, adapter.base_url())
        msg = request.get_json(silent=True)
        if msg is None:
            return cors(jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})), 400
        if isinstance(msg, list):
            # A batch (2025-03-26 clients): at most 20 messages, each counted against the rate limit.
            out = []
            for i, m in enumerate(msg[:20]):
                if i and _limited("call", row["id"]):
                    out.append(_err(m.get("id") if isinstance(m, dict) else None, -32000,
                                    "Rate limit: 120 requests a minute."))
                    continue
                r = handle(ctx, m)
                if r is not None:
                    out.append(r)
        else:
            out = handle(ctx, msg)
        if out is None or out == []:
            return cors(Response(status=202))
        resp = jsonify(out)
        resp.headers["Cache-Control"] = "no-store"
        return cors(resp)

    def handle(ctx, m):
        if not isinstance(m, dict) or m.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        method, mid = m.get("method"), m.get("id")
        if "id" not in m or mid is None:
            return None                       # a notification (initialized, cancelled) or a client's response
        params = m.get("params") or {}
        if not isinstance(params, dict):
            return _err(mid, -32602, "params must be an object")
        if method == "initialize":
            want = str(params.get("protocolVersion") or "")
            return _ok(mid, {"protocolVersion": want if want in PROTOCOL_VERSIONS else LATEST,
                             "capabilities": dict({"tools": {"listChanged": False}},
                                                  **({"prompts": {"listChanged": False}} if adapter.prompts else {})),
                             "serverInfo": _server_info(adapter),
                             "instructions": adapter.instructions})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            listed = [t.describe(ctx) for t in tools.values() if not t.visible or t.visible(ctx)]
            try:
                start = int(base64.urlsafe_b64decode(str(params.get("cursor") or "").encode() + b"==").decode() or 0) \
                    if params.get("cursor") else 0
            except (ValueError, UnicodeDecodeError):
                return _err(mid, -32602, "Invalid cursor")
            res = {"tools": listed[start:start + TOOLS_PAGE]}
            if start + TOOLS_PAGE < len(listed):
                res["nextCursor"] = base64.urlsafe_b64encode(str(start + TOOLS_PAGE).encode()).decode().rstrip("=")
            return _ok(mid, res)
        if method == "tools/call":
            return _ok(mid, call_tool(ctx, str(params.get("name") or ""), params.get("arguments")))
        if method == "prompts/list":
            return _ok(mid, {"prompts": [p.describe() for p in adapter.prompts if p.shown(ctx)]})
        if method == "prompts/get":
            p = next((x for x in adapter.prompts if x.name == params.get("name") and x.shown(ctx)), None)
            if not p:
                return _err(mid, -32602, f"Unknown prompt: {str(params.get('name'))[:60]}")
            pargs = params.get("arguments") or {}
            if not isinstance(pargs, dict) or not all(isinstance(v, str) for v in pargs.values()):
                return _err(mid, -32602, "arguments must be an object of strings")
            missing = [n for n, _, r in p.arguments if r and not pargs.get(n)]
            if missing:
                return _err(mid, -32602, f"Missing argument: {missing[0]}")
            text = p.fn(ctx, {k: v[:4000] for k, v in pargs.items()})
            return _ok(mid, {"description": p.description,
                             "messages": [{"role": "user", "content": {"type": "text", "text": text}}]})
        if method == "resources/list":
            return _ok(mid, {"resources": []})
        if method == "resources/templates/list":
            return _ok(mid, {"resourceTemplates": []})
        return _err(mid, -32601, f"Method not found: {str(method)[:60]}")

    def call_tool(ctx, name, args):
        t = tools.get(name)
        if not t or (t.visible and not t.visible(ctx)):
            return _tool_err(f"Unknown tool {name[:60]!r}.")
        args = {} if args is None else args
        t0 = time.monotonic()
        status = "ok"
        try:
            if t.scope != "read":
                ctx.ensure_write()
                if _limited("write", ctx.prefix):
                    raise ToolError("Rate limit: 30 changes a minute on one key. Wait a minute and try again.")
            schema = t.schema(ctx) if callable(t.schema) else t.schema
            problem = validate(schema, args)
            if problem:
                raise ToolError(f"Invalid arguments: {problem}.")
            out = t.fn(ctx, args)
            if not isinstance(out, dict):
                out = {"result": out}
            return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
                    "structuredContent": out, "isError": False}
        except ToolError as e:
            status = "refused"
            return _tool_err(str(e))
        except Exception:  # noqa: BLE001 — the model gets a plain line, the log gets the trace
            status = "error"
            log.exception("tool %s failed (ws=%s key=%s)", name, ctx.account_id, ctx.prefix)
            return _tool_err("Something went wrong on our side. Nothing more was changed. Try again in a minute.")
        finally:
            # Never the arguments or the token: tool name, workspace, key prefix, result, time.
            log.info("tool=%s ws=%s user=%s key=%s status=%s ms=%d", name, ctx.account_id, ctx.user_id, ctx.prefix,
                     status, (time.monotonic() - t0) * 1000)

    # ---- signed download links

    @bp.route("/mcp/file/<tok>")
    @on
    def file_link(tok):
        from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
        s = URLSafeTimedSerializer(current_app.secret_key, salt="mcp-file")
        try:
            d = s.loads(tok, max_age=FILE_LINK_TTL)
        except SignatureExpired:
            return Response("This download link has expired. Ask for a new one.", status=410)
        except BadSignature:
            return Response("Not found", status=404)
        with adapter.connect() as conn:
            if not adapter.member_role(conn, d["u"], d["a"]):
                return Response("Not found", status=404)
            adapter.enter(conn, d["u"], d["a"])
        resp = adapter.serve_file(d["k"], d["i"])
        resp = current_app.make_response(resp)
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    # ---- OAuth discovery

    @bp.route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
    @bp.route("/.well-known/oauth-protected-resource/mcp", methods=["GET", "OPTIONS"])
    @on
    def protected_resource():
        return cors(jsonify({"resource": resource_url(), "authorization_servers": [adapter.base_url()],
                             "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"],
                             "resource_name": adapter.title, "resource_documentation": adapter.base_url() + adapter.docs_path}))

    @bp.route("/.well-known/oauth-authorization-server", methods=["GET", "OPTIONS"])
    @bp.route("/.well-known/oauth-authorization-server/mcp", methods=["GET", "OPTIONS"])
    @on
    def auth_server():
        b = adapter.base_url()
        return cors(jsonify({"issuer": b, "authorization_endpoint": b + "/oauth/authorize",
                             "token_endpoint": b + "/oauth/token", "registration_endpoint": b + "/oauth/register",
                             "revocation_endpoint": b + "/oauth/revoke", "scopes_supported": list(SCOPES),
                             "response_types_supported": ["code"], "response_modes_supported": ["query"],
                             "grant_types_supported": ["authorization_code", "refresh_token"],
                             "code_challenge_methods_supported": ["S256"],
                             "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                             "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post",
                                                                            "client_secret_basic"],
                             "authorization_response_iss_parameter_supported": True,
                             "client_id_metadata_document_supported": False,
                             "service_documentation": b + adapter.docs_path}))

    # ---- dynamic client registration

    def redirect_ok(uri) -> bool:
        if not isinstance(uri, str) or len(uri) > 500 or "#" in uri:
            return False
        p = urlsplit(uri)
        if p.scheme == "https" and p.hostname:
            return True
        return p.scheme == "http" and (p.hostname or "") in ("localhost", "127.0.0.1", "::1")

    def oauth_error(error, desc, code=400):
        resp = jsonify({"error": error, "error_description": desc})
        resp.status_code = code
        resp.headers["Cache-Control"] = "no-store"
        return cors(resp)

    @bp.route("/oauth/register", methods=["POST", "OPTIONS"])
    @on
    def register():
        if request.method == "OPTIONS":
            return cors(Response(status=204))
        if _limited("register", _ip()):
            return oauth_error("slow_down", "Too many registrations from this address. Try again later.", 429)
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            return oauth_error("invalid_client_metadata", "Send the client metadata as JSON.")
        uris = d.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 5 or not all(redirect_ok(u) for u in uris):
            return oauth_error("invalid_redirect_uri", "redirect_uris: 1 to 5 https URLs (http only for localhost).")
        method = d.get("token_endpoint_auth_method") or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return oauth_error("invalid_client_metadata", "token_endpoint_auth_method is not supported.")
        for gt in d.get("grant_types") or ["authorization_code"]:
            if gt not in ("authorization_code", "refresh_token"):
                return oauth_error("invalid_client_metadata", f"grant type {str(gt)[:40]} is not supported.")
        name = " ".join(str(d.get("client_name") or "MCP client").split())[:80]
        cid = "mcpc_" + secrets.token_urlsafe(18)
        secret = new_token("mcps") if method != "none" else None
        with adapter.connect() as conn:
            ensure_tables(conn)
            conn.execute("INSERT INTO mcp_clients (client_id, secret_hash, name, redirect_uris, auth_method, created_at) "
                         "VALUES (?,?,?,?,?,?)", (cid, token_hash(secret) if secret else None, name, json.dumps(uris),
                                                  method, _now()))
            conn.commit()
        out = {"client_id": cid, "client_id_issued_at": int(time.time()), "client_name": name, "redirect_uris": uris,
               "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
               "response_types": ["code"], "scope": "read write"}
        if secret:
            out.update(client_secret=secret, client_secret_expires_at=0)
        log.info("oauth client registered name=%r method=%s", name, method)
        resp = jsonify(out)
        resp.status_code = 201
        resp.headers["Cache-Control"] = "no-store"
        return cors(resp)

    def client(conn, cid):
        return conn.execute("SELECT * FROM mcp_clients WHERE client_id=?", (str(cid or "")[:80],)).fetchone()

    def client_auth_ok(cl) -> bool:
        """Public clients send only client_id; confidential ones their secret (post or basic)."""
        if cl["auth_method"] == "none":
            return True
        secret = request.form.get("client_secret")
        if not secret and request.authorization and request.authorization.type == "basic":
            secret = request.authorization.password
        return bool(secret) and secrets.compare_digest(token_hash(secret), cl["secret_hash"] or "")

    def form_client_id():
        if request.form.get("client_id"):
            return request.form["client_id"]
        if request.authorization and request.authorization.type == "basic":
            return request.authorization.username
        return ""

    # ---- authorization (consent)

    @bp.route("/oauth/authorize", methods=["GET", "POST"])
    @on
    def authorize():
        src = request.form if request.method == "POST" else request.args
        p = {k: (src.get(k) or "") for k in ("response_type", "client_id", "redirect_uri", "state", "scope",
                                             "code_challenge", "code_challenge_method", "resource")}
        with adapter.connect() as conn:
            ensure_tables(conn)
            cl = client(conn, p["client_id"])
        if not cl:
            return _page_error("This app isn't registered with us. Remove the connector and add it again.")
        uris = json.loads(cl["redirect_uris"])
        if p["redirect_uri"] and p["redirect_uri"] not in uris:
            return _page_error("The sign-in link has a return address we don't know. Nothing was shared.")
        ruri = p["redirect_uri"] or (uris[0] if len(uris) == 1 else "")
        if not ruri:
            return _page_error("The sign-in link has no return address. Nothing was shared.")

        def back(**q):
            q.update(state=p["state"] or None, iss=adapter.base_url())
            sep = "&" if "?" in ruri else "?"
            return redirect(ruri + sep + urlencode({k: v for k, v in q.items() if v is not None}))

        if p["response_type"] != "code":
            return back(error="unsupported_response_type")
        if not p["code_challenge"] or p["code_challenge_method"] != "S256" or not 43 <= len(p["code_challenge"]) <= 128:
            return back(error="invalid_request", error_description="PKCE with S256 is required.")
        if p["resource"] and p["resource"].rstrip("/") != resource_url():
            return back(error="invalid_target", error_description="This server only issues tokens for " + resource_url())
        asked = [s for s in (p["scope"] or "read write").split() if s in SCOPES] or ["read"]
        user = adapter.web_user()
        if not user:
            return redirect(adapter.login_url(request.full_path if request.method == "GET" else
                                              "/oauth/authorize?" + urlencode(p)))
        with adapter.connect() as conn:
            spaces = adapter.workspaces(conn, user["id"])
        if not spaces:
            return _page_error("Your login has no workspace yet. Open the app once, then connect again.")
        current = adapter.web_account()
        if request.method == "POST":
            if not adapter.csrf_ok():
                return _page_error("This page expired. Go back to the app you were connecting and start again.")
            if request.form.get("decision") != "allow":
                log.info("oauth consent denied client=%s user=%s", cl["client_id"], user["id"])
                return back(error="access_denied", error_description="The user said no.")
            try:
                aid = int(request.form.get("workspace") or 0)
            except ValueError:
                aid = 0
            if aid not in {w["id"] for w in spaces}:
                return _page_error("Pick one of your workspaces.")
            scope = "read write" if ("write" in asked and request.form.get("allow_write") == "1") else "read"
            code = secrets.token_urlsafe(32)
            with adapter.connect() as conn:
                gid = conn.execute("INSERT INTO mcp_grants (client_id, user_id, account_id, scope, created_at) "
                                   "VALUES (?,?,?,?,?)", (cl["client_id"], user["id"], aid, scope, _now())).lastrowid
                conn.execute("INSERT INTO mcp_codes (code_hash, grant_id, client_id, redirect_uri, code_challenge, "
                             "expires_at) VALUES (?,?,?,?,?,?)",
                             (token_hash(code), gid, cl["client_id"], ruri, p["code_challenge"], _later(CODE_TTL)))
                conn.commit()
            log.info("oauth consent given client=%s user=%s ws=%s scope=%s", cl["client_id"], user["id"], aid, scope)
            return back(code=code)
        resp = current_app.make_response(render_template(
            adapter.consent_template, client_name=cl["name"], redirect_host=urlsplit(ruri).hostname or "",
            workspaces=spaces, current_id=(current or {}).get("id"), want_write="write" in asked, params=p,
            user=user, csrf=adapter.csrf_value(), settings_path=adapter.settings_path, docs_path=adapter.docs_path))
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _page_error(msg):
        resp = current_app.make_response((render_template(adapter.consent_template, error=msg, docs_path=adapter.docs_path,
                                                          settings_path=adapter.settings_path), 400))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- token endpoint

    def issue(conn, gid, user_id, account_id, scope):
        access, refresh = new_token(adapter.token_prefix + "_at"), new_token(adapter.token_prefix + "_rt")
        now = _now()
        for kind, tok, ttl in (("access", access, ACCESS_TTL), ("refresh", refresh, REFRESH_TTL)):
            conn.execute("INSERT INTO mcp_tokens (kind, token_hash, prefix, name, user_id, account_id, scope, grant_id, "
                         "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (kind, token_hash(tok), tok[:12], None, user_id, account_id, scope, gid, now, _later(ttl)))
        conn.commit()
        resp = jsonify({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                        "refresh_token": refresh, "scope": scope})
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        return cors(resp)

    def end_grant(conn, gid):
        now = _now()
        conn.execute("UPDATE mcp_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (now, gid))
        conn.execute("UPDATE mcp_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE grant_id=?", (now, gid))
        conn.commit()

    @bp.route("/oauth/token", methods=["POST", "OPTIONS"])
    @on
    def token():
        if request.method == "OPTIONS":
            return cors(Response(status=204))
        if _limited("token", _ip()):
            return oauth_error("slow_down", "Too many requests. Try again in a minute.", 429)
        gt = request.form.get("grant_type")
        with adapter.connect() as conn:
            ensure_tables(conn)
            cl = client(conn, form_client_id())
            if not cl or not client_auth_ok(cl):
                return oauth_error("invalid_client", "Unknown client or wrong client secret.", 401)
            if request.form.get("resource") and request.form["resource"].rstrip("/") != resource_url():
                return oauth_error("invalid_target", "This server only issues tokens for " + resource_url())
            if gt == "authorization_code":
                code = request.form.get("code") or ""
                row = conn.execute("SELECT * FROM mcp_codes WHERE code_hash=?", (token_hash(code),)).fetchone()
                if not row or row["client_id"] != cl["client_id"]:
                    return oauth_error("invalid_grant", "Unknown code.")
                if row["used_at"]:
                    end_grant(conn, row["grant_id"])           # a replayed code ends the connection it made
                    return oauth_error("invalid_grant", "This code was already used.")
                conn.execute("UPDATE mcp_codes SET used_at=? WHERE code_hash=?", (_now(), row["code_hash"]))
                conn.commit()
                if row["expires_at"] < _now():
                    return oauth_error("invalid_grant", "The code expired. Connect again.")
                if (request.form.get("redirect_uri") or row["redirect_uri"]) != row["redirect_uri"]:
                    return oauth_error("invalid_grant", "redirect_uri doesn't match.")
                verifier = request.form.get("code_verifier") or ""
                calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if not 43 <= len(verifier) <= 128 or not secrets.compare_digest(calc, row["code_challenge"]):
                    return oauth_error("invalid_grant", "PKCE check failed.")
                gr = conn.execute("SELECT * FROM mcp_grants WHERE id=?", (row["grant_id"],)).fetchone()
                if not gr or gr["revoked_at"] or not adapter.member_role(conn, gr["user_id"], gr["account_id"]):
                    return oauth_error("invalid_grant", "This connection was removed.")
                log.info("oauth token issued client=%s ws=%s", cl["client_id"], gr["account_id"])
                return issue(conn, gr["id"], gr["user_id"], gr["account_id"], gr["scope"])
            if gt == "refresh_token":
                rt = request.form.get("refresh_token") or ""
                row = conn.execute("SELECT * FROM mcp_tokens WHERE token_hash=? AND kind='refresh'",
                                   (token_hash(rt),)).fetchone()
                gr = conn.execute("SELECT * FROM mcp_grants WHERE id=?", (row["grant_id"],)).fetchone() if row else None
                if not row or not gr or gr["client_id"] != cl["client_id"]:
                    return oauth_error("invalid_grant", "Unknown refresh token.")
                if row["revoked_at"]:
                    end_grant(conn, gr["id"])                  # reuse of a rotated token: end the connection
                    return oauth_error("invalid_grant", "This refresh token was already used. Connect again.")
                if gr["revoked_at"] or row["expires_at"] < _now() or \
                        not adapter.member_role(conn, gr["user_id"], gr["account_id"]):
                    return oauth_error("invalid_grant", "This connection expired or was removed. Connect again.")
                asked = request.form.get("scope")
                scope = gr["scope"]
                if asked:
                    if not set(asked.split()) <= set(gr["scope"].split()):
                        return oauth_error("invalid_scope", "A refresh can't widen the access.")
                    scope = " ".join(s for s in SCOPES if s in asked.split())
                now = _now()
                conn.execute("UPDATE mcp_tokens SET revoked_at=? WHERE id=?", (now, row["id"]))
                conn.execute("UPDATE mcp_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE grant_id=? AND kind='access'",
                             (now, gr["id"]))
                return issue(conn, gr["id"], gr["user_id"], gr["account_id"], scope)
        return oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")

    @bp.route("/oauth/revoke", methods=["POST", "OPTIONS"])
    @on
    def revoke():
        if request.method == "OPTIONS":
            return cors(Response(status=204))
        with adapter.connect() as conn:
            ensure_tables(conn)
            cl = client(conn, form_client_id())
            if not cl or not client_auth_ok(cl):
                return oauth_error("invalid_client", "Unknown client.", 401)
            tok = request.form.get("token") or ""
            row = conn.execute("SELECT * FROM mcp_tokens WHERE token_hash=?", (token_hash(tok),)).fetchone()
            if row and row["grant_id"]:
                gr = conn.execute("SELECT client_id FROM mcp_grants WHERE id=?", (row["grant_id"],)).fetchone()
                if gr and gr["client_id"] == cl["client_id"]:
                    if row["kind"] == "refresh":
                        end_grant(conn, row["grant_id"])
                    else:
                        conn.execute("UPDATE mcp_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?",
                                     (_now(), row["id"]))
                        conn.commit()
        return cors(Response(status=200))

    # ---- settings: API keys and connected apps

    @bp.route(adapter.settings_path, methods=["GET", "POST"])
    @on
    def settings():
        user, acct = adapter.web_user(), adapter.web_account()
        if not user:
            return redirect(adapter.login_url(adapter.settings_path))
        if not acct:
            return redirect("/app")
        owner = acct.get("role") == "owner"
        new_key, error = None, None
        with adapter.connect() as conn:
            ensure_tables(conn)
            if request.method == "POST":
                if not adapter.csrf_ok():
                    error = "This page expired. Reload it and try again."
                elif request.form.get("action") == "create":
                    name = " ".join((request.form.get("name") or "").split())[:60] or "API key"
                    scope = "read write" if request.form.get("scope") == "write" else "read"
                    n = conn.execute("SELECT COUNT(*) FROM mcp_tokens WHERE kind='key' AND user_id=? AND account_id=? "
                                     "AND revoked_at IS NULL", (user["id"], acct["id"])).fetchone()[0]
                    if n >= MAX_KEYS_PER_USER:
                        error = f"You have {MAX_KEYS_PER_USER} keys in this workspace. Revoke one first."
                    else:
                        new_key = new_token(adapter.token_prefix)
                        conn.execute("INSERT INTO mcp_tokens (kind, token_hash, prefix, name, user_id, account_id, scope, "
                                     "created_at) VALUES ('key',?,?,?,?,?,?,?)",
                                     (token_hash(new_key), new_key[:12], name, user["id"], acct["id"], scope, _now()))
                        conn.commit()
                        log.info("api key created ws=%s user=%s key=%s scope=%s", acct["id"], user["id"],
                                 new_key[:12], scope)
                elif request.form.get("action") in ("revoke_key", "revoke_grant"):
                    kind = request.form["action"]
                    try:
                        rid = int(request.form.get("id") or 0)
                    except ValueError:
                        rid = 0
                    table = "mcp_tokens" if kind == "revoke_key" else "mcp_grants"
                    extra = " AND kind='key'" if kind == "revoke_key" else ""
                    row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND account_id=?{extra}",  # noqa: S608
                                       (rid, acct["id"])).fetchone()
                    if not row or (row["user_id"] != user["id"] and not owner):
                        error = "That key or connection isn't one you can revoke."
                    elif kind == "revoke_key":
                        conn.execute("UPDATE mcp_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (_now(), rid))
                        conn.commit()
                        log.info("api key revoked ws=%s key=%s", acct["id"], row["prefix"])
                    else:
                        end_grant(conn, rid)
                        log.info("oauth connection revoked ws=%s grant=%s", acct["id"], rid)
            who = "" if owner else " AND t.user_id=?"
            args = (acct["id"],) if owner else (acct["id"], user["id"])
            keys = [dict(r) for r in conn.execute(
                "SELECT t.id, t.prefix, t.name, t.scope, t.created_at, t.last_used_at, t.user_id, u.email "
                f"FROM mcp_tokens t LEFT JOIN users u ON u.id=t.user_id WHERE t.kind='key' AND t.account_id=? {who} "
                "AND t.revoked_at IS NULL ORDER BY t.id DESC", args)]
            grants = [dict(r) for r in conn.execute(
                "SELECT t.id, t.scope, t.created_at, t.last_used_at, t.user_id, c.name AS client_name, u.email "
                "FROM mcp_grants t LEFT JOIN mcp_clients c ON c.client_id=t.client_id LEFT JOIN users u ON u.id=t.user_id "
                f"WHERE t.account_id=? {who} AND t.revoked_at IS NULL ORDER BY t.id DESC", args)]
        resp = current_app.make_response(render_template(
            adapter.settings_template, keys=keys, grants=grants, new_key=new_key, error=error, account=acct,
            user=user, owner=owner, csrf=adapter.csrf_value(), mcp_url=resource_url(), docs_path=adapter.docs_path,
            settings_path=adapter.settings_path, max_keys=MAX_KEYS_PER_USER))
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    return bp


def _server_info(adapter) -> dict:
    """serverInfo for initialize: name, title, version, the docs link and the app's icons as absolute URLs."""
    base = adapter.base_url()
    info = {"name": adapter.name, "title": adapter.title, "version": adapter.version,
            "websiteUrl": base + adapter.docs_path}
    if adapter.icons:
        info["icons"] = [{"src": base + path, "mimeType": mime, "sizes": [size]} for path, mime, size in adapter.icons]
    return info


def wants_page() -> bool:
    """A browser's page load: it accepts HTML and isn't asking for an MCP event stream."""
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "text/event-stream" not in accept


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_err(msg):
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def init(app, adapter: Adapter):
    """Register the blueprint and make the tables (even while switched off, so
    turning it on needs no migration)."""
    with adapter.connect() as conn:
        ensure_tables(conn)
    app.register_blueprint(blueprint(adapter))
