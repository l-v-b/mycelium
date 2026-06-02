"""Thin synchronous facade over the FastMCP async client.

The e2e suite talks to a *real, running* mycelium server over the wire — the
same MCP/SSE (or streamable-http) surface that ContextForge and Claude Code
use. We never import mycelium server code here; the only contract is the
protocol. That is deliberate: a test that imported the server could be fooled
by a stubbed implementation. Talking over the socket cannot.

Every tool returns a plain string from mycelium (either a JSON document or a
human-readable confirmation line). We expose both the raw text and a parsed
form so tests can assert on whichever is appropriate.

A fresh connection is opened per call. That is slower than holding one open,
but it is bulletproof against event-loop reuse issues in sync pytest and keeps
each call independent — which is what we want for an integration probe.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional


class ToolError(RuntimeError):
    """Raised when a tool call fails at the protocol level (e.g. the server
    raised an exception such as the write_note intent guard)."""


class MyceliumClient:
    def __init__(
        self,
        url: str,
        transport: str = "sse",
        token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.transport_kind = transport
        self.token = token
        self.timeout = timeout

    # -- transport construction -------------------------------------------------

    def _make_transport(self):
        # Imported lazily so the module is importable even where fastmcp is not
        # installed (e.g. collection on a machine that only runs whitebox bits).
        from fastmcp.client.transports import SSETransport, StreamableHttpTransport

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        if self.transport_kind == "sse":
            return SSETransport(url=self.url, headers=headers)
        return StreamableHttpTransport(url=self.url, headers=headers)

    # -- core call --------------------------------------------------------------

    def _run(self, coro):
        return asyncio.run(asyncio.wait_for(coro, timeout=self.timeout))

    def call_raw(self, tool: str, **arguments: Any) -> str:
        """Call a tool, return the raw string it produced. Raises ToolError if
        the server reports the call as an error."""
        from fastmcp import Client

        async def _call() -> str:
            async with Client(self._make_transport()) as client:
                result = await client.call_tool(tool, arguments)
                # A tool that errored server-side may either raise (caught
                # below) or come back flagged — normalise both to ToolError so
                # callers like the intent-guard test can assert on failure.
                if getattr(result, "is_error", False):
                    detail = ""
                    if getattr(result, "content", None):
                        detail = getattr(result.content[0], "text", "")
                    raise ToolError(f"{tool} reported error: {detail[:300]}")
                # mycelium tools all return `str`. Prefer the text content block;
                # fall back to structured_content/data for robustness across
                # fastmcp versions.
                if getattr(result, "content", None):
                    for block in result.content:
                        text = getattr(block, "text", None)
                        if text is not None:
                            return text
                data = getattr(result, "data", None)
                if isinstance(data, str):
                    return data
                sc = getattr(result, "structured_content", None)
                if isinstance(sc, dict) and "result" in sc:
                    return sc["result"]
                return json.dumps(sc) if sc is not None else ""

        try:
            return self._run(_call())
        except Exception as exc:  # noqa: BLE001 - normalise to ToolError
            raise ToolError(f"{tool} failed: {exc!r}") from exc

    def call_json(self, tool: str, **arguments: Any) -> Any:
        """Call a tool and parse its response as JSON."""
        raw = self.call_raw(tool, **arguments)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AssertionError(
                f"{tool} did not return JSON. First 300 chars: {raw[:300]!r}"
            ) from exc

    # -- introspection ----------------------------------------------------------

    def list_tools(self) -> list[str]:
        from fastmcp import Client

        async def _list() -> list[str]:
            async with Client(self._make_transport()) as client:
                tools = await client.list_tools()
                return [t.name for t in tools]

        return self._run(_list())

    def ping(self) -> bool:
        from fastmcp import Client

        async def _ping() -> bool:
            async with Client(self._make_transport()) as client:
                await client.ping()
                return True

        try:
            return self._run(_ping())
        except Exception:  # noqa: BLE001
            return False
