"""Append the weekly pulse to the configured Google Doc."""
from __future__ import annotations

import re

from .mcp_client import McpTools

_DOC_ID = re.compile(r"/document/d/([A-Za-z0-9_-]+)")


def pulse_heading(run_id: str) -> str:
    return f"Groww Review Pulse — {run_id}"


def to_plain_text(pulse_md: str) -> str:
    """The pulse without its H1 and Markdown markup, for plain-text Docs/Gmail bodies."""
    lines = pulse_md.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    out = []
    for line in lines:
        line = re.sub(r"^#{2,}\s*", "", line)
        line = re.sub(r"^>\s?", "", line)
        line = line.replace("**", "")
        line = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", line)
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def doc_text(run_id: str, pulse_md: str) -> str:
    return f"{pulse_heading(run_id)}\n\n{to_plain_text(pulse_md)}\n"


def doc_url(document: str) -> str:
    """Canonical URL of the target doc, from a URL or a raw id."""
    match = _DOC_ID.search(document)
    doc_id = match.group(1) if match else document.strip()
    return f"https://docs.google.com/document/d/{doc_id}/edit"


async def append_pulse(tools: McpTools, document: str, text: str, idempotency_key: str) -> dict:
    return await tools.call("docs_append", {
        "document": document, "text": text, "idempotency_key": idempotency_key}, write=True)
