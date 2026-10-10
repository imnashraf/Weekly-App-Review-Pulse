"""Deterministic discovery sample: rating quotas, spread across weeks."""
from __future__ import annotations

import random
from collections import defaultdict

# Relative share of the sample per star rating. Polarized data (mostly 1★ and 5★) would starve
# 2-4★ under a proportional sample, so every rating gets a fixed share; 1★ gets the most because
# complaints carry the themes. With size=300 this gives 90 / 60 / 50 / 50 / 50.
RATING_WEIGHTS = {1: 9, 2: 6, 3: 5, 4: 5, 5: 5}


def rating_quotas(available: dict[int, int], size: int) -> dict[int, int]:
    """How many reviews to take per rating. A rating with fewer reviews than its quota gives all
    of them; the shortfall goes to whichever rating has the most reviews left."""
    total_weight = sum(RATING_WEIGHTS.values())
    take = {r: min(available.get(r, 0), size * w // total_weight) for r, w in RATING_WEIGHTS.items()}
    # flooring and short ratings both leave a gap: fill it from the ratings with most spare reviews
    while sum(take.values()) < size:
        spare = {r: available.get(r, 0) - take[r] for r in take}
        best = max(spare, key=lambda r: (spare[r], -r))
        if spare[best] <= 0:
            break
        take[best] += 1
    return take


def _spread_across_weeks(reviews: list[dict], n: int, rng: random.Random) -> list[dict]:
    by_week: dict[str, list[dict]] = defaultdict(list)
    for review in reviews:
        by_week[review["week"]].append(review)
    weeks = sorted(by_week)
    for week in weeks:
        rng.shuffle(by_week[week])
    rng.shuffle(weeks)  # so the partial last round does not always favour the same weeks
    picked: list[dict] = []
    while len(picked) < n and any(by_week.values()):
        for week in weeks:
            if by_week[week] and len(picked) < n:
                picked.append(by_week[week].pop())
    return picked


def stratified_sample(reviews: list[dict], size: int, seed: int = 0) -> list[dict]:
    """Up to `size` reviews, shuffled. Same input and seed always give the same sample."""
    if len(reviews) <= size:
        sample = list(reviews)
        random.Random(seed).shuffle(sample)
        return sample
    rng = random.Random(seed)
    by_rating: dict[int, list[dict]] = defaultdict(list)
    for review in reviews:
        by_rating[review["rating"]].append(review)
    quotas = rating_quotas({r: len(v) for r, v in by_rating.items()}, size)
    sample: list[dict] = []
    for rating in sorted(quotas):
        sample.extend(_spread_across_weeks(by_rating.get(rating, []), quotas[rating], rng))
    rng.shuffle(sample)
    return sample
