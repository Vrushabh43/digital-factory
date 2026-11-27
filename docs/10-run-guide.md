# 10 · Run guide: every module, step by step

This guide starts from an empty computer and runs each part of the dashboard on its own: the
database, the backend, the mock factory, the frontend, the lab tools, the tests and the Docker
stack. Every command was taken from the code in this repository. Commands are given for
**Linux/macOS (bash)** and **Windows (PowerShell)** where they differ.

> **Safety first.** The backend's default OPC UA endpoint is the **real factory**
> (`opc.tcp://192.168.0.10:4840`). For demos and development always set `OPCUA_ENDPOINT` to the
> mock server, as shown below. The dashboard only ever *reads* from the factory, but connect to the
> real plant only together with the lab staff, following [08-lab-validation.md](08-lab-validation.md).

## Contents

1. [The modules at a glance](#1--the-modules-at-a-glance)
2. [Prerequisites and getting the code](#2--prerequisites-and-getting-the-code)
3. [Fastest way: everything with Docker](#3--fastest-way-everything-with-docker)
4. [Module by module without Docker](#4--module-by-module-without-docker)
   1. [PostgreSQL database](#41-postgresql-database)
   2. [Backend Python environment](#42-backend-python-environment)
   3. [Database migrations](#43-database-migrations)
   4. [Mock OPC UA factory](#44-mock-opc-ua-factory-test-and-demo-only)
   5. [Backend API](#45-backend-api)
   6. [Frontend](#46-frontend)
   7. [Using the dashboard](#47-using-the-dashboard)
5. [Connecting to the real factory](#5--connecting-to-the-real-factory)
6. [Lab tools: verify, record, replay](#6--lab-tools-verify-record-replay)
7. [Node map extraction tool](#7--node-map-extraction-tool)
8. [Tests](#8--tests)
9. [CI pipeline](#9--ci-pipeline)
10. [Stopping and cleaning up](#10--stopping-and-cleaning-up)
11. [Troubleshooting](#11--troubleshooting)

---

## 1 · The modules at a glance

| Module | Folder | What it does | Default port |
|---|---|---|---|
| Database | `database/` (migrations) | PostgreSQL 16: pieces, cell passes, events, tracker snapshots | 5432 |
| Backend | `backend/dmf/` | FastAPI: read-only OPC UA client, tracker, history, CSV/PDF export, REST + WebSocket | 8000 |
| Mock factory | `backend/dmf_mock/` | OPC UA server that imitates the plant: demo traffic, idle, or replay of a recording (**test/demo only**) | 4840 |
| Frontend | `frontend/` | React + TypeScript dashboard: Factory map, Pieces now, History | 5173 (dev), 4173 (preview) |
| Reverse proxy | `deploy/nginx/` | Serves the built frontend and forwards `/api` and `/ws` (Docker only) | 8080 |
| Lab tools | `backend/dmf/cli/` | `verify`, `record`, `replay_check` (all read-only) | — |
| Node map tool | `tools/tia_nodemap/` | Extracts `config/opcua_nodes.yaml` from the TIA project | — |
| Configuration | `config/` | `plant.yaml` (cells, connections, map), `opcua_nodes.yaml` (node ids) | — |

How they connect:

```
 PLC OPC UA server (real plant)  ─┐
   or mock factory (dmf_mock)     ├─ OPC UA (read-only) ─►  backend :8000  ─►  PostgreSQL :5432
                                  ┘                           │  REST /api, WebSocket /ws/live
                                                              ▼
                                         browser ◄── frontend (Vite :5173 / nginx :8080)
```

## 2 · Prerequisites and getting the code

| Tool | Version | Needed for |
|---|---|---|
| Git | any | getting the code |
| Python | **3.12** (CI and the Docker image; the tests also pass on 3.13) | backend, mock, tools, backend tests |
| Node.js | **22** with npm | frontend, frontend and e2e tests |
| PostgreSQL | **16** | database (or run it in Docker, see 4.1) |
| Docker + Docker Compose v2 | recent | only for section 3 |

```bash
git clone https://github.com/Vrushabh43/digital-factory.git
cd digital-factory
```

All paths below are relative to this folder (the *repository root*).

---

## 3 · Fastest way: everything with Docker

### 3.1 Demo with the mock factory (no plant needed)

```bash
cp .env.example .env            # Windows: Copy-Item .env.example .env
```

Open `.env` and set `POSTGRES_PASSWORD` to a password of your choice. Then:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.demo.yml ps     # wait until all are "healthy"
```

Open **http://localhost:8080**. The header shows the badge **"Demo data"** and pieces start moving
after a few seconds. Services: `db` (PostgreSQL), `backend`, `frontend` (nginx) and `mock` (mock
factory); the demo file points the backend at `opc.tcp://mock:4840/`.

Quick checks:

```bash
curl -s http://localhost:8080/api/ready      # {"ready": true, "phase": "running", ...}
curl -s http://localhost:8080/api/status     # version, OPC UA connection, node map check, database writer, recovery
```

### 3.2 Against the real factory

Only after the lab validation of [08-lab-validation.md](08-lab-validation.md):

```bash
# .env: POSTGRES_PASSWORD set, OPCUA_ENDPOINT=opc.tcp://192.168.0.10:4840 (and a read-only user)
docker compose up -d --build
```

### 3.3 Useful Docker commands

```bash
docker compose logs -f backend                 # backend log (JSON lines)
docker compose restart backend                 # numbering and history continue after a restart
docker compose exec backend python -m dmf.cli.verify    # lab tool inside the container (section 6)
docker compose down                            # stop; data stays in the "pgdata" volume
docker compose down -v                         # stop AND delete the database (throw-away data only!)
```

> Docker deployment status: the Compose files were reviewed but **never executed** during
> development (no Docker on the development machine). See the checklist in
> [09-final-project-audit.md §16](09-final-project-audit.md#16--docker-validation-checklist-not-executed-here).

---

## 4 · Module by module without Docker

Use three or four terminals: mock factory, backend, frontend (and tests). Start them in the order
of this section.

### 4.1 PostgreSQL database

**Option A: PostgreSQL installed locally.** Create a user and a database (password `dmf` is fine for
development only):

```bash
psql -U postgres -c "CREATE USER dmf WITH PASSWORD 'dmf';"
psql -U postgres -c "CREATE DATABASE dmf OWNER dmf;"
psql -U postgres -c "CREATE DATABASE dmf_test OWNER dmf;"   # only for the integration tests (section 8)
```

**Option B: only the database in Docker.**

```bash
docker run -d --name dmf-pg -p 5432:5432 \
  -e POSTGRES_USER=dmf -e POSTGRES_PASSWORD=dmf -e POSTGRES_DB=dmf postgres:16.6-alpine
docker exec dmf-pg createdb -U dmf dmf_test                 # for the integration tests
```

(PowerShell: write the `docker run` command on one line, without the `\`.)

The connection string used everywhere below is

```
postgresql+asyncpg://dmf:dmf@localhost:5432/dmf
```

### 4.2 Backend Python environment

```bash
cd backend
python -m venv .venv
. .venv/bin/activate                       # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -c constraints.txt -e ".[dev]" # exact versions used by CI and the Docker image
```

`-e` installs the backend in editable mode, so code changes take effect without reinstalling.
Keep this environment activated in every backend terminal (mock, backend, tools, tests).

> PowerShell refuses to run `Activate.ps1`? Run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### 4.3 Database migrations

The backend applies the migrations **automatically** at start-up (`DB_RUN_MIGRATIONS=true`, the
default). To run them by hand (for example to check a new database), from the repository root with
the backend environment active:

```bash
# bash
DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf alembic -c database/alembic.ini upgrade head
alembic -c database/alembic.ini current     # shows the applied revision (0002 = latest)
```

```powershell
# PowerShell
$env:DATABASE_URL = "postgresql+asyncpg://dmf:dmf@localhost:5432/dmf"
alembic -c database/alembic.ini upgrade head
alembic -c database/alembic.ini current
```

Tables: `pieces`, `cell_passes`, `events`, `cell_status_history`, `tracker_snapshot`, `writer_progress` (details in
[04-database.md](04-database.md)).

### 4.4 Mock OPC UA factory (test and demo only)

The mock publishes the same namespace, node ids and browse names as the plant's server and refuses
every write. Run it in its own terminal, in `backend/` with the environment active:

```bash
python -m dmf_mock --mode demo --endpoint opc.tcp://127.0.0.1:4840/
```

Modes and options:

| Command | Effect |
|---|---|
| `--mode demo` | Random but plausible traffic: pieces put on, moved, sorted, stuck, taken off |
| `--mode idle` | All conveyors empty, nothing moves (used by the end-to-end tests) |
| `--mode replay --file recordings/run.jsonl [--speed 2] [--loop]` | Plays back a recording made with `dmf.cli.record` |
| `--seed 42` | Same random traffic every time (demo mode) |
| `--speed 4` | Faster time (demo and replay) |
| `--control-port 8765` | HTTP control API for the end-to-end tests (**test only**, `0` = off) |
| `--plant`, `--nodes` | Other config files (defaults: `config/plant.yaml`, `config/opcua_nodes.yaml`) |

Leave it running. `Ctrl+C` stops it.

### 4.5 Backend API

Second terminal, in `backend/` with the environment active. The important part is
`OPCUA_ENDPOINT`, which must point at the mock:

```bash
# bash
DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf \
OPCUA_ENDPOINT=opc.tcp://127.0.0.1:4840/ \
SOURCE_LABEL=demo LOG_FORMAT=text \
uvicorn dmf.main:app --host 127.0.0.1 --port 8000
```

```powershell
# PowerShell
$env:DATABASE_URL   = "postgresql+asyncpg://dmf:dmf@localhost:5432/dmf"
$env:OPCUA_ENDPOINT = "opc.tcp://127.0.0.1:4840/"
$env:SOURCE_LABEL   = "demo"
$env:LOG_FORMAT     = "text"
uvicorn dmf.main:app --host 127.0.0.1 --port 8000
```

Instead of environment variables you can put the same lines into a `.env` file in the repository
root or in `backend/` (both are read; `.env` is git-ignored). **Do not** simply copy
`.env.example` for local development: it points at the real factory.

Important: run **one** backend process only (no `--workers`, no `--reload` in production); the
tracker state lives in that process.

Check that it works (a third terminal or the browser):

| URL | Expected answer |
|---|---|
| http://127.0.0.1:8000/api/health | `200`, the process is alive |
| http://127.0.0.1:8000/api/ready | `200` with `"phase": "running"` once the database is ready (`503` with `waiting_for_database` before) |
| http://127.0.0.1:8000/api/status | OPC UA `connected`, node check result, database, recovery |
| http://127.0.0.1:8000/api/docs | Interactive API documentation (`/docs` redirects there) |
| http://127.0.0.1:8000/api/live | Current state of all cells and pieces (JSON) |
| http://127.0.0.1:8000/api/history/export?from=2026-11-27&format=csv | CSV of that day's cell passes (`format=pdf` for PDF) |

All endpoints are listed in [05-api.md](05-api.md); every setting (timings, limits, offline
detection, logging) in [07-operations.md](07-operations.md#71-configuration).

Without any factory (only browse history): `OPCUA_ENABLED=false`.

### 4.6 Frontend

**Development server** (hot reload). Third terminal:

```bash
cd frontend
npm ci                  # once; installs the exact versions of package-lock.json
npm run dev             # http://localhost:5173
```

The dev server forwards `/api` and `/ws` to the backend on `http://127.0.0.1:8000`. If your backend
runs elsewhere, set `DMF_BACKEND` first (bash: `DMF_BACKEND=http://host:8000 npm run dev`,
PowerShell: `$env:DMF_BACKEND = "http://host:8000"; npm run dev`).

**Production build** (what nginx serves in Docker):

```bash
npm run build           # type check + build into frontend/dist
npx vite preview --port 4173 --host 127.0.0.1    # serves dist on http://127.0.0.1:4173, same /api and /ws forwarding
```

### 4.7 Using the dashboard

Open http://localhost:5173 (or :4173 for the preview build):

* **Header**: "Live" when the backend has a factory connection, "Demo data" badge with the mock.
  "No factory", "Not live", "Connecting" or "Tracking stopped" explain what is wrong.
* **Factory map**: the 18 cells along the blueprint, coloured by status (running, waiting, stuck,
  error, offline); click a cell for its conveyors, ports and recent events; *Needs attention* lists
  stuck and faulty cells.
* **Pieces now**: every piece on the line with cell, position, conveyor, time in cell and origin;
  filters *All*, *Moving*, *Need attention*.
* **History**: cell passes by day, cell or piece number (e.g. `P-0001`), the full journey of a
  piece, and **Download** for CSV or PDF (from/to date, up to 31 days).

---

## 5 · Connecting to the real factory

Do this only in the lab, with the lab staff, following
[08-lab-validation.md](08-lab-validation.md) step by step. Short version:

1. Connect the computer to the lab network so that `192.168.0.10:4840` is reachable.
2. Ask for a **read-only OPC UA user** for the dashboard (08 §8.1).
3. Run the node check first (section 6): `python -m dmf.cli.verify` must end with exit code `0`.
4. Start the backend with `OPCUA_ENDPOINT=opc.tcp://192.168.0.10:4840`, `SOURCE_LABEL=live`,
   `OPCUA_USERNAME`/`OPCUA_PASSWORD`, and the frontend as in 4.6.
5. Compare the dashboard with what you see on the plant (08 steps 3–7).

The node map is `verification: derived` (extracted from the TIA project, not yet confirmed on the
real server); nothing has been tested against the real factory yet.

## 6 · Lab tools: verify, record, replay

All three only read. Run them in `backend/` with the environment active and `OPCUA_ENDPOINT` set
(to the mock for practice, to the plant in the lab).

**Check the node map against a server**

```bash
OPCUA_ENDPOINT=opc.tcp://127.0.0.1:4840/ python -m dmf.cli.verify --json verify-report.json
```

Exit codes: `0` everything matches and the user cannot write · `1` missing nodes, different browse
names or values out of range · `2` could not connect · `3` everything matches but the user is
*allowed* to write (ask for a read-only user).

**Record the plant's values** (JSON lines; `recordings/` is git-ignored):

```bash
python -m dmf.cli.record --out recordings/lab-run.jsonl --minutes 30    # without --minutes: until Ctrl+C
```

**Check a recording offline** (no OPC UA, no database):

```bash
python -m dmf.cli.replay_check recordings/lab-run.jsonl            # summary: pieces, lost/found, anomalies
python -m dmf.cli.replay_check recordings/lab-run.jsonl --events   # every event
```

**Show a recording in the dashboard**: start the mock with
`python -m dmf_mock --mode replay --file recordings/lab-run.jsonl --endpoint opc.tcp://127.0.0.1:4840/`
and the backend with `SOURCE_LABEL=replay` (as in 4.5).

(PowerShell: set the variable first, e.g. `$env:OPCUA_ENDPOINT = "opc.tcp://127.0.0.1:4840/"`.)

## 7 · Node map extraction tool

Re-creates `config/opcua_nodes.yaml` from the TIA Portal archive, without TIA Portal. Only needed if
the PLC project changes. From the repository root, with the backend environment active:

```bash
python tools/tia_nodemap/extract_tia_nodemap.py "Project-description/PLC program cell.zip" config/opcua_nodes.yaml
```

It prints `wrote config/opcua_nodes.yaml: 18 positions, ... conveyors`. The result is again
*derived* data and must be checked with `dmf.cli.verify` (section 6). Review the change with
`git diff config/opcua_nodes.yaml` before committing it.

---

## 8 · Tests

### 8.1 Backend

In `backend/` with the environment active:

```bash
ruff check .                  # lint
ruff format --check .         # formatting
pytest -q                     # unit tests only; tests that need PostgreSQL are skipped
```

With PostgreSQL (database `dmf_test` from 4.1; the tests create the tables themselves):

```bash
# bash
TEST_DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf_test pytest -q
TEST_DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf_test pytest -q --cov --cov-report=term   # with coverage
```

```powershell
# PowerShell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://dmf:dmf@localhost:5432/dmf_test"
pytest -q
pytest -q --cov --cov-report=term
```

The full run took about 10 minutes on a laptop (a 30-minute traffic simulation and a server-restart
test are the slowest). Single files or tests: `pytest -q tests/unit/test_engine.py -k stuck`.

### 8.2 Frontend

In `frontend/`:

```bash
npm run lint                  # ESLint
npm run typecheck             # TypeScript
npm test                      # Vitest, all component and unit tests
npx vitest run --coverage     # with coverage
npm run build                 # production build
```

### 8.3 End-to-end (whole stack in a browser)

Needs an empty database, the mock with its control API, the backend with short timings and the
built frontend. Four terminals:

```bash
# 1. empty database
psql -U postgres -c "CREATE DATABASE dmf_e2e OWNER dmf;"

# 2. mock factory, idle, with control API (backend/, environment active)
python -m dmf_mock --mode idle --endpoint opc.tcp://127.0.0.1:48420/ --control-port 8765

# 3. backend with short timings (backend/, environment active)
DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf_e2e \
OPCUA_ENDPOINT=opc.tcp://127.0.0.1:48420/ SOURCE_LABEL=demo \
STUCK_AFTER_S=6 HANDOVER_WINDOW_S=3 OPCUA_RECONNECT_MAX_S=3 \
uvicorn dmf.main:app --port 8000

# 4. built frontend (frontend/)
npm run build && npx vite preview --port 4173 --host 127.0.0.1

# 5. the tests (frontend/, a fifth terminal)
npx playwright install chromium        # once
E2E_BASE_URL=http://127.0.0.1:4173 npx playwright test
```

In PowerShell set the variables of steps 3 and 5 with `$env:NAME = "value"` first. The tests
check after every scenario that the mock received **zero** modifying requests. More details:
[06-testing.md](06-testing.md).

## 9 · CI pipeline

`.gitlab-ci.yml` runs lint → tests (with PostgreSQL and coverage) → end-to-end → Docker image
build. It runs on **GitLab** only; pushing this repository to GitHub does not run it (GitHub
Actions would need its own workflow file). Every command of the pipeline is listed in section 8.
The pipeline has not yet run on GitLab; see
[09-final-project-audit.md §17](09-final-project-audit.md#17--gitlab-validation-checklist).

## 10 · Stopping and cleaning up

* Mock, backend, frontend: `Ctrl+C` in their terminals. The backend saves its state and writes all
  pending events before it exits; after a restart the numbering continues.
* Docker: `docker compose down` (keeps the data) or `docker compose down -v` (deletes it).
* Database container from 4.1 option B: `docker stop dmf-pg` / `docker rm dmf-pg`.
* Test databases: `psql -U postgres -c "DROP DATABASE dmf_e2e;"` (and `dmf_test`).

## 11 · Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `/api/ready` answers 503 with `waiting_for_database` | PostgreSQL is not running, the database does not exist or `DATABASE_URL` is wrong. The backend keeps retrying; fix the database and it starts by itself. |
| Header "No factory" / `/api/status` shows OPC UA not connected | Wrong `OPCUA_ENDPOINT`, or the mock is not running. The backend reconnects automatically (1 s, doubling up to 30 s). |
| Backend tries to reach `192.168.0.10` | `OPCUA_ENDPOINT` was not set; that is the default. Set it to the mock (4.5). |
| Header "Tracking stopped" | A background task of the backend failed; `/api/health` answers 503. Check the backend log and restart it. |
| Browser shows no live updates behind your own reverse proxy | The WebSocket checks the page's origin; add the dashboard URL to `CORS_ORIGINS` (e.g. `https://dmf.lab.example`). |
| `address already in use` | Another process uses 4840, 8000, 5173 or 4173: stop it or choose another port (`--port`, `--endpoint`, `DMF_BACKEND`). |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Integration tests are all *skipped* | `TEST_DATABASE_URL` is not set (8.1). |
| Settings error at start-up | A value in `.env`/the environment is out of range or invalid; the message names the variable ([07-operations.md](07-operations.md)). |

More operational topics (monitoring, backups, image pinning): [07-operations.md](07-operations.md).
