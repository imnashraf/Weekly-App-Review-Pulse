import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from pulse.__main__ import main
from pulse.analyze import themes as T
from pulse.analyze.themes import (
    ClassificationResult, DiscoveryResult, Label, Theme, classify_reviews, discover_themes,
    discovery_model, label_stats, run_themes,
)
from pulse.config import load_config
from pulse.llm import LLMClient, LLMError, ProviderResponse
from pulse.store import RunStore, previous_run_id

FIXTURES = Path(__file__).parent / "fixtures"
DISCOVERY = (FIXTURES / "discovery_response.json").read_text()


def fixture_reviews():
    return [json.loads(l) for l in (FIXTURES / "clean_reviews_60.jsonl").read_text().splitlines()]


def themes():
    return DiscoveryResult.model_validate_json(DISCOVERY).themes


KEYWORDS = [  # fake "LLM": first matching keyword wins
    ("T2", ("support", "ivr", "customer")), ("T3", ("kyc", "withdraw", "credited", "upi")),
    ("T4", ("brokerage", "charge", "interest", "exit load")),
    ("T1", ("lag", "froze", "chart", "slow", "execution", "update")),
    ("T5", ("please", "add", "option", "feature")),
]


def fake_labels(user: str) -> str:
    labels = []
    for line in user.split("Reviews:\n", 1)[1].splitlines():
        n, rating, text = line.split(" | ", 2)
        theme = next((t for t, kws in KEYWORDS if any(k in text.lower() for k in kws)), "other")
        sentiment = "neg" if int(rating) <= 2 else "pos" if int(rating) >= 4 else "neu"
        labels.append({"i": int(n), "theme_id": theme, "sentiment": sentiment})
    return json.dumps({"labels": labels})


class FakeProvider:
    """Answers discovery with the recorded fixture and classification with keyword rules.
    `tamper(user, text)` can corrupt a classification reply to exercise the re-ask paths."""

    def __init__(self, tamper=None, discovery=DISCOVERY):
        self.tamper, self.discovery = tamper, discovery
        self.calls = []

    def complete(self, system, user, temperature):
        self.calls.append((system, user, temperature))
        if "Themes:\n" in user:
            text = fake_labels(user)
            if self.tamper:
                text = self.tamper(user, text)
        else:
            text = self.discovery
        return ProviderResponse(text, 100, 20)


def client(provider=None):
    return LLMClient(provider or FakeProvider(), "fake-model")


# ---------------------------------------------------------------- schemas

def test_schema_rejects_more_than_five_themes():
    raw = json.loads(DISCOVERY)
    raw["themes"].append({**raw["themes"][0], "id": "T6"})
    with pytest.raises(ValidationError):
        DiscoveryResult.model_validate(raw)


def test_schema_rejects_empty_duplicate_and_bad_ids():
    with pytest.raises(ValidationError):
        DiscoveryResult.model_validate({"themes": []})
    raw = json.loads(DISCOVERY)
    raw["themes"][1]["id"] = "T1"
    with pytest.raises(ValidationError, match="unique"):
        DiscoveryResult.model_validate(raw)
    raw = json.loads(DISCOVERY)
    raw["themes"][0]["id"] = "other"
    with pytest.raises(ValidationError):
        DiscoveryResult.model_validate(raw)


def test_configured_cap_below_five_is_enforced():
    with pytest.raises(ValidationError):
        discovery_model(3).model_validate_json(DISCOVERY)
    assert len(discovery_model(5).model_validate_json(DISCOVERY).themes) == 5


def test_label_normalizes_theme_ids_and_rejects_bad_sentiment():
    assert Label(i=1, theme_id=" t2 ", sentiment="neg").theme_id == "T2"
    assert Label(i=1, theme_id="Other", sentiment="pos").theme_id == "other"
    with pytest.raises(ValidationError):
        Label(i=1, theme_id="T1", sentiment="angry")


# ---------------------------------------------------------------- discovery

def test_discovery_returns_themes_and_uses_temperature_zero():
    provider = FakeProvider()
    found = discover_themes(client(provider), fixture_reviews(), max_themes=5)
    assert [t.id for t in found] == ["T1", "T2", "T3", "T4", "T5"]
    system, user, temp = provider.calls[0]
    assert temp == 0 and "Never more than 5" in system and "60" in user.splitlines()[0]
    assert "last week" not in user


def test_discovery_includes_prior_themes_when_given():
    provider = FakeProvider()
    prior = [t.model_dump() for t in themes()[:2]]
    discover_themes(client(provider), fixture_reviews(), prior_themes=prior)
    assert "Themes found last week" in provider.calls[0][1] and "Customer support" in provider.calls[0][1]


