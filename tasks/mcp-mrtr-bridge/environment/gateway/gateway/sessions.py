"""Pool of upstream sessions, one per (principal, upstream) in this process."""

from __future__ import annotations

import httpx

from .config import Settings
from .legacy_client import LegacyClient


class UnknownPrincipal(LookupError):
    pass


class SessionPool:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._clients: dict[tuple[str, str], LegacyClient] = {}

    def client(self, principal: str, upstream: str) -> LegacyClient:
        key = (principal, upstream)
        client = self._clients.get(key)
        if client is None:
            spec = self._settings.upstreams[upstream]
            token = spec.credentials.get(principal)
            if token is None:
                raise UnknownPrincipal(f"no {upstream} credentials for {principal}")
            client = LegacyClient(self._http, spec.url, token)
            self._clients[key] = client
        return client
