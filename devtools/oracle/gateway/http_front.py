"""2026-07-28 Streamable HTTP front end (ASGI).

Every client message is its own POST to /mcp. tools/call is answered with an
SSE stream scoped to that request: progress notifications, then the final
JSON-RPC response. Under this protocol revision the client cancels a request
by closing its response stream, so ResponseStream watches the ASGI receive
channel for `http.disconnect` and exposes it as an event.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import __version__
from .auth import principal_for
from .catalog import CATALOG_TTL_MS
from .config import Settings

PROTOCOL_VERSION = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

SSE_HEADERS = [
    (b"content-type", b"text/event-stream"),
    (b"cache-control", b"no-cache"),
    (b"x-accel-buffering", b"no"),
]


class ResponseStream:
    """The HTTP response for one client request.

    The SSE head is sent lazily on the first message. Writes go through a queue
    drained by one writer task, so producers (the upstream reader forwarding
    progress) never block on a slow client.
    """

    def __init__(self, send, receive) -> None:
        self._send = send
        self.disconnected = asyncio.Event()
        self._watcher = asyncio.create_task(self._watch(receive))
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer: asyncio.Task | None = None
        self.started = False
        self.responded = False
        self._closed = False

    async def _watch(self, receive) -> None:
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                self.disconnected.set()
                return

    def _ensure_sse(self) -> None:
        if not self.started:
            self.started = True
            self._writer = asyncio.create_task(self._write_loop())

    async def _write_loop(self) -> None:
        await self._send({"type": "http.response.start", "status": 200, "headers": SSE_HEADERS})
        while True:
            item = await self._queue.get()
            if item is None:
                break
            if isinstance(item, asyncio.Future):
                if not item.done():
                    item.set_result(not self.disconnected.is_set())
                continue
            if self.disconnected.is_set():
                continue
            await self._send({"type": "http.response.body", "body": item, "more_body": True})
        await self._send({"type": "http.response.body", "body": b"", "more_body": False})

    def notify(self, message: dict[str, Any]) -> None:
        """Queue a notification for this request (dropped once the response is sent)."""
        if self.responded or self._closed or self.disconnected.is_set():
            return
        self._ensure_sse()
        self._queue.put_nowait(_sse(message))

    async def respond(self, message: dict[str, Any]) -> bool:
        """Send the final JSON-RPC response. Returns False if the client was already gone."""
        if self.responded or self._closed or self.disconnected.is_set():
            return False
        self.responded = True
        self._ensure_sse()
        written = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_sse(message))
        self._queue.put_nowait(written)
        return await written

    async def respond_json(self, status: int, payload: Any) -> None:
        """Plain (non-SSE) response; only valid before anything was streamed."""
        if self.started:
            await self.respond(payload)
            return
        self.started = self.responded = True
        await _respond(self._send, status, payload)

    async def raw(self, status: int, headers: list[tuple[bytes, bytes]], chunks) -> None:
        """Relay an already-encoded response (used when forwarding to a peer)."""
        self.started = self.responded = True
        await self._send({"type": "http.response.start", "status": status, "headers": headers})
        async for chunk in chunks:
            await self._send({"type": "http.response.body", "body": chunk, "more_body": True})
        await self._send({"type": "http.response.body", "body": b"", "more_body": False})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            self._queue.put_nowait(None)
            try:
                await self._writer
            except Exception:
                pass
        elif not self.started:
            # Superseded or abandoned before anything was written: end the
            # stream with no response at all.
            self._ensure_sse()
            self._queue.put_nowait(None)
            try:
                await self._writer
            except Exception:
                pass
        self._watcher.cancel()


class Front:
    def __init__(self, settings: Settings, bridge, catalog) -> None:
        self.settings = settings
        self.bridge = bridge
        self.catalog = catalog

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        if scope["path"] == "/healthz":
            await _respond(send, 200, {"ok": True, "instance": self.settings.instance})
            return
        if scope["path"] != "/mcp":
            await _respond(send, 404, None)
            return
        if scope["method"] != "POST":
            await _respond(send, 405, None)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        body = await _read_body(receive)
        if body is None:
            return
        principal = principal_for(self.settings, headers)
        if principal is None:
            await _respond(send, 401, _error(None, -32001, "Unauthorized"))
            return
        try:
            message = json.loads(body)
        except ValueError:
            await _respond(send, 400, _error(None, -32700, "Parse error"))
            return
        if not isinstance(message, dict) or "method" not in message:
            await _respond(send, 400, _error(None, -32600, "Invalid request"))
            return
        if "id" not in message:
            await _respond(send, 202, None)
            return
        problem = _validate_headers(headers, message)
        if problem is not None:
            status, code, text = problem
            await _respond(send, status, _error(message["id"], code, text))
            return

        method, rid = message["method"], message["id"]
        if method == "server/discover":
            await _respond(send, 200, _result(rid, {
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-gateway", "version": __version__},
            }))
        elif method == "tools/list":
            tools = await self.catalog.tools_for(principal)
            await _respond(send, 200, _result(rid, {"tools": tools, "ttlMs": CATALOG_TTL_MS, "cacheScope": "private"}))
        elif method == "tools/call":
            stream = ResponseStream(send, receive)
            try:
                await self.bridge.handle_call(principal, message, headers, body, stream)
            finally:
                await stream.close()
        else:
            await _respond(send, 404, _error(rid, -32601, f"Method not found: {method}"))


def _validate_headers(headers: dict[str, str], message: dict) -> tuple[int, int, str] | None:
    params = message.get("params") or {}
    meta = params.get("_meta") or {}
    version = headers.get("mcp-protocol-version")
    if version is None:
        return 400, HEADER_MISMATCH, "Missing MCP-Protocol-Version header"
    if meta.get(META_VERSION) != version:
        return 400, HEADER_MISMATCH, "MCP-Protocol-Version does not match _meta protocolVersion"
    if version != PROTOCOL_VERSION:
        return 400, UNSUPPORTED_VERSION, f"Unsupported protocol version; supported: [{PROTOCOL_VERSION}]"
    if headers.get("mcp-method") != message["method"]:
        return 400, HEADER_MISMATCH, "Mcp-Method header does not match the request"
    if message["method"] == "tools/call" and headers.get("mcp-name") != params.get("name"):
        return 400, HEADER_MISMATCH, "Mcp-Name header does not match params.name"
    return None


def _sse(message: dict) -> bytes:
    return b"event: message\ndata: " + json.dumps(message).encode() + b"\n\n"


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", **result}}


def _error(rid: Any, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}


async def _read_body(receive) -> bytes | None:
    body = b""
    while True:
        event = await receive()
        if event["type"] == "http.disconnect":
            return None
        body += event.get("body", b"")
        if not event.get("more_body"):
            return body


async def _respond(send, status: int, payload: Any) -> None:
    headers = [(b"content-type", b"application/json")] if payload is not None else []
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode() if payload is not None else b""})
