"""Validation gates run on the composed pulse before anything is published. Architecture §5.8."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..analyze.quotes import check_quote, normalize
from ..clean.pii import has_pii

MIN_REVIEWS_WARNING = 50
PULSE_THEMES = 3
PULSE_QUOTES = 3
PULSE_ACTIONS = 3


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class GateError(RuntimeError):
    """One or more gates failed; the pulse must not be published."""

    def __init__(self, failed: list[GateResult]) -> None:
        self.failed = failed
        super().__init__("; ".join(f"{g.name}: {g.detail}" for g in failed))


_WORD = re.compile(r"[^\W_]", re.U)


def count_words(markdown: str) -> int:
    """Whitespace-separated tokens that contain a letter or digit (markup like `>` or `##` is free)."""
    return sum(1 for token in markdown.split() if _WORD.search(token))


def schema_gate(analysis: dict) -> GateResult:
    from pydantic import BaseModel, ValidationError

    class _Top(BaseModel):
        theme_id: str
        name: str
        share: float
        volume: int

    class _Quote(BaseModel):
        theme_id: str
        review_id: str
        quote: str

    class _Action(BaseModel):
        theme_id: str
        action: str
        owner_hint: str

    class _Analysis(BaseModel):
        top_themes: list[_Top]
        quotes: list[_Quote]
        actions: list[_Action]

    try:
        _Analysis.model_validate(analysis)
    except ValidationError as exc:
        return GateResult("schema", False, f"05_analysis.json does not match the schema: {exc.error_count()} error(s)")
    return GateResult("schema", True)


def theme_cap_gate(all_themes: list[dict], pulse_themes: list[dict], max_themes: int = 5) -> GateResult:
    if len(all_themes) > min(max_themes, 5):
        return GateResult("theme_cap", False, f"{len(all_themes)} themes discovered; the cap is {min(max_themes, 5)}")
    if len(pulse_themes) != PULSE_THEMES:
        return GateResult("theme_cap", False, f"pulse has {len(pulse_themes)} themes; it must have {PULSE_THEMES}")
    return GateResult("theme_cap", True)


def counts_gate(quotes: list, actions: list) -> GateResult:
    if len(quotes) != PULSE_QUOTES or len(actions) != PULSE_ACTIONS:
        return GateResult(
            "counts", False, f"{len(quotes)} quotes and {len(actions)} actions; need {PULSE_QUOTES} and {PULSE_ACTIONS}")
    return GateResult("counts", True)


def verbatim_gate(quotes: list[dict], reviews_by_id: dict[str, dict], pulse_md: str) -> GateResult:
    """Every quote is an excerpt of its source review and appears unaltered in the pulse."""
    body = normalize(pulse_md)
    for q in quotes:
        review = reviews_by_id.get(q["review_id"])
        if review is None:
            return GateResult("verbatim", False, f"source review {q['review_id']} not found")
        problem = check_quote(q["quote"], review["text"])
        if problem:
            return GateResult("verbatim", False, f"quote for {q['theme_id']}: {problem}")
        if normalize(q["quote"]) not in body:
            return GateResult("verbatim", False, f"quote for {q['theme_id']} is missing from the pulse")
    return GateResult("verbatim", True)


def word_count_gate(pulse_md: str, max_words: int) -> GateResult:
    n = count_words(pulse_md)
    if n > max_words:
        return GateResult("word_count", False, f"{n} words; the limit is {max_words}")
    return GateResult("word_count", True, f"{n} words")


def pii_gate(pulse_md: str) -> GateResult:
    if has_pii(pulse_md):
        return GateResult("pii", False, "the PII scrubber would change the pulse; it contains personal data")
    return GateResult("pii", True)


def run_gates(
    *, analysis: dict, all_themes: list[dict], reviews: list[dict], pulse_md: str, max_words: int,
    max_themes: int = 5,
) -> list[GateResult]:
    results = [
        schema_gate(analysis),
        theme_cap_gate(all_themes, analysis.get("top_themes", []), max_themes),
        counts_gate(analysis.get("quotes", []), analysis.get("actions", [])),
    ]
    if results[0].passed:
        results.append(verbatim_gate(analysis["quotes"], {r["id"]: r for r in reviews}, pulse_md))
    else:
        results.append(GateResult("verbatim", False, "skipped: schema gate failed"))
    results += [word_count_gate(pulse_md, max_words), pii_gate(pulse_md)]
    return results


def low_sample_warning(n_reviews: int) -> str | None:
    if n_reviews < MIN_REVIEWS_WARNING:
        return f"only {n_reviews} reviews this run (fewer than {MIN_REVIEWS_WARNING}); treat the themes as indicative"
    return None
