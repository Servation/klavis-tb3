"""Bridging 2026-07-28 tools/call onto 2025-11-25 upstream servers.

A 2026-07-28 client cannot receive server-to-client requests. When an
upstream tool asks for input mid-call (elicitation, sampling, roots), we
answer the client's request with an InputRequiredResult and park the upstream
call; the client's retry carries `inputResponses` and our `requestState`, we
pass the answers upstream and the parked call continues, streaming its
progress and result to the retry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .catalog import Catalog
from .config import Settings
from .legacy_client import LegacyClient, LegacyError, SessionExpired
from .sessions import SessionPool, UnknownPrincipal
from .state import StateStore

log = logging.getLogger("gateway.bridge")

SWEEP_INTERVAL = 0.5


@dataclass
class BridgedCall:
    principal: str
    request_id: Any  # JSON-RPC id of the client request currently served
    tool: str
    arguments: dict[str, Any]
    client: LegacyClient
    progress_token: Any
    stream: Any = None
    rounds: int = 0
    reader: asyncio.Task | None = None
    outcome: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class Bridge:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, pool: SessionPool, catalog: Catalog) -> None:
        self.settings = settings
        self.http = http
        self.pool = pool
        self.catalog = catalog
        self.states = StateStore(settings.state_ttl)
        # In-flight calls by (principal, JSON-RPC id). Ids are unique per
        # session (2025-11-25 base protocol), so they identify a call.
        self.calls: dict[tuple[str, Any], BridgedCall] = {}

    async def handle_call(self, principal: str, message: dict, headers: dict[str, str], body: bytes, stream) -> None:
        rid = message["id"]
        params = message.get("params") or {}
        tool = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            await stream.respond_json(200, _error(rid, -32602, "tools/call needs a name and an arguments object"))
            return
        token = params.get("requestState")
        if token is None:
            await self._initial(principal, rid, tool, arguments, params, stream)
        else:
            await self._resume(principal, rid, token, params, stream)

    async def _initial(self, principal: str, rid: Any, tool: str, arguments: dict, params: dict, stream) -> None:
        try:
            upstream = await self.catalog.resolve(principal, tool)
        except (LegacyError, httpx.HTTPError) as exc:
            await stream.respond_json(200, _error(rid, -32603, f"Upstream unavailable: {exc}"))
            return
        if upstream is None:
            await stream.respond_json(200, _error(rid, -32602, f"Unknown tool: {tool}"))
            return
        try:
            client = self.pool.client(principal, upstream)
        except UnknownPrincipal as exc:
            await stream.respond_json(200, _error(rid, -32602, str(exc)))
            return
        call = BridgedCall(
            principal=principal,
            request_id=rid,
            tool=tool,
            arguments=arguments,
            client=client,
            # progressToken is stable for the life of a request.
            progress_token=(params.get("_meta") or {}).get("progressToken"),
            stream=stream,
        )
        self.calls[(principal, rid)] = call
        call.reader = asyncio.create_task(self._read_upstream(call))
        await self._serve(call, rid, stream)

    async def _resume(self, principal: str, rid: Any, token: str, params: dict, stream) -> None:
        parked = self.states.take(token, principal)
        if parked is None:
            await stream.respond_json(200, _error(rid, -32602, "Invalid requestState: unknown or expired"))
            return
        call = parked.call
        answers = params.get("inputResponses") or {}
        if parked.input_key not in answers:
            await stream.respond_json(200, _error(rid, -32602, f"Missing inputResponses[{parked.input_key!r}]"))
            return
        # The retry is a new request: move the call over to it.
        self.calls.pop((call.principal, call.request_id), None)
        call.request_id = rid
        call.stream = stream
        call.outcome = asyncio.get_running_loop().create_future()
        self.calls[(principal, rid)] = call
        try:
            await call.client.respond(parked.upstream_rid, answers[parked.input_key])
        except (LegacyError, httpx.HTTPError) as exc:
            await stream.respond_json(200, _error(rid, -32603, f"Upstream rejected the input: {exc}"))
            return
        await self._serve(call, rid, stream)

    async def _serve(self, call: BridgedCall, rid: Any, stream) -> None:
        """Stream the call's next outcome to this request."""
        gone = asyncio.create_task(stream.disconnected.wait())
        try:
            done, _ = await asyncio.wait({call.outcome, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
        if call.outcome in done:
            await stream.respond({"jsonrpc": "2.0", "id": rid, **call.outcome.result()})
            return
        # The client closed the stream. Under 2026-07-28 Streamable HTTP the
        # server MUST treat a client disconnect as cancellation of that request.
        await self._cancel(call)

    async def _read_upstream(self, call: BridgedCall) -> None:
        try:
            try:
                await self._consume(call)
            except SessionExpired:
                await self._consume(call)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("upstream call %s failed: %s", call.request_id, exc)
            self._finish(call, {"error": {"code": -32603, "message": f"Upstream failure: {exc}"}})
            return
        if not call.outcome.done():
            self._finish(call, {"error": {"code": -32603, "message": "Upstream ended the call without a result"}})

    async def _consume(self, call: BridgedCall) -> None:
        messages = call.client.call_tool(call.request_id, call.tool, call.arguments, call.progress_token)
        async with contextlib.aclosing(messages):
            async for message in messages:
                if "method" in message and "id" in message:
                    call.rounds += 1
                    key = f"input-{call.rounds}"
                    token = self.states.park(call.principal, call, message["id"], key)
                    self._settle(call, {"result": {
                        "resultType": "input_required",
                        "inputRequests": {key: {"method": message["method"], "params": message.get("params") or {}}},
                        "requestState": token,
                    }})
                elif message.get("method") == "notifications/progress":
                    self._progress(call, message.get("params") or {})
                elif "id" in message and ("result" in message or "error" in message):
                    if "error" in message:
                        self._finish(call, {"error": message["error"]})
                    else:
                        self._finish(call, {"result": {"resultType": "complete", **(message.get("result") or {})}})
                    return

    def _settle(self, call: BridgedCall, outcome: dict) -> None:
        if not call.outcome.done():
            call.outcome.set_result(outcome)

    def _finish(self, call: BridgedCall, outcome: dict) -> None:
        if call.stream is None or call.stream.disconnected.is_set():
            # Nobody is listening for this call any more; free its resources.
            self.calls.pop((call.principal, call.request_id), None)
            return
        self._settle(call, outcome)
        self.calls.pop((call.principal, call.request_id), None)

    def _progress(self, call: BridgedCall, params: dict) -> None:
        target = self.calls.get((call.principal, call.request_id))
        if target is None or target.stream is None or call.progress_token is None:
            return
        target.stream.notify({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})

    async def _cancel(self, call: BridgedCall) -> None:
        self.calls.pop((call.principal, call.request_id), None)
        call.stream = None
        if call.reader is not None:
            # Closing our upstream request is the cancellation signal.
            call.reader.cancel()

    async def sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            dropped = self.states.expire()
            if dropped:
                log.info("expired %d parked requestState(s) at %.0f", dropped, time.time())


def _error(rid: Any, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}
