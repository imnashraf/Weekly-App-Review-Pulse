"""Google Play adapter: pages public reviews newest-first until the window is exhausted."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date, datetime
from typing import Any, Callable, Iterator

from .base import RawReview

log = logging.getLogger(__name__)

SOURCE = "google_play"
PAGE_SIZE = 200
MAX_ATTEMPTS = 5

# fetch_page(continuation_token) -> (raw review dicts, next continuation token or None)
FetchPage = Callable[[Any], tuple[list[dict], Any]]


def review_id(original_id: str) -> str:
    return hashlib.sha256(f"{SOURCE}:{original_id}".encode()).hexdigest()[:16]


def to_raw_review(item: dict) -> RawReview | None:
    """Keep only non-identifying fields. Author name, avatar and replies are dropped here."""
    at = item.get("at")
    text = (item.get("content") or "").strip()
    if not isinstance(at, datetime) or not text:
        return None
    return RawReview(
        id=review_id(str(item["reviewId"])),
        source=SOURCE,
        rating=int(item["score"]),
        text=text,
        date=at.date().isoformat(),
        app_version=item.get("appVersion") or item.get("reviewCreatedVersion") or None,
    )


def _default_fetch_page(app_id: str, lang: str, country: str) -> FetchPage:
    from google_play_scraper import Sort, reviews

    def fetch(token: Any) -> tuple[list[dict], Any]:
        return reviews(
            app_id, lang=lang, country=country, sort=Sort.NEWEST,
            count=PAGE_SIZE, continuation_token=token,
        )

    return fetch


class GooglePlayAdapter:
    source = SOURCE

    def __init__(
        self,
        app_id: str,
        lang: str = "en",
        country: str = "in",
        page_delay: float = 1.5,
        fetch_page: FetchPage | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fetch_page = fetch_page or _default_fetch_page(app_id, lang, country)
        self.page_delay = page_delay
        self.sleep = sleep
        self.pages_fetched = 0
        self.skipped_empty = 0

    def _fetch_with_backoff(self, token: Any) -> tuple[list[dict], Any]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self.fetch_page(token)
            except Exception as exc:  # network, throttling, scraper errors
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Google Play fetch failed after {MAX_ATTEMPTS} attempts: {exc}"
                    ) from exc
                delay = 2 ** attempt
                log.warning("page fetch failed, retrying", extra={
                    "attempt": attempt, "retry_in_s": delay, "error": str(exc)})
                self.sleep(delay)
        raise AssertionError("unreachable")

    def fetch(self, window_start: date, max_reviews: int) -> Iterator[RawReview]:
        token: Any = None
        yielded = 0
        while yielded < max_reviews:
            items, token = self._fetch_with_backoff(token)
            self.pages_fetched += 1
            if not items:
                return
            reached_window_end = False
            for item in items:
                review = to_raw_review(item)
                if review is None:
                    self.skipped_empty += 1
                    continue
                if date.fromisoformat(review["date"]) < window_start:
                    reached_window_end = True
                    continue
                yield review
                yielded += 1
                if yielded >= max_reviews:
                    return
            # Newest-first ordering: once a page reaches past the window, later pages are older.
            if reached_window_end or token is None:
                return
            self.sleep(self.page_delay)
