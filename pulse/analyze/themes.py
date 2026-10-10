"""Theme discovery (Pass A) and review classification (Pass B). Architecture §5.3."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..config import Settings
from ..llm import LLMClient, LLMError, build_client, load_prompt
from ..store import RunStore, previous_run_id
from .sample import stratified_sample

log = logging.getLogger(__name__)

CLEAN_FILE = "02_clean_reviews.jsonl"
THEMES_FILE = "03_themes.json"
LABELED_FILE = "04_labeled_reviews.jsonl"
STATS_FILE = "04_label_stats.json"

DISCOVERY_PROMPT = "discovery_v1"
CLASSIFICATION_PROMPT = "classification_v1"
OTHER = "other"
SMALL_THEME_SHARE = 0.03
MAX_UNRESOLVED_SHARE = 0.20   # beyond this, labeling is considered failed rather than degraded


# ---------------------------------------------------------------------------- schemas

class Theme(BaseModel):
    id: str = Field(pattern=r"^T\d{1,2}$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    include_cues: list[str] = Field(default_factory=list)


class DiscoveryResult(BaseModel):
    themes: list[Theme] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [t.id for t in self.themes]
        if len(set(ids)) != len(ids):
            raise ValueError(f"theme ids must be unique, got {ids}")
        return self


def discovery_model(max_themes: int) -> type[DiscoveryResult]:
    """DiscoveryResult with the configured theme cap (never above the hard cap of 5)."""
    class _Discovery(DiscoveryResult):
        themes: list[Theme] = Field(min_length=1, max_length=min(max_themes, 5))
    return _Discovery


class Label(BaseModel):
    i: int
    theme_id: str
    sentiment: Literal["neg", "neu", "pos"]

    @field_validator("theme_id")
    @classmethod
    def _tidy(cls, value: str) -> str:
        value = value.strip()
        return OTHER if value.lower() == OTHER else value.upper()


class ClassificationResult(BaseModel):
    labels: list[Label]


# ------------------------------------------------------------------- Pass A: discovery

def _review_lines(reviews: list[dict]) -> str:
    return "\n".join(f"{r['rating']} | {r['text']}" for r in reviews)


def discover_themes(
    client: LLMClient,
    sample: list[dict],
    max_themes: int = 5,
    prior_themes: list[dict] | None = None,
) -> list[Theme]:
    system = load_prompt(DISCOVERY_PROMPT, max_themes=max_themes)
    user = f"Reviews ({len(sample)}), one per line as `rating | text`:\n{_review_lines(sample)}"
    if prior_themes:
        prior = "\n".join(f"- {t['id']} {t['name']}: {t['description']}" for t in prior_themes)
        user += (
            "\n\nThemes found last week. Keep a theme (same name) if this week's reviews still "
            f"show it, rename or drop it if not, and add new ones if needed:\n{prior}"
        )
    result = client.complete_json(
        system, user, discovery_model(max_themes), temperature=0, label="discovery")
    return result.themes


# --------------------------------------------------------------- Pass B: classification

def _theme_block(themes: list[Theme]) -> str:
    lines = []
    for t in themes:
        cues = f" (cues: {', '.join(t.include_cues)})" if t.include_cues else ""
        lines.append(f"{t.id} {t.name}: {t.description}{cues}")
    lines.append(f"{OTHER}: none of the above, praise only, or too vague")
    return "\n".join(lines)


def _sentiment_from_rating(rating: int) -> str:
    return "neg" if rating <= 2 else "neu" if rating == 3 else "pos"


def _label_batch(
    client: LLMClient, system: str, theme_block: str, batch: list[dict], valid_ids: set[str],
) -> dict[int, tuple[str, str]]:
    """Label one batch. Returns {1-based position in batch: (theme_id, sentiment)} for the
    reviews that got exactly one valid label; the rest are left for the caller to re-ask."""
    lines = "\n".join(f"{n} | {r['rating']} | {r['text']}" for n, r in enumerate(batch, 1))
    user = f"Themes:\n{theme_block}\n\nReviews:\n{lines}"
    result = client.complete_json(system, user, ClassificationResult, temperature=0, label="classify")

    seen: dict[int, set[tuple[str, str]]] = {}
    for label in result.labels:
        if 1 <= label.i <= len(batch) and label.theme_id in valid_ids:
            seen.setdefault(label.i, set()).add((label.theme_id, label.sentiment))
        elif 1 <= label.i <= len(batch):
            seen.setdefault(label.i, set()).add(("?", "?"))   # unknown theme id: invalid
    # accepted only if there is a single consistent valid answer (duplicates must agree)
    return {i: next(iter(v)) for i, v in seen.items() if len(v) == 1 and ("?", "?") not in v}


def classify_reviews(
    client: LLMClient, reviews: list[dict], themes: list[Theme], batch_size: int,
) -> tuple[list[dict], dict]:
    """Label every review. Returns (labeled reviews in input order, counters)."""
    system = load_prompt(CLASSIFICATION_PROMPT)
    theme_block = _theme_block(themes)
    valid_ids = {t.id for t in themes} | {OTHER}
    labels: dict[int, tuple[str, str]] = {}        # position in `reviews` -> label
    counters = {"batches": 0, "reask_batches": 0, "failed_calls": 0, "unresolved": 0}

    def run(positions: list[int]) -> None:
        batch = [reviews[p] for p in positions]
        try:
            got = _label_batch(client, system, theme_block, batch, valid_ids)
        except LLMError as exc:
            counters["failed_calls"] += 1
            log.warning("classification call failed", extra={"stage": "classify", "error": str(exc)})
            return
        for n, label in got.items():
            labels[positions[n - 1]] = label

    reask_size = max(1, batch_size // 2)
    for start in range(0, len(reviews), batch_size):
        positions = list(range(start, min(start + batch_size, len(reviews))))
        counters["batches"] += 1
        run(positions)
        missing = [p for p in positions if p not in labels]
        for k in range(0, len(missing), reask_size):
            counters["reask_batches"] += 1
            run(missing[k:k + reask_size])

    labeled = []
    for pos, review in enumerate(reviews):
        if pos in labels:
            theme_id, sentiment = labels[pos]
        else:
            counters["unresolved"] += 1
            theme_id, sentiment = OTHER, _sentiment_from_rating(review["rating"])
        labeled.append({**review, "theme_id": theme_id, "sentiment": sentiment})

    if reviews and counters["unresolved"] / len(reviews) > MAX_UNRESOLVED_SHARE:
        raise LLMError(
            f"classification failed for {counters['unresolved']} of {len(reviews)} reviews "
            f"({counters['failed_calls']} failed calls); check the provider and model settings")
    return labeled, counters


# ------------------------------------------------------------------------------ stats

def label_stats(
    labeled: list[dict], themes: list[Theme], counters: dict, max_other_share: float, usage: dict,
) -> dict:
    total = len(labeled)
    counts = Counter(r["theme_id"] for r in labeled)
    by_theme = {
        t.id: {"name": t.name, "count": counts.get(t.id, 0),
               "share": round(counts.get(t.id, 0) / total, 4) if total else 0.0}
        for t in themes
    }
    other_share = round(counts.get(OTHER, 0) / total, 4) if total else 0.0
    warnings = []
    if other_share > max_other_share:
        warnings.append(
            f"{other_share:.0%} of reviews are labeled 'other' (limit {max_other_share:.0%}); "
            "the themes may miss a big topic")
    for theme_id, info in by_theme.items():
        if total and info["share"] < SMALL_THEME_SHARE:
            warnings.append(
                f"theme {theme_id} '{info['name']}' covers only {info['share']:.1%} of reviews; "
                "consider merging it into another")
    for text in warnings:
        log.warning(text, extra={"stage": "themes"})
    return {
        "total": total,
        "themes": by_theme,
        "other": {"count": counts.get(OTHER, 0), "share": other_share},
        "sentiment": dict(Counter(r["sentiment"] for r in labeled)),
        "rating_sentiment_disagreements": {
            "low_rating_labeled_pos": sum(r["rating"] <= 2 and r["sentiment"] == "pos" for r in labeled),
            "high_rating_labeled_neg": sum(r["rating"] >= 4 and r["sentiment"] == "neg" for r in labeled),
        },
        "classification": counters,
        "llm": usage,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- stage

def _prior_themes(settings: Settings, store: RunStore) -> list[dict] | None:
    path = settings.runs_dir / previous_run_id(store.run_id) / THEMES_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text()).get("themes")


def run_themes(settings: Settings, store: RunStore, client: LLMClient | None = None) -> dict:
    if not store.exists(CLEAN_FILE):
        raise RuntimeError(
            f"{CLEAN_FILE} not found for run {store.run_id}; run `python -m pulse clean` first")
    reviews = list(store.read_jsonl(CLEAN_FILE))
    if not reviews:
        raise RuntimeError(f"{CLEAN_FILE} is empty; nothing to analyze")
    client = client or build_client(settings)
    cfg = settings.analysis
    t0 = time.monotonic()

    sample = stratified_sample(reviews, cfg.sample_size)
    themes = discover_themes(
        client, sample, settings.limits.max_themes, _prior_themes(settings, store))
    log.info("themes discovered", extra={
        "stage": "themes", "themes": [t.name for t in themes], "sample_size": len(sample)})
    store.write_json(THEMES_FILE, {
        "run_id": store.run_id,
        "prompt_versions": {"discovery": DISCOVERY_PROMPT, "classification": CLASSIFICATION_PROMPT},
        "provider": settings.llm.provider,
        "model": client.model,
        "sample_size": len(sample),
        "sample_ids": [r["id"] for r in sample],
        "themes": [t.model_dump() for t in themes],
    })

    labeled, counters = classify_reviews(client, reviews, themes, cfg.batch_size)
    store.write_jsonl(LABELED_FILE, labeled)
    stats = label_stats(labeled, themes, counters, cfg.max_other_share, client.usage())
    store.write_json(STATS_FILE, stats)
    log.info("themes complete", extra={
        "stage": "themes", "duration_s": round(time.monotonic() - t0, 1),
        "reviews": len(labeled), "other_share": stats["other"]["share"], **client.usage()})
    return stats
