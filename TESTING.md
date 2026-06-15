# Testing Drydock (Linux + Windows / Docker Desktop)

Goal: **prove the killer feature — a bad update auto-rolls-back** — using only public images
(no custom builds). Works the same on Linux and Windows because Drydock talks to Docker through
the SDK (Linux socket OR the Windows named pipe, automatically).

## 0. One-time setup
Install Python 3.10+ and the deps (on the machine with Docker running):
```
pip install -r requirements.txt
```
*(Windows: run in PowerShell. Docker Desktop must be running.)*

## 1. Start a healthy container (traefik/whoami serves HTTP 200 on /)
```
docker run -d --name whoami -p 8088:80 \
  --label drydock.enable=true \
  --label drydock.healthcheck=http://localhost:8088/ \
  --label drydock.rollback_window=30 \
  traefik/whoami
```
Confirm it's up: open http://localhost:8088 (you'll see the whoami response).

## 2. See Drydock recognize it
```
python -m drydock status
```
→ lists `whoami` as managed.

## 3. THE TEST — force a bad update, watch the auto-rollback
We force an "update" to `alpine` (which has no web server and exits immediately), so the health
check fails and Drydock must roll back to whoami:
```
python -m drydock apply whoami --to alpine:latest
```
Expected output:
```
[drydock] applying alpine:latest to whoami (health-check 30s, auto-rollback on fail)
[drydock] whoami: rolled_back (alpine:latest)
[drydock] result: rolled_back
```

## 4. Verify it recovered
```
docker ps           # whoami is running again
```
Open http://localhost:8088 again → whoami responds. **The bad update was reverted automatically.**
A record is written to `drydock-history.json`.

## 5. (Optional) prove a GOOD update applies cleanly
```
python -m drydock apply whoami --to traefik/whoami:latest   # health passes -> "updated"
```

## Cleanup
```
docker rm -f whoami
```

## Running Drydock itself as a container (the real distribution)
Linux:
```
docker run -d -v /var/run/docker.sock:/var/run/docker.sock neutralsystems/drydock
```
Windows / Docker Desktop (note the leading `//`):
```
docker run -d -v //var/run/docker.sock:/var/run/docker.sock neutralsystems/drydock
```

## What to report back
- The exact output of step 3 (did it say `rolled_back`?)
- Any Python tracebacks (likely spots: container recreate / config preservation, or the registry
  digest check in `status`). Paste them and I'll fix.
