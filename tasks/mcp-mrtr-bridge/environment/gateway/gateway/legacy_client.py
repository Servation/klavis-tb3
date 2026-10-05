"""Client for upstream MCP servers that speak protocol 2025-11-25.

These servers are session-oriented: `initialize` returns an MCP-Session-Id
that every later request repeats. A `tools/call` is answered on an SSE stream
that may carry server-to-client requests (elicitation, sampling, roots) and
progress notifications before the final response; the client answers those
requests by POSTing JSON-RPC responses on the same session.

Closing the response stream of a request ends it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from . import __version__

PROTOCOL_VERSION = "2025-11-25"
CLIENT_CAPABILITIES = {"elicitation": {"form": {}}, "sampling": {}, "roots": {"listChanged": False}}


class LegacyError(RuntimeError):
    pass


class SessionExpired(LegacyError):
    """The upstream answered 404 for our MCP-Session-Id."""


class LegacyClient:
    """One upstream session on behalf of one principal."""

    def __init__(self, http: httpx.AsyncClient, url: str, token: str) -> None:
        self._http = http
        self.url = url
        self._token = token
        self.session_id: str | None = None
        self._init_lock = asyncio.Lock()
        self._ids = 0

    async def ensure_session(self) -> None:
        async with self._init_lock:
            if self.session_id is not None:
                return
            response = await self._http.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": "init",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": CLIENT_CAPABILITIES,
                        "clientInfo": {"name": "mcp-gateway", "version": __version__},
                    },
                },
                headers=self._headers(session=False),
            )
            if response.status_code != 200:
                raise LegacyError(f"initialize failed: HTTP {response.status_code}")
            session_id = response.headers.get("mcp-session-id")
            if not session_id:
                raise LegacyError("upstream did not assign a session")
            self.session_id = session_id
            ack = await self._http.post(
                self.url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=self._headers(),
            )
            if ack.status_code != 202:
                raise LegacyError(f"initialized notification rejected: HTTP {ack.status_code}")

    def reset(self) -> None:
        self.session_id = None

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.ensure_session()
        response = await self._http.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._next_id("list"), "method": "tools/list"},
            headers=self._headers(),
        )
        if response.status_code == 404:
            self.reset()
            raise SessionExpired("session expired")
        body = response.json()
        if "error" in body:
            raise LegacyError(body["error"].get("message", "tools/list failed"))
        return list(body["result"].get("tools", []))

    async def call_tool(
        self, request_id: str, name: str, arguments: dict[str, Any], progress_token: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every message the upstream sends for this tools/call."""
        await self.ensure_session()
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if progress_token is not None:
            params["_meta"] = {"progressToken": progress_token}
        attempt = 0
        while True:
            call_id = request_id if attempt == 0 else f"{request_id}.{attempt}"
            payload = {"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": params}
            async with self._http.stream("POST", self.url, json=payload, headers=self._headers()) as response:
                if response.status_code == 404:
                    self.reset()
                    raise SessionExpired("session expired")
                if response.status_code != 200:
                    raise LegacyError(f"tools/call failed: HTTP {response.status_code}")
                if response.headers.get("content-type", "").startswith("application/json"):
                    yield json.loads(await response.aread())
                    return
                async for message in _iter_sse(response):
                    yield message
                    if message.get("id") == call_id and ("result" in message or "error" in message):
                        return
            # The upstream closed the stream before sending the result. Our
            # upstream tools are idempotent, so the call is simply made again.
            attempt += 1
            if attempt > 3:
                raise LegacyError("upstream keeps closing the stream")

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        """Answer a server-to-client request."""
        response = await self._http.post(
            self.url,
            json={"jsonrpc": "2.0", "id": request_id, "result": result},
            headers=self._headers(),
        )
        if response.status_code != 202:
            raise LegacyError(f"upstream rejected response to {request_id}: HTTP {response.status_code}")

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids}"

    def _headers(self, session: bool = True) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self._token}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if session:
            headers["mcp-session-id"] = self.session_id or ""
            headers["mcp-protocol-version"] = PROTOCOL_VERSION
        return headers


async def _iter_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    # Only `data` matters to us; events without data (such as the priming
    # event servers send first) carry no message.
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            payload = "\n".join(data).strip()
            if payload:
                yield json.loads(payload)
            data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    payload = "\n".join(data).strip()
    if payload:
        yield json.loads(payload)
