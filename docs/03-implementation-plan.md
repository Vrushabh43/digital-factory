# 3 · Master implementation plan

Each phase ends with its own review + tests before the next starts.
Legend: `[x]` done and verified · `[~]` done, but not verifiable in this environment (lab or Docker needed; see note) · `[ ]` open.

## Phase 0 — Analysis & architecture
- [x] Analyse the project description, the 27 design frames, the PLC documentation and the TIA project
- [x] Recover OPC UA node ids, browse names, cell types, routing ports and state machines from the TIA project
- [x] Record inconsistencies, missing information and the lab verification list (`01-project-analysis.md`)
- [x] Compare architectures and choose one (`02-architecture.md`)

## Phase 1 — Repository & configuration
- [x] Monorepo layout, git init, `.gitignore`, `.editorconfig`
- [x] `tools/tia_nodemap` extractor → `config/opcua_nodes.yaml` (generated, verification: derived)
- [x] `config/plant.yaml`: positions↔cells, cell types/ports, connections, robots, storage, thresholds, map layout
- [x] `.env.example` with every environment variable documented

## Phase 2 — Backend core (pure)
- [x] Settings (pydantic-settings), JSON logging
- [x] Plant config loader + validation + directed conveyor graph (topology)
- [x] Tracker engine: occupancy, pairing window, appear/remove, robot FIFO, storage, stuck, reconcile, uncertainty
- [x] Cell status rules
- [x] Unit tests for topology, engine scenarios, status rules (incl. duplicates, out-of-order, restart)

## Phase 3 — OPC UA ingest (read-only)
- [x] Read-only client facade, namespace resolution by URI, browse-name validation
- [x] Subscription manager with reconnect/back-off and resync
- [~] Optional stale-data check (source timestamps) — implemented, off by default until timestamps are verified in the lab
- [x] Mock OPC UA server (same namespace + node ids) with scenario player and recording replay — test/demo only
- [x] Recorder CLI (`python -m dmf.cli.record`), verifier CLI (`python -m dmf.cli.verify`), offline replay check (`python -m dmf.cli.replay_check`)
- [x] Integration tests: backend ↔ mock server, reconnect after server restart, read-only guard test

## Phase 4 — Persistence
- [x] SQLAlchemy models, Alembic migration `0001`
- [x] Event sink (bounded queue, batched transactions, retry), idempotent writes
- [x] Snapshot save/restore, piece numbering continues after restart
- [x] Repository tests against PostgreSQL

## Phase 5 — API
- [x] REST: health, ready, status, plant, live, cell events, pieces now, history passes, journey
- [x] WebSocket `/ws/live` (full live snapshot incl. latest events per cell, coalesced, heartbeat)
- [x] CSV and PDF export
- [x] API tests (httpx, real uvicorn + PostgreSQL + mock OPC UA) + WebSocket test + backend restart test

## Phase 6 — Frontend
- [x] Vite + React + TypeScript scaffold, design tokens from the Figma frames
- [x] Live data provider (REST bootstrap + WebSocket with reconnect)
- [x] Factory map: summary cards, Needs attention, production line, legend, selected cell panel
- [x] Pieces now: KPI cards, filters, table, pieces per cell
- [x] History: filters, passes table with paging, journey panel, download pop-up (CSV/PDF)
- [x] Connection/offline/alarm banners, empty and error states (plant-wide offline card instead of 18 identical ones)
- [x] Vitest component tests (79 tests after the audit, 95 % line coverage) · visual check against the design PDF via screenshots · layout check 320–1440 px

## Phase 7 — End-to-end
- [x] Playwright tests on the full stack (PostgreSQL + backend + mock OPC UA with test control API + built frontend), 5 scenarios, zero modifying requests checked after every test (run twice in a row on 2026-11-26/27: 5/5 both times)

## Phase 8 — Deployment
- [~] Dockerfiles (backend, frontend/nginx; mock reuses the backend image), `docker-compose.yml` + `docker-compose.demo.yml`. Compose YAML checked and the wheel the image installs checked; **images not built here (no Docker on the development machine)**. CI job `build:images` builds them
- [~] Health checks, non-root containers, read-only root FS, dropped capabilities, security headers, env-driven config (same Docker caveat)
- [~] GitLab CI/CD: lint (+ format check), unit, integration (PostgreSQL service) + coverage + `alembic check`/downgrade/upgrade, frontend tests + coverage, dependency audit, e2e, image build. Every command was run locally and the file validated against the CI schema; the pipeline itself has not run on GitLab yet

## Phase 9 — Documentation
- [x] README (quick start), architecture, DB schema, API reference, testing, configuration, operations & troubleshooting
- [x] Lab validation guide (step-by-step checklist with expected observations, acceptance criteria, failure handling and a server-side read-only account)
- [x] Final project audit (`09-final-project-audit.md`)

## Phase 10 — Laboratory validation (requires plant access)
- [ ] Run `python -m dmf.cli.verify` against `opc.tcp://192.168.0.10:4840`; set `verification: verified`
- [ ] Confirm ZelleN = position N, port connections, robot behaviour, timestamp refresh
- [ ] Record ≥ 30 min with `python -m dmf.cli.record`; check with `python -m dmf.cli.replay_check`; compare with manual observation
- [ ] Tune `HANDOVER_WINDOW_S`, `STUCK_AFTER_S`, robot transit times
- [ ] Ask the lab for a read-only OPC UA user (08 §8.1); `verify` exit code 0
- [ ] Confirm that 14→15 is a robot/manual transfer (08 §8.4, item 3.4)

## Phase 11 — Audit and hardening (2026-11-26/27)
- [x] Requirements traceability, architecture, OPC UA safety, tracking edge cases, database, backend, frontend, security, tests, performance, Docker, CI and documentation reviewed; 95 distinct issues recorded; all P0 and P1 fixed, one P2 partially (dev-tool majors), two P3 advisories accepted, one P3 test gap open (details in 09)
- [x] New tests for every fix; test changes that alter an expected result are justified in 09 §7
- [x] Measured performance (09 §12)
- [~] Docker images and Compose stack still not built or run here (no Docker); GitLab pipeline not run
- [ ] Real-factory validation (Phase 10)
