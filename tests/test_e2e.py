"""End to end on fixtures: Play items -> ingest -> clean -> themes -> rank -> compose -> publish.

Only the network edges are faked (Google Play pages, the LLM, the MCP session); every pipeline
stage in between is the real code.
"""
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import pytest

import pulse.analyze.insights as insights_mod
import pulse.analyze.themes as themes_mod
import pulse.compose.composer as composer_mod
import pulse.ingest as ingest_mod
import pulse.integrations.publish as publish_mod
from pulse.clean.pii import has_pii
from pulse.ingest.google_play import GooglePlayAdapter
from pulse.llm import LLMClient
from pulse.orchestrator import STAGES, run_all
from pulse.store import RunStore, current_run_id
from pulse.validate.gates import count_words
from tests.test_compose import ComposeProvider
from tests.test_publish import FakeSession, NAMES, factory_for
from tests.test_themes import FakeProvider, fixture_reviews
from tests.test_insights import make_settings

AUTHOR = "Jane Q. Reviewer"
SECRET_EMAIL, SECRET_PHONE = "jane.doe@example.com", "9876543210"
RECIPIENT = "me@example.com"
DOC = "https://docs.google.com/document/d/e2eDoc123/edit"


class Router:
    """Discovery and classification go to the keyword fake, the rest to the compose fake."""
    def __init__(self):
        self.fixture, self.rest = FakeProvider(), ComposeProvider()

    def complete(self, system, user, temperature):
        target = self.fixture if (user.startswith("Reviews (") or "Themes:\n" in user) else self.rest
        return target.complete(system, user, temperature)


def play_items():
    """Google Play style items, including reviewer identity and some messy reviews."""
    rows = fixture_reviews()
    shift = date.today() - timedelta(days=1) - max(date.fromisoformat(r["date"]) for r in rows)
    items = []
    for i, r in enumerate(rows):
        day = date.fromisoformat(r["date"]) + shift
        items.append({
            "reviewId": f"gp-original-{i}", "userName": AUTHOR, "userImage": "http://img/jane.png",
            "content": r["text"], "score": r["rating"], "thumbsUpCount": 2, "appVersion": "9.9.9",
            "at": datetime.combine(day, datetime.min.time()), "replyContent": "thanks"})
    latest = items[0]["at"]
    extra = [
        f"Support never answered, mail me at {SECRET_EMAIL} or call {SECRET_PHONE} about my withdrawal issue please",
        "good app",                                                    # too short
        "Great app but the charts keep freezing during market hours \U0001F621 very annoying",  # emoji
        "Yeh app bahut accha hai aur paisa bhi milta hai mujhe kaafi time se use kar raha hoon",  # Hinglish
    ]
    items += [{"reviewId": f"gp-extra-{i}", "userName": AUTHOR, "userImage": "x", "content": t,
               "score": 1, "at": latest} for i, t in enumerate(extra)]
    items.append(dict(items[1], reviewId="gp-duplicate"))              # same text again
    return items


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    s = s.model_copy(update={
        "mcp_access_token": "t" * 40,
        "mcp": s.mcp.model_copy(update={"docs": s.mcp.docs.model_copy(update={"document": DOC}),
                                        "url": "https://mcp.example/mcp"}),
        "delivery": s.delivery.model_copy(update={"recipient": RECIPIENT})})
    store = RunStore(s.runs_dir, current_run_id())
    items = play_items()
    monkeypatch.setattr(ingest_mod, "GooglePlayAdapter", lambda *a, **k: GooglePlayAdapter(
        "app", fetch_page=lambda token: (items, None), sleep=lambda _s: None))
    router = Router()
    for module in (themes_mod, insights_mod, composer_mod):
        monkeypatch.setattr(module, "build_client", lambda settings, r=router: LLMClient(r, "fake"))
    return s, store


def use_session(monkeypatch, session):
    monkeypatch.setattr(publish_mod, "open_session", factory_for(session))


