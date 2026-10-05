"""Minimal MCP 2026-07-28 client for poking at the gateway.

Calls one tool through the load balancer and drives the MRTR loop: every time
the gateway answers with an InputRequiredResult, the next --answer is sent
back for each requested input, together with the requestState.

  python3 client.py create_ticket '{"title": "VPN down"}' \
      --answer '{"action": "accept", "content": {"confirm": true}}'

Every SSE message received is printed as one JSON line.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json

import httpx

PROTOCOL_VERSION = "2026-07-28"
_ids = itertools.count(1)


def _request(tool: str, arguments: dict, progress: bool, extra: dict | None = None) -> dict:
    rid = next(_ids)
    meta = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "dev-client", "version": "0.1"},
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}, "sampling": {}, "roots": {}},
    }
    if progress:
        meta["progressToken"] = f"dev-progress-{rid}"
    params = {"name": tool, "arguments": arguments, "_meta": meta, **(extra or {})}
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params}


async def call(url: str, token: str, message: dict) -> dict | None:
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": PROTOCOL_VERSION,
        "mcp-method": message["method"],
        "mcp-name": message["params"]["name"],
    }
    final = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as http:
        async with http.stream("POST", url, json=message, headers=headers) as response:
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                final = json.loads(await response.aread())
                print(json.dumps(final))
                return final
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    event = json.loads(line[5:].strip())
                    print(json.dumps(event))
                    if event.get("id") == message["id"]:
                        final = event
    return final


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool")
    parser.add_argument("arguments", help="JSON object")
    parser.add_argument("--answer", action="append", default=[], help="JSON result for each round's input requests")
    parser.add_argument("--url", default="http://127.0.0.1:8100/mcp")
    parser.add_argument("--token", default="dev-alice-token")
    parser.add_argument("--progress", action="store_true", help="send a progressToken with every request")
    args = parser.parse_args()

    arguments = json.loads(args.arguments)
    answers = [json.loads(a) for a in args.answer]
    response = await call(args.url, args.token, _request(args.tool, arguments, args.progress))
    while response and (response.get("result") or {}).get("resultType") == "input_required":
        if not answers:
            print("# input required but no more --answer values; stopping")
            return
        answer = answers.pop(0)
        result = response["result"]
        extra = {
            "inputResponses": {key: answer for key in result.get("inputRequests", {})},
            "requestState": result.get("requestState"),
        }
        response = await call(args.url, args.token, _request(args.tool, arguments, args.progress, extra))


if __name__ == "__main__":
    asyncio.run(main())