def test_discovery_with_too_many_themes_retries_then_fails():
    raw = json.loads(DISCOVERY)
    raw["themes"].append({**raw["themes"][0], "id": "T6"})
    c = client(FakeProvider(discovery=json.dumps(raw)))
    with pytest.raises(LLMError, match="after 3 attempts"):
        discover_themes(c, fixture_reviews())
    assert c.calls == 3


# ------------------------------------------------------------ classification

def test_every_review_is_labeled_in_order():
    reviews = fixture_reviews()
    labeled, counters = classify_reviews(client(), reviews, themes(), batch_size=25)
    assert [r["id"] for r in labeled] == [r["id"] for r in reviews]
    assert all(r["theme_id"] in {"T1", "T2", "T3", "T4", "T5", "other"} for r in labeled)
    assert all(r["sentiment"] in {"neg", "neu", "pos"} for r in labeled)
    assert counters == {"batches": 3, "reask_batches": 0, "failed_calls": 0, "unresolved": 0}
    # original review fields survive
    assert labeled[0]["text"] == reviews[0]["text"] and labeled[0]["week"] == reviews[0]["week"]


def drop_indices(*drop, first_call_only=True):
    state = {"done": False}

    def tamper(user, text):
        if first_call_only and state["done"]:
            return text
        state["done"] = True
        data = json.loads(text)
        data["labels"] = [l for l in data["labels"] if l["i"] not in drop]
        return json.dumps(data)

    return tamper


def test_missing_labels_are_re_asked_in_a_smaller_batch():
    provider = FakeProvider(tamper=drop_indices(2, 5))
    labeled, counters = classify_reviews(client(provider), fixture_reviews()[:10], themes(), 10)
    assert counters["reask_batches"] == 1 and counters["unresolved"] == 0
    assert len(provider.calls) == 2
    reask_lines = provider.calls[1][1].split("Reviews:\n")[1].splitlines()
    assert len(reask_lines) == 2 and reask_lines[0].startswith("1 | ")   # renumbered from 1


def test_conflicting_duplicates_and_unknown_theme_ids_are_re_asked():
    def tamper(user, text):
        if "tampered" in user:
            return text
        data = json.loads(text)
        data["labels"] += [{"i": 1, "theme_id": "T5", "sentiment": "neu"}]   # conflicts with 1st
        data["labels"][2]["theme_id"] = "T9"                                    # unknown theme
        return json.dumps(data)

    first = {"n": 0}

    def once(user, text):
        first["n"] += 1
        return tamper(user, text) if first["n"] == 1 else text

    labeled, counters = classify_reviews(
        client(FakeProvider(tamper=once)), fixture_reviews()[:6], themes(), 6)
    assert counters["reask_batches"] == 1 and counters["unresolved"] == 0
    assert all(r["theme_id"] != "T9" for r in labeled)


def test_identical_duplicates_are_accepted():
    def tamper(user, text):
        data = json.loads(text)
        data["labels"].append(dict(data["labels"][0]))
        return json.dumps(data)

    _, counters = classify_reviews(client(FakeProvider(tamper=tamper)), fixture_reviews()[:5], themes(), 5)
    assert counters["reask_batches"] == 0


def test_unresolved_become_other_with_rating_based_sentiment():
    reviews = fixture_reviews()[:10]
    target = reviews[2]["text"]

    def tamper(user, text):                 # the model never labels this one review
        lines = user.split("Reviews:\n", 1)[1].splitlines()
        gone = {int(l.split(" | ")[0]) for l in lines if l.endswith(target)}
        data = json.loads(text)
        data["labels"] = [l for l in data["labels"] if l["i"] not in gone]
        return json.dumps(data)

    provider = FakeProvider(tamper=tamper)
    labeled, counters = classify_reviews(client(provider), reviews, themes(), 10)
    assert counters["unresolved"] == 1                    # 10% is under the failure limit
    bad = labeled[2]
    assert bad["theme_id"] == "other"
    assert bad["sentiment"] == ("neg" if bad["rating"] <= 2 else "neu" if bad["rating"] == 3 else "pos")


def test_failed_call_is_retried_in_smaller_batches():
    class Flaky(FakeProvider):
        def complete(self, system, user, temperature):
            if not self.calls:
                self.calls.append(None)
                raise LLMError("HTTP 503")
            return super().complete(system, user, temperature)

    labeled, counters = classify_reviews(client(Flaky()), fixture_reviews()[:20], themes(), 20)
    assert counters["failed_calls"] == 1 and counters["reask_batches"] == 2
    assert counters["unresolved"] == 0 and len(labeled) == 20


def test_mostly_unresolved_raises():
    class Down(FakeProvider):
        def complete(self, system, user, temperature):
            raise LLMError("HTTP 500")

    with pytest.raises(LLMError, match="classification failed"):
        classify_reviews(client(Down()), fixture_reviews()[:10], themes(), 10)


