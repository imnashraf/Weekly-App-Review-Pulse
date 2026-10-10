import json
from pathlib import Path

import pytest

from pulse.__main__ import main
from pulse.analyze.actions import actions_model, write_actions
from pulse.analyze.insights import ANALYSIS_FILE, run_insights
from pulse.analyze.quotes import (
    check_quote, fallback_quote, pick_candidates, select_quote,
)
from pulse.analyze.rank import rank_themes, top_themes, trend
from pulse.analyze.themes import classify_reviews
from pulse.config import load_config
from pulse.llm import LLMClient, ProviderResponse
from pulse.store import RunStore
from tests.test_themes import DISCOVERY, FakeProvider, fixture_reviews, themes

THEME_DOCS = [t.model_dump() for t in themes()]


def labeled_fixture():
    labeled, _ = classify_reviews(
        LLMClient(FakeProvider(), "fake"), fixture_reviews(), themes(), batch_size=30)
    return labeled


def row(tid, sentiment="neg", week="2026-W40", rating=1, i=0, text="some review text here"):
    return {"id": f"{tid}{sentiment}{week}{i}", "theme_id": tid, "sentiment": sentiment,
            "week": week, "rating": rating, "text": text}


# ------------------------------------------------------------------ ranking

def test_score_uses_volume_and_negativity():
    reviews = ([row("T1", "neg", i=i) for i in range(10)]                 # 10 x all negative
               + [row("T2", "pos", i=i) for i in range(12)]                # 12 x all positive
               + [row("other", "pos", i=i) for i in range(30)])
    ranked = rank_themes(reviews, THEME_DOCS[:2])
    assert [r["theme_id"] for r in ranked] == ["T1", "T2"]    # 10*1.5=15 beats 12*0.5=6
    assert ranked[0]["score"] == 15.0 and ranked[1]["score"] == 6.0
    assert ranked[0]["rank"] == 1 and ranked[0]["neg_share"] == 1.0


def test_other_is_never_ranked_and_ties_go_to_lower_id():
    reviews = [row("T2", i=i) for i in range(5)] + [row("T1", i=i) for i in range(5)]
    ranked = rank_themes(reviews, THEME_DOCS[:2] + [{"id": "other", "name": "o", "description": "d"}])
    assert [r["theme_id"] for r in ranked] == ["T1", "T2"]


def test_trend_compares_recent_weeks_with_earlier_ones():
    weeks = ["2026-W36", "2026-W37", "2026-W38", "2026-W39", "2026-W40", "2026-W41"]
    reviews = []
    for w in weeks[:2]:                       # earlier: T1 is 0 of 2
        reviews += [row("other", week=w, i=1), row("other", week=w, i=2)]
    for w in weeks[2:]:                       # recent: T1 is 1 of 2
        reviews += [row("T1", week=w), row("other", week=w, i=9)]
    assert trend(reviews, "T1") == 0.5
    assert trend(reviews[:4], "T1") == 0.0    # too few weeks: no trend


def test_rising_trend_bonus_is_capped_at_ten_percent():
    reviews = [row("other", week=f"2026-W{30 + n}", i=1) for n in range(6)]
    reviews += [row("T1", week=f"2026-W{36 + n}", i=n) for n in range(3)]
    ranked = rank_themes(reviews, THEME_DOCS[:1])
    assert ranked[0]["trend"] > 0.10
    assert ranked[0]["score"] == round(3 * 1.5 * 1.10, 2)


def test_ranking_snapshot_on_fixture():
    ranked = rank_themes(labeled_fixture(), THEME_DOCS)
    assert [r["rank"] for r in ranked] == [1, 2, 3, 4, 5]
    assert ranked == sorted(ranked, key=lambda r: (-r["score"], r["theme_id"]))
    assert sum(r["volume"] for r in ranked) <= 60


def test_top_themes_needs_three_with_reviews():
    ranked = rank_themes([row("T1"), row("T2")], THEME_DOCS)
    with pytest.raises(RuntimeError, match="needs 3"):
        top_themes(ranked)


# ------------------------------------------------------------------- quotes

SOURCE = "The app   keeps freezing at 9:15.  Orders get stuck, and support doesn’t reply."


@pytest.mark.parametrize("quote", [
    "The app keeps freezing at 9:15.",                 # exact
    "the app   KEEPS freezing at 9:15",                # whitespace and case normalized
    "support doesn't reply",                           # curly vs straight apostrophe
])
def test_verbatim_quotes_pass(quote):
    assert check_quote(quote, SOURCE) is None


@pytest.mark.parametrize("quote", [
    "The app often freezes at 9:15",                   # reworded
    "The app keeps freezing. Orders get stuck",        # two parts joined (gap removed)
    "",
    " ".join(["word"] * 26),
])
def test_reworded_empty_or_long_quotes_fail(quote):
    assert check_quote(quote, SOURCE) is not None


class QuoteProvider:
    """Replies to quote prompts with a scripted list of answers, then repeats the last."""
    def __init__(self, *answers):
        self.answers, self.users = list(answers), []

    def complete(self, system, user, temperature):
        self.users.append(user)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return ProviderResponse(json.dumps(a), 10, 5)


CANDS = [
    {"id": "a1", "rating": 1, "sentiment": "neg", "text": "Customer support never replies to my tickets. Very bad."},
    {"id": "a2", "rating": 2, "sentiment": "neg", "text": "Waited two weeks and nobody called me back at all."},
]
THEME = {"theme_id": "T2", "name": "Customer support", "description": "Support is slow."}


