"""The engine: detect updates, then apply them SAFELY (health-check + auto-rollback).

Safety contract: an update must NEVER leave a container worse off than before it ran. To honor
that, we never destroy the original container until a replacement is confirmed healthy:

    pull target  ->  stop original  ->  rename original aside (kept intact)
                 ->  create + start the new container (faithful full-config clone, new image)
                 ->  health-check
                       healthy : remove the old one            -> "updated"
                       unhealthy: kill new, restore original    -> "rolled_back"
                       any error: kill new (if any), restore original -> "error"

The original is a real, untouched container the whole time, so rollback restarts the *exact*
prior container (same volumes, config, identity) rather than a lossy reconstruction.
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

# Network modes that aren't user-defined networks (handled by HostConfig.NetworkMode directly).
_SPECIAL_NET_MODES = ("", "default", "host", "none")


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
    """Running containers that opted in via drydock.enable=true (never drydock itself)."""
    return [c for c in client.containers.list()
            if policy_for(c.labels).enabled and not _is_self(c)]


def _is_self(container) -> bool:
    """Don't let drydock update/kill the container it's running in."""
    self_id = os.environ.get("HOSTNAME", "")
    if self_id and container.id.startswith(self_id):
        return True
    image = (container.attrs.get("Config", {}) or {}).get("Image") or ""
    return "drydock" in image.lower()


# --------------------------------------------------------------------------- detect

def _split_tag(image_ref: str) -> str:
    """The tag portion of an image ref, defaulting to 'latest'. Handles registry ports & digests."""
    ref = image_ref.split("@", 1)[0]          # drop any @sha256:... digest
    last = ref.rsplit("/", 1)[-1]             # last path segment (where the tag lives)
    return last.rsplit(":", 1)[1] if ":" in last else "latest"


def _running_repo_digest(container) -> str | None:
    """The manifest digest the running container's image was pulled at (RepoDigests)."""
    digests = container.image.attrs.get("RepoDigests") or []
    return digests[0].split("@", 1)[1] if digests and "@" in digests[0] else None


def check_update(client, container) -> Update | None:
    """Compare the running image's manifest digest to the registry's current digest.

    Returns None (no update / can't tell) for: locally-built images with no RepoDigest, and
    registries we can't reach or read (private without creds) — we never guess.
    """
    image_ref = container.attrs["Config"]["Image"]
    running_digest = _running_repo_digest(container)
    if running_digest is None:
        return None  # locally-built / never-pushed image: nothing to compare against
    try:
        remote_digest = client.images.get_registry_data(image_ref).id
    except docker.errors.APIError:
        return None  # registry unreachable / private without creds -> skip quietly
    if running_digest == remote_digest:
        return None
    tag = _split_tag(image_ref)
    level = "unknown" if tag in ("latest", "") else safety.classify(tag, tag)
    return Update(container.name, image_ref, tag, level)


# --------------------------------------------------------------------------- health

def health_ok(container, policy: ContainerPolicy) -> bool:
    """Is the (re)started container healthy within the rollback window?

    Priority: explicit drydock.healthcheck (http:// or cmd:...) > the image's Docker HEALTHCHECK >
    "still running when the window elapses". Fails fast on exited/dead/unhealthy.
    """
    deadline = time.time() + policy.rollback_window
    hc = policy.healthcheck
    while time.time() < deadline:
        try:
            container.reload()
        except docker.errors.NotFound:
            return False
        status = container.status
        if status in ("exited", "dead", "removing"):
            return False
        if status != "running":          # created / restarting -> keep waiting
            time.sleep(1)
            continue
        if hc and hc.startswith("http"):
            if _http_ok(hc):
                return True
        elif hc and hc.startswith("cmd:"):
            if _exec_ok(container, hc[len("cmd:"):]):
                return True
        else:
            health = (container.attrs.get("State", {}).get("Health") or {}).get("Status")
            if health == "healthy":
                return True
            if health == "unhealthy":
                return False
            # health is None -> no HEALTHCHECK defined; "running past the window" is our signal
        time.sleep(3)
    try:
        container.reload()
    except docker.errors.NotFound:
        return False
    return container.status == "running"


def _http_ok(url: str) -> bool:
    import requests
    try:
        return requests.get(url, timeout=5).ok
    except requests.RequestException:
        return False


def _exec_ok(container, cmd: str) -> bool:
    """Run a command inside the container; healthy iff it exits 0 (like Docker's CMD healthcheck)."""
    try:
        return container.exec_run(cmd).exit_code == 0
    except docker.errors.APIError:
        return False


# --------------------------------------------------------------------------- apply

