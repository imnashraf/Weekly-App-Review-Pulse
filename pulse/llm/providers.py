"""Concrete providers (Gemini, OpenAI, Anthropic) over plain HTTPS; no vendor SDK needed."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .client import LLMClient, LLMError, ProviderResponse

log = logging.getLogger(__name__)

MAX_HTTP_ATTEMPTS = 4
TRANSIENT = {408, 429, 500, 502, 503, 504}

# post(url, headers, body) -> parsed JSON response. Injectable for tests.
Post = Callable[[str, dict, dict], dict]


def http_post_json(url: str, headers: dict, body: dict, timeout: float = 180,
                   sleep: Callable[[float], None] = time.sleep) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers})
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            if exc.code not in TRANSIENT or attempt == MAX_HTTP_ATTEMPTS:
                raise LLMError(f"LLM request failed (HTTP {exc.code}): {detail}") from exc
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt == MAX_HTTP_ATTEMPTS:
                raise LLMError(f"LLM request failed: {exc}") from exc
            reason = str(exc)
        delay = 2 ** attempt
        log.warning("LLM request failed, retrying", extra={
            "attempt": attempt, "retry_in_s": delay, "reason": reason})
        sleep(delay)
    raise AssertionError("unreachable")


class GeminiProvider:
    def __init__(self, api_key: str, model: str, max_output_tokens: int, post: Post = http_post_json):
        self.api_key, self.model, self.max_tokens, self.post = api_key, model, max_output_tokens, post

    def complete(self, system: str, user: str, temperature: float) -> ProviderResponse:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        data = self.post(url, {"x-goog-api-key": self.api_key}, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
            },
        })
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected Gemini response: {str(data)[:300]}") from exc
        usage = data.get("usageMetadata", {})
        return ProviderResponse(
            text, usage.get("promptTokenCount", 0),
            usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0))


class OpenAIProvider:
    def __init__(self, api_key: str, model: str, max_output_tokens: int, post: Post = http_post_json):
        self.api_key, self.model, self.max_tokens, self.post = api_key, model, max_output_tokens, post

    def complete(self, system: str, user: str, temperature: float) -> ProviderResponse:
        data = self.post("https://api.openai.com/v1/chat/completions",
                         {"Authorization": f"Bearer {self.api_key}"}, {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        })
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected OpenAI response: {str(data)[:300]}") from exc
        usage = data.get("usage", {})
        return ProviderResponse(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, max_output_tokens: int, post: Post = http_post_json):
        self.api_key, self.model, self.max_tokens, self.post = api_key, model, max_output_tokens, post

    def complete(self, system: str, user: str, temperature: float) -> ProviderResponse:
        data = self.post("https://api.anthropic.com/v1/messages",
                         {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Unexpected Anthropic response: {str(data)[:300]}") from exc
        usage = data.get("usage", {})
        return ProviderResponse(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0))


PROVIDERS: dict[str, Any] = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}


def build_client(settings, post: Post | None = None) -> LLMClient:
    """Create the LLMClient described by `settings.llm`."""
    cfg = settings.llm
    if cfg.provider not in PROVIDERS:
        raise LLMError(f"Unknown llm.provider {cfg.provider!r}; use one of {sorted(PROVIDERS)}")
    if not cfg.model or cfg.model.startswith("<"):
        raise LLMError("Set llm.model in config.yaml to a real model name for your provider")
    if not settings.llm_api_key:
        raise LLMError("LLM_API_KEY is not set")
    kwargs = {"post": post} if post else {}
    provider = PROVIDERS[cfg.provider](
        settings.llm_api_key, cfg.model, cfg.max_output_tokens, **kwargs)
    return LLMClient(provider, cfg.model, cfg.temperature,
                     cfg.price_per_mtok_in, cfg.price_per_mtok_out)
