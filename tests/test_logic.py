"""Pure-logic + safety-contract tests for Drydock — no Docker daemon required.

The recreate/rollback flow is simulated with fake Docker objects so we can assert the one rule
that matters most: *no failure path ever leaves the user without their original container.*
"""
import sys
from pathlib import Path

import pytest
from docker import errors as derr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drydock import core, safety  # noqa: E402
from drydock.config import ContainerPolicy, policy_for  # noqa: E402


# --------------------------------------------------------------------------- semver

@pytest.mark.parametrize("cur,new,level", [
    ("1.2.3", "2.0.0", "major"),
    ("1.2.3", "1.3.0", "minor"),
    ("1.2.3", "1.2.4", "patch"),
    ("1.2.3", "1.2.3", "patch"),
    ("v1.2.3", "v1.2.3", "patch"),
    ("1.25", "1.25", "patch"),          # 2-part tag (nginx-style)
    ("1.25", "1.26", "minor"),
    ("1.25", "2.0", "major"),
    ("1.2.3-alpine", "1.2.4-alpine", "patch"),  # suffixed tag
    ("16-bullseye", "17-bullseye", "major"),
    ("latest", "latest", "unknown"),
    ("1.2.3", "weird", "unknown"),
])
def test_classify(cur, new, level):
    assert safety.classify(cur, new) == level


def test_auto_safe_blocks_major_and_unknown():
    assert safety.is_auto_safe("patch") and safety.is_auto_safe("minor")
    assert not safety.is_auto_safe("major")
    assert not safety.is_auto_safe("unknown")


# --------------------------------------------------------------------------- policy

def test_policy_defaults_when_no_labels():
    p = policy_for({})
    assert p.enabled is False and p.mode == "approve"


def test_policy_reads_labels():
    p = policy_for({
        "drydock.enable": "TRUE", "drydock.mode": "auto-safe",
        "drydock.healthcheck": "http://localhost/health", "drydock.rollback_window": "15",
    })
    assert p.enabled and p.mode == "auto-safe" and p.rollback_window == 15


# --------------------------------------------------------------------------- ref parsing

@pytest.mark.parametrize("ref,tag", [
    ("nginx", "latest"),
    ("nginx:1.25", "1.25"),
    ("traefik/whoami:latest", "latest"),
    ("registry:5000/team/app:2.1.0", "2.1.0"),
    ("registry:5000/team/app", "latest"),
    ("nginx@sha256:abcd", "latest"),
])
def test_split_tag(ref, tag):
    assert core._split_tag(ref) == tag


def test_aliases_drops_auto_short_id():
    old = _FakeContainer("c", cid="abc123def456ghi", attrs={})
    ep = {"Aliases": ["web", "abc123def456", None]}
    assert core._aliases(ep, old) == ["web"]


# --------------------------------------------------------------------------- fakes

class _Img:
    def __init__(self, repo_digests):
        self.attrs = {"RepoDigests": repo_digests}


class _FakeContainer:
    def __init__(self, name, cid="id000000000000", attrs=None, status="running", repo_digests=None):
        self.name = name
        self.id = cid
        self.attrs = attrs if attrs is not None else {"Config": {"Image": "app:1.0.0"}}
        self.status = status
        self.image = _Img(repo_digests or [])
        self.events = []

    def reload(self):
        self.events.append("reload")

    def stop(self):
        self.events.append("stop"); self.status = "exited"

    def start(self):
        self.events.append("start"); self.status = "running"

    def rename(self, new):
        self.events.append(f"rename:{new}"); self.name = new

    def remove(self, force=False):
        self.events.append("remove")

    def exec_run(self, cmd):
        class R: exit_code = 0
        return R()


class _FakeAPI:
    def __init__(self, new_container, fail_create=False):
        self._new = new_container
        self.fail_create = fail_create
        self.created_kwargs = None
        self.connected = []

    def create_networking_config(self, d):
        from docker.types import NetworkingConfig
        return NetworkingConfig(d)

    def create_endpoint_config(self, **kw):
        from docker.types import EndpointConfig
        return EndpointConfig("1.45", **kw)

    def create_container(self, **kwargs):
        if self.fail_create:
            raise derr.APIError("boom: create failed")
        self.created_kwargs = kwargs
        return {"Id": self._new.id}

    def connect_container_to_network(self, cid, net, **opts):
        self.connected.append((net, opts))

    def start(self, cid):
        pass


class _FakeContainers:
    def __init__(self, new_container, registry):
        self._new = new_container
        self._registry = registry  # name -> container (for .get)

    def get(self, name):
        if name in self._registry:
            return self._registry[name]
        if name == self._new.id:
            return self._new
        raise derr.NotFound(f"no such container {name}")


