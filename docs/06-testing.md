# 6 · Testing

No test touches the real factory. OPC UA is provided by the **mock server** (`backend/dmf_mock`),
which publishes the same namespace, node ids and browse names as `config/opcua_nodes.yaml`, makes
every variable read-only for clients, and **refuses and counts** every modifying request (Write, Call,
AddNodes, DeleteNodes, AddReferences, DeleteReferences, HistoryUpdate). Every test that starts it
asserts that the count is zero. What the mock can and cannot prove is listed in
[6.6](#66-what-the-mock-cannot-prove).

Results of the final audit run (2026-11-26/27, Windows 11, Python 3.13.2 with the exact versions of
`backend/constraints.txt`, PostgreSQL 16, Node 22): see [09-final-project-audit.md](09-final-project-audit.md#11-final-test-results).

| Layer | Tool | Location | Needs |
|---|---|---|---|
| Backend unit | pytest | `backend/tests/unit` | nothing |
| Backend integration | pytest | `backend/tests/integration` | PostgreSQL (`TEST_DATABASE_URL`); the mock OPC UA server starts automatically |
| Frontend component | Vitest + Testing Library (jsdom) | `frontend/src/**/*.test.ts(x)` | nothing |
| End-to-end | Playwright (Chromium) | `frontend/e2e` | full stack (below) |

## 6.1 Backend

```bash
cd backend
pip install -c constraints.txt -e ".[dev]"
ruff check . && ruff format --check .
pytest -q                                   # unit tests; DB tests are skipped
TEST_DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf_test pytest -q   # everything
TEST_DATABASE_URL=... pytest -q --cov --cov-report=term                         # with coverage
```

The integration database is truncated before each test, so **use a dedicated database**.

What is covered:

* **Plant model** (`test_plant_model`): config loading, the exact position→cell table and retrofit
  cells of the project description, port roles and merger priorities, every connection and robot
  link, consistency of the node map.
* **Status rules** (`test_status`): every priority branch of the cell status.
* **Tracker engine** (`test_engine`, `test_engine_concurrent`, `test_engine_robustness`): hand-over
  in both orders, splitters, mergers (priority), turntables (state 6 = empty), robot FIFO, storage,
  taken off, the exact stuck boundary, Bad quality, invalid values, connection loss and reconcile
  (order of several vanished pieces, uncertain/lost/found), hand-overs held through cell errors and
  slow take-ins, no phantom pieces after a blip or restart, timers surviving a restart, stalled
  tracker and clock steps, partial cell data, snapshot export/restore, many pieces at once with a
  strict occupancy invariant. The steps come from `dmf/testing/sequences.py`, which reproduces the
  PLC's state sequences.
* **Simulator vs tracker** (`test_simulator_vs_tracker`): 30 simulated minutes of random traffic.
  The simulator gives every piece an identity; the tracker must keep the same number per piece,
  through robot transfers (FIFO), with correct stuck timing, no anomalies and no lost pieces.
* **Recorded data** (`test_replay_recording`): `tests/data/mock-demo-recording.jsonl` replayed
  through the tracker against a golden summary. Add lab recordings here (see 08-lab-validation.md).
* **Read-only guard** (`test_readonly_guard`): scans the source for every mutating asyncua API
  (derived from asyncua itself), dynamic `getattr` access and raw UA request types.
* **OPC UA ingest** (`test_opcua_ingest`, `test_ingest_backoff`): subscription snapshot, changes,
  server restart and reconnect, subscription status change, back-off doubling and no reset by
  flapping sessions, refused monitored items, wrong node map entries, browse-name mismatches
  (values ignored), late initial notifications, PLC clock offset, zero write attempts.
* **Lab tools** (`test_cli_tools`): `dmf.cli.verify` exit codes 0/1/2/3 and its report, credentials
  never printed; a `dmf.cli.record` recording replayed by `dmf.cli.replay_check`.
* **Persistence** (`test_persistence`, `test_persistence_history`, `test_writer_buffer`): batched
  writes, replay protection (`writer_progress`), database outage with buffering (count, order and
  timestamps of what is written), SQLSTATE classification, overflow drops the oldest unwritten item,
  snapshot de-duplication, `was_stuck`/`stuck_since`, day boundaries on both clock-change days,
  restart recovery incl. pieces newer than the snapshot and the robot queue.
* **Service and API** (`test_tracker_service`, `test_api`, `test_platform_robustness`,
  `test_platform_units`, `test_export`, `test_logging_setup`): every piece status and ready flag,
  every endpoint, 422 bounds, 503 on a database outage, `/docs` redirect, redacted credentials,
  WebSocket client limit (1013), foreign Origin (403), binary frames, crashed background task →
  health 503, one bad message does not stop tracking, ordered shutdown, journey of a piece not yet
  written, every CSV cell (BOM, CRLF, quoting, Berlin time, formula defusing), PDF content and
  truncation, invalid settings rejected, and a backend restart that continues the numbering.

## 6.2 Frontend

```bash
cd frontend
npm ci
npm run typecheck && npm run lint
npm test                       # Vitest
npx vitest run --coverage     # coverage (v8)
```

Components are rendered with `StaticLiveProvider` and fixture data (`src/test/fixtures.tsx`), and
`fetch` and `WebSocket` are mocked (fake timers, no real waiting). The tests cover the factory map
(counts derived independently of the fixture, Needs attention, cell panel incl. the PLC's own ready
flags, offline cell, plant-wide offline card, frozen durations when data is old), Pieces now (every
status against the Moving and Need-attention filters, server stuck limit, origin fallback), History
(loading, request races, URL validation, journey, field-level errors, CSV/PDF download with the
server's day limit, failures in plain words, focus trap and return), the live provider (exact
back-off delays, 1013, silent-socket watchdog, REST start-up never shown as "Live", clock separate
from data), the status bar and the formatting helpers (Berlin time incl. daylight saving, durations,
dates when not today).

## 6.3 End-to-end

The Playwright tests drive deterministic situations through the mock's **HTTP control API** (test
only: `--control-port`). The backend runs with short timings so the tests stay fast.

```bash
# 1. empty database
createdb dmf_e2e

# 2. mock factory, idle, with control API
cd backend
python -m dmf_mock --mode idle --endpoint opc.tcp://127.0.0.1:48420/ --control-port 8765

# 3. backend (second terminal)
DATABASE_URL=postgresql+asyncpg://dmf:dmf@localhost:5432/dmf_e2e \
OPCUA_ENDPOINT=opc.tcp://127.0.0.1:48420/ SOURCE_LABEL=demo \
STUCK_AFTER_S=6 HANDOVER_WINDOW_S=3 OPCUA_RECONNECT_MAX_S=3 \
uvicorn dmf.main:app --port 8000

# 4. built frontend (third terminal; vite preview proxies /api and /ws to :8000)
cd frontend && npm run build && npx vite preview --port 4173 --host 127.0.0.1

# 5. tests
npx playwright install chromium        # once
E2E_BASE_URL=http://127.0.0.1:4173 npx playwright test
```

Scenarios: all 18 cells from the blueprint, a piece numbered once and followed through three cells
into its journey and taken off, error and stuck cells under Needs attention, CSV/PDF downloads (BOM
checked byte by byte), and losing and regaining the factory connection. Waits poll the page instead
of sleeping, assertions use each test's own piece number (so a database left over from an earlier
run does not make a test pass), and **after every test** the suite checks that the mock received
zero modifying requests.

