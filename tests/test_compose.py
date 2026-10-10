import json

import pytest

from pulse.analyze.insights import ANALYSIS_FILE, run_insights
from pulse.compose.composer import (
    PULSE_FILE, REPORT_FILE, fit_to_limit, one_liners_model, render, run_compose,
)
from pulse.llm import LLMClient, ProviderResponse
from pulse.store import RunStore
from pulse.validate.gates import (
    GateError, counts_gate, count_words, low_sample_warning, pii_gate, run_gates,
    schema_gate, theme_cap_gate, verbatim_gate, word_count_gate,
)
from tests.test_insights import (
    StageProvider, THEME_DOCS, labeled_fixture, make_settings,
)


# ------------------------------------------------------------------- gates

def test_count_words_ignores_markup():
    assert count_words("# Title here\n> \"two words\"\n1. **bold** — yes") == 7


def test_word_count_gate():
    assert word_count_gate("one two three", 3).passed
    assert not word_count_gate("one two three four", 3).passed


def test_pii_gate_blocks_email_and_passes_clean_text():
    assert pii_gate("Support never replies to tickets.").passed
    assert not pii_gate("mail me at someone@example.com").passed
    assert not pii_gate("call 9876543210 now").passed


def test_counts_and_theme_cap_gates():
    assert counts_gate([1, 2, 3], [1, 2, 3]).passed
    assert not counts_gate([1, 2], [1, 2, 3]).passed
    assert not counts_gate([1, 2, 3], [1, 2, 3, 4]).passed
    assert theme_cap_gate(THEME_DOCS, [1, 2, 3]).passed
    assert not theme_cap_gate(THEME_DOCS + [{}], [1, 2, 3]).passed      # 6 themes
    assert not theme_cap_gate(THEME_DOCS, [1, 2]).passed                # 2 in pulse


def test_verbatim_gate_pass_fail_and_missing_from_pulse():
    reviews = {"r1": {"id": "r1", "text": "Support never replies to my tickets."}}
    q = [{"theme_id": "T1", "review_id": "r1", "quote": "never replies to my tickets"}]
    assert verbatim_gate(q, reviews, '> "never replies to my tickets"').passed
    assert not verbatim_gate(q, reviews, "> something else").passed
    q[0]["quote"] = "always replies to my tickets"
    assert not verbatim_gate(q, reviews, '> "always replies to my tickets"').passed
    assert not verbatim_gate(q, {}, "x").passed


def test_schema_gate():
    ok = {"top_themes": [{"theme_id": "T1", "name": "n", "share": 0.1, "volume": 3}],
          "quotes": [{"theme_id": "T1", "review_id": "r", "quote": "q"}],
          "actions": [{"theme_id": "T1", "action": "a", "owner_hint": "Eng"}]}
    assert schema_gate(ok).passed
    assert not schema_gate({"top_themes": [{"theme_id": "T1"}], "quotes": [], "actions": []}).passed


def test_low_sample_warning():
    assert low_sample_warning(10) and low_sample_warning(50) is None


# ----------------------------------------------------------------- composer

class ComposeProvider(StageProvider):
    """Quotes/actions as in the stage test, plus one-liners. `long` makes drafts oversized."""
    def __init__(self, one_liner="Users report this problem often", action=None):
        self.one_liner, self.action, self.users = one_liner, action, []

    def complete(self, system, user, temperature):
        self.users.append(user)
        if "one-line summaries" in system:
            ids = [l.split()[0] for l in user.splitlines()[1:] if l[:1] == "T"]
            short = "Max 8" in user
            text = "Short summary" if "at most 8 words" in system else self.one_liner
            return ProviderResponse(json.dumps({"one_liners": [{"theme_id": i, "text": text} for i in ids]}), 5, 5)
        return super().complete(system, user, temperature)


@pytest.fixture
def analyzed(tmp_path):
    s = make_settings(tmp_path)
    store = RunStore(s.runs_dir, "2026-W41")
    store.write_jsonl("04_labeled_reviews.jsonl", labeled_fixture())
    store.write_json("03_themes.json", {"themes": THEME_DOCS, "prompt_versions": {"discovery": "d"}})
    store.write_json("01_ingest_meta.json", {"window_start": "2026-07-17", "newest_review": "2026-10-08",
                                             "window_weeks": 12, "review_count": 99})
    run_insights(s, store, LLMClient(StageProvider(), "fake"))
    return s, store


