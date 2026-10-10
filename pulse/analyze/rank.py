"""Deterministic theme ranking. Architecture §5.4."""
from __future__ import annotations

from collections import Counter

from .themes import OTHER

TOP_N = 3
RECENT_WEEKS = 4        # trend compares the newest weeks with the weeks before them
MAX_TREND_BONUS = 0.10  # a rising theme can gain at most 10% on its score


def _share_by_group(reviews: list[dict], theme_id: str) -> float:
    return sum(r["theme_id"] == theme_id for r in reviews) / len(reviews) if reviews else 0.0


def trend(labeled: list[dict], theme_id: str) -> float:
    """Change in the theme's share of reviews: newest RECENT_WEEKS weeks minus the earlier weeks.

    Shares, not counts, so partial weeks and busy weeks do not distort it. 0.0 when the data
    does not cover more than RECENT_WEEKS weeks.
    """
    weeks = sorted({r["week"] for r in labeled})
    if len(weeks) <= RECENT_WEEKS:
        return 0.0
    recent_weeks = set(weeks[-RECENT_WEEKS:])
    recent = [r for r in labeled if r["week"] in recent_weeks]
    earlier = [r for r in labeled if r["week"] not in recent_weeks]
    return round(_share_by_group(recent, theme_id) - _share_by_group(earlier, theme_id), 4)


def rank_themes(labeled: list[dict], themes: list[dict]) -> list[dict]:
    """Score every theme (not `other`) and return them best first.

    score = volume * (0.5 + neg_share) * (1 + trend bonus), bonus = clamp(trend, 0, 0.10).
    Ties go to the lower theme id so the order is stable.
    """
    counts = Counter(r["theme_id"] for r in labeled)
    neg = Counter(r["theme_id"] for r in labeled if r["sentiment"] == "neg")
    total = len(labeled)
    ranked = []
    for theme in themes:
        tid = theme["id"]
        if tid == OTHER:
            continue
        volume = counts.get(tid, 0)
        neg_share = neg.get(tid, 0) / volume if volume else 0.0
        t = trend(labeled, tid)
        bonus = min(MAX_TREND_BONUS, max(0.0, t))
        ranked.append({
            "theme_id": tid,
            "name": theme["name"],
            "description": theme["description"],
            "volume": volume,
            "share": round(volume / total, 4) if total else 0.0,
            "neg_share": round(neg_share, 4),
            "trend": t,
            "score": round(volume * (0.5 + neg_share) * (1 + bonus), 2),
        })
    ranked.sort(key=lambda r: (-r["score"], r["theme_id"]))
    for position, row in enumerate(ranked, 1):
        row["rank"] = position
    return ranked


def top_themes(ranked: list[dict], n: int = TOP_N) -> list[dict]:
    top = [r for r in ranked if r["volume"] > 0][:n]
    if len(top) < n:
        raise RuntimeError(
            f"only {len(top)} theme(s) have labeled reviews, but the pulse needs {n}; "
            "re-run `python -m pulse themes` or check the theme discovery output")
    return top
