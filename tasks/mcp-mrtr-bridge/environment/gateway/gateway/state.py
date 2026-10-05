"""requestState store.

When a bridged call pauses for client input we hand the client an opaque
requestState and park the upstream call under it. The token is a random,
unguessable string, and we bind it to the principal that received it.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Parked:
    principal: str
    call: Any  # bridge.BridgedCall
    upstream_rid: str  # the upstream server-to-client request awaiting the answer
    input_key: str
    expires_at: float


class StateStore:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._parked: dict[str, Parked] = {}

    def park(self, principal: str, call: Any, upstream_rid: str, input_key: str) -> str:
        token = secrets.token_urlsafe(24)
        self._parked[token] = Parked(principal, call, upstream_rid, input_key, time.time() + self.ttl)
        return token

    def take(self, token: str, principal: str) -> Parked | None:
        """Redeem a requestState.

        requestState is single-use: MRTR's security considerations warn that
        binding alone does not prevent replay, so a redeemed state is removed.
        """
        parked = self._parked.get(token)
        if parked is None or parked.principal != principal or time.time() >= parked.expires_at:
            return None
        return self._parked.pop(token)

    def expire(self) -> int:
        """Forget states past their TTL. Returns how many were dropped."""
        now = time.time()
        stale = [t for t, p in self._parked.items() if now >= p.expires_at]
        for token in stale:
            # Nothing to tell upstream: legacy servers time out idle elicitations
            # themselves, we only need to free our side.
            del self._parked[token]
        return len(stale)
