"""Drydock CLI.

  drydock run                  # daemon: poll + notify (or auto-apply safe updates)
  drydock status               # list managed containers + whether an update is available
  drydock apply <name>         # safely apply the detected update (health-check + rollback)
  drydock apply <name> --to IMG  # force-update to a specific image (used to DEMO rollback)
"""
from __future__ import annotations

import argparse
import sys
import time

from . import __version__, config, core, safety
from .config import policy_for


def cmd_run(client, _args) -> None:
    print(f"drydock {__version__} — mode={config.GLOBAL_MODE} poll={config.POLL_SECONDS}s")
    while True:
        try:
            for c in core.managed_containers(client):
                pol = policy_for(c.labels)
                up = core.check_update(client, c)
                if not up:
                    continue
                if pol.mode == "auto-safe" and safety.is_auto_safe(up.level):
                    print(f"[drydock] {up.container_name}: {up.level} update -> applying safely")
                    core.safe_update(client, c, up.image, pol)
                else:
                    print(f"[drydock] UPDATE for {up.container_name} ({up.image}, {up.level}) "
                          f"-> approve with: drydock apply {up.container_name}")
        except Exception as e:  # never let one cycle kill the daemon
            print(f"[drydock] cycle error: {e}")
        time.sleep(config.POLL_SECONDS)


def cmd_status(client, _args) -> None:
    found = False
    for c in core.managed_containers(client):
        found = True
        up = core.check_update(client, c)
        state = f"update available ({up.level})" if up else "up to date"
        print(f"  {c.name:<28} {c.attrs['Config']['Image']:<28} {state}")
    if not found:
        print("  (no managed containers — add label drydock.enable=true)")


def cmd_apply(client, args) -> None:
    try:
        c = client.containers.get(args.name)
    except Exception:
        print(f"no such container: {args.name}"); sys.exit(1)
    pol = policy_for(c.labels)
    if args.to:
        target = args.to                                  # forced (manual / rollback demo)
    else:
        up = core.check_update(client, c)
        if not up:
            print(f"{args.name}: already up to date"); return
        target = up.image
    print(f"[drydock] applying {target} to {args.name} (health-check {pol.rollback_window}s, auto-rollback on fail)")
    print(f"[drydock] result: {core.safe_update(client, c, target, pol)}")


def main() -> None:
    p = argparse.ArgumentParser(prog="drydock", description="Safe, reversible Docker updates.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="daemon: poll and notify/auto-apply")
    sub.add_parser("status", help="list managed containers + update status")
    ap = sub.add_parser("apply", help="safely apply an update (with rollback)")
    ap.add_argument("name")
    ap.add_argument("--to", help="force-update to a specific image (for testing/rollback demo)")

    args = p.parse_args()
    client = core.get_client()
    {"run": cmd_run, "status": cmd_status, "apply": cmd_apply}.get(args.cmd or "run", cmd_run)(client, args)


if __name__ == "__main__":
    main()
