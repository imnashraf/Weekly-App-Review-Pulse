import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from pulse.__main__ import main
from pulse.compose.composer import PULSE_FILE, REPORT_FILE
from pulse.integrations.docs_publisher import doc_text, doc_url, to_plain_text
from pulse.integrations.mcp_client import McpError, McpTools, McpToolError, parse_result
from pulse.integrations.publish import PublishError, run_publish
from pulse.store import RunStore
from tests.test_insights import make_settings

PULSE = '''# Groww App Review Pulse — Week of 5 Oct 2026
_60 public reviews · 17 Jul – 8 Oct · Avg rating 2.9_

## Top themes
1. **App speed** — Orders lag at market open (30% of reviews)

## What users said

> "Customer support never replies"

## Action ideas
1. Fix the order lag _(Eng)_
'''
NAMES = {"status": "google_workspace_status", "docs_append": "google_docs_append_text",
         "gmail_send": "gmail_send_email"}
DOC = "https://docs.google.com/document/d/abc123_XYZ/edit"


def ok(**extra):
    return {"success": True, "request_id": "req-1", **extra}


def fail(code, outcome="not_performed", hint=""):
    return {"success": False, "request_id": "req-2",
            "error": {"code": code, "message": "boom", "outcome": outcome, "hint": hint}}


