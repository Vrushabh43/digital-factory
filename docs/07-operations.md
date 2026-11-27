# 7 · Configuration & operations

## 7.1 Configuration

All settings are environment variables (`backend/dmf/settings.py`). **`.env.example` lists every
variable with its default and an explanation.** Copy it to `.env`; Docker Compose reads it
automatically. The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `OPCUA_ENDPOINT` | `opc.tcp://192.168.0.10:4840` | Plant OPC UA server (lab verification required) |
| `OPCUA_USERNAME` / `OPCUA_PASSWORD` | empty (anonymous) | Use a **read-only** OPC UA user if the server supports it |
| `OPCUA_SECURITY` | empty (None) | asyncua security string, e.g. `Basic256Sha256,SignAndEncrypt,cert.der,key.pem` |
| `OPCUA_SUBSCRIBE_SENSORS` | `true` | Also subscribe to the light barriers (cross-check for anomalies) |
| `OPCUA_ENABLED` | `true` | `false` = history only, no factory connection |
| `HANDOVER_WINDOW_S` | `8` | Max. time between "left A" and "arrived at B" to count as one hand-over |
| `STUCK_AFTER_S` | `120` | A piece waiting at an exit (or rotating turntable) longer than this is *stuck* |
| `OFFLINE_DETECTION` | `off` | `timestamps` = mark cells offline when source timestamps stop changing (only after lab check, see 08) |
| `DATABASE_URL` | built by Compose | `postgresql+asyncpg://user:password@host:5432/db` |
| `POSTGRES_PASSWORD` | none, **required** | Compose refuses to start without it |
| `TIMEZONE` | `Europe/Berlin` | Day boundaries of History and exports; times in the UI |
| `SOURCE_LABEL` | `live` | `demo` / `replay` shows a "Demo data" badge so nobody mistakes simulated data for the plant |
| `HTTP_PORT` / `HTTP_BIND` | `8080` / `0.0.0.0` | Where the dashboard is reachable |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `text` is easier to read on a console |
| `CORS_ORIGINS` | empty | Extra browser origins allowed for the API and the WebSocket (needed when an outside reverse proxy changes the host name) |
| `WS_MAX_CLIENTS` | `50` | Concurrent WebSocket clients; more are closed with code 1013 |
| `EXPORT_MAX_DAYS` | `31` | Longest range of one CSV/PDF export (the download dialog uses the same value) |

Settings are **validated at start-up**: an unknown time zone, a `DATABASE_URL` without
`postgresql+asyncpg://`, an endpoint without `opc.tcp://`, a zero/negative interval, a buffer
smaller than one write batch or `OPCUA_RECONNECT_MAX_S < OPCUA_RECONNECT_MIN_S` stop the backend
with a message naming the variable, instead of failing later at runtime.

Plant structure is configuration too: `config/plant.yaml` holds positions, cells, ports, connections,
robots, storage and the map layout, and `config/opcua_nodes.yaml` holds the node ids. Compose mounts
`./config` read-only into the backend, so after editing, run `docker compose restart backend`. No
rebuild is needed, and the frontend gets the new layout from `/api/plant`.

## 7.2 Deployment

```bash
cp .env.example .env && $EDITOR .env
docker compose up -d --build
docker compose ps                      # all services "healthy"
curl -s localhost:8080/api/ready       # {"ready":true,...,"opcua":"connected"}
```

* **Network:** the backend must reach the plant network (`192.168.0.10:4840`). On a Linux host,
  Docker's default bridge with NAT is enough. If the PLC network needs the host's own address, add
  `network_mode: host` to the backend in an override file (and point nginx at the host).
* **Containers:** non-root users, read-only root file systems, `no-new-privileges`, all
  capabilities dropped. Only the frontend publishes a port. Two networks: `data` (database +
  backend, `internal: true`, no outside access at all) and `app` (backend + frontend; the backend
  reaches the plant through it). The frontend cannot reach the database.
* **Reproducible builds:** the backend image installs exactly the versions in
  `backend/constraints.txt` (`pip install -c constraints.txt .`); the frontend uses `npm ci` with
  `package-lock.json`. Base images are pinned to a version line (`python:3.12-slim-bookworm`,
  `node:22.11-alpine`, `nginx-unprivileged:1.27-alpine`, `postgres:16.6-alpine`), not to a digest,
  so a rebuild picks up security patches of that line; pin digests if bit-for-bit rebuilds are required.
* **Stopping:** `docker compose stop` sends SIGTERM; the backend stops reading from the plant,
  processes what is queued, writes a final snapshot and flushes the writer (uvicorn graceful timeout
  20 s, Compose `stop_grace_period` 30 s), so a planned restart loses nothing.
