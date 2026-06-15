"""The engine: detect updates, then apply them SAFELY (health-check + auto-rollback).

v0.1 status: implemented against the Docker SDK; needs live-Docker testing (Linux + Windows/Docker
Desktop). Run-config preservation on recreate covers the common cases (image/env/ports/volumes/
restart/labels/network-mode); exotic options (caps, devices, extra mounts) are TODO(harden).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import docker  # type: ignore

from . import safety
from .config import ContainerPolicy, policy_for

HISTORY_PATH = Path(os.environ.get("DRYDOCK_HISTORY", "drydock-history.json"))


@dataclass
class Update:
    container_name: str
    image: str            # full ref, e.g. "myapp:1.2.3"
    tag: str
    level: str            # major | minor | patch | unknown


def get_client():
    # from_env() respects DOCKER_HOST and works with the Linux socket AND the
    # Windows/Docker-Desktop named pipe (npipe:////./pipe/docker_engine) — cross-platform.
    return docker.from_env()


def managed_containers(client):
    """Running containers that opted in via drydock.enable=true."""
    return [c for c in client.containers.list() if policy_for(c.labels).enabled]


def _running_repo_digest(container) -> str | None:
    """The manifest digest the running container's image was pulled at (RepoDigests)."""
    digests = container.image.attrs.get("RepoDigests") or []
    return digests[0].split("@", 1)[1] if digests and "@" in digests[0] else None


def check_update(client, container) -> Update | None:
    """Compare the running image's manifest digest to the registry's current digest.

    (Fix vs first draft: compare manifest digest to manifest digest — NOT image.id, which is a
    different sha and would always look 'changed'.)
    """
    image_ref = container.attrs["Config"]["Image"]
    tag = image_ref.split(":")[-1] if ":" in image_ref else "latest"
    try:
        remote_digest = client.images.get_registry_data(image_ref).id
    except docker.errors.APIError:
        return None  # registry unreachable / private without creds -> skip quietly
    if _running_repo_digest(container) == remote_digest:
        return None
    level = "unknown" if tag in ("latest", "") else safety.classify(tag, tag)
    return Update(container.name, image_ref, tag, level)


def health_ok(container, policy: ContainerPolicy) -> bool:
    """Is the (re)started container healthy within the rollback window?"""
    deadline = time.time() + policy.rollback_window
    while time.time() < deadline:
        container.reload()
        if container.status != "running":
            return False
        if policy.healthcheck and policy.healthcheck.startswith("http"):
            if _http_ok(policy.healthcheck):
                return True
        else:
            health = container.attrs.get("State", {}).get("Health", {}).get("Status")
            if health == "healthy":
                return True
            if health is None:  # no HEALTHCHECK defined -> "still running after window" = success
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


def safe_update(client, container, target_image: str, policy: ContainerPolicy) -> str:
    """Pull target_image, recreate the container on it, health-check, ROLL BACK on failure.

    Returns 'updated' | 'rolled_back' | 'error'. target_image lets the daemon pass the detected
    newer image, and lets `drydock apply --to` force a specific image (used for the rollback demo).
    """
    name = container.name
    previous_image = container.image.id  # local id is fine for *rollback* (re-run exact prior image)
    run_config = _capture_run_config(container)
    try:
        client.images.pull(target_image)
        container.stop()
        container.remove()
        run_config["image"] = target_image
        new = client.containers.run(**run_config)
        if health_ok(new, policy):
            _log(name, target_image, "updated")
            return "updated"
        # --- rollback ---
        new.stop()
        new.remove()
        run_config["image"] = previous_image
        client.containers.run(**run_config)
        _log(name, target_image, "rolled_back")
        return "rolled_back"
    except docker.errors.APIError as e:
        _log(name, target_image, f"error: {e}")
        return "error"


def _capture_run_config(container) -> dict:
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
        "network_mode": host.get("NetworkMode") or None,
        "labels": cfg.get("Labels") or {},
    }


def _ports(port_bindings) -> dict:
    out: dict = {}
    for cont_port, binds in (port_bindings or {}).items():
        if binds:
            out[cont_port] = binds[0].get("HostPort")
    return out


def _log(container: str, image: str, result: str) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "container": container, "image": image, "result": result}
    try:
        history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else []
        history.append(entry)
        HISTORY_PATH.write_text(json.dumps(history, indent=2))
    except OSError:
        pass
    print(f"[drydock] {container}: {result} ({image})")
