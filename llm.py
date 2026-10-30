"""The writing model behind the copy studio, content optimizer, SEO optimizer
and campaign planner.

Plain HTTPS, no SDK. Anthropic's Messages API is the main provider (and the
only one that runs the planner's tool loop); Groq's OpenAI-compatible API is an
optional fallback for single structured calls. Settings:

    ANTHROPIC_API_KEY   main key; ANTHROPIC_MODEL optional (default below)
    GROQ_API_KEY        optional fallback; GROQ_MODEL optional
    LLM_USD_IN_PER_MTOK / LLM_USD_OUT_PER_MTOK   override the price table

Members pay in credits for what each call actually cost: tokens used x the
provider's list price x RS_MARKUP, rounded up (catalog.MARKUP). When no key is
set, `configured()` is False and the tools say so instead of failing.
"""
import json
import math
import os
import re
import time

import requests

import catalog

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
# Cheaper model for small, structured jobs (URL import, angle ideas, the setup
# assistant). ANTHROPIC_FAST_MODEL overrides; a model the API doesn't know
# falls back to the main model once.
DEFAULT_FAST_MODEL = "claude-haiku-4-5"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
TIMEOUT = 120

# USD per million tokens (input, output), list prices. Matched by substring of
# the model id; the first match wins. LLM_USD_*_PER_MTOK override all of them.
PRICES = [
    ("haiku", (1.0, 5.0)),
    ("opus", (5.0, 25.0)),
    ("sonnet", (3.0, 15.0)),
    ("gpt-oss-120b", (0.15, 0.75)),
    ("gpt-oss-20b", (0.10, 0.50)),
    ("llama", (0.59, 0.79)),
]
FALLBACK_PRICE = (3.0, 15.0)


class LLMError(RuntimeError):
    pass


