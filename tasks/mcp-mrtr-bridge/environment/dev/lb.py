"""Round-robin TCP load balancer for the development stack.

Like production, it has no session affinity: consecutive client connections
alternate between gateway instances. It relays bytes only, so a client closing
its connection reaches the gateway as a closed connection.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main(listen: int, backends: list[tuple[str, int]]) -> None:
    choice = itertools.cycle(backends)

    async def handle(client_reader, client_writer):
        host, port = next(choice)
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))

    server = await asyncio.start_server(handle, "127.0.0.1", listen)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, default=8100)
    parser.add_argument("--backend", action="append", required=True, help="host:port, repeatable")
    args = parser.parse_args()
    targets = [(b.rsplit(":", 1)[0], int(b.rsplit(":", 1)[1])) for b in args.backend]
    asyncio.run(main(args.listen, targets))