# -------------------------------------------------------------------- stats

def labeled_rows(spec):
    return [{"rating": r, "theme_id": t, "sentiment": s} for r, t, s in spec]


def test_stats_counts_shares_and_disagreements():
    ths = themes()[:2]
    rows = labeled_rows([(1, "T1", "neg")] * 6 + [(1, "T1", "pos")] + [(5, "T2", "neg")]
                        + [(5, "other", "pos")] * 2)
    stats = label_stats(rows, ths, {"unresolved": 0}, 0.40, {"calls": 1})
    assert stats["total"] == 10
    assert stats["themes"]["T1"]["count"] == 7 and stats["themes"]["T1"]["share"] == 0.7
    assert stats["other"] == {"count": 2, "share": 0.2}
    assert stats["rating_sentiment_disagreements"] == {
        "low_rating_labeled_pos": 1, "high_rating_labeled_neg": 1}
    assert stats["sentiment"] == {"neg": 7, "pos": 3}
    assert stats["warnings"] == []


def test_small_theme_warning():
    rows = labeled_rows([(1, "T1", "neg")] * 99 + [(1, "T2", "neg")])
    warnings = label_stats(rows, themes()[:2], {}, 0.40, {})["warnings"]
    assert len(warnings) == 1 and "T2" in warnings[0] and "1.0%" in warnings[0]


def test_other_share_warning_fires_above_threshold_only():
    rows = labeled_rows([(1, "T1", "neg")] * 5 + [(5, "other", "pos")] * 5)
    assert any("'other'" in w for w in label_stats(rows, themes()[:1], {}, 0.40, {})["warnings"])
    assert not label_stats(rows, themes()[:1], {}, 0.50, {})["warnings"]


# --------------------------------------------------------------------- stage

CONFIG = ("app: {google_play_id: x}\nllm: {provider: openai, model: fake}\n"
          "analysis: {sample_size: 30, batch_size: 20}\n"
          "mcp: {docs: {command: d}, gmail: {command: g}}\n")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    settings = load_config(cfg)
    settings.runs_dir = tmp_path / "runs"
    store = RunStore(settings.runs_dir, "2026-W41")
    store.write_jsonl(T.CLEAN_FILE, fixture_reviews())
    return cfg, settings, store


def test_run_themes_writes_all_outputs(setup):
    _, settings, store = setup
    stats = run_themes(settings, store, client())
    reviews = fixture_reviews()

    meta = store.read_json(T.THEMES_FILE)
    assert len(meta["themes"]) <= 5 and meta["sample_size"] == 30 and len(meta["sample_ids"]) == 30
    assert meta["prompt_versions"] == {"discovery": "discovery_v1", "classification": "classification_v1"}
    assert set(meta["sample_ids"]) <= {r["id"] for r in reviews}

    labeled = list(store.read_jsonl(T.LABELED_FILE))
    assert [r["id"] for r in labeled] == [r["id"] for r in reviews]      # every review labeled
    assert all(r["theme_id"] and r["sentiment"] for r in labeled)
    assert store.read_json(T.STATS_FILE) == stats
    assert stats["total"] == 60 and stats["llm"]["calls"] == 1 + 3         # discovery + 3 batches
    assert stats["classification"]["unresolved"] == 0


def test_run_themes_passes_previous_weeks_themes(setup):
    _, settings, store = setup
    prev = RunStore(settings.runs_dir, previous_run_id("2026-W41"))
    prev.write_json(T.THEMES_FILE, {"themes": [t.model_dump() for t in themes()]})
    provider = FakeProvider()
    run_themes(settings, store, client(provider))
    assert "Themes found last week" in provider.calls[0][1]


def test_run_themes_requires_clean_file(setup):
    _, settings, _ = setup
    with pytest.raises(RuntimeError, match="pulse clean"):
        run_themes(settings, RunStore(settings.runs_dir, "2026-W10"), client())


def test_previous_run_id_crosses_year_boundary():
    assert previous_run_id("2026-W41") == "2026-W40"
    assert previous_run_id("2026-W01") == "2025-W52"


def test_cli_themes_needs_api_key(setup, monkeypatch, capsys):
    cfg, _, _ = setup
    monkeypatch.delenv("LLM_API_KEY")
    assert main(["--config", str(cfg), "themes"]) == 1
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_cli_themes_end_to_end(setup, monkeypatch, capsys):
    cfg, settings, store = setup
    monkeypatch.setattr(T, "build_client", lambda s: client())
    monkeypatch.setattr("pulse.__main__.load_config", lambda *a, **k: settings)
    assert main(["--config", str(cfg), "themes", "--run", "2026-W41"]) == 0
    assert "Labeled 60 reviews" in capsys.readouterr().out
