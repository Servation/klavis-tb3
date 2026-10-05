"""Happy-path tests: one gateway instance, one client, no interruptions.

Runs the development ticketing server and a gateway instance in-process.
Run from /app/gateway with: python -m unittest discover -s unittests
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
DEV = os.path.abspath(os.path.join(HERE, "..", "..", "dev"))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(DEV, "servers"))
sys.path.insert(0, DEV)

import legacykit  # noqa: E402
import tickets  # noqa: E402

from gateway.bridge import Bridge  # noqa: E402
from gateway.catalog import Catalog  # noqa: E402
from gateway.config import Settings, Upstream  # noqa: E402
from gateway.http_front import Front  # noqa: E402
from gateway.sessions import SessionPool  # noqa: E402

PROTOCOL = "2026-07-28"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _call(rid: int, arguments: dict, **extra) -> dict:
    meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL, "progressToken": f"p{rid}"}
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": "create_ticket", "arguments": arguments, "_meta": meta, **extra}}


class HappyPath(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        upstream_port, self.port = _free_port(), _free_port()
        self.tickets = tickets.build({"up-token": "alice"})
        settings = Settings(
            instance="a",
            instances={"a": f"http://127.0.0.1:{self.port}"},
            clients={"client-token": "alice"},
            upstreams={"tickets": Upstream("tickets", f"http://127.0.0.1:{upstream_port}/mcp", {"alice": "up-token"})},
            state_dir=Path(self.tmp.name),
            state_key=secrets.token_bytes(32),
            state_ttl=60,
        )
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
        pool = SessionPool(settings, self.http)
        catalog = Catalog(settings, pool)
        bridge = Bridge(settings, self.http, pool, catalog)
        self.servers = [
            uvicorn.Server(uvicorn.Config(self.tickets, host="127.0.0.1", port=upstream_port, log_level="warning", lifespan="off")),
            uvicorn.Server(uvicorn.Config(Front(settings, bridge, catalog), host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")),
        ]
        self.tasks = [asyncio.create_task(s.serve()) for s in self.servers]
        while not all(s.started for s in self.servers):
            await asyncio.sleep(0.05)

    async def asyncTearDown(self) -> None:
        for server in self.servers:
            server.should_exit = True
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.http.aclose()
        self.tmp.cleanup()

    async def post(self, message: dict) -> list[dict]:
        headers = {
            "authorization": "Bearer client-token",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": PROTOCOL,
            "mcp-method": message["method"],
        }
        if message["method"] == "tools/call":
            headers["mcp-name"] = message["params"]["name"]
        events = []
        async with self.http.stream("POST", f"http://127.0.0.1:{self.port}/mcp", json=message, headers=headers) as resp:
            if resp.headers.get("content-type", "").startswith("text/event-stream"):
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:]))
            else:
                events.append(json.loads(await resp.aread()))
        return events

    async def test_tools_list(self) -> None:
        meta = {"io.modelcontextprotocol/protocolVersion": PROTOCOL}
        [response] = await self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}})
        self.assertEqual([t["name"] for t in response["result"]["tools"]], ["create_ticket"])

    async def test_confirm_and_file(self) -> None:
        arguments = {"title": "Printer on fire"}
        first = (await self.post(_call(1, arguments)))[-1]
        self.assertEqual(first["result"]["resultType"], "input_required")
        [(key, request)] = first["result"]["inputRequests"].items()
        self.assertEqual(request["method"], "elicitation/create")

        answers = {key: {"action": "accept", "content": {"confirm": True}}}
        events = await self.post(_call(2, arguments, inputResponses=answers, requestState=first["result"]["requestState"]))
        final = events[-1]
        self.assertEqual(final["id"], 2)
        self.assertEqual(final["result"]["resultType"], "complete")
        self.assertTrue(final["result"]["structuredContent"]["filed"])
        self.assertEqual(len(self.tickets.recorder.select("commit")), 1)
        progress = [e for e in events if e.get("method") == "notifications/progress"]
        self.assertTrue(progress)

    async def test_unknown_state_is_rejected(self) -> None:
        [response] = await self.post(_call(1, {"title": "x"}, inputResponses={}, requestState="bogus"))
        self.assertEqual(response["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
