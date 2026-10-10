"""Cleaning stage: normalize, scrub PII, filter, de-duplicate -> 02_clean_reviews.jsonl."""
from __future__ import annotations

import logging
import time
from collections import Counter

from ..config import Settings
from ..store import RunStore
from .dedupe import text_key
from .normalize import has_emoji, is_hinglish, is_latin_script, normalize, word_count
from .pii import scrub

log = logging.getLogger(__name__)

RAW_FILE = "01_raw_reviews.jsonl"
CLEAN_FILE = "02_clean_reviews.jsonl"
META_FILE = "02_clean_meta.json"


def clean_reviews(raw_reviews, min_words: int) -> tuple[list[dict], dict]:
    """Return (clean reviews, stats). Keeps English, emoji-free reviews of at least `min_words` words."""
    dropped: Counter = Counter()
    redactions: Counter = Counter()
    seen: set[str] = set()
    kept: list[dict] = []
    total = 0

    for raw in raw_reviews:
        total += 1
        review = normalize(raw, scrub(raw["text"], redactions))
        words = word_count(review["text"])
        if words == 0:
            dropped["empty"] += 1
        elif words < min_words:
            dropped["too_short"] += 1
        elif has_emoji(review["text"]):
            dropped["emoji"] += 1
        elif not is_latin_script(review["text"]):
            dropped["non_english_script"] += 1
        elif is_hinglish(review["text"]):
            dropped["hinglish"] += 1
        else:
            key = text_key(review["text"])
            if key in seen:
                dropped["duplicate"] += 1
            else:
                seen.add(key)
                kept.append(review)

    stats = {
        "input_count": total,
        "output_count": len(kept),
        "min_review_words": min_words,
        "dropped": {k: dropped.get(k, 0) for k in
                    ("empty", "too_short", "emoji", "non_english_script", "hinglish", "duplicate")},
        "pii_redactions": dict(sorted(redactions.items())),
    }
    return kept, stats


def run_clean(settings: Settings, store: RunStore) -> dict:
    if not store.exists(RAW_FILE):
        raise RuntimeError(
            f"{RAW_FILE} not found for run {store.run_id}; run `python -m pulse ingest` first")
    t0 = time.monotonic()
    kept, stats = clean_reviews(store.read_jsonl(RAW_FILE), settings.limits.min_review_words)
    store.write_jsonl(CLEAN_FILE, kept)
    store.write_json(META_FILE, stats)
    log.info("clean complete", extra={
        "stage": "clean", "duration_s": round(time.monotonic() - t0, 1), **stats})
    return stats
