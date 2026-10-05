"""Entry point: `python -m gateway.app` (see run.sh)."""

from __future__ import annotations

import asyncio
import logging

import httpx
import uvicorn

from . import config
from .bridge import Bridge
from .catalog import Catalog
from .http_front import Front
from .sessions import SessionPool


async def serve(settings: config.Settings) -> None:
    timeout = httpx.Timeout(10.0, read=None)
    async with httpx.AsyncClient(timeout=timeout) as http:
        pool = SessionPool(settings, http)
        catalog = Catalog(settings, pool)
        bridge = Bridge(settings, http, pool, catalog)
        sweeper = asyncio.create_task(bridge.sweep_forever())
        host, port = settings.listen
        server = uvicorn.Server(uvicorn.Config(
            Front(settings, bridge, catalog), host=host, port=port,
            http="h11", lifespan="off", log_level="warning",
        ))
        try:
            await server.serve()
        finally:
            sweeper.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(serve(config.load()))


if __name__ == "__main__":
    main()
