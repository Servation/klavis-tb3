"""Development legacy MCP server: ticketing.

`create_ticket` asks the user to confirm (elicitation), then takes a few
seconds to file the ticket upstream. Filed tickets are listed at
`GET /_dev/tickets` so duplicates are easy to spot.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import legacykit  # noqa: E402


def build(tokens: dict[str, str]) -> legacykit.KitServer:
    app = legacykit.KitServer("tickets", tokens)
    numbers = itertools.count(1001)
    filed: list[dict] = []

    @app.tool(
        "create_ticket",
        "File a support ticket after the user confirms it.",
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "priority": {"type": "string"}},
            "required": ["title"],
        },
    )
    async def create_ticket(ctx: legacykit.ToolContext, title: str, priority: str = "normal") -> dict:
        answer = await ctx.elicit(
            f"File ticket {title!r} with priority {priority}?",
            {"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"]},
        )
        if answer.get("action") != "accept" or not (answer.get("content") or {}).get("confirm"):
            return {"structuredContent": {"filed": False}}
        await ctx.progress(1, 2, "filing ticket")
        await ctx.sleep(3)
        ticket = f"T-{next(numbers)}"
        ctx.commit(ticket=ticket, title=title)
        filed.append({"ticket": ticket, "title": title, "principal": ctx.principal})
        await ctx.progress(2, 2, "filed")
        return {"structuredContent": {"filed": True, "ticket": ticket}}

    async def list_tickets(scope, receive, send):
        await legacykit._respond(send, 200, filed)

    app.extra_routes[("GET", "/_dev/tickets")] = list_tickets
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--tokens", required=True, help="JSON file: bearer token -> principal")
    args = parser.parse_args()
    asyncio.run(legacykit.serve(build(legacykit.load_tokens(args.tokens)), "127.0.0.1", args.port))
