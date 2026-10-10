"""Client for our Streamable HTTP MCP server (Docs append + Gmail send)."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol

log = logging.getLogger(__name__)

REQUIRED_TOOLS = ("status", "docs_append", "gmail_send")


class Session(Protocol):
    """The part of `mcp.ClientSession` we use (tests substitute a fake)."""
    async def list_tools(self) -> Any: ...
    async def call_tool(self, name: str, arguments: dict) -> Any: ...


class McpError(RuntimeError):
    """Setup problem: server unreachable, tool missing, or Google not authorized."""


class McpToolError(RuntimeError):
    """A tool call failed. `outcome` is not_performed | performed | unknown."""

    def __init__(self, code: str, message: str, outcome: str = "not_performed",
                 hint: str = "", request_id: str | None = None) -> None:
        super().__init__(f"{code}: {message}" + (f" ({hint})" if hint else ""))
        self.code, self.message, self.outcome = code, message, outcome
        self.hint, self.request_id = hint, request_id


@asynccontextmanager
async def open_session(url: str, token: str) -> AsyncIterator[Session]:
    """Connect over Streamable HTTP with the bearer token on every request."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=60) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def _attr(obj: Any, *names: str) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def parse_result(result: Any) -> dict:
    """The tool's JSON payload, from structured content or the first text block."""
    payload = _attr(result, "structured_content", "structuredContent")
    if not payload:
        for block in _attr(result, "content") or []:
            text = _attr(block, "text")
            if text:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"success": False, "error": {"code": "INTERNAL_ERROR", "message": text[:200]}}
                break
    if not isinstance(payload, dict):
        raise McpToolError("INTERNAL_ERROR", "the server returned no result", outcome="unknown")
    return payload


class McpTools:
    def __init__(self, session: Session, tool_names: dict[str, str]) -> None:
        self.session = session
        self.names = tool_names
        self.capabilities: dict = {}

    async def preflight(self) -> dict:
        """Fail fast if a mapped tool is missing or Google is not authorized."""
        missing_keys = [k for k in REQUIRED_TOOLS if k not in self.names]
        if missing_keys:
            raise McpError(f"mcp.tools is missing: {', '.join(missing_keys)}")
        try:
            listed = await self.session.list_tools()
        except Exception as exc:
            raise McpError(f"cannot list tools on the MCP server: {exc}") from exc
        available = {t.name for t in _attr(listed, "tools") or []}
        gone = [self.names[k] for k in REQUIRED_TOOLS if self.names[k] not in available]
        if gone:
            raise McpError(
                f"the MCP server does not offer: {', '.join(gone)} (it offers: {sorted(available)}); "
                "check mcp.tools in config.yaml and ENABLE_GMAIL_SEND / ENABLE_DOCS_APPEND on the server")
        try:
            status = await self.call("status", {})
        except McpToolError as exc:
            raise McpError(f"MCP status check failed: {exc}") from exc
        auth = status.get("google_authorization", {})
        if auth.get("state") != "ok":
            raise McpError(
                f"the MCP server's Google authorization is '{auth.get('state')}'; "
                "run `npm run auth` on the server")
        self.capabilities = status.get("capabilities", {})
        for cap, tool in (("google_docs_append", "docs_append"), ("gmail_send", "gmail_send")):
            if self.capabilities.get(cap) is False:
                raise McpError(f"{self.names[tool]} is disabled on the MCP server")
        log.info("mcp ready", extra={"stage": "mcp", "account": auth.get("account")})
        return status

    async def call(self, key: str, args: dict, *, write: bool = False) -> dict:
        """Call a mapped tool. Raises McpToolError when it reports failure."""
        name = self.names[key]
        try:
            result = await self.session.call_tool(name, args)
        except Exception as exc:
            # A write that may have reached the server must be treated as unknown.
            raise McpToolError("NETWORK_ERROR", str(exc)[:200],
                               outcome="unknown" if write else "not_performed") from exc
        payload = parse_result(result)
        if not payload.get("success", False):
            err = payload.get("error", {})
            raise McpToolError(
                err.get("code", "INTERNAL_ERROR"), err.get("message", "tool call failed"),
                outcome=err.get("outcome", "unknown" if write else "not_performed"),
                hint=err.get("hint", ""), request_id=payload.get("request_id"))
        return payload
