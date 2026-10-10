"""Stage: publish the composed pulse to Google Docs and Gmail through MCP. Architecture §6."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any, Callable

from ..compose.composer import PULSE_FILE, REPORT_FILE
from ..config import Settings
from ..store import RunStore
from ..validate.gates import pii_gate
from . import docs_publisher, mail_sender
from .mcp_client import McpError, McpTools, McpToolError, open_session

log = logging.getLogger(__name__)

DOC_TEXT_FILE = "06_doc_text.txt"
EMAIL_FILE = "06_email.txt"
_EMAIL = re.compile(r"^[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+$")   # plain local@domain only

# session_factory(url, token) -> async context manager yielding an MCP session
SessionFactory = Callable[[str, str], Any]


class PublishError(RuntimeError):
    """Publishing stopped; the message says why and what to do next."""


def _check_ready(settings: Settings, store: RunStore) -> tuple[str, dict]:
    if not store.exists(REPORT_FILE) or not store.exists(PULSE_FILE):
        raise PublishError(
            f"{PULSE_FILE} / {REPORT_FILE} not found for run {store.run_id}; "
            "run `python -m pulse analyze` first")
    report = store.read_json(REPORT_FILE)
    failed = [g["name"] for g in report.get("gates", []) if not g["passed"]]
    if report.get("status") != "passed" or failed:
        raise PublishError(f"validation gates did not all pass ({', '.join(failed) or 'status failed'}); "
                           "nothing was published")
    return store.read_text(PULSE_FILE), report


def _step_failure(step: dict, exc: McpToolError) -> None:
    step.update({
        "status": "unknown" if exc.outcome == "unknown" else "failed",
        "error_code": exc.code, "error": exc.message, "hint": exc.hint,
        "request_id": exc.request_id,
    })


async def _run_step(name, step, key_base, do_call, retry_unknown, save):
    """Run one write step unless it already completed; never re-send an unknown outcome."""
    if step.get("status") == "done":
        log.info("step already done, skipping", extra={"stage": "publish", "step": name})
        return
    if step.get("status") == "unknown" and not retry_unknown:
        raise PublishError(
            f"the {name} step ended with an unknown outcome earlier. Check the "
            f"{'document' if name == 'doc' else 'Sent folder'} first; if nothing was written, re-run "
            "with --retry-unknown")
    attempts = step.get("attempts", 0)
    key = key_base if not attempts or step.get("status") != "unknown" else f"{key_base}-r{attempts}"
    step["attempts"] = attempts + 1
    step["idempotency_key"] = key
    try:
        result = await do_call(key)
    except McpToolError as exc:
        _step_failure(step, exc)
        save()
        raise PublishError(
            f"{name} step {step['status']}: {exc}" +
            ("; do not retry blindly, check the result in Google first" if step["status"] == "unknown" else "")
        ) from exc
    for k in ("error_code", "error", "hint"):
        step.pop(k, None)
    step.update({"status": "done", "request_id": result.get("request_id"),
                 **{k: result[k] for k in ("message_id", "thread_id") if k in result}})
    save()


async def _publish(settings, store, pulse_md, report, *, dry_run, retry_unknown, factory) -> dict:
    cfg = settings.mcp
    run_id = store.run_id
    recipient = settings.delivery.recipient
    document = cfg.docs.document
    if not dry_run:
        missing = [n for n, v in (("mcp.url", cfg.url), ("MCP_ACCESS_TOKEN", settings.mcp_access_token),
                                  ("PULSE_DOC_ID", document), ("PULSE_RECIPIENT", recipient)) if not v]
        if missing:
            raise PublishError(f"missing configuration: {', '.join(missing)}")
        if not _EMAIL.match(recipient):
            raise PublishError("PULSE_RECIPIENT must be a plain address like name@example.com")

    text = docs_publisher.doc_text(run_id, pulse_md)
    url = docs_publisher.doc_url(document) if document else "(PULSE_DOC_ID not set)"
    subject, body = mail_sender.email_parts(run_id, pulse_md, url)
    # PII re-check on exactly what leaves the machine (the doc link is added by us, not from reviews).
    for label, content in (("doc text", text), ("email subject", subject),
                           ("email body", body.rsplit("\n\nRunning log:", 1)[0])):
        if not pii_gate(content).passed:
            raise PublishError(f"PII check failed on the {label}; nothing was published")

    store.write_text(DOC_TEXT_FILE, text)
    store.write_text(EMAIL_FILE, f"To: {recipient or '(PULSE_RECIPIENT not set)'}\nSubject: {subject}\n\n{body}")

    token = settings.mcp_access_token
    if dry_run:
        result = {"dry_run": True, "doc_text": str(store.path(DOC_TEXT_FILE)),
                  "email": str(store.path(EMAIL_FILE)), "mcp_status": "not checked"}
        if cfg.url and token:                       # read-only status call is allowed
            try:
                async with factory(cfg.url, token) as session:
                    await McpTools(session, cfg.tools).preflight()
                result["mcp_status"] = "ok"
            except McpError as exc:
                result["mcp_status"] = f"problem: {exc}"
        return result

    digest = hashlib.sha256(pulse_md.encode()).hexdigest()[:8]
    state = report.get("publish") or {}
    if state.get("content_hash") != digest:
        state = {"content_hash": digest, "doc": {}, "mail": {}}
    report["publish"] = state
    report["doc_url"] = url

    def save() -> None:
        store.write_json(REPORT_FILE, report)

    try:
        async with factory(cfg.url, token) as session:
            tools = McpTools(session, cfg.tools)
            await tools.preflight()
            await _run_step(
                "doc", state["doc"], f"pulse-doc-{run_id}-{digest}",
                lambda key: docs_publisher.append_pulse(tools, url, text, key),
                retry_unknown, save)
            await _run_step(
                "mail", state["mail"], f"pulse-mail-{run_id}-{digest}",
                lambda key: mail_sender.send_pulse(tools, recipient, subject, body, key),
                retry_unknown, save)
    except McpError as exc:
        raise PublishError(str(exc)) from exc
    return state


def run_publish(
    settings: Settings, store: RunStore, *, dry_run: bool = False, retry_unknown: bool = False,
    session_factory: SessionFactory | None = None,
) -> dict:
    pulse_md, report = _check_ready(settings, store)
    return asyncio.run(_publish(
        settings, store, pulse_md, report, dry_run=dry_run,
        retry_unknown=retry_unknown, factory=session_factory or open_session))
