"""Run every stage in order, persist each one's output, and resume after a failure.

A stage counts as done when its final artifact exists, so re-running the same run id skips
finished work. `start_from` forces that stage and everything after it to run again.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .config import Settings
from .store import RunStore

log = logging.getLogger(__name__)

STAGES = ("ingest", "clean", "themes", "rank", "compose", "publish")


class StageError(RuntimeError):
    """A stage failed. `stage` says which, so the caller can print the resume command."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class StageResult:
    name: str
    status: str            # ran | skipped
    seconds: float = 0.0


def _publish_done(store: RunStore) -> bool:
    if not store.exists("run_report.json"):
        return False
    state = store.read_json("run_report.json").get("publish") or {}
    return state.get("doc", {}).get("status") == "done" and state.get("mail", {}).get("status") == "done"


def _compose_done(store: RunStore) -> bool:
    return store.exists("pulse.md") and store.exists("run_report.json") \
        and store.read_json("run_report.json").get("status") == "passed"


def is_done(stage: str, store: RunStore) -> bool:
    return {
        "ingest": lambda: store.exists("01_ingest_meta.json"),
        "clean": lambda: store.exists("02_clean_meta.json"),
        "themes": lambda: store.exists("04_label_stats.json"),
        "rank": lambda: store.exists("05_analysis.json"),
        "compose": lambda: _compose_done(store),
        "publish": lambda: _publish_done(store),
    }[stage]()


def _runners(settings: Settings, store: RunStore, dry_run: bool, retry_unknown: bool) -> dict[str, Callable[[], object]]:
    # Imported here so `--help` and config errors do not load every stage.
    from .analyze.insights import run_insights
    from .analyze.themes import run_themes
    from .clean import run_clean
    from .compose.composer import run_compose
    from .ingest import run_ingest
    from .integrations.publish import run_publish

    return {
        "ingest": lambda: run_ingest(settings, store),
        "clean": lambda: run_clean(settings, store),
        "themes": lambda: run_themes(settings, store),
        "rank": lambda: run_insights(settings, store),
        "compose": lambda: run_compose(settings, store),
        "publish": lambda: run_publish(settings, store, dry_run=dry_run, retry_unknown=retry_unknown),
    }


def run_all(
    settings: Settings, store: RunStore, *, start_from: str | None = None,
    dry_run: bool = False, retry_unknown: bool = False,
    runners: dict[str, Callable[[], object]] | None = None,
) -> list[StageResult]:
    """Run the stages in order. Raises StageError on the first failing stage."""
    if start_from is not None and start_from not in STAGES:
        raise ValueError(f"unknown stage {start_from!r}; choose one of {', '.join(STAGES)}")
    runners = runners or _runners(settings, store, dry_run, retry_unknown)
    rerun = False                          # once True, every later stage runs again
    results: list[StageResult] = []
    for stage in STAGES:
        if stage == start_from:
            rerun = True
        always = stage == "publish" and dry_run      # a dry run writes no state to skip on
        if not rerun and not always and is_done(stage, store):
            log.info("stage already done, skipping", extra={"stage": stage, "run_id": store.run_id})
            results.append(StageResult(stage, "skipped"))
            continue
        t0 = time.monotonic()
        log.info("stage start", extra={"stage": stage, "run_id": store.run_id})
        try:
            runners[stage]()
        except Exception as exc:           # every stage reports problems as exceptions
            raise StageError(stage, str(exc)) from exc
        results.append(StageResult(stage, "ran", round(time.monotonic() - t0, 1)))
        rerun = True                       # a stage that ran invalidates everything after it
    return results