def safe_update(client, container, target_image: str, policy: ContainerPolicy) -> str:
    """Pull target_image, recreate the container on it, health-check, ROLL BACK on any failure.

    Returns 'updated' | 'rolled_back' | 'error'. The original container is preserved (stopped +
    renamed) until a healthy replacement exists, so no failure path can leave the user with nothing.
    """
    name = container.name
    old = container
    backup = f"{name}__drydock_bak"

    # 1. Pull FIRST — fully non-destructive. A bad ref / network error aborts before we touch anything.
    try:
        client.images.pull(target_image)
    except docker.errors.APIError as e:
        _log(name, target_image, f"error: pull failed: {e}")
        return "error"

    try:
        old.reload()
    except docker.errors.NotFound:
        _log(name, target_image, "error: container disappeared before update")
        return "error"

    _remove_if_exists(client, backup)        # clear any leftover backup from a prior crash

    new = None
    renamed = False
    try:
        # 2. Stop + rename the original aside. It stays intact for an exact rollback.
        old.stop()
        old.rename(backup)
        renamed = True
        # 3. Create + start the replacement on the new image, cloning the full config.
        new = _recreate_on_image(client, old, target_image, name)
        # 4. Health-check.
        if health_ok(new, policy):
            _remove(old)                     # success: discard the old container
            _log(name, target_image, "updated")
            return "updated"
        # 5. Unhealthy -> roll back: destroy the new one, restore the original exactly.
        _remove(new)
        new = None
        old.rename(name)
        renamed = False
        old.start()
        _log(name, target_image, "rolled_back")
        return "rolled_back"
    except Exception as e:  # noqa: BLE001 — last line of defense; the original MUST come back
        if new is not None:
            _remove(new)
        if renamed:
            try:
                old.rename(name)
            except docker.errors.APIError:
                pass
        try:
            old.reload()
            if old.status != "running":
                old.start()
        except docker.errors.APIError:
            pass
        _log(name, target_image, f"error: {e}")
        return "error"


def _recreate_on_image(client, old, target_image: str, name: str):
    """Create + start a container named `name` on target_image, faithfully cloning `old`'s config.

    Everything in the original's HostConfig (caps, devices, mounts, restart policy, log config,
    sysctls, ulimits, network mode, ...) is passed through verbatim, so we don't silently drop
    settings the way a hand-picked subset would. User-defined networks are re-attached with their
    aliases preserved for service discovery.
    """
    api = client.api
    attrs = old.attrs
    config = attrs.get("Config", {}) or {}
    host_config = attrs.get("HostConfig", {}) or {}
    networks = (attrs.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}
    net_mode = host_config.get("NetworkMode", "default")

    # User-defined networks (not the default bridge / host / none / container:). These carry
    # aliases + static IPs we must re-attach explicitly; the default bridge is handled by NetworkMode.
    user_nets = {}
    if net_mode not in _SPECIAL_NET_MODES and not str(net_mode).startswith("container:"):
        user_nets = {n: ep for n, ep in networks.items() if n != "bridge"}

    networking_config = None
    primary = None
    if user_nets:
        primary = net_mode if net_mode in user_nets else next(iter(user_nets))
        networking_config = api.create_networking_config({primary: _endpoint(api, user_nets[primary], old)})

    create_kwargs = {
        "image": target_image,
        "command": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
        "environment": config.get("Env"),
        "labels": config.get("Labels") or {},
        "user": config.get("User") or "",
        "working_dir": config.get("WorkingDir") or "",
        "hostname": config.get("Hostname") or None,
        "stop_signal": config.get("StopSignal") or None,
        "healthcheck": config.get("Healthcheck") or None,
        "name": name,
        "detach": True,
        "host_config": host_config,
        "networking_config": networking_config,
    }
    create_kwargs = {k: v for k, v in create_kwargs.items() if v is not None}

    new_id = api.create_container(**create_kwargs)["Id"]
    # Attach any remaining user-defined networks (multi-network containers).
    for n, ep in user_nets.items():
        if n == primary:
            continue
        try:
            api.connect_container_to_network(new_id, n, **_connect_opts(ep, old))
        except docker.errors.APIError:
            pass
    api.start(new_id)
    return client.containers.get(new_id)


def _endpoint(api, ep: dict, old):
    ep = ep or {}
    ipam = ep.get("IPAMConfig") or {}
    return api.create_endpoint_config(
        aliases=_aliases(ep, old) or None,
        ipv4_address=ipam.get("IPv4Address") or None,
        ipv6_address=ipam.get("IPv6Address") or None,
    )


def _connect_opts(ep: dict, old) -> dict:
    ep = ep or {}
    ipam = ep.get("IPAMConfig") or {}
    opts: dict = {}
    aliases = _aliases(ep, old)
    if aliases:
        opts["aliases"] = aliases
    if ipam.get("IPv4Address"):
        opts["ipv4_address"] = ipam["IPv4Address"]
    return opts


def _aliases(ep: dict, old) -> list:
    """User-defined network aliases, minus the auto short-id alias tied to the old container."""
    short_id = old.id[:12]
    return [a for a in ((ep or {}).get("Aliases") or []) if a and a != short_id]


# --------------------------------------------------------------------------- helpers

def _remove(container) -> None:
    try:
        container.remove(force=True)
    except docker.errors.APIError:
        pass


def _remove_if_exists(client, name: str) -> None:
    try:
        client.containers.get(name).remove(force=True)
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError:
        pass


def _log(container: str, image: str, result: str) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "container": container, "image": image, "result": result}
    try:
        history = json.loads(HISTORY_PATH.read_text()) if HISTORY_PATH.exists() else []
        history.append(entry)
        HISTORY_PATH.write_text(json.dumps(history, indent=2))
    except OSError:
        pass
    print(f"[drydock] {container}: {result} ({image})")
