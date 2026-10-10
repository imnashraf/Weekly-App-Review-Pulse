from collections import Counter

from pulse.analyze.sample import rating_quotas, stratified_sample


def make(n_by_rating, weeks=("2026-W38", "2026-W39", "2026-W40")):
    rows = []
    for rating, n in n_by_rating.items():
        for i in range(n):
            rows.append({"id": f"{rating}-{i}", "rating": rating, "week": weeks[i % len(weeks)],
                         "text": "x"})
    return rows


def test_quotas_for_300():
    q = rating_quotas({1: 664, 2: 96, 3: 140, 4: 150, 5: 538}, 300)
    assert q == {1: 90, 2: 60, 3: 50, 4: 50, 5: 50}


def test_short_rating_gives_all_and_shortfall_goes_to_largest():
    q = rating_quotas({1: 500, 2: 10, 3: 140, 4: 150, 5: 538}, 300)
    assert q[2] == 10 and sum(q.values()) == 300
    assert q[5] == 100 and q[1] == 90       # the 40-review gap went to 5 star, which has most spare


def test_quotas_never_exceed_availability():
    q = rating_quotas({1: 5, 2: 5, 3: 5, 4: 5, 5: 5}, 300)
    assert q == {1: 5, 2: 5, 3: 5, 4: 5, 5: 5}


def test_sample_uses_rating_quotas_not_proportions():
    rows = make({1: 664, 2: 96, 3: 140, 4: 150, 5: 538})
    sample = stratified_sample(rows, 300)
    assert len(sample) == 300 and len({r["id"] for r in sample}) == 300
    assert Counter(r["rating"] for r in sample) == {1: 90, 2: 60, 3: 50, 4: 50, 5: 50}


def test_sample_spreads_across_weeks():
    rows = make({1: 300, 5: 300}, weeks=("W1", "W2", "W3", "W4", "W5"))
    # skew: put most 1-star reviews in W1
    for r in rows:
        if r["rating"] == 1 and r["id"].endswith(("0", "1", "2", "3", "4", "5")):
            r["week"] = "W1"
    sample = [r for r in stratified_sample(rows, 100) if r["rating"] == 1]
    weeks = Counter(r["week"] for r in sample)
    assert len(weeks) == 5
    assert max(weeks.values()) - min(weeks.values()) <= 1 or min(weeks.values()) >= 5


def test_sample_is_deterministic_and_seed_sensitive():
    rows = make({1: 200, 2: 100, 3: 100, 4: 100, 5: 200})
    a = [r["id"] for r in stratified_sample(rows, 120, seed=1)]
    assert a == [r["id"] for r in stratified_sample(rows, 120, seed=1)]
    assert a != [r["id"] for r in stratified_sample(rows, 120, seed=2)]


def test_small_input_returns_everything():
    rows = make({1: 5, 5: 5})
    assert {r["id"] for r in stratified_sample(rows, 300)} == {r["id"] for r in rows}