# --------------------------------------------------------------------- end to end

def test_dry_run_end_to_end_produces_a_valid_pulse(pipeline):
    s, store = pipeline
    s = s.model_copy(update={"mcp_access_token": None})      # dry run makes no MCP call at all
    results = run_all(s, store, dry_run=True)
    assert [r.name for r in results] == list(STAGES) and all(r.status == "ran" for r in results)

    pulse = store.read_text("pulse.md")
    report = store.read_json("run_report.json")
    assert count_words(pulse) <= 250 and report["status"] == "passed"
    assert all(g["passed"] for g in report["gates"])
    assert len(store.read_json("05_analysis.json")["quotes"]) == 3
    assert pulse.count("\n> \"") == 3 and "## Action ideas" in pulse
    # messy reviews were dropped by the real cleaning stage
    meta = store.read_json("02_clean_meta.json")
    assert meta["dropped"]["too_short"] >= 1 and meta["dropped"]["emoji"] >= 1
    assert meta["dropped"]["hinglish"] >= 1 and meta["dropped"]["duplicate"] >= 1
    assert store.exists("06_doc_text.txt") and store.exists("06_email.txt")
    assert report["publish"] is None                          # dry run records nothing as published


def test_full_run_publishes_once_and_resumes_cleanly(pipeline, monkeypatch):
    s, store = pipeline
    session = FakeSession()
    use_session(monkeypatch, session)
    run_all(s, store)
    assert [c[0] for c in session.writes()] == [NAMES["docs_append"], NAMES["gmail_send"]]
    again = run_all(s, store)
    assert all(r.status == "skipped" for r in again) and len(session.writes()) == 2


# --------------------------------------------------------------- privacy checklist

def test_no_reviewer_identity_or_pii_in_any_stored_artifact(pipeline, monkeypatch):
    s, store = pipeline
    use_session(monkeypatch, FakeSession())
    run_all(s, store)
    for path in store.dir.iterdir():
        content = path.read_text()
        for needle in (AUTHOR, "jane.png", "userName", "userImage", "gp-original", "gp-extra", "gp-dup"):
            assert needle not in content, f"{needle!r} found in {path.name}"
        if path.name != "01_raw_reviews.jsonl":                   # raw keeps the public text as written
            assert SECRET_EMAIL not in content and SECRET_PHONE not in content, path.name
    for stage_file in ("02_clean_reviews.jsonl", "04_labeled_reviews.jsonl"):
        for row in store.read_jsonl(stage_file):
            assert not has_pii(row["text"]), stage_file
            assert set(row) <= {"id", "source", "rating", "title", "text", "date", "app_version",
                                "week", "theme_id", "sentiment"}
    raw = list(store.read_jsonl("01_raw_reviews.jsonl"))
    assert all(set(r) == {"id", "source", "rating", "text", "date", "app_version"} for r in raw)
    assert not has_pii(store.read_text("pulse.md"))


def test_email_goes_only_to_the_configured_recipient(pipeline, monkeypatch):
    s, store = pipeline
    session = FakeSession()
    use_session(monkeypatch, session)
    run_all(s, store)
    sends = [args for name, args in session.calls if name == NAMES["gmail_send"]]
    assert len(sends) == 1 and sends[0]["to"] == RECIPIENT
    assert not ({"cc", "bcc", "reply_to", "body_html"} & set(sends[0]))
    assert {name for name, _ in session.calls} <= set(NAMES.values())   # no other tool is ever called


def test_pii_in_review_text_never_reaches_doc_or_email(pipeline, monkeypatch):
    s, store = pipeline
    session = FakeSession()
    use_session(monkeypatch, session)
    run_all(s, store)
    sent = json.dumps(session.writes())
    assert SECRET_EMAIL not in sent and SECRET_PHONE not in sent
    assert not re.search(r"\d{10}", sent)