class _FakeClient:
    def __init__(self, new_container, fail_create=False, fail_pull=False):
        self.api = _FakeAPI(new_container, fail_create=fail_create)
        self._fail_pull = fail_pull
        self.containers = _FakeContainers(new_container, {})

        class _Images:
            def pull(_self, ref):
                if fail_pull:
                    raise derr.APIError("pull failed")
        self.images = _Images()


def _policy(window=0):
    return ContainerPolicy(enabled=True, mode="approve", healthcheck=None, rollback_window=window)


# --------------------------------------------------------------------------- safety contract

def test_safe_update_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "HISTORY_PATH", tmp_path / "h.json")
    old = _FakeContainer("web", attrs={"Config": {"Image": "app:1.0.0"}, "HostConfig": {"NetworkMode": "default"}})
    new = _FakeContainer("web", cid="newid", status="running")
    client = _FakeClient(new)
    result = core.safe_update(client, old, "app:1.1.0", _policy())
    assert result == "updated"
    # original was stopped, renamed aside, then removed only after success
    assert "stop" in old.events and any(e.startswith("rename:web__drydock_bak") for e in old.events)
    assert "remove" in old.events
    # the new container was created on the NEW image, full host_config passed through
    assert client.api.created_kwargs["image"] == "app:1.1.0"
    assert client.api.created_kwargs["host_config"] == {"NetworkMode": "default"}


def test_safe_update_rolls_back_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "HISTORY_PATH", tmp_path / "h.json")
    old = _FakeContainer("web", attrs={"Config": {"Image": "app:1.0.0"}, "HostConfig": {}})
    new = _FakeContainer("web", cid="newid", status="exited")  # comes up dead
    client = _FakeClient(new)
    result = core.safe_update(client, old, "bad:latest", _policy())
    assert result == "rolled_back"
    # original was renamed back to its real name and restarted; it was never removed
    assert old.name == "web"
    assert old.events.count("start") >= 1
    assert "remove" not in old.events


def test_safe_update_create_failure_restores_original(tmp_path, monkeypatch):
    """THE regression test: if creating the replacement fails, the original must come back."""
    monkeypatch.setattr(core, "HISTORY_PATH", tmp_path / "h.json")
    old = _FakeContainer("db", attrs={"Config": {"Image": "pg:15"}, "HostConfig": {}})
    new = _FakeContainer("db", cid="newid")
    client = _FakeClient(new, fail_create=True)
    result = core.safe_update(client, old, "pg:16", _policy())
    assert result == "error"
    assert old.name == "db"            # restored to its real name
    assert "remove" not in old.events  # NEVER destroyed
    assert old.events.count("start") >= 1


def test_safe_update_pull_failure_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "HISTORY_PATH", tmp_path / "h.json")
    old = _FakeContainer("web")
    client = _FakeClient(_FakeContainer("web", cid="newid"), fail_pull=True)
    result = core.safe_update(client, old, "app:1.1.0", _policy())
    assert result == "error"
    assert old.events == []            # original wasn't touched at all


def test_recreate_reattaches_user_network_with_aliases():
    attrs = {
        "Config": {"Image": "app:1.0.0", "Cmd": ["run"], "Env": ["A=1"], "Labels": {"x": "y"}},
        "HostConfig": {"NetworkMode": "myapp_net"},
        "NetworkSettings": {"Networks": {"myapp_net": {"Aliases": ["web", "id0000000000"], "IPAMConfig": {}}}},
    }
    old = _FakeContainer("web", cid="id0000000000abc", attrs=attrs)
    new = _FakeContainer("web", cid="newid")
    client = _FakeClient(new)
    core._recreate_on_image(client, old, "app:1.1.0", "web")
    nc = client.api.created_kwargs["networking_config"]
    assert "myapp_net" in nc["EndpointsConfig"]
    assert nc["EndpointsConfig"]["myapp_net"]["Aliases"] == ["web"]  # short-id alias dropped


# --------------------------------------------------------------------------- check_update

def test_check_update_skips_locally_built():
    c = _FakeContainer("x", attrs={"Config": {"Image": "myimg:1.0.0"}}, repo_digests=[])
    assert core.check_update(_FakeClient(c), c) is None


def test_check_update_detects_digest_drift():
    c = _FakeContainer("x", attrs={"Config": {"Image": "nginx:1.25"}},
                       repo_digests=["nginx@sha256:OLD"])

    class Client:
        class images:
            @staticmethod
            def get_registry_data(ref):
                return type("D", (), {"id": "sha256:NEW"})()
    up = core.check_update(Client(), c)
    assert up is not None and up.level == "patch" and up.tag == "1.25"


def test_check_update_none_when_digest_matches():
    c = _FakeContainer("x", attrs={"Config": {"Image": "nginx:1.25"}},
                       repo_digests=["nginx@sha256:SAME"])

    class Client:
        class images:
            @staticmethod
            def get_registry_data(ref):
                return type("D", (), {"id": "sha256:SAME"})()
    assert core.check_update(Client(), c) is None
