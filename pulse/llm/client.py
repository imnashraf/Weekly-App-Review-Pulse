"""Provider-agnostic LLM client: JSON-mode calls, pydantic validation, retry, usage logging."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_VALIDATION_RETRIES = 2


class LLMError(RuntimeError):
    """The provider failed, or kept returning output that does not match the schema."""


@dataclass
class ProviderResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


class Provider(Protocol):
    def complete(self, system: str, user: str, temperature: float) -> ProviderResponse:
        """One call asking for a JSON object back. Raises LLMError on provider failure."""
        ...


def extract_json(text: str):
    """Parse a JSON object from a model reply, tolerating code fences and surrounding prose."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors())
    return f"not valid JSON ({exc})"


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        model: str,
        temperature: float = 0.0,
        price_in: float | None = None,
        price_out: float | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.price_in = price_in
        self.price_out = price_out
        self.calls = 0
        self.retries = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        temperature: float | None = None,
        label: str = "llm",
    ) -> T:
        """Call the model and validate the reply against `schema`.

        On invalid JSON or a schema violation the error is fed back to the model and the call
        is repeated, up to MAX_VALIDATION_RETRIES times.
        """
        temp = self.temperature if temperature is None else temperature
        prompt = user
        problem = ""
        for attempt in range(MAX_VALIDATION_RETRIES + 1):
            t0 = time.monotonic()
            response = self.provider.complete(system, prompt, temp)
            self._record(label, attempt, response, time.monotonic() - t0)
            try:
                return schema.model_validate(extract_json(response.text))
            except (json.JSONDecodeError, ValidationError) as exc:
                problem = _describe(exc)
                log.warning("LLM output rejected", extra={
                    "stage": label, "attempt": attempt + 1, "problem": problem[:300]})
                self.retries += 1
                prompt = (
                    f"{user}\n\n---\nYour previous reply was rejected.\n"
                    f"Previous reply:\n{response.text[:2000]}\n"
                    f"Problem: {problem}\n"
                    "Reply again with only the corrected JSON object."
                )
        raise LLMError(
            f"{label}: model output still invalid after {MAX_VALIDATION_RETRIES + 1} attempts: "
            f"{problem}")

    def _record(self, label: str, attempt: int, response: ProviderResponse, secs: float) -> None:
        self.calls += 1
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out
        log.info("llm call", extra={
            "stage": label, "attempt": attempt + 1, "model": self.model,
            "tokens_in": response.tokens_in, "tokens_out": response.tokens_out,
            "duration_s": round(secs, 1)})

    @property
    def cost_usd(self) -> float | None:
        if self.price_in is None or self.price_out is None:
            return None
        return round((self.tokens_in * self.price_in + self.tokens_out * self.price_out) / 1e6, 4)

    def usage(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "validation_retries": self.retries,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
        }