* **nginx** (`deploy/nginx/default.conf`): serves the SPA, proxies `/api` and `/ws` (passing the
  original `Host` incl. port, which the backend compares with the browser `Origin` for WebSockets),
  allows only GET/HEAD on `/api`, limits request bodies to 16 kB, redirects `/docs` to `/api/docs`,
  and sets CSP (a relaxed policy only for the Swagger page), `X-Frame-Options: DENY`, `nosniff`,
  `no-referrer`, `Permissions-Policy` and `Cross-Origin-Opener-Policy` in every location. It
  resolves the backend through Docker's DNS per request, so a restarted backend is found again.
* **No login:** the dashboard is read-only and intended for the lab network. Bind it to
  `127.0.0.1` (`HTTP_BIND`) or put it behind the institute's reverse proxy or VPN if it must not be
  visible to the whole network.
* **Update:** `git pull && docker compose up -d --build`. Migrations run on start, and tracking
  resumes from the snapshot.
* **Backup:** see [04-database.md](04-database.md#45-size-and-retention).

## 7.3 Monitoring

| What | How |
|---|---|
| Liveness | `GET /api/health`, Docker health check of the backend (503 and container *unhealthy* when a background task died — restart the backend and report the log) |
| Readiness | `GET /api/ready` (503 = still starting, database down, or `phase: failed`) |
| Details | `GET /api/status`: phase/failure, handler errors, OPC UA state, reconnect attempts, last error, node mismatches, refused items, database writer backlog (`pending`, `dropped`, `skipped`), recovery report |
| Logs | `docker compose logs -f backend`: JSON lines (`ts`, `level`, `logger`, `msg`, extra fields); rotated at 20 MB × 5 |
| UI | Banners for factory connection lost, dashboard server lost, plant alarm, plant not enabled, events waiting for the database, and "Demo data" |

## 7.4 Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| All cells *Offline*, banner "No connection to the factory" | Server unreachable, wrong endpoint, firewall, certificate/user rejected | `/api/status → opcua.last_error`; from the backend container: `python -c "import socket;socket.create_connection(('192.168.0.10',4840),3)"`; check `OPCUA_*` |
| Connected, but some cells *Offline – no valid data* | Node id wrong, browse name does not match (such values are ignored on purpose), or PLC not publishing (Bad quality) | `python -m dmf.cli.verify`; `/api/status → opcua.node_mismatches` |
| A cell shows *Running* but the panel says some signals have no valid data | One conveyor state of that cell is Bad or not an integer (reason `partial_data`) | `/api/status → opcua.refused`, `python -m dmf.cli.verify`; pieces in that cell may be mis-tracked until fixed |
| Live page stays empty / "Connection to the dashboard server lost" behind another proxy | WebSocket refused because the browser `Origin` does not match the `Host` the backend sees | Add the public URL (e.g. `https://dmf.lab.example`) to `CORS_ORIGINS`, or let the proxy pass the original `Host` |
| Container *unhealthy*, `/api/health` 503 | A background task crashed (`/api/status → failure`) | `docker compose restart backend`; keep `docker compose logs backend` for the bug report |
| Pieces appear as new numbers in the middle of the line (`found`) or get *Lost track* | Missing or wrong connection in `config/plant.yaml`, hand-over slower than `HANDOVER_WINDOW_S`, data gap | Record with `dmf.cli.record` and analyse with `dmf.cli.replay_check --events`; adjust connections or timing |
| Many pieces "stuck" that are only waiting normally | `STUCK_AFTER_S` too low for this line's cycle | Increase `STUCK_AFTER_S` |
| Pieces end as *Lost track* after a robot transfer and reappear with a new number | Robot transit longer than the configured limit | Increase `max_transit_s` of that `robot_links` entry in `config/plant.yaml` |
| Banner "events are waiting" | Database unreachable or slow | `docker compose ps db`, `docker compose logs db`. Events are kept in memory (up to `DB_BUFFER_MAX`) and written once the database is back |
| History empty, live pages fine | Wrong day or timezone; database just recovered | Check `TIMEZONE`; `/api/status → database` |
| Banner "Connection to the dashboard server lost" | Backend restarting, or a proxy dropping WebSockets | `docker compose logs backend`; a proxy in front of nginx must pass `Upgrade` headers |
| Backend restarts in a loop | Invalid setting, invalid `config/*.yaml` or database credentials | The first log lines name the variable, file or field |

## 7.5 Tools (read-only)

Run inside the backend container (`docker compose exec backend ...`) or in the backend venv:

| Command | Purpose |
|---|---|
| `python -m dmf.cli.verify [--json report.json]` | Check every node id, browse name, value range and whether the user could write |
| `python -m dmf.cli.record --out /tmp/run.jsonl [--minutes 30]` | Record OPC UA values (copy out with `docker compose cp backend:/tmp/run.jsonl recordings/`) |
| `python -m dmf.cli.replay_check recordings/run.jsonl [--events]` | Replay a recording through the tracker offline and summarise |
| `python -m dmf_mock --mode replay --file ...` | Replay a recording to the dashboard (demo/test only) |
