"""Verbatim quote selection, one per top theme. Architecture §5.5."""
from __future__ import annotations

import logging
import re
import unicodedata

from pydantic import BaseModel

from ..llm import LLMClient, LLMError, load_prompt

log = logging.getLogger(__name__)

QUOTES_PROMPT = "quotes_v1"
CANDIDATES = 30
MAX_QUOTE_WORDS = 25
MAX_ATTEMPTS = 3        # first try plus two retries with the rejection reason fed back

_QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


class QuotePick(BaseModel):
    n: int
    quote: str


def normalize(text: str) -> str:
    """Case, whitespace and quote-style insensitive form used for the verbatim check."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_CHARS)
    return re.sub(r"\s+", " ", text).strip().casefold()


def clean_quote(quote: str) -> str:
    """Strip wrapping quote marks and ellipses the model may add around an excerpt."""
    quote = quote.strip().translate(_QUOTE_CHARS)
    quote = re.sub(r'^["\'\u2026.\s]+|["\'\u2026.\s]+$', "", quote)
    return quote.strip()


def check_quote(quote: str, source_text: str) -> str | None:
    """None when the quote is acceptable, else the reason it is not."""
    q = normalize(quote)
    if not q:
        return "the quote is empty"
    if len(q.split()) > MAX_QUOTE_WORDS:
        return f"the quote has {len(q.split())} words; the limit is {MAX_QUOTE_WORDS}"
    if q not in normalize(source_text):
        return "the quote is not an exact excerpt of that review's text (do not reword or fix typos)"
    return None


def pick_candidates(reviews: list[dict], limit: int = CANDIDATES) -> list[dict]:
    """Deterministic candidate set for one theme: negative reviews first, then by id."""
    return sorted(reviews, key=lambda r: (r["sentiment"] != "neg", r["rating"], r["id"]))[:limit]


def fallback_quote(candidates: list[dict]) -> tuple[dict, str]:
    """First sentence of the best candidate, cut to MAX_QUOTE_WORDS. Always verbatim."""
    review = candidates[0]
    text = review["text"].strip()
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    words = list(re.finditer(r"\S+", sentence))
    if len(words) > MAX_QUOTE_WORDS:
        sentence = sentence[:words[MAX_QUOTE_WORDS - 1].end()]
    return review, sentence


def select_quote(
    client: LLMClient, theme: dict, candidates: list[dict],
) -> dict:
    """Ask for one quote for `theme`; verify it; fall back to a safe excerpt after 3 bad tries."""
    system = load_prompt(QUOTES_PROMPT, max_words=MAX_QUOTE_WORDS)
    lines = "\n".join(f"{n} | {r['rating']} | {r['text']}" for n, r in enumerate(candidates, 1))
    base = (f"Theme: {theme['name']}: {theme['description']}\n\n"
            f"Reviews, one per line as `number | rating | text`:\n{lines}")
    user, problem = base, ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            pick = client.complete_json(system, user, QuotePick, temperature=0, label="quote")
        except LLMError as exc:
            problem = str(exc)
        else:
            if not 1 <= pick.n <= len(candidates):
                problem = f"review number {pick.n} is not in the list (1..{len(candidates)})"
            else:
                review = candidates[pick.n - 1]
                quote = clean_quote(pick.quote)
                problem = check_quote(quote, review["text"]) or ""
                if not problem:
                    return {"theme_id": theme["theme_id"], "review_id": review["id"],
                            "quote": quote, "source": "llm"}
        log.warning("quote rejected", extra={
            "stage": "quote", "theme": theme["theme_id"], "attempt": attempt, "problem": problem[:200]})
        user = f"{base}\n\n---\nYour previous answer was rejected: {problem}.\nTry again."
    review, quote = fallback_quote(candidates)
    return {"theme_id": theme["theme_id"], "review_id": review["id"],
            "quote": quote, "source": "fallback"}
