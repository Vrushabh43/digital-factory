# DMF Workpiece Tracking Dashboard

A live dashboard for the **Digital Model Factory (DMF)**. It follows every workpiece through the 18
cells (conveyors, splitters, mergers, turntables) and shows three pages:

* **Factory map**: status of every cell (running / piece stuck / error / offline), what needs
  attention, and details of the selected cell.
* **Pieces now**: every piece currently on the line, with where it is, how long it has been there,
  where it came from and whether it needs attention.
* **History**: every cell pass by day, cell or piece, the full journey of a piece, and CSV/PDF downloads.

The dashboard **only reads** from the factory. It subscribes to the plant's OPC UA server and never
writes, calls methods or changes PLC values (see [Read-only guarantee](#read-only-guarantee)).

> **Status: not yet validated on the real factory.** Everything has been tested against a mock
> OPC UA server that mirrors the PLC program, using recorded and simulated data. The node map is
> `verification: derived`: it was extracted from the TIA project and has **not** been confirmed on
> `opc.tcp://192.168.0.10:4840`. Follow [docs/08-lab-validation.md](docs/08-lab-validation.md)
> before relying on the dashboard in the lab.

**New here?** Follow the [step-by-step run guide](docs/10-run-guide.md).

## Quick start (Docker)

```bash
cp .env.example .env              # set POSTGRES_PASSWORD; check OPCUA_ENDPOINT
docker compose up -d --build      # db + backend + frontend
open http://localhost:8080
```

Demo without the factory (mock OPC UA server with simulated pieces; marked "Demo data" in the UI):

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

## Development (without Docker)

Requirements: Python 3.12, Node 22 (18+ works), PostgreSQL 16.

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -c constraints.txt -e ".[dev]"            # exact versions used in CI and the image
python -m dmf_mock --mode demo --endpoint opc.tcp://127.0.0.1:4840/ &   # mock factory (optional)
DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf \
OPCUA_ENDPOINT=opc.tcp://127.0.0.1:4840/ SOURCE_LABEL=demo LOG_FORMAT=text \
uvicorn dmf.main:app --port 8000                     # migrations run automatically

# frontend (second terminal)
cd frontend
npm ci
npm run dev                                          # http://localhost:5173, proxies /api and /ws to :8000
```

## Repository layout

| Path | Content |
|---|---|
| `Project-description/` | Original task description, design and PLC project (unchanged) |
| `config/plant.yaml` | Plant model: positions ↔ cells, connections, robots, storage, map layout |
| `config/opcua_nodes.yaml` | OPC UA node map (generated from the TIA project, **verification: derived**) |
| `backend/dmf/` | FastAPI app: read-only OPC UA client, tracker engine, persistence, REST/WebSocket, exports, CLI tools |
| `backend/dmf_mock/` | Mock OPC UA server, demo simulator, replay, e2e control API (**test/demo only**) |
| `backend/tests/` | Unit and integration tests (mock OPC UA + PostgreSQL) |
| `database/` | Alembic migrations |
| `frontend/` | React + TypeScript dashboard (Vite), Vitest and Playwright tests |
| `deploy/nginx/` | nginx config (static files, `/api` and `/ws` proxy, security headers) |
| `tools/tia_nodemap/` | Extracts the node map from the TIA project |
| `recordings/` | Lab recordings (JSON lines, git-ignored) |
| `docs/` | Documentation (below) |

## Documentation

1. [Project analysis](docs/01-project-analysis.md): requirements, PLC findings, interpretations, open points
2. [Architecture](docs/02-architecture.md): components, tracking engine, status rules, reliability
3. [Implementation plan](docs/03-implementation-plan.md): master checklist
4. [Database](docs/04-database.md): schema, migrations, retention
5. [API](docs/05-api.md): REST and WebSocket reference
6. [Testing](docs/06-testing.md): unit, integration, frontend and end-to-end tests
7. [Configuration & operations](docs/07-operations.md): environment variables, deployment, monitoring, troubleshooting
8. [Lab validation](docs/08-lab-validation.md): step-by-step checklist for the real factory, incl. a read-only OPC UA account
9. [Final project audit](docs/09-final-project-audit.md): requirements traceability, issues found and fixed, measurements, readiness
10. [Run guide](docs/10-run-guide.md): every module step by step (database, backend, mock factory, frontend, lab tools, tests, Docker), for bash and PowerShell

Interactive API docs are served at `/api/docs` (`/docs` redirects there) while the backend is running.

## Read-only guarantee

* `backend/dmf/opcua/client.py` is the only module that imports the asyncua client. Its facade
  exposes only connect, read, browse and subscribe.
* `tests/unit/test_readonly_guard.py` fails the build if any code uses an OPC UA write or call API.
* Integration and e2e tests check that the mock server received **zero** write attempts.
* `python -m dmf.cli.verify` reports whether the configured OPC UA user *could* write (exit code 3)
  by reading the `UserAccessLevel` attribute. Use a read-only user on the server as a second line of
  defence ([docs/08 §8.1](docs/08-lab-validation.md#81-read-only-opc-ua-account-server-side-enforcement)).
