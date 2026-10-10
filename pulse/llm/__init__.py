"""LLM access: provider-agnostic client plus versioned prompt files."""
from __future__ import annotations

from pathlib import Path

from .client import LLMClient, LLMError, ProviderResponse
from .providers import build_client

PROMPTS_DIR = Path(__file__).parent / "prompts"

__all__ = ["LLMClient", "LLMError", "ProviderResponse", "build_client", "load_prompt"]


def load_prompt(name: str, **variables: object) -> str:
    """Read `prompts/<name>.md` and fill `{{var}}` placeholders (e.g. `discovery_v1`)."""
    text = (PROMPTS_DIR / f"{name}.md").read_text()
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
