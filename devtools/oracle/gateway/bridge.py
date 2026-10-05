"""Bridging 2026-07-28 tools/call (MRTR) onto 2025-11-25 upstream executions.

The two protocol revisions disagree about lifetimes:

* 2026-07-28: a request lives exactly as long as its response stream; closing
  the stream cancels that request. Input is gathered by returning an
  InputRequiredResult and having the client retry the call (a new request)
  with `inputResponses` and the `requestState` we issued.
* 2025-11-25: one `tools/call` request lives across every server-to-client
  request it makes; a dropped connection is not a cancellation, only
  `notifications/cancelled` is.

So an upstream execution must outlive the client request that started it. It
is modelled here as an `Execution` with its own identity, owned by the
instance that started it (its home), and its output is cut into rounds:

  round 0 outcome  = what answers the initial request
  round k outcome  = what answers a retry carrying state_k (k >= 1)

where each outcome is either the next InputRequiredResult (carrying
state_{k+1}) or the final result. Outcomes are kept until the latest state
expires, so a re-issued retry (after a broken stream, on either instance, with
the same state) receives the outcome of its round, and the answers of a round
are forwarded upstream exactly once.

An execution is cancelled upstream only when no client can obtain its outcome
any more: the initial request's stream closed before it was answered (the
client has no state to come back with), or the latest state expired while no
request was attached.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import peers
from .catalog import Catalog
from .config import Settings
from .legacy_client import LegacyClient, LegacyError, SessionExpired
from .sessions import SessionPool, UnknownPrincipal
from .state import StateCodec, StateError, call_digest

log = logging.getLogger("gateway.bridge")

SWEEP_INTERVAL = 0.5
SUPERSEDED = object()


@dataclass
class Waiter:
    """A client request currently attached to an execution."""

    round: int
    stream: Any
    progress_token: Any
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


@dataclass
class Execution:
    id: str
    principal: str
    tool: str
    arguments: dict[str, Any]
    digest: str
    client: LegacyClient
    upstream_id: str
    rounds: int = 0
    pending: dict[int, tuple[str, str]] = field(default_factory=dict)  # round -> (input key, upstream rid)
    consumed: set[int] = field(default_factory=set)
    outcomes: dict[int, dict[str, Any]] = field(default_factory=dict)  # round -> JSON-RPC result/error body
    waiter: Waiter | None = None
    # Expiry is enforced here, on the home instance, with the monotonic clock:
    # the wall clock can step (NTP, VM time sync) and must not revive a state.
    issued: dict[int, float] = field(default_factory=dict)  # round -> monotonic time state_k was issued
    expires_at: float | None = None  # monotonic deadline of the latest state
    done: bool = False
    closed: bool = False
    reader: asyncio.Task | None = None


class Bridge:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, pool: SessionPool, catalog: Catalog) -> None:
        self.settings = settings
        self.http = http
        self.pool = pool
        self.catalog = catalog
        self.codec = StateCodec(settings.state_key, settings.state_ttl)
        self.executions: dict[str, Execution] = {}

    # ----- entry point ------------------------------------------------------

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
            return
        try:
            state = self.codec.open(token, principal=principal, digest=call_digest(tool, arguments))
        except StateError as exc:
            await stream.respond_json(200, _error(rid, -32602, f"Invalid requestState: {exc}"))
            return
        if state["h"] != self.settings.instance:
            home = self.settings.instances.get(state["h"])
            if home is None:
                await stream.respond_json(200, _error(rid, -32602, "Invalid requestState: unknown instance"))
                return
            await peers.forward(self.http, home, self.settings.instance, headers, body, stream)
            return
        await self._retry(rid, state, params, stream)

    # ----- initial request --------------------------------------------------

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
        exec_id = secrets.token_hex(16)
        execution = Execution(
            id=exec_id,
            principal=principal,
            tool=tool,
            arguments=arguments,
            digest=call_digest(tool, arguments),
            client=client,
            upstream_id=f"gw-{self.settings.instance}-{exec_id}",
        )
        self.executions[exec_id] = execution
        waiter = self._attach(execution, 0, params, stream)
        execution.reader = asyncio.create_task(self._read_upstream(execution))
        await self._wait(execution, waiter, rid, stream, initial=True)

    # ----- retries ----------------------------------------------------------

    async def _retry(self, rid: Any, state: dict, params: dict, stream) -> None:
        execution = self.executions.get(state["e"])
        if execution is None or execution.closed:
            await stream.respond_json(200, _error(rid, -32602, "Invalid requestState: the call is no longer available"))
            return
        k = int(state["k"])
        if time.monotonic() >= execution.issued.get(k, 0.0) + self.settings.state_ttl:
            await stream.respond_json(200, _error(rid, -32602, "Invalid requestState: requestState has expired"))
            return
        if k in execution.outcomes:
            # Re-issued retry for a round that already has its outcome.
            await self._deliver(execution, rid, execution.outcomes[k], stream)
            return
        if k in execution.consumed:
            # Re-issued while the round is still running: take over delivery.
            await self._wait(execution, self._attach(execution, k, params, stream), rid, stream, initial=False)
            return
        key, upstream_rid = execution.pending[k]
        answers = params.get("inputResponses") or {}
        if key not in answers:
            # MRTR: ask again for missing input instead of failing.
            await self._deliver(execution, rid, execution.outcomes[k - 1], stream)
            return
        # Claim the round before awaiting anything so a concurrent duplicate of
        # this retry cannot forward the answers a second time, and attach first
        # so progress the upstream emits right after the answer reaches us.
        execution.consumed.add(k)
        waiter = self._attach(execution, k, params, stream)
        try:
            await execution.client.respond(upstream_rid, answers[key])
        except (LegacyError, httpx.HTTPError) as exc:
            execution.consumed.discard(k)
            if execution.waiter is waiter:
                execution.waiter = None
            await stream.respond_json(200, _error(rid, -32603, f"Upstream rejected the input: {exc}"))
            return
        await self._wait(execution, waiter, rid, stream, initial=False)

    # ----- attaching a client request to an execution ---------------------------

    def _attach(self, execution: Execution, k: int, params: dict, stream) -> Waiter:
        progress_token = (params.get("_meta") or {}).get("progressToken")
        waiter = Waiter(round=k, stream=stream, progress_token=progress_token)
        previous = execution.waiter
        execution.waiter = waiter
        if previous is not None and not previous.future.done():
            # At most one attached request per call: the newest one wins and the
            # previous stream gets nothing more.
            previous.future.set_result(SUPERSEDED)
        if k in execution.outcomes:
            waiter.future.set_result(execution.outcomes[k])
        return waiter

    async def _wait(self, execution: Execution, waiter: Waiter, rid: Any, stream, *, initial: bool) -> None:
        gone = asyncio.create_task(stream.disconnected.wait())
        try:
            done, _ = await asyncio.wait({waiter.future, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            gone.cancel()
        if execution.waiter is waiter:
            execution.waiter = None

        if waiter.future.done():
            outcome = waiter.future.result()
            if outcome is SUPERSEDED:
                return
            delivered = await self._deliver(execution, rid, outcome, stream)
            if delivered or not initial:
                return
        # The client closed this request's stream before it got an answer.
        if initial:
            # Nobody holds a requestState for this call, so nobody can ever
            # obtain its outcome: this is the 2026 cancellation of the request,
            # translated to the explicit 2025 cancellation upstream.
            await self._cancel(execution, "client closed the request stream")
        # A retry's stream closing only detaches it; the execution stays
        # resumable with its state until that state expires.

    async def _deliver(self, execution: Execution, rid: Any, outcome: dict, stream) -> bool:
        delivered = await stream.respond({"jsonrpc": "2.0", "id": rid, **outcome})
        if delivered and execution.done and execution.expires_at is None:
            # Completed without ever issuing a state: nobody can retry it.
            self._close(execution)
        return delivered

    # ----- upstream side ----------------------------------------------------

    async def _read_upstream(self, execution: Execution) -> None:
        try:
            try:
                await self._consume(execution)
            except SessionExpired:
                # The session ended before the call started: start a new one.
                await self._consume(execution)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # upstream failure ends the call with an error
            log.warning("upstream call %s failed: %s", execution.upstream_id, exc)
            if not execution.done:
                execution.done = True
                self._publish(execution, execution.rounds, {"error": {"code": -32603, "message": f"Upstream failure: {exc}"}})
            return
        if not execution.done and not execution.closed:
            execution.done = True
            self._publish(execution, execution.rounds, {"error": {"code": -32603, "message": "Upstream ended the call without a result"}})

    async def _consume(self, execution: Execution) -> None:
        messages = execution.client.call_tool(execution.upstream_id, execution.tool, execution.arguments, execution.id)
        async with contextlib.aclosing(messages):
            async for message in messages:
                if self._on_upstream_message(execution, message):
                    return

    def _on_upstream_message(self, execution: Execution, message: dict) -> bool:
        """Handle one upstream message; return True once the call is finished."""
        if "method" in message and "id" in message:
            execution.rounds += 1
            k = execution.rounds
            key = f"input-{k}"
            execution.pending[k] = (key, message["id"])
            state, _ = self.codec.issue(
                execution=execution.id, round=k, home=self.settings.instance,
                principal=execution.principal, digest=execution.digest,
            )
            execution.issued[k] = time.monotonic()
            execution.expires_at = execution.issued[k] + self.settings.state_ttl
            self._publish(execution, k - 1, {"result": {
                "resultType": "input_required",
                "inputRequests": {key: {"method": message["method"], "params": message.get("params") or {}}},
                "requestState": state,
            }})
            return False
        if message.get("method") == "notifications/progress":
            self._forward_progress(execution, message.get("params") or {})
            return False
        if message.get("id") == execution.upstream_id:
            execution.done = True
            if "error" in message:
                outcome = {"error": message["error"]}
            else:
                outcome = {"result": {"resultType": "complete", **(message.get("result") or {})}}
            self._publish(execution, execution.rounds, outcome)
            return True
        return False

    def _publish(self, execution: Execution, k: int, outcome: dict) -> None:
        execution.outcomes[k] = outcome
        waiter = execution.waiter
        if waiter is not None and waiter.round == k and not waiter.future.done():
            waiter.future.set_result(outcome)

    def _forward_progress(self, execution: Execution, params: dict) -> None:
        waiter = execution.waiter
        if waiter is None or waiter.future.done() or waiter.progress_token is None:
            return
        forwarded = {k: v for k, v in params.items() if k in ("progress", "total", "message")}
        forwarded["progressToken"] = waiter.progress_token
        waiter.stream.notify({"jsonrpc": "2.0", "method": "notifications/progress", "params": forwarded})

    # ----- cancellation and expiry ------------------------------------------

    async def _cancel(self, execution: Execution, reason: str) -> None:
        if execution.closed:
            return
        self._close(execution)
        if not execution.done:
            try:
                await execution.client.cancel(execution.upstream_id, reason)
            except (LegacyError, httpx.HTTPError) as exc:
                log.warning("could not cancel %s upstream: %s", execution.upstream_id, exc)
        if execution.reader is not None:
            execution.reader.cancel()

    def _close(self, execution: Execution) -> None:
        execution.closed = True
        self.executions.pop(execution.id, None)

    async def sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            now = time.monotonic()
            for execution in list(self.executions.values()):
                if execution.waiter is not None or execution.expires_at is None:
                    continue
                if now < execution.expires_at:
                    continue
                if execution.done:
                    self._close(execution)
                else:
                    await self._cancel(execution, "requestState expired")


def _error(rid: Any, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}
