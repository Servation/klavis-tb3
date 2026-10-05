"""Tool catalogue: which upstream serves which tool.

tools/list fans out to every upstream the principal has credentials for. The
merged name -> upstream map is cached in memory and written to
GATEWAY_STATE_DIR/catalog.json so a freshly started instance can route calls
before it has listed anything itself.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .config import Settings
from .legacy_client import SessionExpired
from .sessions import SessionPool

CATALOG_TTL_MS = 30_000


class Catalog:
    def __init__(self, settings: Settings, pool: SessionPool) -> None:
        self._settings = settings
        self._pool = pool
        self._routes: dict[str, str] = {}
        self._path = settings.state_dir / "catalog.json"
        self._load()

    async def tools_for(self, principal: str) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for name, spec in self._settings.upstreams.items():
            if principal not in spec.credentials:
                continue
            client = self._pool.client(principal, name)
            try:
                listed = await client.list_tools()
            except SessionExpired:
                listed = await client.list_tools()
            for tool in listed:
                self._routes[tool["name"]] = name
                tools.append(tool)
        tools.sort(key=lambda t: t["name"])
        self._save()
        return tools

    async def resolve(self, principal: str, tool: str) -> str | None:
        if tool not in self._routes:
            await self.tools_for(principal)
        upstream = self._routes.get(tool)
        if upstream is None or principal not in self._settings.upstreams[upstream].credentials:
            return None
        return upstream

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                routes = json.load(fh)
        except (OSError, ValueError):
            return
        self._routes.update({k: v for k, v in routes.items() if v in self._settings.upstreams})

    def _save(self) -> None:
        tmp = self._path.with_name(f".catalog.{self._settings.instance}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._routes, fh, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError:
            pass
