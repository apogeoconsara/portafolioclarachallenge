"""LLM providers. AnthropicLLM is the real model (Messages API, forced tool use for structured output).
ScriptedLLM / ReplayLLM return recorded outputs so tests and offline demos never need a key.

The API key is read ONLY from ANTHROPIC_API_KEY. ORCH_ANTHROPIC_BASE_URL may point to a proxy; the generic
ANTHROPIC_BASE_URL is deliberately ignored so an environment variable meant for something else is never picked up.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .prompts import DEFAULT_MODEL


# Reading a reply is classification and extraction, so the model gets no room for variety: the same text should give the same
# answer as far as the API allows. (Left at the default of 1.0, two live evals of the same prompt scored differently.)
TEMPERATURE = 0


@dataclass
class LLMResponse:
    raw: str
    model: str
    mode: str                 # live | replay | scripted
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class LLMUnavailable(Exception):
    pass


class AnthropicLLM:
    mode = "live"

    def __init__(self, model: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.model = model or os.environ.get("ORCH_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.base = os.environ.get("ORCH_ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")

    def run(self, system: str, user: str, tool: dict, max_tokens: int = 700) -> LLMResponse:
        body = json.dumps({"model": self.model, "max_tokens": max_tokens, "temperature": TEMPERATURE, "system": system,
                           "messages": [{"role": "user", "content": user}],
                           "tools": [tool], "tool_choice": {"type": "tool", "name": tool["name"]}}).encode()
        req = urllib.request.Request(f"{self.base}/v1/messages", data=body, method="POST", headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        last = None
        for attempt in range(3):                          # the model API itself can rate-limit or fail
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                    time.sleep(float(e.headers.get("retry-after") or 2 ** attempt))
                    continue
                raise LLMUnavailable(f"Anthropic API error {e.code}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMUnavailable(f"Anthropic API unreachable: {e}") from e
        else:
            raise LLMUnavailable(str(last))
        raw = ""
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                raw = json.dumps(block.get("input"), ensure_ascii=False)
                break
            if block.get("type") == "text":
                raw = block.get("text", "")
        u = data.get("usage", {})
        return LLMResponse(raw, data.get("model", self.model), "live", int((time.time() - t0) * 1000),
                           u.get("input_tokens", 0), u.get("output_tokens", 0))


class ScriptedLLM:
    """Returns pre-set raw outputs in order (or from a function). For tests and recorded demo runs."""
    mode = "scripted"

    def __init__(self, outputs=None, fn=None, model="scripted"):
        self.outputs = list(outputs or [])
        self.fn = fn
        self.model = model
        self.calls = []

    def run(self, system, user, tool, max_tokens=700) -> LLMResponse:
        self.calls.append({"tool": tool["name"], "user": user})
        raw = self.fn(tool["name"], user) if self.fn else (self.outputs.pop(0) if self.outputs else "")
        return LLMResponse(raw, self.model, self.mode)


class UnavailableLLM:
    """Used when no key is configured: every AI step degrades to its safe fallback."""
    mode = "unavailable"
    model = "none"

    def run(self, *a, **k):
        raise LLMUnavailable("no LLM configured")
