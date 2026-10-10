"""Stage: rank themes, pick quotes, write actions -> 05_analysis.json."""
from __future__ import annotations

import logging
import time

from ..config import Settings
from ..llm import LLMClient, build_client
from ..store import RunStore
from . import actions as actions_mod
from . import quotes as quotes_mod
from .rank import rank_themes, top_themes
from .themes import LABELED_FILE, THEMES_FILE

log = logging.getLogger(__name__)

ANALYSIS_FILE = "05_analysis.json"


def run_insights(settings: Settings, store: RunStore, client: LLMClient | None = None) -> dict:
    for name in (LABELED_FILE, THEMES_FILE):
        if not store.exists(name):
            raise RuntimeError(
                f"{name} not found for run {store.run_id}; run `python -m pulse themes` first")
    labeled = list(store.read_jsonl(LABELED_FILE))
    if not labeled:
        raise RuntimeError(f"{LABELED_FILE} is empty; nothing to analyze")
    themes_doc = store.read_json(THEMES_FILE)
    client = client or build_client(settings)
    t0 = time.monotonic()

    ranked = rank_themes(labeled, themes_doc["themes"])
    top = top_themes(ranked)

    quotes, warnings = [], []
    for theme in top:
        members = [r for r in labeled if r["theme_id"] == theme["theme_id"]]
        quote = quotes_mod.select_quote(client, theme, quotes_mod.pick_candidates(members))
        if quote["source"] == "fallback":
            warnings.append(
                f"no valid LLM quote for {theme['theme_id']}; used the first sentence of a review")
        quotes.append(quote)

    actions = actions_mod.write_actions(client, top, quotes)
    for text in warnings:
        log.warning(text, extra={"stage": "insights"})

    analysis = {
        "run_id": store.run_id,
        "prompt_versions": {"quotes": quotes_mod.QUOTES_PROMPT, "actions": actions_mod.ACTIONS_PROMPT},
        "review_count": len(labeled),
        "ranking": ranked,
        "top_themes": top,
        "quotes": quotes,
        "actions": actions,
        "warnings": warnings,
        "llm": client.usage(),
    }
    store.write_json(ANALYSIS_FILE, analysis)
    log.info("insights complete", extra={
        "stage": "insights", "duration_s": round(time.monotonic() - t0, 1),
        "top": [t["theme_id"] for t in top], **client.usage()})
    return analysis
