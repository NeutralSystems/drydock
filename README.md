# 🛠️ Drydock — safe, reversible Docker container updates

> *Watchtower, but it won't break your stack at 2am.*

Drydock watches your running containers for new image versions and updates them **safely**:
it health-checks every update and **automatically rolls back** if the container comes up
unhealthy — and by default it asks before touching anything. No more silent 2am major-version
bumps that take down your database.

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
*(early development — v0.1 in progress)*

---

## Why

[Watchtower](https://github.com/containrrr/watchtower) — the de-facto container auto-updater —
was **archived in December 2025**. It also had a dangerous habit: it applied every update blindly,
with **no health check, no rollback, and no approval.** One silent major-version bump (the classic
2am Postgres upgrade) and your stack is down with no easy way back.

The other options either only *notify* you (DIUN) or auto-update much like Watchtower did. **Nobody
owns the safe, reversible middle.** Drydock does.

## What makes it different

- ✅ **Health-checked updates** — after updating, Drydock verifies the container is actually healthy.
- ↩️ **Automatic rollback** — if the new version fails its health check, Drydock restores the previous
  image automatically. You wake up to a *working* stack, not a broken one.
- 🙋 **Approval mode (default)** — Drydock never auto-applies; it tells you what's available and you
  approve. Or switch to `auto-safe` to auto-apply patch/minor only.
- 🚦 **Major-version guardrails** — semver-aware: major bumps are always flagged, never silent.
- 🧾 **Update history** — what changed, when, and whether it rolled back.

## Quickstart *(planned)*

```yaml
# docker-compose.yml
services:
  drydock:
    image: neutralsystems/drydock:latest
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - DRYDOCK_MODE=approve        # approve | auto-safe
  myapp:
    image: myapp:1.2.3
    labels:
      - drydock.enable=true
      - drydock.healthcheck=http://localhost:8080/health
```

> **Windows / Docker Desktop:** same thing — Drydock talks to Docker through the SDK, which uses the
> Windows named pipe automatically. If running Drydock *as a container*, mount the socket with a leading
> double-slash: `-v //var/run/docker.sock:/var/run/docker.sock`. See [TESTING.md](TESTING.md).

## Config (per-container labels)

| Label | Values | Meaning |
|-------|--------|---------|
| `drydock.enable` | `true`/`false` | opt this container in/out |
| `drydock.mode` | `approve`/`auto-safe` | override global mode |
| `drydock.healthcheck` | URL or `cmd:...` | how to verify health post-update (falls back to Docker HEALTHCHECK) |
| `drydock.rollback_window` | seconds (default 60) | how long to watch before declaring success |

## Status / roadmap

- [ ] v0.1 — watch + detect updates + safety classification + **safe-apply with rollback** + approval mode
- [ ] v0.2 — notifications (email/Discord/webhook), update history UI
- [ ] later — multi-host fleet + hosted dashboard (the paid tier; the tool itself stays free & open)

## Platform support

Drydock talks to Docker through the Docker API (via the SDK), so it runs **anywhere Docker runs**:

| OS | Docker | Status |
|----|--------|--------|
| **Linux** | native Docker Engine (no Docker Desktop needed) | ✅ supported — primary target |
| **Windows** | Docker Desktop (WSL2) | ✅ supported — uses the named pipe automatically |
| **macOS** | Docker Desktop | 🟡 should work (same Docker-API path) — *not yet verified, no Mac on hand* |

If you're on macOS and it works (or doesn't), please open an issue — we want to confirm it.

## License

AGPL-3.0 — open and auditable; if you run a modified version as a service, share your changes.
Built by [Neutral Systems](https://neutralsystems.ca).
