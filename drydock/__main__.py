"""Drydock entrypoint — poll for updates and apply them safely (or ask first)."""
from __future__ import annotations

import time

from . import __version__, config, core, safety
from .config import policy_for


def run_once(client) -> None:
    for container in core.managed_containers(client):
        policy = policy_for(container.labels)
        update = core.check_update(client, container)
        if not update:
            continue

        if policy.mode == "auto-safe" and safety.is_auto_safe(update.level):
            print(f"[drydock] {update.container_name}: {update.level} update -> applying safely")
            result = core.safe_update(client, container, update, policy)
            print(f"[drydock] {update.container_name}: {result}")
        else:
            # approval mode (default) OR a major/unknown update in auto-safe mode
            print(
                f"[drydock] UPDATE AVAILABLE for {update.container_name} "
                f"({update.image}, {update.level}). Needs approval — "
                f"run: drydock apply {update.container_name}"
            )
            # TODO(v0.2): persist to history + send notification (email/Discord/webhook)


def main() -> None:
    print(f"drydock {__version__} — safe, reversible container updates")
    print(f"mode={config.GLOBAL_MODE}  poll={config.POLL_SECONDS}s")
    client = core.get_client()
    while True:
        try:
            run_once(client)
        except Exception as e:  # never let one bad cycle kill the daemon
            print(f"[drydock] cycle error: {e}")
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
