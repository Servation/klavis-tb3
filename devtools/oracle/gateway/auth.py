"""Client authentication: bearer token -> principal."""

from __future__ import annotations

from .config import Settings


def principal_for(settings: Settings, headers: dict[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return settings.clients.get(auth[7:].strip())
