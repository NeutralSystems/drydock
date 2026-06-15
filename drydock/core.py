"""The engine: detect updates, then apply them SAFELY (health-check + auto-rollback).

NOTE (v0.1 status): registry-check and the safe-update/rollback flow are implemented against the
Docker SDK but still need hardening + live-Docker testing — especially full run-config preservation
on container recreate (ports/volumes/networks/env/restart-policy). Marked with TODO(harden).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import docker  # type: ignore

from . import safety
from .config import ContainerPolicy, policy_for


@dataclass
class Update:
    container_name: str
    image: str
    current_tag: str
    new_tag: str
    level: str          # major | minor | patch | unknown


def get_client():
    return docker.from_env()


def managed_containers(client):
    """Running containers that opted in via drydock.enable=true."""
    for c in client.containers.list():
        if policy_for(c.labels).enabled:
            yield c


def check_update(client, container) -> Update | None:
    """Compare the container's running image digest to the registry's current digest."""
    image_ref = container.attrs["Config"]["Image"]          # e.g. "myapp:1.2.3"
    tag = image_ref.split(":")[-1] if ":" in image_ref else "latest"
    try:
        running_digest = container.image.id
        remote = client.images.get_registry_data(image_ref)
        remote_digest = remote.id
    except docker.errors.APIError:
        return None                                          # registry unreachable -> skip quietly
    if remote_digest == running_digest:
        return None
    # NOTE: same tag, new digest -> "unknown" level (e.g. :latest moved). Real semver compare
    # happens when the container is pinned to a versioned tag and a newer tag is offered.
    level = safety.classify(tag, tag) if tag not in ("latest", "") else "unknown"
    return Update(container.name, image_ref, tag, tag, level)


def health_ok(container, policy: ContainerPolicy) -> bool:
    """Verify the (re)started container is healthy within the rollback window."""
    deadline = time.time() + policy.rollback_window
    while time.time() < deadline:
        container.reload()
        if container.status != "running":
            return False
        state = container.attrs.get("State", {})
        health = state.get("Health", {}).get("Status")       # if image defines HEALTHCHECK
        if policy.healthcheck and policy.healthcheck.startswith("http"):
            if _http_ok(policy.healthcheck):
                return True
        elif health == "healthy":
            return True
        elif health is None and policy.healthcheck is None:
            # no healthcheck defined anywhere -> treat "still running after window" as success
            pass
        time.sleep(3)
    container.reload()
    return container.status == "running"


def _http_ok(url: str) -> bool:
    import requests
    try:
        return requests.get(url, timeout=5).ok
    except requests.RequestException:
        return False


def safe_update(client, container, update: Update, policy: ContainerPolicy) -> str:
    """Pull new image, recreate the container, health-check, and ROLL BACK on failure.

    Returns: 'updated' | 'rolled_back' | 'error'.
    """
    previous_image = container.image.id                      # snapshot for rollback
    run_config = _capture_run_config(container)              # TODO(harden): cover all run opts
    try:
        client.images.pull(update.image)
        container.stop()
        container.remove()
        new = client.containers.run(update.image, **run_config)
        if health_ok(new, policy):
            return "updated"
        # --- rollback ---
        new.stop(); new.remove()
        run_config["image"] = previous_image
        client.containers.run(**run_config)
        return "rolled_back"
    except docker.errors.APIError:
        return "error"


def _capture_run_config(container) -> dict:
    """Capture enough of a container's config to recreate it. TODO(harden): networks, mounts,
    restart policy, capabilities, etc. v0.1 covers the common cases."""
    attrs = container.attrs
    cfg = attrs["Config"]
    host = attrs["HostConfig"]
    return {
        "image": cfg["Image"],
        "name": container.name,
        "detach": True,
        "environment": cfg.get("Env") or [],
        "ports": _ports(host.get("PortBindings")),
        "volumes": host.get("Binds") or [],
        "restart_policy": host.get("RestartPolicy") or None,
        "labels": cfg.get("Labels") or {},
    }


def _ports(port_bindings) -> dict:
    out: dict = {}
    for cont_port, binds in (port_bindings or {}).items():
        if binds:
            out[cont_port] = binds[0].get("HostPort")
    return out