## 6.4 CI

`.gitlab-ci.yml` runs the same commands: `lint` (ruff check, **ruff format --check**, tsc, eslint) →
`test` (pytest with a PostgreSQL service and coverage report; `alembic upgrade` → `check` →
`downgrade base` → `upgrade`; Vitest with coverage and the production build; `deps:audit` with
pip-audit and npm audit, reported but not blocking) → `e2e` (the stack above in one job; Playwright
report and logs kept as artifacts) → `build` (Docker images; pushed to the GitLab registry only from
the default branch or tags). Every command was run locally; **the pipeline itself has not run on
GitLab** (see 09 §17).

## 6.5 Manual checks with the demo

`docker compose -f docker-compose.yml -f docker-compose.demo.yml up` runs the random demo simulator
(pieces flowing, occasional stuck pieces and cell errors). Use it to check the UI by eye, never as
proof of the real plant's behaviour.

## 6.6 What the mock cannot prove

The mock makes the tests repeatable and safe, but it is built from the same configuration the
dashboard uses, so some things pass **by construction** and only the lab can check them:

| Area | Why the mock cannot prove it | Checked in the lab by |
|---|---|---|
| Node ids, browse names, data types | The mock creates its nodes from `config/opcua_nodes.yaml` | `dmf.cli.verify` (08 §8.3) |
| `ZelleN` = position N, port wiring, 14→15 transfer | The mock uses `config/plant.yaml` | 08 §8.4 |
| Short states being seen (state 4) | The mock's server queues every change; a real server with a coarser sampling interval may skip states, then the fallback pairing marks pieces *uncertain* | 08 §8.4 item 3.7, recordings |
| Real timings (hand-over, robot, stuck) | The simulator's timings are invented | recordings, 08 §8.6 |
| Robot behaviour | The simulator's robot keeps strict FIFO and has unlimited capacity; under 4× overload it holds pieces longer than `max_transit_s`, so they are (correctly) reported lost | 08 §8.5 |
| Offline PLC behaviour | Whether a switched-off PLC leaves its last values and whether timestamps keep advancing is unknown | 08 §8.7 |
| Server-side read-only account, security policies | The mock has no users or certificates | 08 §8.1 |
| Reconnect against the real server | Tested against asyncua's server (restart, BadShutdown, BadTimeout); a Siemens server may behave differently (session timeouts, subscription transfer) | 08 §8.8 item 7.6 |
| Performance | Measured on a laptop against the mock (09 §12) | 08 §8.8 under real traffic |
