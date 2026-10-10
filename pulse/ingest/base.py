"""Source adapter interface, so other stores (e.g. App Store) can be added later."""
from __future__ import annotations

from datetime import date
from typing import Iterator, Protocol, TypedDict


class RawReview(TypedDict):
    """A review as stored in 01_raw_reviews.jsonl. Contains no reviewer identity."""

    id: str                  # sha256(source + original id), truncated
    source: str              # "google_play"
    rating: int              # 1-5
    text: str
    date: str                # YYYY-MM-DD
    app_version: str | None


class SourceAdapter(Protocol):
    source: str

    def fetch(self, window_start: date, max_reviews: int) -> Iterator[RawReview]:
        """Yield reviews dated on or after `window_start`, newest first, at most `max_reviews`."""
        ...
