# mcp-gateway

Front door for our MCP tool catalogue. Clients speak MCP **2026-07-28** over
Streamable HTTP; the tool servers behind the gateway still speak
**2025-11-25**. The gateway lists their tools and bridges `tools/call`,
including tools that ask the user for input in the middle of a call
(elicitation, sampling, roots), which 2026-07-28 clients handle as
multi round-trip requests (MRTR).

## Running

Each instance is started with `./run.sh` (Python 3.12, only the packages that
are already installed). Production runs two instances behind a load balancer
with no session affinity. All instances of a deployment share the config file,
`GATEWAY_STATE_DIR` and `GATEWAY_STATE_KEY`.

| Variable | Meaning |
|---|---|
| `GATEWAY_CONFIG` | Path to the deployment config (below). |
| `GATEWAY_INSTANCE` | This instance's id; a key of `instances` in the config. The instance listens on the host and port of its URL. |
| `GATEWAY_STATE_DIR` | Directory shared by all instances (writable). |
| `GATEWAY_STATE_KEY` | Hex-encoded secret shared by all instances. |
| `GATEWAY_STATE_TTL` | Seconds an issued `requestState` stays valid (default 300). |

Config file:

```json
{
  "instances": {"a": "http://127.0.0.1:8101", "b": "http://127.0.0.1:8102"},
  "clients":   {"<client bearer token>": "<principal>"},
  "upstreams": {
    "<name>": {
      "url": "http://127.0.0.1:9101/mcp",
      "credentials": {"<principal>": "<bearer token for this upstream>"}
    }
  }
}
```

A principal can use an upstream's tools only if the upstream lists
credentials for it. The gateway keeps one upstream session per principal and
upstream.

## Client-facing endpoint

`POST /mcp`, protocol 2026-07-28. Requests must carry `Authorization: Bearer
<client token>`, `MCP-Protocol-Version: 2026-07-28` (matching
`_meta["io.modelcontextprotocol/protocolVersion"]`), `Mcp-Method`, and for
`tools/call` also `Mcp-Name`. Supported methods: `server/discover`,
`tools/list`, `tools/call`. `tools/call` is answered with an SSE stream
(progress notifications for the request's `progressToken`, then the response);
validation failures are answered with plain JSON.

`GET /healthz` reports the instance id.

### Error codes

| Code | HTTP | When |
|---|---|---|
| `-32001` | 401 | Missing or unknown client token. |
| `-32020` | 400 | Header does not match the body (`HeaderMismatch`). |
| `-32022` | 400 | Unsupported protocol version. |
| `-32601` | 404 | Unknown method. |
| `-32602` | 200 | Unknown tool, bad arguments, or a `requestState` that is invalid, expired, or was issued to another principal or for another call. |
| `-32603` | 200 | Upstream failure. |

## Layout

```
gateway/app.py           process entry point (uvicorn)
gateway/http_front.py    2026-07-28 endpoint, header validation, SSE responses
gateway/bridge.py        tools/call bridging and MRTR
gateway/state.py         requestState handling
gateway/legacy_client.py 2025-11-25 upstream client
gateway/sessions.py      upstream session pool
gateway/catalog.py       tool catalogue (tools/list fan-out, routing)
gateway/config.py        configuration
gateway/auth.py          client authentication
unittests/               unit tests (python -m unittest discover -s unittests)
```