class FakeSession:
    def __init__(self, tools=tuple(NAMES.values()), auth="ok", caps=None, docs=None, mail=None):
        self.tools, self.auth = tools, auth
        self.caps = caps or {"gmail_send": True, "google_docs_append": True,
                             "gmail_requires_confirm_send": False}
        self.docs, self.mail = docs or ok(), mail or ok(message_id="m1", thread_id="t1")
        self.calls = []

    async def list_tools(self):
        return NS(tools=[NS(name=n) for n in self.tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == NAMES["status"]:
            payload = ok(google_authorization={"state": self.auth, "account": "me@x.com"},
                         capabilities=self.caps)
        elif name == NAMES["docs_append"]:
            payload = self.docs
        else:
            payload = self.mail
        if isinstance(payload, Exception):
            raise payload
        return NS(structured_content=payload, content=[], is_error=not payload.get("success"))

    def writes(self):
        return [c for c in self.calls if c[0] != NAMES["status"]]


def factory_for(session, seen=None):
    @asynccontextmanager
    async def factory(url, token):
        if seen is not None:
            seen.append((url, token))
        yield session
    return factory


@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ACCESS_TOKEN", "t" * 40)
    s = make_settings(tmp_path)
    s = s.model_copy(update={
        "mcp_access_token": "t" * 40,
        "mcp": s.mcp.model_copy(update={"docs": s.mcp.docs.model_copy(update={"document": DOC}),
                                        "url": "https://mcp.example/mcp"}),
        "delivery": s.delivery.model_copy(update={"recipient": "me@example.com"})})
    store = RunStore(s.runs_dir, "2026-W41")
    store.write_text(PULSE_FILE, PULSE)
    store.write_json(REPORT_FILE, {"status": "passed", "gates": [{"name": "pii", "passed": True}],
                                   "doc_url": None, "publish": None})
    return s, store


# ----------------------------------------------------------------- text helpers

def test_plain_text_drops_markdown_and_h1():
    text = to_plain_text(PULSE)
    assert "Groww App Review Pulse" not in text and "**" not in text and ">" not in text
    assert "#" not in text and "_" not in text
    assert "1. App speed — Orders lag at market open (30% of reviews)" in text
    assert "Fix the order lag (Eng)" in text and 'Customer support never replies' in text


def test_doc_text_has_dated_heading_and_url_from_id_or_link():
    assert doc_text("2026-W41", PULSE).startswith("Groww Review Pulse — 2026-W41\n\n")
    assert doc_url(DOC) == doc_url("abc123_XYZ") == "https://docs.google.com/document/d/abc123_XYZ/edit"


# ------------------------------------------------------------------- mcp client

def test_preflight_ok_and_records_capabilities():
    t = McpTools(FakeSession(), NAMES)
    asyncio.run(t.preflight())
    assert t.capabilities["gmail_send"] is True


def test_preflight_fails_fast_on_missing_tool_unauthorized_or_disabled():
    with pytest.raises(McpError, match="does not offer: gmail_send_email"):
        asyncio.run(McpTools(FakeSession(tools=[NAMES["status"], NAMES["docs_append"]]), NAMES).preflight())
    with pytest.raises(McpError, match="npm run auth"):
        asyncio.run(McpTools(FakeSession(auth="auth_required"), NAMES).preflight())
    with pytest.raises(McpError, match="disabled"):
        asyncio.run(McpTools(FakeSession(caps={"gmail_send": False}), NAMES).preflight())


def test_call_raises_typed_error_with_code_and_outcome():
    t = McpTools(FakeSession(docs=fail("AUTH_EXPIRED_OR_REVOKED", hint="re-run npm run auth")), NAMES)
    with pytest.raises(McpToolError) as e:
        asyncio.run(t.call("docs_append", {}, write=True))
    assert (e.value.code, e.value.outcome) == ("AUTH_EXPIRED_OR_REVOKED", "not_performed")
    assert "re-run npm run auth" in str(e.value)


def test_transport_error_on_write_is_unknown_on_read_is_not_performed():
    t = McpTools(FakeSession(docs=TimeoutError("timed out")), NAMES)
    with pytest.raises(McpToolError) as e:
        asyncio.run(t.call("docs_append", {}, write=True))
    assert e.value.outcome == "unknown"
    with pytest.raises(McpToolError) as e:
        asyncio.run(t.call("docs_append", {}, write=False))
    assert e.value.outcome == "not_performed"


def test_parse_result_reads_text_block_fallback():
    res = NS(content=[NS(text=json.dumps({"success": True, "x": 1}))])
    assert parse_result(res)["x"] == 1


def test_open_session_sends_bearer_header(monkeypatch):
    import httpx2
    from pulse.integrations import mcp_client
    seen = {}

    class Boom(Exception):
        pass

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)
            raise Boom

    monkeypatch.setattr(httpx2, "AsyncClient", FakeClient)

    async def go():
        async with mcp_client.open_session("https://x/mcp", "secret"):
            pass
    with pytest.raises(Boom):
        asyncio.run(go())
    assert seen["headers"] == {"Authorization": "Bearer secret"}


# ----------------------------------------------------------------------- publish

def test_publish_appends_doc_then_sends_one_email(ready):
    s, store = ready
    sess, seen = FakeSession(), []
    state = run_publish(s, store, session_factory=factory_for(sess, seen))
    assert seen == [("https://mcp.example/mcp", "t" * 40)]
    (dname, dargs), (mname, margs) = sess.writes()
    assert dname == NAMES["docs_append"] and dargs["document"] == DOC
    assert dargs["text"].startswith("Groww Review Pulse — 2026-W41")
    assert dargs["idempotency_key"].startswith("pulse-doc-2026-W41-")
    assert mname == NAMES["gmail_send"] and margs["to"] == "me@example.com"
    assert set(margs) == {"to", "subject", "body_text", "idempotency_key"}
    assert margs["subject"] == "Groww Review Pulse — 2026-W41"
    assert margs["body_text"].rstrip().endswith("https://docs.google.com/document/d/abc123_XYZ/edit")
    assert state["doc"]["status"] == state["mail"]["status"] == "done"
    report = store.read_json(REPORT_FILE)
    assert report["publish"]["mail"]["message_id"] == "m1" and report["doc_url"] == doc_url(DOC)


def test_rerun_does_not_duplicate(ready):
    s, store = ready
    first = FakeSession()
    run_publish(s, store, session_factory=factory_for(first))
    second = FakeSession()
    run_publish(s, store, session_factory=factory_for(second))
    assert len(first.writes()) == 2 and second.writes() == []


def test_confirm_send_only_when_server_requires_it(ready):
    s, store = ready
    sess = FakeSession(caps={"gmail_send": True, "google_docs_append": True,
                             "gmail_requires_confirm_send": True})
    run_publish(s, store, session_factory=factory_for(sess))
    assert sess.writes()[1][1]["confirm_send"] is True


def test_mail_failure_keeps_doc_and_rerun_sends_only_mail(ready):
    s, store = ready
    broken = FakeSession(mail=fail("RATE_LIMITED"))
    with pytest.raises(PublishError, match="mail step failed"):
        run_publish(s, store, session_factory=factory_for(broken))
    pub = store.read_json(REPORT_FILE)["publish"]
    assert pub["doc"]["status"] == "done" and pub["mail"]["status"] == "failed"
    fixed = FakeSession()
    run_publish(s, store, session_factory=factory_for(fixed))
    assert [w[0] for w in fixed.writes()] == [NAMES["gmail_send"]]


def test_unknown_outcome_is_not_retried_without_flag(ready):
    s, store = ready
    with pytest.raises(PublishError, match="check the result in Google"):
        run_publish(s, store, session_factory=factory_for(FakeSession(docs=fail("OUTCOME_UNKNOWN", "unknown"))))
    again = FakeSession()
    with pytest.raises(PublishError, match="--retry-unknown"):
        run_publish(s, store, session_factory=factory_for(again))
    assert again.writes() == []
    run_publish(s, store, retry_unknown=True, session_factory=factory_for(again))
    keys = [w[1]["idempotency_key"] for w in again.writes()]
    assert keys[0].endswith("-r1")                       # fresh key after an unknown outcome


def test_failed_gate_blocks_publish(ready):
    s, store = ready
    report = store.read_json(REPORT_FILE)
    report["status"], report["gates"][0]["passed"] = "failed", False
    store.write_json(REPORT_FILE, report)
    sess = FakeSession()
    with pytest.raises(PublishError, match="gates did not all pass"):
        run_publish(s, store, session_factory=factory_for(sess))
    assert sess.calls == []


def test_pii_in_pulse_blocks_publish(ready):
    s, store = ready
    store.write_text(PULSE_FILE, PULSE + "\nCall 9876543210 now\n")
    sess = FakeSession()
    with pytest.raises(PublishError, match="PII check failed"):
        run_publish(s, store, session_factory=factory_for(sess))
    assert sess.calls == []


def test_missing_pulse_or_config_stops_before_any_call(ready):
    s, store = ready
    s2 = s.model_copy(update={"delivery": s.delivery.model_copy(update={"recipient": None})})
    sess = FakeSession()
    with pytest.raises(PublishError, match="PULSE_RECIPIENT"):
        run_publish(s2, store, session_factory=factory_for(sess))
    s3 = s.model_copy(update={"delivery": s.delivery.model_copy(update={"recipient": "A B <a@b.com>"})})
    with pytest.raises(PublishError, match="plain address"):
        run_publish(s3, store, session_factory=factory_for(sess))
    assert sess.calls == []
    store.path(PULSE_FILE).unlink()
    with pytest.raises(PublishError, match="python -m pulse analyze"):
        run_publish(s, store, session_factory=factory_for(sess))


def test_unauthorized_server_stops_before_writes(ready):
    s, store = ready
    sess = FakeSession(auth="auth_expired_or_revoked")
    with pytest.raises(PublishError, match="authorization"):
        run_publish(s, store, session_factory=factory_for(sess))
    assert sess.writes() == []


def test_dry_run_writes_files_and_only_reads_status(ready):
    s, store = ready
    sess = FakeSession()
    out = run_publish(s, store, dry_run=True, session_factory=factory_for(sess))
    assert out["mcp_status"] == "ok" and sess.writes() == []
    assert store.path("06_doc_text.txt").read_text().startswith("Groww Review Pulse")
    assert "To: me@example.com" in store.read_text("06_email.txt")


def test_dry_run_without_token_makes_no_calls(ready):
    s, store = ready
    s = s.model_copy(update={"mcp_access_token": None})
    sess = FakeSession()
    out = run_publish(s, store, dry_run=True, session_factory=factory_for(sess))
    assert sess.calls == [] and out["mcp_status"] == "not checked"


def test_publish_cli_requires_env(monkeypatch, capsys):
    for v in ("PULSE_RECIPIENT", "MCP_ACCESS_TOKEN", "PULSE_DOC_ID"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("pulse.config.load_dotenv", lambda *a, **k: None)
    assert main(["publish", "--run", "2026-W41"]) == 1
    err = capsys.readouterr().err
    assert "MCP_ACCESS_TOKEN" in err and "PULSE_DOC_ID" in err
