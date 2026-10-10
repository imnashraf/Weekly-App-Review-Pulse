"""Send the weekly pulse by email to PULSE_RECIPIENT (the server has no draft tool)."""
from __future__ import annotations

from .docs_publisher import pulse_heading, to_plain_text
from .mcp_client import McpTools


def email_parts(run_id: str, pulse_md: str, url: str) -> tuple[str, str]:
    """(subject, plain-text body). The doc link is added after the PII check, not before."""
    return pulse_heading(run_id), f"{to_plain_text(pulse_md)}\n\nRunning log: {url}\n"


async def send_pulse(
    tools: McpTools, recipient: str, subject: str, body: str, idempotency_key: str,
) -> dict:
    args = {"to": recipient, "subject": subject, "body_text": body,
            "idempotency_key": idempotency_key}
    if tools.capabilities.get("gmail_requires_confirm_send"):
        args["confirm_send"] = True
    return await tools.call("gmail_send", args, write=True)
