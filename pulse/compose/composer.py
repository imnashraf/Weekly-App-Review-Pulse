"""Compose pulse.md from 05_analysis.json and write run_report.json. Architecture §5.7-5.8."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field, field_validator, model_validator

from ..analyze.insights import ANALYSIS_FILE
from ..analyze.themes import CLEAN_FILE, LABELED_FILE, STATS_FILE, THEMES_FILE
from ..config import Settings
from ..llm import LLMClient, LLMError, build_client, load_prompt
from ..store import RunStore, previous_run_id
from ..validate.gates import GateError, count_words, low_sample_warning, run_gates

log = logging.getLogger(__name__)

PULSE_FILE = "pulse.md"
REPORT_FILE = "run_report.json"
ONE_LINERS_PROMPT = "one_liners_v1"
ONE_LINER_WORDS = 18
SHORT_ONE_LINER_WORDS = 8     # asked for when the first draft is over the word limit
MIN_TRIM_WORDS = 3            # deterministic truncation never cuts a slot below this

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent), autoescape=False,
    trim_blocks=False, keep_trailing_newline=True)


# ----------------------------------------------------------------- one-liner slots

class OneLiner(BaseModel):
    theme_id: str
    text: str = Field(min_length=1)

    @field_validator("theme_id")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("text")
    @classmethod
    def _clean(cls, v: str) -> str:
        return " ".join(v.split())


def one_liners_model(theme_ids: list[str], max_words: int) -> type[BaseModel]:
    expected = sorted(theme_ids)

    class _OneLiners(BaseModel):
        one_liners: list[OneLiner] = Field(min_length=len(expected), max_length=len(expected))

        @model_validator(mode="after")
        def _check(self):
            got = sorted(o.theme_id for o in self.one_liners)
            if got != expected:
                raise ValueError(f"need exactly one summary for each of {expected}, got {got}")
            for o in self.one_liners:
                if len(o.text.split()) > max_words:
                    raise ValueError(f"{o.theme_id}: summary has more than {max_words} words")
            return self

    return _OneLiners


def fill_one_liners(
    client: LLMClient, top: list[dict], quotes: list[dict], max_words: int,
) -> dict[str, str]:
    quote_for = {q["theme_id"]: q["quote"] for q in quotes}
    blocks = [
        f"{t['theme_id']} {t['name']}: {t['description']}\n"
        f"  reviews: {t['volume']}\n  quote: \"{quote_for[t['theme_id']]}\""
        for t in top
    ]
    ids = [t["theme_id"] for t in top]
    result = client.complete_json(
        load_prompt(ONE_LINERS_PROMPT, max_words=max_words), "Top themes:\n" + "\n".join(blocks),
        one_liners_model(ids, max_words), temperature=0, label="one_liners")
    return {o.theme_id: o.text for o in result.one_liners}


# --------------------------------------------------------------------- rendering

def _week_start(run_id: str) -> str:
    year, week = run_id.split("-W")
    return date.fromisocalendar(int(year), int(week), 1).strftime("%-d %b %Y")


def _avg(reviews: list[dict]) -> float | None:
    return sum(r["rating"] for r in reviews) / len(reviews) if reviews else None


def _prior_avg(settings: Settings, run_id: str) -> float | None:
    path = settings.runs_dir / previous_run_id(run_id) / CLEAN_FILE
    if not path.is_file():
        return None
    import json
    return _avg([json.loads(l) for l in path.read_text().splitlines() if l.strip()])


def _window(meta: dict) -> str:
    if meta.get("window_start") and meta.get("newest_review"):
        start = datetime.fromisoformat(meta["window_start"]).strftime("%-d %b")
        end = datetime.fromisoformat(meta["newest_review"]).strftime("%-d %b")
        return f"{start} – {end}"
    return f"last {meta.get('window_weeks', '?')} weeks"


def render(content: dict) -> str:
    return _env.get_template("template.md.j2").render(**content)


def _trim_longest(content: dict) -> bool:
    """Drop the last word of the longest one-liner or action. False when nothing can shrink."""
    slots = [(t, "one_liner") for t in content["themes"]] + [(a, "text") for a in content["actions"]]
    slots = [(d, k) for d, k in slots if len(d[k].split()) > MIN_TRIM_WORDS]
    if not slots:
        return False
    d, k = max(slots, key=lambda s: len(s[0][s[1]].split()))
    d[k] = re.sub(r"[\s,;:\-–—]+$", "", " ".join(d[k].split()[:-1]))
    return True


def fit_to_limit(content: dict, max_words: int) -> bool:
    """Deterministic truncation of one-liners and actions. Quotes are never touched."""
    while count_words(render(content)) > max_words:
        if not _trim_longest(content):
            return False
    return True


# ------------------------------------------------------------------------- stage

def _usage_total(**stages: dict | None) -> dict:
    parts = {name: u for name, u in stages.items() if u}
    costs = [u["cost_usd"] for u in parts.values() if u.get("cost_usd") is not None]
    return {
        "calls": sum(u.get("calls", 0) for u in parts.values()),
        "tokens_in": sum(u.get("tokens_in", 0) for u in parts.values()),
        "tokens_out": sum(u.get("tokens_out", 0) for u in parts.values()),
        "cost_usd": round(sum(costs), 4) if costs else None,
        "by_stage": parts,
    }


def run_compose(settings: Settings, store: RunStore, client: LLMClient | None = None) -> dict:
    for name in (ANALYSIS_FILE, LABELED_FILE, THEMES_FILE):
        if not store.exists(name):
            raise RuntimeError(
                f"{name} not found for run {store.run_id}; run `python -m pulse rank` first")
    analysis = store.read_json(ANALYSIS_FILE)
    reviews = list(store.read_jsonl(LABELED_FILE))
    themes_doc = store.read_json(THEMES_FILE)
    ingest_meta = store.read_json("01_ingest_meta.json") if store.exists("01_ingest_meta.json") else {}
    clean_meta = store.read_json("02_clean_meta.json") if store.exists("02_clean_meta.json") else {}
    label_stats = store.read_json(STATS_FILE) if store.exists(STATS_FILE) else {}
    max_words = settings.limits.pulse_max_words
    client = client or build_client(settings)

    top, quotes, actions = analysis["top_themes"], analysis["quotes"], analysis["actions"]
    warnings = list(analysis.get("warnings", []))
    remedies = []

    # Gates that must hold before spending an LLM call on wording.
    early = [g for g in run_gates(
        analysis=analysis, all_themes=themes_doc["themes"], reviews=reviews,
        pulse_md="", max_words=max_words, max_themes=settings.limits.max_themes)
        if g.name in {"schema", "theme_cap", "counts"} and not g.passed]
    if early:
        _write_report(settings, store, analysis, themes_doc, ingest_meta, clean_meta, label_stats,
                      early, warnings, None, client, remedies)
        raise GateError(early)

    quote_text = {q["theme_id"]: q["quote"] for q in quotes}
    action_for = {a["theme_id"]: a for a in actions}
    one_liners = fill_one_liners(client, top, quotes, ONE_LINER_WORDS)

    avg = _avg(reviews)
    prior = _prior_avg(settings, store.run_id)
    content = {
        "week_start": _week_start(store.run_id),
        "n_reviews": len(reviews),
        "window": _window(ingest_meta),
        "avg_rating": f"{avg:.1f}" if avg is not None else "n/a",
        "delta": f"{avg - prior:+.1f}" if avg is not None and prior is not None else "",
        "themes": [{"name": t["name"], "one_liner": one_liners[t["theme_id"]],
                    "share": round(t["share"] * 100)} for t in top],
        "quotes": [quote_text[t["theme_id"]] for t in top],
        "actions": [{"text": action_for[t["theme_id"]]["action"],
                     "owner": action_for[t["theme_id"]]["owner_hint"]} for t in top],
    }

    if count_words(render(content)) > max_words:
        remedies.append("asked the LLM to shorten the one-liners")
        try:
            short = fill_one_liners(client, top, quotes, SHORT_ONE_LINER_WORDS)
            for theme, t in zip(content["themes"], top):
                theme["one_liner"] = short[t["theme_id"]]
        except LLMError as exc:
            log.warning("shorten failed", extra={"stage": "compose", "error": str(exc)})
        if count_words(render(content)) > max_words:
            remedies.append("truncated one-liners and actions deterministically")
            fit_to_limit(content, max_words)
    pulse_md = render(content)

    gates = run_gates(
        analysis=analysis, all_themes=themes_doc["themes"], reviews=reviews,
        pulse_md=pulse_md, max_words=max_words, max_themes=settings.limits.max_themes)
    low = low_sample_warning(len(reviews))
    if low:
        warnings.append(low)
    failed = [g for g in gates if not g.passed]
    report = _write_report(settings, store, analysis, themes_doc, ingest_meta, clean_meta,
                           label_stats, gates, warnings, count_words(pulse_md), client, remedies)
    if failed:
        # Never leave a pulse behind that failed a gate (especially PII).
        store.path(PULSE_FILE).unlink(missing_ok=True)
        raise GateError(failed)
    store.write_text(PULSE_FILE, pulse_md)
    log.info("pulse composed", extra={
        "stage": "compose", "words": report["pulse"]["words"], "gates": "all passed"})
    return report


def _write_report(settings, store, analysis, themes_doc, ingest_meta, clean_meta, label_stats,
                  gates, warnings, words, client, remedies) -> dict:
    report = {
        "run_id": store.run_id,
        "status": "passed" if all(g.passed for g in gates) else "failed",
        "window": {k: ingest_meta.get(k) for k in
                   ("run_date", "window_start", "window_weeks", "oldest_review", "newest_review")},
        "counts": {
            "fetched": ingest_meta.get("review_count"),
            "cleaned": clean_meta.get("output_count"),
            "labeled": analysis.get("review_count"),
            "dropped": clean_meta.get("dropped", {}),
            "pii_redactions": clean_meta.get("pii_redactions", {}),
        },
        "themes": {
            "discovered": themes_doc["themes"],
            "stats": label_stats.get("themes", {}),
            "other": label_stats.get("other"),
            "ranking": analysis.get("ranking", []),
            "in_pulse": [t["theme_id"] for t in analysis.get("top_themes", [])],
        },
        "pulse": {"file": PULSE_FILE if all(g.passed for g in gates) else None,
                  "words": words, "max_words": settings.limits.pulse_max_words,
                  "word_remedies": remedies},
        "gates": [g.to_dict() for g in gates],
        "warnings": warnings + list(label_stats.get("warnings", [])),
        "prompt_versions": {
            **themes_doc.get("prompt_versions", {}), **analysis.get("prompt_versions", {}),
            "one_liners": ONE_LINERS_PROMPT},
        "llm": _usage_total(themes=label_stats.get("llm"), insights=analysis.get("llm"),
                           compose=client.usage()),
        "doc_url": None,
        "publish": None,
    }
    store.write_json(REPORT_FILE, report)
    return report