def test_compose_writes_valid_pulse_and_report(analyzed):
    s, store = analyzed
    report = run_compose(s, store, LLMClient(ComposeProvider(), "fake"))
    md = store.read_text(PULSE_FILE)
    assert md.startswith("# Groww App Review Pulse — Week of 5 Oct 2026")
    assert "17 Jul – 8 Oct" in md and "60 public reviews" in md
    assert md.count("\n> \"") == 3 and md.count("**") == 6
    assert "## Top themes" in md and "## What users said" in md and "## Action ideas" in md
    assert count_words(md) <= 250
    assert report["status"] == "passed" and all(g["passed"] for g in report["gates"])
    assert [g["name"] for g in report["gates"]] == [
        "schema", "theme_cap", "counts", "verbatim", "word_count", "pii"]
    assert report == store.read_json(REPORT_FILE)
    assert len(report["themes"]["discovered"]) == 5 and len(report["themes"]["in_pulse"]) == 3
    assert report["llm"]["calls"] >= 1 and report["doc_url"] is None


def test_oversized_pulse_is_shortened_by_llm(analyzed):
    s, store = analyzed
    long = " ".join(["word"] * 18)
    p = ComposeProvider(one_liner=long)
    s = s.model_copy(update={"limits": s.limits.model_copy(update={"pulse_max_words": 130})})
    report = run_compose(s, store, LLMClient(p, "fake"))
    assert "Short summary" in store.read_text(PULSE_FILE)
    assert report["pulse"]["word_remedies"] == ["asked the LLM to shorten the one-liners"]


def test_still_oversized_pulse_is_truncated_deterministically(analyzed):
    s, store = analyzed
    s = s.model_copy(update={"limits": s.limits.model_copy(update={"pulse_max_words": 100})})
    try:
        report = run_compose(s, store, LLMClient(ComposeProvider(), "fake"))
    except GateError:
        report = store.read_json(REPORT_FILE)
    assert "truncated one-liners and actions deterministically" in report["pulse"]["word_remedies"]


def test_impossible_word_limit_fails_without_pulse(analyzed):
    s, store = analyzed
    s = s.model_copy(update={"limits": s.limits.model_copy(update={"pulse_max_words": 20})})
    with pytest.raises(GateError, match="word_count"):
        run_compose(s, store, LLMClient(ComposeProvider(), "fake"))
    assert not store.exists(PULSE_FILE)
    assert store.read_json(REPORT_FILE)["status"] == "failed"


def test_pii_in_output_blocks_pulse(analyzed):
    s, store = analyzed
    p = ComposeProvider(one_liner="Contact helpdesk@example.com for help")
    with pytest.raises(GateError, match="pii"):
        run_compose(s, store, LLMClient(p, "fake"))
    assert not store.exists(PULSE_FILE)
    failed = [g for g in store.read_json(REPORT_FILE)["gates"] if not g["passed"]]
    assert [g["name"] for g in failed] == ["pii"]


def test_stale_pulse_removed_when_gate_fails(analyzed):
    s, store = analyzed
    store.write_text(PULSE_FILE, "old")
    p = ComposeProvider(one_liner="Mail a@b.com")
    with pytest.raises(GateError):
        run_compose(s, store, LLMClient(p, "fake"))
    assert not store.exists(PULSE_FILE)


def test_wrong_quote_count_fails_before_llm(analyzed):
    s, store = analyzed
    a = store.read_json(ANALYSIS_FILE)
    a["quotes"] = a["quotes"][:2]
    store.write_json(ANALYSIS_FILE, a)
    p = ComposeProvider()
    with pytest.raises(GateError, match="counts"):
        run_compose(s, store, LLMClient(p, "fake"))
    assert p.users == []


def test_missing_analysis_points_to_rank(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(RuntimeError, match="python -m pulse rank"):
        run_compose(s, RunStore(s.runs_dir, "2026-W41"), LLMClient(ComposeProvider(), "fake"))


def test_one_liner_schema_rejects_long_or_wrong_themes():
    m = one_liners_model(["T1", "T2", "T3"], 5)
    ok = {"one_liners": [{"theme_id": t, "text": "short text"} for t in ("T1", "T2", "T3")]}
    m.model_validate(ok)
    with pytest.raises(Exception):
        m.model_validate({"one_liners": [{"theme_id": t, "text": "one two three four five six"} for t in ("T1", "T2", "T3")]})
    with pytest.raises(Exception):
        m.model_validate({"one_liners": ok["one_liners"][:2] + [{"theme_id": "T1", "text": "x"}]})


def test_fit_to_limit_never_touches_quotes():
    content = {"week_start": "5 Oct 2026", "n_reviews": 9, "window": "w", "avg_rating": "3.0", "delta": "",
               "themes": [{"name": "A", "one_liner": "w " * 30, "share": 10}] * 3,
               "quotes": ["keep this quote intact"] * 3,
               "actions": [{"text": "do " * 30, "owner": "Eng"}] * 3}
    assert fit_to_limit(content, 90)
    assert content["quotes"] == ["keep this quote intact"] * 3
    assert count_words(render(content)) <= 90
