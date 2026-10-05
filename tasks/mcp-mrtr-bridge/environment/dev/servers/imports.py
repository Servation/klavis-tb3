"""Development legacy MCP server: contact imports and exports.

`import_contacts` asks which field mapping to use (elicitation), streams
progress while it parses, asks the client's model for a one-line summary
(sampling), then commits the import. `sync_contacts` runs for a few seconds
without asking anything, then commits. `export_contacts` asks for the client's
roots before confirming where to write. Committed imports and syncs are listed
at `GET /_dev/imports`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import legacykit  # noqa: E402


def build(tokens: dict[str, str]) -> legacykit.KitServer:
    app = legacykit.KitServer("imports", tokens)
    committed: list[dict] = []

    @app.tool(
        "import_contacts",
        "Import contacts from a CSV source after choosing a field mapping.",
        {
            "type": "object",
            "properties": {"source": {"type": "string"}, "rows": {"type": "integer"}},
            "required": ["source"],
        },
    )
    async def import_contacts(ctx: legacykit.ToolContext, source: str, rows: int = 120) -> dict:
        mapping = await ctx.elicit(
            f"Which column holds the e-mail address in {source}?",
            {"type": "object", "properties": {"email_column": {"type": "string"}}, "required": ["email_column"]},
        )
        column = (mapping.get("content") or {}).get("email_column", "email")
        for step in range(1, 5):
            await ctx.sleep(0.4)
            await ctx.progress(step, 8, f"parsed {step * rows // 4} rows")
        summary = await ctx.sample(f"Summarise an import of {rows} contacts from {source} in one line.")
        text = ((summary.get("content") or {}).get("text") or "").strip()
        for step in range(5, 9):
            await ctx.sleep(0.4)
            await ctx.progress(step, 8, "writing contacts")
        ctx.commit(source=source, rows=rows, email_column=column)
        committed.append({"source": source, "rows": rows, "principal": ctx.principal})
        return {"structuredContent": {"imported": rows, "email_column": column, "summary": text}}

    @app.tool(
        "sync_contacts",
        "Re-sync contacts from a source, overwriting local changes. Runs without asking anything.",
        {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"]},
    )
    async def sync_contacts(ctx: legacykit.ToolContext, source: str) -> dict:
        for step in range(1, 7):
            await ctx.sleep(0.5)
            await ctx.progress(step, 6, f"compared {step * 50} contacts")
        ctx.commit(source=source, synced=True)
        committed.append({"source": source, "synced": True, "principal": ctx.principal})
        return {"structuredContent": {"synced": True, "source": source}}

    @app.tool(
        "export_contacts",
        "Export contacts into one of the client's workspace roots.",
        {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]},
    )
    async def export_contacts(ctx: legacykit.ToolContext, filename: str) -> dict:
        roots = (await ctx.list_roots()).get("roots") or []
        if not roots:
            return {"structuredContent": {"exported": False, "reason": "no roots"}}
        target = roots[0]["uri"].rstrip("/") + "/" + filename
        answer = await ctx.elicit(
            f"Write contacts to {target}?",
            {"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"]},
        )
        if answer.get("action") != "accept" or not (answer.get("content") or {}).get("confirm"):
            return {"structuredContent": {"exported": False}}
        await ctx.sleep(1)
        ctx.commit(target=target)
        return {"structuredContent": {"exported": True, "target": target}}

    async def list_imports(scope, receive, send):
        await legacykit._respond(send, 200, committed)

    app.extra_routes[("GET", "/_dev/imports")] = list_imports
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9102)
    parser.add_argument("--tokens", required=True, help="JSON file: bearer token -> principal")
    args = parser.parse_args()
    asyncio.run(legacykit.serve(build(legacykit.load_tokens(args.tokens)), "127.0.0.1", args.port))
