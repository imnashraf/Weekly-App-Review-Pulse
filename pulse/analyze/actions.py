"""Three action ideas, one per top theme. Architecture §5.5."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..llm import LLMClient, load_prompt

ACTIONS_PROMPT = "actions_v1"
MAX_ACTION_WORDS = 25


class Action(BaseModel):
    theme_id: str
    action: str = Field(min_length=1)
    owner_hint: Literal["Product", "Eng", "Support"]

    @field_validator("theme_id")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("action")
    @classmethod
    def _short(cls, value: str) -> str:
        value = value.strip()
        if len(value.split()) > MAX_ACTION_WORDS:
            raise ValueError(f"an action may have at most {MAX_ACTION_WORDS} words")
        return value


def actions_model(theme_ids: list[str]) -> type[BaseModel]:
    """Schema requiring exactly one action for each of `theme_ids`."""
    expected = sorted(theme_ids)

    class _Actions(BaseModel):
        actions: list[Action] = Field(min_length=len(expected), max_length=len(expected))

        @model_validator(mode="after")
        def _one_per_theme(self):
            got = sorted(a.theme_id for a in self.actions)
            if got != expected:
                raise ValueError(f"need exactly one action for each of {expected}, got {got}")
            return self

    return _Actions


def write_actions(client: LLMClient, top: list[dict], quotes: list[dict]) -> list[dict]:
    """One action per top theme, returned in theme rank order."""
    quote_for = {q["theme_id"]: q["quote"] for q in quotes}
    blocks = [
        f"{t['theme_id']} {t['name']}: {t['description']}\n"
        f"  reviews: {t['volume']}, negative: {t['neg_share']:.0%}\n"
        f"  quote: \"{quote_for[t['theme_id']]}\""
        for t in top
    ]
    ids = [t["theme_id"] for t in top]
    result = client.complete_json(
        load_prompt(ACTIONS_PROMPT), "Top themes:\n" + "\n".join(blocks),
        actions_model(ids), temperature=0, label="actions")
    by_theme = {a.theme_id: a for a in result.actions}
    return [by_theme[tid].model_dump() for tid in ids]
