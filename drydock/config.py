"""Configuration: global (env) + per-container (labels)."""
from __future__ import annotations

import os
from dataclasses import dataclass

GLOBAL_MODE = os.environ.get("DRYDOCK_MODE", "approve")          # approve | auto-safe
POLL_SECONDS = int(os.environ.get("DRYDOCK_POLL_SECONDS", "3600"))
DEFAULT_ROLLBACK_WINDOW = int(os.environ.get("DRYDOCK_ROLLBACK_WINDOW", "60"))


@dataclass
class ContainerPolicy:
    """Resolved per-container settings, from labels with global fallback."""
    enabled: bool
    mode: str                       # approve | auto-safe
    healthcheck: str | None         # "http://..." | "cmd:..." | None (use Docker HEALTHCHECK)
    rollback_window: int            # seconds to watch for health before declaring success


def policy_for(labels: dict[str, str]) -> ContainerPolicy:
    """Build a ContainerPolicy from a container's labels (drydock.*)."""
    def b(v: str | None, default: bool) -> bool:
        if v is None:
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

    return ContainerPolicy(
        enabled=b(labels.get("drydock.enable"), default=False),
        mode=labels.get("drydock.mode", GLOBAL_MODE),
        healthcheck=labels.get("drydock.healthcheck"),
        rollback_window=int(labels.get("drydock.rollback_window", DEFAULT_ROLLBACK_WINDOW)),
    )
