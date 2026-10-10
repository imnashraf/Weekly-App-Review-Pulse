"""Ingestion stage: fetch raw reviews for the window and write 01_raw_reviews.jsonl."""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from ..config import Settings
from ..store import RunStore
from .base import SourceAdapter
from .google_play import GooglePlayAdapter

log = logging.getLogger(__name__)

RAW_FILE = "01_raw_reviews.jsonl"
META_FILE = "01_ingest_meta.json"


def window_start(run_date: date, weeks: int) -> date:
    return run_date - timedelta(weeks=weeks)


def run_ingest(
    settings: Settings,
    store: RunStore,
    adapter: SourceAdapter | None = None,
    run_date: date | None = None,
) -> dict:
    run_date = run_date or date.today()
    start = window_start(run_date, settings.window.weeks)
    adapter = adapter or GooglePlayAdapter(
        settings.app.google_play_id, settings.app.language, settings.app.country
    )
    cap = settings.limits.max_reviews_per_source

    t0 = time.monotonic()
    reviews = list(adapter.fetch(start, cap))
    store.write_jsonl(RAW_FILE, reviews)

    dates = [r["date"] for r in reviews]
    meta = {
        "source": adapter.source,
        "run_date": run_date.isoformat(),
        "window_start": start.isoformat(),
        "window_weeks": settings.window.weeks,
        "review_count": len(reviews),
        "oldest_review": min(dates) if dates else None,
        "newest_review": max(dates) if dates else None,
        "hit_cap": len(reviews) >= cap,
        "pages_fetched": getattr(adapter, "pages_fetched", None),
        "skipped_empty": getattr(adapter, "skipped_empty", None),
    }
    store.write_json(META_FILE, meta)
    log.info("ingest complete", extra={
        "stage": "ingest", "duration_s": round(time.monotonic() - t0, 1), **meta})
    if meta["hit_cap"]:
        log.warning("review cap reached; older reviews in the window were not fetched",
                    extra={"stage": "ingest", "cap": cap})
    return meta
