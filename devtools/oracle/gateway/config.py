"""Gateway configuration.

Everything comes from environment variables so that every instance of a
deployment can share one config file and differ only in GATEWAY_INSTANCE:

  GATEWAY_CONFIG      path to the deployment JSON (instances, clients, upstreams)
  GATEWAY_INSTANCE    id of this instance; must be a key of "instances"
  GATEWAY_STATE_DIR   directory shared by all instances of the deployment
  GATEWAY_STATE_KEY   hex-encoded secret shared by all instances
  GATEWAY_STATE_TTL   lifetime of an issued requestState, in seconds
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str
    # principal -> bearer token this gateway presents upstream on their behalf
    credentials: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    instance: str
    instances: dict[str, str]
    clients: dict[str, str]
    upstreams: dict[str, Upstream]
    state_dir: Path
    state_key: bytes
    state_ttl: float

    @property
    def listen(self) -> tuple[str, int]:
        parts = urlsplit(self.instances[self.instance])
        return parts.hostname or "127.0.0.1", parts.port or 80


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def load() -> Settings:
    with open(_require("GATEWAY_CONFIG"), encoding="utf-8") as fh:
        raw = json.load(fh)
    instance = _require("GATEWAY_INSTANCE")
    instances = {str(k): str(v).rstrip("/") for k, v in raw["instances"].items()}
    if instance not in instances:
        raise ConfigError(f"GATEWAY_INSTANCE={instance!r} is not listed in the config")
    upstreams = {
        name: Upstream(name=name, url=spec["url"], credentials=dict(spec.get("credentials", {})))
        for name, spec in raw["upstreams"].items()
    }
    state_dir = Path(_require("GATEWAY_STATE_DIR"))
    state_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        instance=instance,
        instances=instances,
        clients=dict(raw["clients"]),
        upstreams=upstreams,
        state_dir=state_dir,
        state_key=bytes.fromhex(_require("GATEWAY_STATE_KEY")),
        state_ttl=float(os.environ.get("GATEWAY_STATE_TTL", "300")),
    )