def _key(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def anthropic_ready() -> bool:
    return bool(_key("ANTHROPIC_API_KEY"))


def configured() -> bool:
    return anthropic_ready() or bool(_key("GROQ_API_KEY"))


def model(provider: str = "anthropic") -> str:
    if provider == "groq":
        return _key("GROQ_MODEL") or DEFAULT_GROQ_MODEL
    return _key("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL


def fast_model() -> str:
    return _key("ANTHROPIC_FAST_MODEL") or DEFAULT_FAST_MODEL


def status() -> str:
    """For the admin page: which provider is on, never the key."""
    if anthropic_ready():
        return f"on ({model('anthropic')})" + (" + fallback" if _key("GROQ_API_KEY") else "")
    if _key("GROQ_API_KEY"):
        return f"fallback only ({model('groq')}); the campaign planner needs ANTHROPIC_API_KEY"
    return "not configured (add ANTHROPIC_API_KEY)"


def price_per_mtok(model_id: str) -> tuple[float, float]:
    env_in, env_out = _key("LLM_USD_IN_PER_MTOK"), _key("LLM_USD_OUT_PER_MTOK")
    if env_in and env_out:
        try:
            return float(env_in), float(env_out)
        except ValueError:
            pass
    m = (model_id or "").lower()
    for needle, price in PRICES:
        if needle in m:
            return price
    return FALLBACK_PRICE


def cost_usd(model_id: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = price_per_mtok(model_id)
    return tokens_in / 1e6 * pin + tokens_out / 1e6 * pout


def credits_for(usd: float) -> int:
    """What a call costs a member: at least 1 credit, cost x markup, rounded up."""
    return max(1, int(math.ceil(usd * catalog.MARKUP * 100 - 1e-9)))


def estimate_credits(tokens_in: int, tokens_out: int, provider: str = "anthropic", model_id: str | None = None) -> int:
    """A quote before running: worst case for the given token budget."""
    return credits_for(cost_usd(model_id or model(provider), tokens_in, tokens_out))


class Usage:
    """Running total of tokens and cost across calls (one tool run)."""

    def __init__(self):
        self.tokens_in = self.tokens_out = 0
        self.usd = 0.0
        self.calls = 0

    def add(self, model_id: str, tin: int, tout: int, cache_write: int = 0, cache_read: int = 0):
        """Prompt-cache writes bill at 1.25x the input price and reads at 0.1x."""
        pin, pout = price_per_mtok(model_id)
        self.tokens_in += tin + cache_write + cache_read
        self.tokens_out += tout
        self.usd += (tin + cache_write * 1.25 + cache_read * 0.1) / 1e6 * pin + tout / 1e6 * pout
        self.calls += 1

    @property
    def credits(self) -> int:
        return credits_for(self.usd) if self.calls else 0


# ---------------------------------------------------------------- raw calls

def _anthropic(body: dict) -> dict:
    key = _key("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("not configured")
    for attempt in range(3):
        try:
            r = requests.post(ANTHROPIC_URL, json=body, timeout=TIMEOUT,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"})
        except requests.RequestException as e:
            if attempt == 2:
                raise LLMError(f"unreachable: {e.__class__.__name__}") from e
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            if r.status_code in (401, 403):
                raise LLMError("the writing service rejected our API key")
            raise LLMError(f"{r.status_code}: {r.text[:200]}")
        return r.json()
    raise LLMError("busy, try again")


def _groq(body: dict) -> dict:
    key = _key("GROQ_API_KEY")
    if not key:
        raise LLMError("not configured")
    try:
        r = requests.post(GROQ_URL, json=body, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise LLMError(f"unreachable: {e.__class__.__name__}") from e
    if r.status_code >= 400:
        raise LLMError(f"{r.status_code}: {r.text[:200]}")
    return r.json()


def chat(*, system, messages: list, tools: list | None = None, tool_choice: dict | None = None,
         max_tokens: int = 2000, usage: Usage | None = None, temperature: float = 0.7, model_id: str | None = None) -> dict:
    """One Anthropic Messages call. Returns the raw response (content blocks,
    stop_reason); adds its tokens to `usage`. `model_id` picks another model
    (e.g. fast_model()); if the API rejects it, the main model is used."""
    body = {"model": model_id or model("anthropic"), "max_tokens": max_tokens, "system": system, "messages": messages,
            "temperature": temperature}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    try:
        resp = _anthropic(body)
    except LLMError as e:
        if not model_id or model_id == model("anthropic") or not str(e).startswith(("404", "400")) \
                or "model" not in str(e).lower():
            raise
        body["model"] = model("anthropic")
        resp = _anthropic(body)
    u = resp.get("usage") or {}
    if usage is not None:
        usage.add(resp.get("model") or body["model"], int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                  int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0))
    return resp


def structured(*, system: str, prompt: str, schema: dict, name: str = "submit", max_tokens: int = 3000,
               usage: Usage | None = None, temperature: float = 0.7, model_id: str | None = None,
               images: list | None = None) -> dict:
    """Ask for one JSON object matching `schema`. Anthropic: a forced tool call
    (reliable JSON). Groq fallback: JSON mode with the schema in the prompt.
    `images` ([(bytes, media type)]) go to Anthropic with the prompt (vision);
    the text-only fallback never sees them, so prompts must not rely on them."""
    if anthropic_ready():
        content = prompt
        if images:
            import base64
            content = [{"type": "image", "source": {"type": "base64", "media_type": mt,
                                                    "data": base64.b64encode(data).decode("ascii")}}
                       for data, mt in images if data] + [{"type": "text", "text": prompt}]
        resp = chat(system=system, messages=[{"role": "user", "content": content}], max_tokens=max_tokens,
                    tools=[{"name": name, "description": "Return the result.", "input_schema": schema}],
                    tool_choice={"type": "tool", "name": name}, usage=usage, temperature=temperature,
                    model_id=model_id)
        for block in resp.get("content") or []:
            if block.get("type") == "tool_use":
                return block.get("input") or {}
        raise LLMError("no structured result")
    if _key("GROQ_API_KEY"):
        mid = model("groq")
        body = {"model": mid, "max_tokens": max_tokens, "temperature": temperature,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system + "\n\nReply with one JSON object that matches "
                              "this JSON Schema, and nothing else:\n" + json.dumps(schema)},
                             {"role": "user", "content": prompt}]}
        if "gpt-oss" in mid:
            body["reasoning_effort"] = "low"
        resp = _groq(body)
        u = resp.get("usage") or {}
        if usage is not None:
            usage.add(mid, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return parse_json(text)
    raise LLMError("not configured")


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise LLMError("the reply wasn't valid JSON")
