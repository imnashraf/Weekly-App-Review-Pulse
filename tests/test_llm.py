import io
import json
import urllib.error

import pytest
from pydantic import BaseModel

from pulse.config import load_config
from pulse.llm import LLMClient, LLMError, ProviderResponse, build_client, load_prompt
from pulse.llm import providers
from pulse.llm.client import extract_json


class Answer(BaseModel):
    value: int


class Scripted:
    """Provider returning scripted replies in order; records every prompt."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user, temperature):
        self.prompts.append((system, user, temperature))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ProviderResponse(reply, tokens_in=100, tokens_out=10)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": 1} hope it helps') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        extract_json("no json here")


def test_complete_json_validates_and_tracks_usage():
    client = LLMClient(Scripted('{"value": 3}'), "m", price_in=1.0, price_out=2.0)
    assert client.complete_json("sys", "usr", Answer, temperature=0).value == 3
    assert client.usage() == {"model": "m", "calls": 1, "validation_retries": 0,
                              "tokens_in": 100, "tokens_out": 10, "cost_usd": 0.0001}
    assert client.provider.prompts[0][2] == 0


def test_cost_is_none_without_prices():
    client = LLMClient(Scripted('{"value": 3}'), "m")
    client.complete_json("s", "u", Answer)
    assert client.cost_usd is None


def test_invalid_json_triggers_retry_with_error_fed_back():
    provider = Scripted("not json at all", '{"value": 7}')
    client = LLMClient(provider, "m")
    assert client.complete_json("s", "question", Answer).value == 7
    assert client.calls == 2 and client.retries == 1
    retry_prompt = provider.prompts[1][1]
    assert "question" in retry_prompt and "not json at all" in retry_prompt and "not valid JSON" in retry_prompt


def test_schema_violation_triggers_retry():
    provider = Scripted('{"value": "abc"}', '{"value": 1}')
    assert LLMClient(provider, "m").complete_json("s", "u", Answer).value == 1
    assert "value" in provider.prompts[1][1]


def test_gives_up_after_two_retries():
    provider = Scripted("bad", "bad", "bad", '{"value": 1}')
    client = LLMClient(provider, "m")
    with pytest.raises(LLMError, match="after 3 attempts"):
        client.complete_json("s", "u", Answer, label="discovery")
    assert client.calls == 3 and len(provider.replies) == 1


def test_provider_error_is_not_retried_as_validation():
    client = LLMClient(Scripted(LLMError("HTTP 401")), "m")
    with pytest.raises(LLMError, match="401"):
        client.complete_json("s", "u", Answer)
    assert client.calls == 0


def test_load_prompt_fills_placeholders():
    text = load_prompt("discovery_v1", max_themes=4)
    assert "{{" not in text and "never more than 4" in text.lower()
    assert "labels" in load_prompt("classification_v1")


# ---- providers (HTTP is injected) ----

class FakePost:
    def __init__(self, response):
        self.response, self.calls = response, []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        return self.response


def test_gemini_request_and_response():
    post = FakePost({"candidates": [{"content": {"parts": [{"text": '{"a":1}'}]}}],
                     "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3,
                                       "thoughtsTokenCount": 2}})
    out = providers.GeminiProvider("KEY", "gem-model", 100, post).complete("sys", "usr", 0)
    url, headers, body = post.calls[0]
    assert url.endswith("/models/gem-model:generateContent") and headers == {"x-goog-api-key": "KEY"}
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert (out.text, out.tokens_in, out.tokens_out) == ('{"a":1}', 12, 5)


def test_openai_request_and_response():
    post = FakePost({"choices": [{"message": {"content": '{"a":1}'}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 4}})
    out = providers.OpenAIProvider("KEY", "gpt", 100, post).complete("sys", "usr", 0.5)
    _, headers, body = post.calls[0]
    assert headers["Authorization"] == "Bearer KEY"
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0.5
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert (out.tokens_in, out.tokens_out) == (10, 4)


def test_anthropic_request_and_response():
    post = FakePost({"content": [{"type": "text", "text": '{"a":'}, {"type": "text", "text": '1}'}],
                     "usage": {"input_tokens": 9, "output_tokens": 2}})
    out = providers.AnthropicProvider("KEY", "claude-x", 256, post).complete("sys", "usr", 0)
    _, headers, body = post.calls[0]
    assert headers["x-api-key"] == "KEY" and "anthropic-version" in headers
    assert body["system"] == "sys" and body["max_tokens"] == 256
    assert out.text == '{"a":1}' and (out.tokens_in, out.tokens_out) == (9, 2)


def test_unexpected_response_shape_raises():
    with pytest.raises(LLMError, match="Unexpected"):
        providers.OpenAIProvider("K", "m", 10, FakePost({"oops": 1})).complete("s", "u", 0)


def test_http_post_retries_transient_errors(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "slow", {}, io.BytesIO(b"slow down"))
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    assert providers.http_post_json("http://x", {}, {}, sleep=sleeps.append) == {"ok": True}
    assert len(attempts) == 3 and sleeps == [2, 4]


def test_http_post_does_not_retry_client_errors(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"bad key"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMError, match="401.*bad key"):
        providers.http_post_json("http://x", {"x-api-key": "SECRET"}, {}, sleep=lambda s: None)


def settings_for(tmp_path, monkeypatch, llm="{provider: openai, model: gpt-x}", key="k"):
    if key:
        monkeypatch.setenv("LLM_API_KEY", key)
    else:
        monkeypatch.delenv("LLM_API_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"app: {{google_play_id: x}}\nllm: {llm}\n"
                   "mcp: {docs: {command: d}, gmail: {command: g}}\n")
    return load_config(cfg)


def test_build_client_ok(tmp_path, monkeypatch):
    s = settings_for(tmp_path, monkeypatch, "{provider: anthropic, model: m, price_per_mtok_in: 1, price_per_mtok_out: 5}")
    client = build_client(s)
    assert isinstance(client.provider, providers.AnthropicProvider) and client.price_out == 5


def test_build_client_errors(tmp_path, monkeypatch):
    with pytest.raises(LLMError, match="llm.model"):
        build_client(settings_for(tmp_path, monkeypatch, "{provider: openai, model: '<model-name>'}"))
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        build_client(settings_for(tmp_path, monkeypatch, key=None))
    with pytest.raises(LLMError, match="provider"):
        build_client(settings_for(tmp_path, monkeypatch, "{provider: nope, model: m}"))