def test_valid_quote_is_accepted_and_maps_back_to_review():
    p = QuoteProvider({"n": 2, "quote": "nobody called me back at all"})
    q = select_quote(LLMClient(p, "m"), THEME, CANDS)
    assert q == {"theme_id": "T2", "review_id": "a2", "quote": "nobody called me back at all", "source": "llm"}


def test_reworded_quote_is_retried_with_reason_then_accepted():
    p = QuoteProvider({"n": 1, "quote": "support never answers tickets"},
                      {"n": 1, "quote": "Customer support never replies to my tickets."})
    q = select_quote(LLMClient(p, "m"), THEME, CANDS)
    assert q["source"] == "llm" and q["quote"].endswith("tickets")
    assert len(p.users) == 2 and "not an exact excerpt" in p.users[1]


def test_three_bad_answers_fall_back_to_first_sentence():
    p = QuoteProvider({"n": 1, "quote": "totally made up"})
    q = select_quote(LLMClient(p, "m"), THEME, CANDS)
    assert q["source"] == "fallback" and q["review_id"] == "a1"
    assert q["quote"] == "Customer support never replies to my tickets."
    assert len(p.users) == 3


def test_out_of_range_choice_is_rejected():
    p = QuoteProvider({"n": 9, "quote": "x"}, {"n": 1, "quote": "Very bad"})
    assert select_quote(LLMClient(p, "m"), THEME, CANDS)["review_id"] == "a1"


def test_fallback_is_capped_and_still_verbatim():
    text = "word " * 40 + "end."
    review, quote = fallback_quote([{"id": "x", "text": text}])
    assert len(quote.split()) == 25 and check_quote(quote, text) is None


def test_candidates_prefer_negative_and_are_deterministic():
    rows = [row("T1", "pos", rating=5, i=1), row("T1", "neg", rating=2, i=2), row("T1", "neg", rating=1, i=3)]
    picked = pick_candidates(rows, limit=2)
    assert [r["rating"] for r in picked] == [1, 2]
    assert picked == pick_candidates(list(reversed(rows)), limit=2)


# ------------------------------------------------------------------ actions

TOP = [{"theme_id": t, "name": f"Theme {t}", "description": "d", "volume": 10, "neg_share": 0.5}
       for t in ("T1", "T2", "T3")]
QUOTES = [{"theme_id": t["theme_id"], "quote": "q"} for t in TOP]


def acts(*ids, owner="Eng", text="Investigate the root cause"):
    return {"actions": [{"theme_id": i, "action": text, "owner_hint": owner} for i in ids]}


def test_actions_schema_requires_exactly_one_per_top_theme():
    model = actions_model(["T1", "T2", "T3"])
    model.model_validate(acts("T1", "T2", "T3"))
    for bad in (acts("T1", "T2"), acts("T1", "T2", "T2"), acts("T1", "T2", "T4"),
                acts("T1", "T2", "T3", "T1"), acts("T1", "T2", "T3", owner="Legal"),
                acts("T1", "T2", "T3", text="word " * 26)):
        with pytest.raises(Exception):
            model.model_validate(bad)


def test_actions_are_returned_in_theme_rank_order_and_linked():
    p = QuoteProvider(acts("T3", "T1", "T2"))
    out = write_actions(LLMClient(p, "m"), TOP, QUOTES)
    assert [a["theme_id"] for a in out] == ["T1", "T2", "T3"]
    assert "T1 Theme T1" in p.users[0]


# -------------------------------------------------------------------- stage

class StageProvider:
    def complete(self, system, user, temperature):
        if user.startswith("Top themes:"):
            ids = [l.split()[0] for l in user.splitlines()[1:] if l[:1] == "T"]
            return ProviderResponse(json.dumps(acts(*ids)), 10, 5)
        first = next(l for l in user.splitlines() if " | " in l)
        text = first.split(" | ", 2)[2]
        return ProviderResponse(json.dumps({"n": 1, "quote": " ".join(text.split()[:8])}), 10, 5)


def make_settings(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(Path("config.yaml").read_text())
    s = load_config(cfg)
    return s.model_copy(update={"runs_dir": tmp_path / "runs"})


def test_stage_writes_analysis_with_three_themes_quotes_actions(tmp_path):
    s = make_settings(tmp_path)
    store = RunStore(s.runs_dir, "2026-W41")
    labeled = labeled_fixture()
    store.write_jsonl("04_labeled_reviews.jsonl", labeled)
    store.write_json("03_themes.json", {"themes": THEME_DOCS})
    by_id = {r["id"]: r for r in labeled}

    a = run_insights(s, store, LLMClient(StageProvider(), "fake"))
    saved = store.read_json(ANALYSIS_FILE)
    assert saved == a
    assert len(a["top_themes"]) == len(a["quotes"]) == len(a["actions"]) == 3
    assert [q["theme_id"] for q in a["quotes"]] == [t["theme_id"] for t in a["top_themes"]]
    assert {x["theme_id"] for x in a["actions"]} == {t["theme_id"] for t in a["top_themes"]}
    for q in a["quotes"]:                      # verbatim, anonymous, from the right theme
        review = by_id[q["review_id"]]
        assert review["theme_id"] == q["theme_id"]
        assert check_quote(q["quote"], review["text"]) is None
        assert set(q) == {"theme_id", "review_id", "quote", "source"}


def test_stage_needs_labeled_reviews(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(RuntimeError, match="python -m pulse themes"):
        run_insights(s, RunStore(s.runs_dir, "2026-W41"), LLMClient(StageProvider(), "fake"))


def test_rank_cli_requires_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr("pulse.config.load_dotenv", lambda *a, **k: None)   # ignore the real .env
    assert main(["rank", "--run", "2026-W41"]) == 1
    assert "LLM_API_KEY" in capsys.readouterr().err
