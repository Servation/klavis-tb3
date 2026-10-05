"""Client for upstream MCP servers that speak protocol 2025-11-25.

These servers are session-oriented: `initialize` returns an MCP-Session-Id
that every later request repeats. A `tools/call` is answered on an SSE stream
that may carry server-to-client requests (elicitation, sampling, roots) and
progress notifications before the final response; the client answers those
requests by POSTing JSON-RPC responses on the same session.

In this protocol revision a dropped connection is not a cancellation. To stop
a call the client must send `notifications/cancelled` with the request id.
Servers may also close the connection themselves without ending the call
(polling); the client then resumes the stream (see call_tool).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
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
        """Yield every message the upstream sends for this tools/call.

        2025-11-25 servers may close the connection without ending the stream
        (they send a `retry` field first); the stream is then resumed with GET
        and Last-Event-ID, which replays what came after that event. The call
        is never re-sent: a second POST would start a second execution.
        """
        await self.ensure_session()
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if progress_token is not None:
            params["_meta"] = {"progressToken": progress_token}
        payload = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
        last_event_id: str | None = None
        retry_ms: int | None = None
        while True:
            if last_event_id is None:
                request = self._http.stream("POST", self.url, json=payload, headers=self._headers())
            else:
                await asyncio.sleep((retry_ms or 0) / 1000)
                request = self._http.stream("GET", self.url, headers={**self._headers(), "last-event-id": last_event_id})
            retry_ms = None
            async with request as response:
                if response.status_code == 404 and last_event_id is None:
                    self.reset()
                    raise SessionExpired("session expired")
                if response.status_code != 200:
                    raise LegacyError(f"tools/call stream failed: HTTP {response.status_code}")
                if response.headers.get("content-type", "").startswith("application/json"):
                    yield json.loads(await response.aread())
                    return
                async for event in _iter_sse(response):
                    if event.retry is not None:
                        retry_ms = event.retry
                    if event.id is not None:
                        last_event_id = event.id
                    if event.message is None:
                        continue
                    yield event.message
                    if event.message.get("id") == request_id and ("result" in event.message or "error" in event.message):
                        return
            if retry_ms is None or last_event_id is None:
                # Closed without a retry hint: the stream really ended.
                raise LegacyError("upstream ended the stream without a result")

    async def cancel_in_session(self, session_id: str, request_id: str, reason: str) -> None:
        """Cancel a call made on a given session (possibly by another process)."""
        response = await self._http.post(
            self.url,
            json={"jsonrpc": "2.0", "method": "notifications/cancelled",
                  "params": {"requestId": request_id, "reason": reason}},
            headers={**self._headers(session=False), "mcp-session-id": session_id,
                     "mcp-protocol-version": PROTOCOL_VERSION},
        )
        if response.status_code not in (202, 404):
            raise LegacyError(f"upstream rejected cancellation: HTTP {response.status_code}")

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        """Answer a server-to-client request."""
        response = await self._http.post(
            self.url,
            json={"jsonrpc": "2.0", "id": request_id, "result": result},
            headers=self._headers(),
        )
        if response.status_code != 202:
            raise LegacyError(f"upstream rejected response to {request_id}: HTTP {response.status_code}")

    async def cancel(self, request_id: str, reason: str) -> None:
        response = await self._http.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": reason},
            },
            headers=self._headers(),
        )
        if response.status_code != 202:
            raise LegacyError(f"upstream rejected cancellation: HTTP {response.status_code}")

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


@dataclass
class SseEvent:
    id: str | None
    retry: int | None
    message: dict[str, Any] | None  # None for events without data (e.g. priming)


async def _iter_sse(response: httpx.Response) -> AsyncIterator[SseEvent]:
    data: list[str] = []
    event_id: str | None = None
    retry: int | None = None
    async for line in response.aiter_lines():
        if line == "":
            if data or event_id is not None or retry is not None:
                payload = "\n".join(data).strip()
                yield SseEvent(event_id, retry, json.loads(payload) if payload else None)
            data, event_id, retry = [], None, None
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "data":
            data.append(value)
        elif name == "id":
            event_id = value
        elif name == "retry" and value.isdigit():
            retry = int(value)
    if data or event_id is not None or retry is not None:
        payload = "\n".join(data).strip()
        yield SseEvent(event_id, retry, json.loads(payload) if payload else None)
