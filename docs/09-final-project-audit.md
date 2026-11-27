# 9 · Final project audit

Audit of the complete repository against the original project description, carried out on
2026-11-26/27. Every issue found was fixed where it could be fixed and verified in this environment;
what could not be verified is said so explicitly. **Nothing in this report was tested against the
real factory, and Docker and GitLab CI were not executed** (no Docker on the audit machine, no GitLab
access). After the audit the code was committed and pushed to GitHub
(`github.com/Vrushabh43/digital-factory`, branch `main`, 2026-11-27), where `.gitlab-ci.yml` does
not run. The scores in §13 are an **internal assessment**; the project
description contains no official marking criteria.

Terms used for evidence:

* **Verified** — executed here and passed (test, command or measurement named).
* **Partially verified** — part executed (e.g. against the mock, or statically checked), part not.
* **Unverified** — not executed; needs the real factory, Docker or GitLab.

---

## 1 · Executive summary

The dashboard implements every function of the project description: live position of every piece
(cell, position, conveyor), cell status in words and colours, history of every cell pass with
CSV/PDF export, a strictly read-only OPC UA subscription, PostgreSQL with Alembic, React/TypeScript,
WebSocket updates, Docker Compose and GitLab CI.

The audit found **95 distinct issues** (1 P0, 15 P1, 43 P2, 36 P3; §4–5). The serious ones were not in the
happy path but in interruptions: the writer could **silently discard history** when PostgreSQL
refused work for reasons other than a network drop (P0); hand-overs interrupted by a cell error, a
1 s connection blip or a backend restart **renumbered real pieces** or invented phantom pieces; a
re-sent batch could corrupt history; background tasks could die silently while the page kept
showing "Live"; exports froze tracking for up to 10 s; the start-up query would have stopped the
backend from starting after a few years of history; the download dialog made the "To" date
impossible to type; and many tests passed by construction (23 of 33 seeded backend mutants
survived the original suite).

All P0 and P1 issues are fixed; three of these fixes still need proof on the plant or in Docker
(T-05, A-01, B-05). Of the P2 issues one is only partially fixed (A-08: the remaining dev-tool
advisories need major upgrades); two P3 advisories are accepted, and parts of four issues are kept
by design (§7.3). Every fix that can be tested here has a test (§4); §11.4 shows which tests were
run against the old code or a seeded mutant. One P3 test gap is still open (Q-14). In the last
complete runs after the fixes, 197/197 backend tests passed (unit + PostgreSQL integration, 87 %
coverage of statements and branches), 79/79 frontend tests passed (95.29 % of lines), the 5
end-to-end scenarios passed twice in a row on the full local stack, and the mock received **zero**
modifying requests in every run. Two small changes came after those runs (§11.5). The final full
re-run that would have covered them was stopped by the development tool because the machine ran out
of memory, after 177 of 198 tests had passed and none had failed; it was not repeated. Performance was measured, not estimated (§12).

**Verdicts (§18):** Academic submission — **CONDITIONALLY READY**. Real factory deployment —
**CONDITIONAL GO** (only for the supervised, read-only validation of docs/08; not for unattended use
before it has passed).

## 2 · Original project requirements

Source: `Project-description/DMF workpiece tracking dashboard – project description.pdf` (7 pages,
unchanged), the design `Team 11 DMF – Dashboard v2 (blueprint layout).pdf` and the PLC project
`PLC program cell.zip`. The constraints of the original build request are listed as C1–C6.

| ID | Requirement (short) | Source |
|---|---|---|
| R1 | Live: cell, position on the line and conveyor of every piece | p.1 Goal |
| R2 | Cell status: running, waiting for the next cell, stopped/error, offline | p.1, p.3–4 lamp table |
| R3 | Every cell a piece passed, with enter and leave time | p.1, p.4 item 4 |
| R4 | Out of scope: manual data entry, high-bay storage tracking, plant simulator, login | p.1 |
| R5 | 18-cell loop along the blueprint; branch priorities (out to storage > circulate > into storage) | p.1 |
| R6 | Position ↔ cell mapping kept as configuration (table) | p.1–2 |
| R7 | Retrofit positions 1, 6, 7, 8, 9, 10, 17, 18 | p.2 |
| R8 | Linear cells (3 conveyors, 2 ports), cross cells (4 conveyors + turntable, 3 ports) | p.2 |
| R9 | ≤ 1 piece per conveyor; pieces put on / taken off mid-line; pieces between sensors at PLC start not detected | p.2 |
| R10 | OPC UA server `opc.tcp://192.168.0.10:4840`; model `Allgemein`, `Zelle1…18` (lamp, ports, conveyors) | p.2–3 |
| R11 | Conveyor state values 0–6 | p.3 |
| R12 | Lamp values 1–4 map to dashboard colours; PLC without OPC → lamp 1 | p.3–4 |
| R13 | Derived events: entered, moved inside, ready to leave (stuck after a set time, e.g. 2 min), left | p.4 |
| R14 | Numbering P-0001…; hand-over = 4→0 paired with 1→2; appear without hand-over = put on; disappear at end sensor = taken off | p.4 |
| R15 | Each cell visit saved with enter time, leave time and result (passed or stuck) | p.4 |
| R16 | First/last conveyor depends on direction and port (input/target/alternative port) | p.4 |
| R17 | Subscription (no polling) to all conveyor states, lamps, ports, global alarm | p.5 |
| R18 | Read-only access, never writes to the plant | p.5 |
| R19 | Plain words for non-technical users; every status as a word and a colour | p.5 |
| R20 | Factory map page (blueprint, colours, conveyors, piece count, robots/storage drawn, cell panel, counts, Needs attention) | p.5 |
| R21 | Pieces now page (cell, position, conveyor, time in cell, came from; filters All/Moving/Need attention) | p.5 |
| R22 | History page (passes with entered/left/time/result; filter day/cell/piece; journey; CSV/PDF pop-up with from/to and type) | p.5 |
| R23 | Backend Python 3.12 + FastAPI, API docs at `/docs` | p.5 |
| R24 | OPC UA client asyncua | p.5 |
| R25 | PostgreSQL, SQLAlchemy, Alembic | p.5 |
| R26 | React + TypeScript (Vite), following the Figma design | p.5 |
| R27 | WebSocket live updates | p.6 |
| R28 | CSV and PDF exports from the backend | p.6 |
| R29 | Docker Compose with `db`, `backend`, `frontend`; one command | p.6 |
| R30 | GitLab repository | p.6 |
| R31 | Lab checks: ZelleN = position?, node ids, splitter/merger and port wiring, Alarm global?, read-only access | p.7 |
| C1 | Never assume unverified PLC variables / node ids / connections; document what needs lab verification | build request |
| C2 | Strictly read-only communication with the factory | build request |
| C3 | No unnecessary dependencies, over-engineering, hard-coded values or incomplete implementations | build request |
| C4 | Review, test and validate each phase | build request |
| C5 | Do not claim live integration without testing at the factory | build request |
| C6 | Preserve `Project-description/` unchanged | build request |

## 3 · Requirements traceability matrix

Status values: **Verified** · **Partially verified** · **Requires real factory validation**
(implemented and verified against the mock, correctness depends on the plant) · **N/A** (with
reason). No requirement is classified *incorrect* or *missing* after the audit.

| Req. | Implementation | Test evidence | Status | Issues (§4) |
|---|---|---|---|---|
| R1 | `backend/dmf/tracker/engine.py`, `services/tracker_service.py` (`pieces_now`, live payload), `frontend/src/pages/PiecesNow.tsx`, `FactoryMap.tsx` | `test_engine*`, `test_simulator_vs_tracker` (identity per piece), `test_tracker_service`, e2e test 2 | Requires real factory validation | T-01…T-16 |
| R2 | `tracker/status.py` (running + detail `waiting`/`idle`/`transporting`, stuck, error, offline, `partial_data`), `lib/text.ts` | `test_status` (all branches), `StatusBar.test`, `FactoryMap.test`, e2e test 3 | Requires real factory validation | T-08, T-13, U-03 |
| R3 | `persistence/writer.py` (`cell_passes`), `queries.py`, `GET /api/pieces/{code}/journey`, `History.tsx` | `test_persistence*`, `test_api` journey, e2e test 2 | Verified (mock) | B-01, B-03, B-11 |
| R4 | No data entry, no login, storage drawn but not tracked; the mock/simulator is test tooling only (`dmf_mock`, labelled "Demo data") | code review; `SOURCE_LABEL` badge test | N/A — out of scope respected | — |
| R5 | `config/plant.yaml` connections, merger priorities; the PLC sorts, the dashboard follows | `test_plant_model` (all connections, priorities), `test_merger_prefers_primary…` | Requires real factory validation | T-05 |
| R6 | `config/plant.yaml` `cells:` | `test_plant_model` exact position→cell table | Verified | Q-07 |
| R7 | `plant.yaml` `retrofit: true` | `test_plant_model` retrofit set | Verified (affects no tracking logic; the PLC adapts itself) | — |
| R8 | `plant/model.py` linear/cross builders, `plant.yaml` | `test_plant_model` port roles | Verified against the PLC project; requires lab | — |
| R9 | `engine.py` (one piece per conveyor, put on / taken off, start-up numbering of occupied conveyors) | `test_piece_put_on_mid_line…`, `test_startup_numbers_pieces_found_on_belts` | Verified (mock); undetectable pieces at PLC start remain invisible — plant limitation (docs/01 §1.5) | — |
| R10 | `config/opcua_nodes.yaml` (generated by `tools/tia_nodemap`, `verification: derived`), namespace resolved by URI | `test_opcua_ingest`, `test_cli_tools` (verify against mock) | **Requires real factory validation** (docs/08 §8.3) | T-12, C1 |
| R11 | `engine.py` `OCCUPIED={2,3,4,5}`, `EMPTY={0,1,6}` (decision I-1: state 6 = empty, PLC code contradicts p.4) | `test_turntable_state_6_counts_as_empty`, sequences | Verified against the PLC SCL; requires lab (08 §8.4 item 3.6) | — |
| R12 | `status.py` lamp mapping | `test_status` | Verified (mock); requires lab | — |
| R13 | `engine.py` events, `STUCK_AFTER_S` (default 120 s) | stuck boundary tests (+59.9 s not stuck, +60.0 s stuck), e2e test 3 | Requires real factory validation (timing, 08 §8.6) | T-01, T-11 |
| R14 | `engine.py` pairing window, hold rules, fallback marked uncertain | `test_engine`, `test_engine_robustness`, `test_simulator_vs_tracker` | Requires real factory validation | T-01…T-04, T-09, T-10, T-16 |
| R15 | `cell_passes` (`entered_at`, `left_at`, `result`, `was_stuck`, `stuck_since`), "Passed (was stuck)" | `test_persistence` (was_stuck), `test_export` | Verified (mock) | Q-10 |
| R16 | `plant.yaml` ports + roles; turntable chain | `test_plant_model`, merger/splitter tests | Requires real factory validation | — |
| R17 | `opcua/ingest.py` subscription (sampling 50 ms, queue 10), resync after reconnect | `test_opcua_ingest` | Requires real factory validation (sampling, 08 §8.4 item 3.7) | T-14, T-15 |
| R18 | `opcua/client.py` facade without write/call/node management | `test_readonly_guard`, mock refuses and counts 7 modifying services, 0 in every test and e2e run | Verified (code + mock); server-side read-only account requires lab (08 §8.1) | A-05, Q-08 |
| R19 | `lib/text.ts`, status words + colours, contrast fixes | frontend tests, agent's contrast measurement (§11) | Partially verified (no user test) | U-03, U-09, U-14, U-17, U-28 |
| R20 | `FactoryMap.tsx` | `FactoryMap.test` (17), e2e tests 1 and 3 | Verified (mock) | U-07, U-15, U-16, U-18 |
| R21 | `PiecesNow.tsx` | `PiecesNow.test` (10) | Verified (mock) | U-19, U-23 |
| R22 | `History.tsx`, `DownloadDialog.tsx`, `/api/history/*` | `History.test` (21), `DownloadDialog.test`, `test_api`, `test_export`, e2e test 4 | Verified (mock) | U-01, U-04, U-12, U-13, U-29 |
| R23 | FastAPI app; `/docs` → `/api/docs` | `test_docs_path_from_the_project_description_redirects` | Partially verified — tests ran on Python 3.13.2; the image and CI use 3.12 (not run) | — |
| R24 | asyncua 1.1.5 (pinned) | integration tests | Verified (against asyncua's server) | — |
| R25 | `persistence/schema.py`, `database/migrations` 0001–0002 | `alembic upgrade/check/downgrade/upgrade`, migration with existing data | Verified | B-03, B-08 |
| R26 | `frontend/` | lint, typecheck, Vitest, build, design comparison | Verified | U-* |
| R27 | `api/ws.py` | `test_api` WebSocket, `test_platform_robustness` (1013, Origin, binary), endurance run (§12) | Verified | B-13, A-01 |
| R28 | `services/export.py`, `/api/history/export` | `test_export` (every CSV cell, PDF content), e2e test 4 | Verified | B-02, B-15, A-03 |
| R29 | `docker-compose.yml`, `docker-compose.demo.yml`, Dockerfiles | static review only | **Partially verified — never built or run** | A-01, A-11 |
| R30 | `.gitlab-ci.yml`; repository on GitHub (`Vrushabh43/digital-factory`, `main`) | CI schema validation, every command run locally | **Partially verified — on GitHub, not yet on GitLab; pipeline never run** | — |
| R31 | `docs/08-lab-validation.md`, `dmf.cli.verify/record/replay_check` | `test_cli_tools` | Requires real factory validation | D-05 |
| C1 | node map `verification: derived`, UI footer note, docs/01 §1.4, docs/08 | review | Verified (documented) | T-05, D-01 |
| C2 | see R18 | see R18 | Verified (code + mock) | — |
| C3 | no new runtime dependency added in the audit; limits made settings (§7) | review | Verified | B-16 |
| C4 | every fix has a test that fails on the old code (§11.4) | §11 | Verified | — |
| C5 | README status box, this report | review | Verified | — |
| C6 | `Project-description/` — 3 files, unchanged | file listing, sizes and dates 2026-11-26 17:14–17:23 | Verified | — |

## 4 · Identified mistakes and weaknesses

Severity scale of the audit brief: **P0** critical safety/data integrity/fundamental requirement,
**P1** major functional or reliability problem, **P2** maintainability, performance, usability,
**P3** minor. ID prefixes: **T** tracking & OPC UA, **B** backend platform, **U** frontend, **Q** test
suite, **D** documentation, **A** further findings of the final review. "Test" in the verification
column names the test that fails on the old code and passes now.

### 4.1 Tracking and OPC UA (T)

| ID | Issue | Sev. | Affected files | Fix | Verification | Status |
|---|---|---|---|---|---|---|
| T-01 | A hand-over interrupted > 8 s (cell error, enable off, slow take-in) ended the piece as *removed* and the same piece got a new number | P1 | `tracker/engine.py` | Departures are held while the successor is taking in (state 1, up to `STUCK_AFTER_S`) or a cell is halted (lamp 1, not enabled, stale, unknown lamp) | Test `test_slow_take_in_does_not_remove_the_piece`, `test_hand_over_interrupted_by_a_cell_error…`, control tests that the window still ends | Fixed |
| T-02 | A 1 s connection blip during a hand-over turned the piece *lost* and renumbered it | P1 | `engine.py` (`_reconcile`) | Reconcile keeps departures whose successor is taking in, and treats it as a target | Test `test_blip_during_slow_take_in_keeps_the_piece` | Fixed |
| T-03 | Phantom piece after a resync/restart when the arrival was paired first and the source still showed 4 | P1 | `engine.py` (`export_state`, `_reconcile`) | `released` and pending arrivals are kept in the snapshot and across reconcile | Tests `test_no_phantom_piece_after_a_blip…`, `…after_a_restart…` | Fixed |
| T-04 | Crash recovery marked pieces newer than the last snapshot as lost and renumbered them | P1 | `persistence/recovery.py`, `services/tracker_service.py` | Active pieces in the database are adopted (position from the open pass, marked uncertain, anomaly event); snapshot also on every new and ended piece | Persistence recovery tests; restart test in `test_api` | Fixed |
| T-05 | 14→15 modelled as a conveyor connection, but the PLC wiring has no neighbour pair there and the blueprint shows a gap (robot) | P1 if confirmed | `config/plant.yaml`, `docs/01` | Modelled as robot link `robot-14-15` (`max_transit_s` 300); lab check added (08 §8.4 item 3.4) | `test_plant_model` robot links | Fixed in config — **requires real factory validation** |
| T-06 | Background tasks unsupervised: one exception stopped tracking silently while the page showed "Live" | P1 | `tracker_service.py`, `ingest.py`, `api/routes.py`, frontend | Supervision: phase `failed`, `/api/health` and `/api/ready` 503, error counter; one bad message no longer stops the consumer; **the UI now shows "Tracking stopped"** (A-13) | Tests `test_a_crashed_background_task_is_reported…`, `test_an_error_while_handling_one_message…`, `StatusBar.test` | Fixed |
| T-07 | Start-up treated every exception as "database not ready" (malformed snapshot → endless loop) | P2 | `tracker_service.py`, `recovery.py` | Malformed snapshot is discarded and reported; the log names the exception type. Waiting for the database before tracking is kept on purpose (numbering continues from the database) | Test `test_start_up_waits_for_the_database_and_then_runs`, recovery tests | Fixed (waiting by design, documented in docs/02) |
| T-08 | A single offline PLC invisible by default; timestamp check depended on clock sync between PLC and backend | P2 | `opcua/ingest.py` | Stale detection measures *when the source timestamp last advanced* in local time (PLC clock offset irrelevant); default stays `off` until the lab confirms timestamps refresh | Test `test_offline_detection_ignores_plc_clock_offset…` | Fixed — default **requires lab** (08 §8.7) |
| T-09 | Robot "FIFO" took the newest departure before the queue head | P3 | `engine.py` | Queue head first | Test `test_robot_places_pieces_first_in_first_out` | Fixed |
| T-10 | A put-on could be paired with an upstream piece waiting in state 3 (identity swap) | P3 | `engine.py` (`_best_source`) | Ranking: state 4 departures first, state-3 departures only as fallback and marked *uncertain* | Tests `test_missed_handing_state_pairs…`, merger tests | Fixed (ambiguity flagged, not silently decided) |
| T-11 | Waiting/stuck timers not persisted; reconcile kept stale `waiting_since` | P3 | `engine.py` | Exported and restored; recomputed after reconcile | Test `test_waiting_time_and_stuck_survive_a_restart` | Fixed |
| T-12 | Lenient values (3.7 → 3, `True` → 1, "3" → 3); nodes with a wrong browse name still used | P3 | `engine.py`, `ingest.py` | Strict int/bool; invalid values give an anomaly and an unknown state; values of mismatching nodes are ignored | Tests `test_values_of_the_wrong_type…`, `test_values_of_a_node_with_the_wrong_browse_name…` | Fixed |
| T-13 | A Bad or invalid conveyor state did not affect the cell status | P3 | `tracker/status.py` | `running` with reason `partial_data` | Test `test_a_conveyor_without_valid_data_is_reported_on_the_cell` | Fixed |
| T-14 | A late initial notification could overwrite a newer value read after subscribing | P3 | `ingest.py` | Per-key timestamp floor | Test `test_a_late_initial_notification_does_not_overwrite…` | Fixed |
| T-15 | Minor: (a) unbounded writer id cache, (b) `cells_passed` counted lost cells, (c) wall-clock windows (NTP step / stalled loop), (d) a flapping server reset the reconnect back-off every time, (e) a put-on next to a busy predecessor waits for the window | P3 | `writer.py`, `engine.py`, `ingest.py` | (a) LRU, (b) only cells left, (c) windows shifted on stalls and clock steps (`max_tick_gap_s`), (d) back-off resets only after a session lasted `OPCUA_RECONNECT_MAX_S`, (e) kept by design (it may be a hand-over) | Tests `test_cells_passed…`, `test_a_stalled_tracker…`, `test_clock_stepped_back…`, `test_flapping_sessions_keep_backing_off`, `test_put_on_while_predecessor_busy_waits_for_window` | Fixed; (e) accepted by design |
| T-16 | Reconcile sorted vanished pieces with a key that read the list while `list.sort()` had emptied it — the upstream→downstream order after a reconnect was random (present in the original code) | P2 | `engine.py` | Positions captured before sorting | Test `test_reconnect_with_two_vanished_pieces_keeps_their_order` (found by the new test) | Fixed |

### 4.2 Backend platform, database and security (B)

| ID | Issue | Sev. | Affected files | Fix | Verification | Status |
|---|---|---|---|---|---|---|
| B-01 | The writer **discarded history for good** when PostgreSQL refused work for reasons other than a network drop (starting up, admin shutdown, disk full, read-only, timeout, wrong password) | **P0** | `persistence/writer.py` | Only SQLSTATE classes 22 and 23 (bad data) skip an item; everything else is retried with back-off | `test_only_data_and_integrity_errors_count_as_bad_data`, persistence refusing test | Fixed |
| B-02 | CSV/PDF exports ran on the event loop and froze tracking (hand-overs expired → false removals) | P1 | `api/routes.py`, `services/export.py` | Rendering in a worker thread | Measured: loop stall 9 790 ms → 43 ms (§12) | Fixed |
| B-03 | Re-applying a committed batch (lost COMMIT acknowledgement) corrupted history | P1 | `writer.py`, migration `0002_writer_progress` | Replay protection: epoch + sequence stored in the same transaction | Persistence replay test; migration up/down/up and 0001→0002 with data | Fixed |
| B-04 | Duplicate of T-06 (supervision, readiness), found by both audits; not counted again | — | | | | see T-06 |
| B-05 | Restart renumbered pieces even after a clean shutdown (snapshot before draining; no graceful timeout) | P1 | `tracker_service.py`, `Dockerfile`, Compose | Ordered shutdown (stop ingest → drain → snapshot → flush), `--timeout-graceful-shutdown 20`, `stop_grace_period 30s`, plus T-04 | Test `test_shutdown_handles_queued_messages_before_the_final_snapshot` | Fixed (container stop timing unverified: no Docker) |
| B-06 | Snapshots piled up in the writer buffer during a database outage (~300 MB/day) | P2 | `writer.py` | Only the newest pending snapshot is kept | Test `test_only_the_newest_pending_snapshot_is_kept` | Fixed |
| B-07 | Buffer overflow during an in-flight batch lost the **newest** events instead of the oldest | P2 | `writer.py` | Overflow drops the oldest item outside the in-flight window | Test `test_full_buffer_drops_oldest_item_that_is_not_being_written` | Fixed |
| B-08 | Start-up query scanned all events (window function) — would exceed the 30 s timeout after a few years | P2 | `persistence/queries.py` | LATERAL per position on `(position, ts)` | Measured 2 994 ms → 17 ms at 1 M events (§12) | Fixed |
| B-09 | Only direct dependencies pinned | P2 | `backend/constraints.txt`, `Dockerfile`, CI | 53 exact pins used by image, CI and README; FastAPI 0.162.5 / Starlette 1.10.0 | `pip check`, pip-audit (§10), resolution for Linux/py3.12 | Fixed |
| B-10 | A database failure after start-up answered 500 instead of 503 | P2 | `api/app.py` | Handler for database/pool/OS/timeout errors → 503 | Test `test_database_outage_after_start_up_answers_503_not_500` | Fixed |
| B-11 | Journey of a live piece answered 404 while the writer lagged | P2 | `routes.py` | Answered from the tracker with `"pending": true` | Test `test_journey_of_a_piece_the_database_has_not_got_yet` | Fixed |
| B-12 | Edge inputs (year 9999, huge offset) answered 500 | P3 | `routes.py` | 422 with bounds 2000–2100, offset ≤ 10 000 000; messages use the UI's field names | Test `test_out_of_range_dates_and_offsets_are_rejected_with_422` | Fixed |
| B-13 | WebSocket: client 51 got HTTP 403 instead of 1013; race could exceed the limit; no Origin check | P3 | `api/ws.py` | Slot reserved before the first await, 1013 after accept, foreign Origin refused (403) | Tests `test_websocket_client_limit…`, `…foreign_web_page_is_refused`, endurance run | Fixed |
| B-14 | Credentials in `OPCUA_ENDPOINT` echoed in `/api/status` and logs | P3 | `settings.py`, `ingest.py`, `app.py`, `cli/*` | `redact_url` everywhere (incl. `verify` and `record`, A-05) | Tests `test_endpoint_credentials_are_redacted`, `test_credentials_in_the_endpoint_are_not_shown`, `test_verify_exit_code_2…` | Fixed |
| B-15 | Negative durations formatted as "-1 h 59 min 55 s" | P3 | `services/export.py` | Clamped to ≥ 0 | Test `test_negative_durations_are_clamped` | Fixed |
| B-16 | Settings not validated (invalid zone → 500 at request time, zero buffer killed the consumer, zero intervals = tight loops); `.env` only found from the working directory; pool size hard-coded | P3 | `settings.py`, `db.py` | Bounds and validators, both `.env` locations, `DB_POOL_SIZE`. Constants kept on purpose: batch size, heartbeat 5 s, PDF row limit, flush interval (documented) | Test `test_invalid_settings_are_rejected_at_start_up` (parametrised) | Fixed (constants accepted) |
| B-17 | Duplicate of T-15 (a); not counted again | — | | | | see T-15 |
| B-18 | A failed recovery retry duplicated robot-queue entries | P3 | `engine.py` | Restore replaces, never extends | Test `test_restoring_twice_does_not_duplicate_robot_queue_entries` | Fixed |
| B-19 | uvicorn lines bypassed the JSON formatter; docs drift (event kinds, `piece_code`); Swagger blocked by the CSP behind nginx | P3 | `logging_setup.py`, `deploy/nginx/*`, docs | uvicorn routed through the formatter (and A-04), docs corrected, relaxed CSP only for `/api/docs` | `test_logging_setup`; nginx statically only | Fixed (nginx unverified) |

### 4.3 Frontend (U)

All fixed in `frontend/src`; verification = frontend tests (34 targeted mutants, all detected) plus,
for layout and contrast, a Playwright measurement at 320–1440 px run during the fixes (not repeated
after the final small banner change).

| ID | Issue | Sev. | Affected files | Fix | Verification | Status |
|---|---|---|---|---|---|---|
| U-01 | Download dialog pulled focus back every second: the "To" date could not be typed | P1 | `DownloadDialog.tsx`, `History.tsx` | Stable callbacks, focus effect once | `DownloadDialog.test`, History "no refocus" | Fixed |
| U-02 | Old data presented as live (durations kept ticking offline) | P1 | `api/live.tsx`, `StaleNote.tsx`, `text.ts`, `Banners.tsx` | Clock frozen at the last state, "Last known state" texts | FactoryMap/PiecesNow stale tests | Fixed |
| U-03 | "Waiting for the next cell" never shown; running explanation false | P1 | `lib/text.ts` | Waiting and `partial_data` sentences | `FactoryMap.test` | Fixed |
| U-04 | History pages could overtake each other (race) | P1 | `History.tsx` | Request numbering + abort | Two race tests | Fixed |
| U-05 | An invalid `day` in the URL blanked the whole app | P1 | `History.tsx`, `FactoryMap.tsx`, `ErrorBoundary.tsx` | Parameter validation, error boundary | History URL tests, ErrorBoundary test | Fixed |
| U-06 | Stale socket never closed, so never reconnected | P2 | `live.tsx` | 15 s watchdog (heartbeat 5 s) | `live.test` | Fixed |
| U-07 | Map cells lost conveyors/piece count below ~1100 px | P2 | `app.css` | Minimum drawing width, scrolls inside its frame | Layout measurement | Fixed |
| U-08 | Sideways page scroll at 375 px | P2 | `app.css` | `minmax(0,1fr)`, wrapping | Layout measurement | Fixed |
| U-09 | Connection status only a colour dot on phones | P2 | `Header.tsx` | Short word ("Live", "Not live", …) | `StatusBar.test` | Fixed |
| U-10 | Header live region announced every second | P2 | `Header.tsx` | Coarse status without time | `StatusBar.test` | Fixed |
| U-11 | Banners recreated every second (re-announced) | P2 | `Banners.tsx` | Stable keys, countdown `aria-hidden` | `StatusBar.test` | Fixed |
| U-12 | Dialog without focus trap / focus return | P2 | `DownloadDialog.tsx` | Trap and return | History test | Fixed |
| U-13 | Export failures silent; day limit hard-coded | P2 | `DownloadDialog.tsx`, `types.ts` | Fetch + blob with plain error; limit from `export_max_days` | History tests (limit, limit+1, 503, 422) | Fixed |
| U-14 | API errors in technical words | P2 | `lib/errors.ts`, `api/client.ts` | `problemText()` | `errors.test` | Fixed |
| U-15 | Stuck cell panel overrode the PLC's own ready flags | P2 | `lib/text.ts` | PLC value shown, "Unknown" when offline | `FactoryMap.test` | Fixed (expected text changed, §7.2) |
| U-16 | Factory-offline texts contradicted the display | P2 | `text.ts`, `FactoryMap.tsx` | Consistent texts, disabled connection handled | `FactoryMap.test` | Fixed |
| U-17 | "Stuck" pill contrast 3.64:1 | P2 | `tokens.css` | `#8f5e0f`, 5.56:1 | Contrast measurement | Fixed |
| U-18 | Selected-cell panel updated off-screen on narrow screens | P2 | `FactoryMap.tsx` | Scrolled into view | `FactoryMap.test` | Fixed |
| U-19 | "Came from" showed "–" for sourced/put-on pieces | P2 | `PiecesNow.tsx`, `text.ts` | Falls back to origin | `PiecesNow.test` | Fixed |
| U-20 | 1 s clock re-rendered every consumer | P3 | `live.tsx`, `FactoryMap.tsx` | Separate clock context, memoised line | `live.test` | Fixed |
| U-21 | Stale journey error survived a new search | P3 | `History.tsx` | Cleared with request numbering | History test | Fixed |
| U-22 | Day error shown under "Piece number" | P3 | `History.tsx` | Field-level errors, `aria-invalid` | History test | Fixed |
| U-23 | "Long stay" threshold hard-coded 120 s | P3 | `PiecesNow.tsx` | `plant.stuck_after_s` | `PiecesNow.test` | Fixed |
| U-24 | REST start-up data marked the header "Live" | P3 | `live.tsx` | Never sets online | `live.test` | Fixed |
| U-25 | Dates missing where times span days | P3 | `format.ts` | `fmtWhen()` | `format.test` | Fixed |
| U-26 | Focus and selection looked the same | P3 | `tokens.css`, `app.css` | Distinct ring/outline | Layout check | Fixed |
| U-27 | Uncertainty reason only in a tooltip | P3 | `PiecesNow.tsx`, `History.tsx` | Visible note | `PiecesNow.test` | Fixed |
| U-28 | Footer contrast 2.86:1 and jargon | P3 | `tokens.css`, `App.tsx` | 5.44:1, plain text | `StatusBar.test` (Footer) | Fixed |
| U-29 | Piece search kept the pre-filled day | P3 | `History.tsx` | Searches all days unless changed | History test | Fixed |

### 4.4 Test suite (Q)

| ID | Issue | Sev. | Affected files | Fix | Verification | Status |
|---|---|---|---|---|---|---|
| Q-01 | Replay test passed with a tracker that tracked nothing | P2 | `test_replay_recording.py` | Golden summary | Mutant "ignore state changes" killed | Fixed |
| Q-02 | Stuck boundary ±1.2 s; waiting on any conveyor not caught | P2 | `test_engine.py` | Exact boundary, non-exit never stuck | M03, M04, M24 killed | Fixed |
| Q-03 | Invalid-state test only checked "some anomaly" | P2 | `test_engine.py` | Piece kept, state unknown, exact text | Mutant "invalid → 0" killed | Fixed |
| Q-04 | Near-vacuous concurrency invariant | P2 | `test_engine_concurrent.py` | Strict invariant | 2 mutants killed | Fixed |
| Q-05 | Reconnect test moved one piece only | P2 | `test_engine.py` | Two vanished pieces, long gap → uncertain | Found T-16 | Fixed |
| Q-06 | Simulator test compared sets, not identities | P2 | `test_simulator_vs_tracker.py`, `dmf_mock/simulator.py` | Identity per piece, FIFO, stuck timing | Robot LIFO, M03/M04 killed | Fixed |
| Q-07 | Position→cell table not pinned (swap of 5/10 survived) | P2 | `test_plant_model.py` | Exact table, retrofit, ports, links | M19 killed | Fixed |
| Q-08 | Read-only guard matched names only; mock counted only writes | P2 | `test_readonly_guard.py`, `dmf_mock/server.py` | Guard derived from asyncua's mutating API, `getattr`, request types; mock refuses and counts Write, Call, AddNodes, DeleteNodes, AddReferences, DeleteReferences, HistoryUpdate | M13 caught statically; runtime `call_method` caught | Fixed |
| Q-09 | Ingest tests: tautological mismatch check, fixed sleeps, back-off not asserted | P2 | `test_opcua_ingest.py` | Wrong node map, BadTimeout, back-off, refused items, polling | M14, M29 killed | Fixed |
| Q-10 | Persistence: idempotency only without movement; `day_bounds` test could not fail; outage test weak | P2 | `test_persistence*.py` | Clock-change days as UTC instants, exact stored events, `was_stuck` | M16, M28, M30, M31 killed | Fixed |
| Q-11 | API tests: readiness not enforced, sleeps, 3 of 44 CSV cells, no 31/32-day check, no WS limits | P2 | `test_api.py`, `test_export.py`, `test_tracker_service.py` | Every cell, limits, polling, service statuses | M10–M12, M17, M18, M27, M35, M36 killed | Fixed |
| Q-12 | Frontend tests: echoed fixture counts, Moving filter untested, 31-day limit untested, real timers | P2 | `frontend/src/**/*.test.*` | Rewritten (see §4.3) | 34/34 mutants killed | Fixed |
| Q-13 | e2e: fixed waits, write check reset by test 5, state carried between runs, BOM hidden by `trimStart` | P2 | `frontend/e2e/dashboard.spec.ts` | Polling, check after every test, own piece codes, byte check | Two consecutive passing runs on one database | Fixed |
| Q-14 | `JsonFormatter`, the production default log format (`LOG_FORMAT=json`), is not covered by any test (all tests log as text); found at the very end of the audit | P3 | `dmf/logging_setup.py`, `tests/unit/test_logging_setup.py` | A unit test that formats a record with an exception and extra fields and parses the line as JSON | — | **Open**: not written, because no further test run was possible (§11.5); reviewed by reading only |

### 4.5 Documentation (D)

| ID | Issue | Sev. | Affected files | Fix | Verification | Status |
|---|---|---|---|---|---|---|
| D-01 | docs/01: I-8 parenthesis wrong, I-10 wrong (the PLC does wire neighbours), §1.5 "FIFO" did not match code | P3 | `docs/01` | Corrected, I-11 (14→15) added | Review against PLC data | Fixed |
| D-02 | docs/04: wrong event kind names, `writer_progress` missing, no backup/restore | P3 | `docs/04` | Corrected; `pg_dump -Fc`/`pg_restore`; "nothing deletes production data" | Review | Fixed |
| D-03 | docs/05: `piece`/`piece_code`, 1013, Origin, 503, `/docs`, `export_max_days`, `partial_data`, `pending`, health 503 | P3 | `docs/05` | Updated | Review against routes and tests | Fixed |
| D-04 | docs/07: networks, settings validation, graceful stop, health semantics, image pinning | P3 | `docs/07`, `.env.example` | Updated | Review | Fixed |
| D-05 | docs/08 had no expected observations, acceptance criteria or failure handling, and no server-side read-only account; it promised `verify` exit 0 = cannot write, which the code did not do | P2 | `docs/08`, `cli/verify.py` | Rewritten; `verify` exit code 3 when the user may write | `test_cli_tools` (exit 0/1/2/3) | Fixed |
| D-06 | docs/06: counts outdated, no statement of what the mock cannot prove | P3 | `docs/06` | Updated, new §6.6 | Review | Fixed |

### 4.6 Further findings of the final review (A)

| ID | Issue | Sev. | Affected files | Fix | Verification | Status |
|---|---|---|---|---|---|---|
| A-01 | nginx passed `Host` without the port; with the new WebSocket Origin check (B-13) the dashboard's own WebSocket on port 8080 would have been refused — the live view would never have updated in the Docker deployment | P1 | `deploy/nginx/default.conf` | `proxy_set_header Host $http_host`; `CORS_ORIGINS` documented for outside proxies | Static review; backend Origin logic tested; **nginx not run** | Fixed (unverified at runtime) |
| A-02 | `WS_MAX_CLIENTS`, `DB_POOL_SIZE` not passed by Compose nor listed in `.env.example` | P3 | `docker-compose.yml`, `.env.example` | Added | Static | Fixed |
| A-03 | PDF layout quadratic (reportlab re-measured remaining rows at each page split) | P2 | `services/export.py` | Fixed row heights (same 15.6 pt layout, same 157 pages) | Measured 9.25 s → 5.38 s; `test_export` | Fixed |
| A-04 | `LOG_LEVEL` did not apply to uvicorn's loggers (INFO access lines at WARNING) | P3 | `logging_setup.py` | Levels reset to follow the root logger | `test_logging_setup` (fails on old code) | Fixed |
| A-05 | `verify` and `record` printed/stored the endpoint with credentials; `verify` exit 0 although writable (see D-05) | P2 | `cli/verify.py`, `cli/record.py` | `redact_url`, exit code 3 | `test_cli_tools` (2 tests fail on old code) | Fixed |
| A-06 | Lab tools had 0 % test coverage | P2 | `tests/integration/test_cli_tools.py` | 5 tests against the mock | Run | Fixed |
| A-07 | Ruff formatting inconsistent (37 files); no format check in CI | P3 | backend, `.gitlab-ci.yml` | `ruff format`, `ruff format --check` in CI | 62 files formatted, check clean | Fixed |
| A-08 | Dev-tool advisories: vitest 2.1.8 (remote code execution while the Vitest API server listens), vite dev server | P2 | `frontend/package.json` | vitest/coverage 2.1.9, vite 5.4.21 (same minor). Remaining advisories need major upgrades (vitest 5, vite 8, typescript-eslint) — not done in the audit | npm audit; all frontend checks pass after the upgrade | Partially fixed (dev only) |
| A-09 | react-router-dom 6.30.6: 2 moderate advisories (open redirect via backslash in `<Link>`/`useNavigate`; SSR hydration) — fix only in v7 | P3 | `frontend/package.json` | Accepted: no SSR, both links are built from integer positions and server-generated `P-NNNN` codes | Code review | Accepted |
| A-10 | pytest 8.3.4: PYSEC-2026-1845 (fix 9.0.3, needs pytest-asyncio 1.x) | P3 | `backend/pyproject.toml` | Accepted: test-only, not in the image; CI `deps:audit` reports it | pip-audit | Accepted |
| A-11 | OPC UA client keys could be baked into the image (`config/` is copied); the documented `/certs` path was not mountable | P2 | `.dockerignore`, `.gitignore`, `.env.example` | Keys/certs ignored; mount via override file documented | Static | Fixed (unverified at runtime) |
| A-12 | Generated junk from the audit itself (2 `bash.exe.stackdump`, stale preview server) | P3 | repository root, `backend/` | Removed / stopped | `git status` shows only project files | Fixed |
| A-13 | The UI had no state for the new `failed` phase: heartbeats kept arriving, so the header still said "Live" | P2 | `types.ts`, `Header.tsx`, `Banners.tsx` | "Tracking stopped" header and alert banner | `StatusBar.test` (2 new assertions) | Fixed |

## 5 · Severity classifications

| Severity | Distinct issues | Fixed and verified here | Fixed, runtime proof outstanding | Partially fixed | Accepted / open |
|---|---|---|---|---|---|
| P0 | 1 | 1 | 0 | 0 | 0 |
| P1 | 15 | 12 | 3 (T-05 lab, A-01 nginx, B-05 container stop) | 0 | 0 |
| P2 | 43 | 40 | 2 (T-08 default needs lab, A-11 Docker) | 1 (A-08) | 0 |
| P3 | 36 | 31 | 2 (B-19 nginx CSP, A-02 Compose) | 0 | 2 accepted (A-09, A-10), 1 open (Q-14) |
| **Total** | **95** | **84** | **7** | **1** | **2 accepted, 1 open** |

Counted by row of §4: 96 rows, of which B-04 and B-17 are duplicates of T-06 and T-15 (a). Parts of
T-07, T-10, T-15 (e) and B-16 are kept by design (§7.3); those issues count as fixed because their
defect is fixed. By area: tracking/OPC UA 16, backend platform 17, frontend 29, test suite 14,
documentation 6, final review 13.

## 6 · Root causes

1. **Designed for the uninterrupted sequence.** The tracker's time windows expired regardless of what
   the plant was doing, and reconcile only knew occupied conveyors (T-01…T-03, T-08). A PLC hand-over
   can legitimately pause for minutes (error, enable off); "no news" is not "removed".
2. **Persistence assumed only network errors and exactly-once commits** (B-01, B-03, B-07). The
   database can refuse work in many recoverable ways, and a COMMIT can succeed without the client
   hearing it.
3. **Blocking work and unsupervised tasks on a single event loop** (B-02, T-06). The tracker shares the
   loop with the API, so a slow export is a tracking fault.
4. **Lifecycle order never specified** (B-05, T-04): snapshot, drain and recovery were each correct
   alone but not in sequence.
5. **Tests checked outcomes loosely, and the mock is generated from the same configuration**, so
   several checks were true by construction (Q-01, Q-07, Q-09); 23 of 33 seeded backend mutants and
   6 of 12 frontend mutants survived.
6. **Configuration not cross-checked against the PLC wiring** (T-05, D-01).
7. **UI effects with unstable dependencies, no request sequencing and no explicit "stale" model**
   (U-01, U-02, U-04, U-06).
8. **Limits and defaults not validated; documentation written before the code moved on** (B-16,
   D-01…D-06).
9. **A fix not followed through the deployment path** (A-01: an Origin check that the nginx Host
   header would have defeated) — found by review, because nginx could not be run here.

## 7 · Implemented fixes

Every fix is listed with its test in §4. Principles kept: no new runtime dependency, no rewrite of
working functionality, read-only communication unchanged, the original scope unchanged.

### 7.1 Main changes

* **Tracker** (`engine.py`, `status.py`): hold rules, reconcile of taking-in successors, `released`
  and timers in the snapshot, adoption on restart, strict values, ranked pairing with *uncertain*
  marking, robot FIFO, partial data, stall/clock-step guard, deterministic reconcile order.
* **Persistence**: SQLSTATE classification, replay protection (`writer_progress`, migration 0002),
  overflow and snapshot de-duplication, LATERAL start-up query, recovery adoption, pool size setting.
* **Service/API**: single ordered message queue (ticks included), supervision, ordered shutdown, 503
  mapping, 422 bounds, `/docs` redirect, journey fallback, exports in a worker thread, redaction,
  WebSocket limit/Origin/binary frames, settings validation, uvicorn logging.
* **OPC UA ingest**: timestamp floor, untrusted (mismatching) nodes ignored, stale detection by
  timestamp advancement, back-off not reset by flapping sessions.
* **Lab tools**: `verify` exit code 3, redaction.
* **Frontend**: U-01…U-29 and A-13.
* **Deployment/CI**: constraints file, graceful shutdown, nginx Host header and CSP, Compose
  variables, `.dockerignore` for keys and generated files, CI format check, coverage, migration
  round trip, dependency audit, e2e image, DinD TLS.
* **Config**: 14→15 as robot link (pending lab check).

### 7.2 Test expectations that were changed (not weakened)

| Test | Old expectation | New expectation | Justification |
|---|---|---|---|
| `test_api` / persistence restart tests | a piece put on after the last snapshot is *lost* and returns as a new number | it keeps its number (adopted, marked uncertain) | The old expectation encoded bug T-04; the project description numbers a piece once; docs/08 7.7 acceptance criterion |
| merger test in `test_engine` | a put-on next to a waiting piece was paired with it | realistic hand-over scenario + new priority test | The old test encoded the identity swap T-10 |
| `test_plant_model` robot links | links 11→12, 17→18 | plus 14→15 | Deliberate configuration change T-05 |
| `FactoryMap.test` cell panel | "No, exit is blocked" for a stuck cell | the PLC's own values | U-15: the dashboard must show the PLC's data, not override it |

No test was skipped, deleted to make the suite pass, or loosened; several were tightened (§4.4).

### 7.3 Accepted, with reason

T-07 (waiting for the database before tracking: numbering must continue from the database), T-10
(remaining ambiguity is flagged *uncertain* instead of guessed), T-15e (a put-on next to a busy
predecessor may be a hand-over; it gets its number after the window), B-16 (a few internal constants
stay constants), A-09 (react-router advisory not reachable: no SSR, no user-controlled links),
A-10 (pytest advisory, test-only).

## 8 · Modified files

Files changed or added during the audit (by modification time; the audit took place before the
first commit, so there is no diff). `Project-description/` is not among them.

Backend code was compared with a copy taken before the fixes (after normalising both with
`ruff format`); the other areas are listed by modification time.

* **Root:** `.dockerignore`, `.env.example`, `.gitignore`, `.gitlab-ci.yml`, `README.md`,
  `docker-compose.yml`, `docker-compose.demo.yml`.
* **Config / database / deploy:** `config/plant.yaml`,
  `database/migrations/versions/0002_writer_progress.py` (new), `deploy/nginx/default.conf`,
  `deploy/nginx/security-headers.conf`.
* **Backend code:** `backend/Dockerfile`, `backend/constraints.txt` (new), `backend/pyproject.toml`,
  `dmf/api/{app,routes,ws}.py`, `dmf/cli/{record,verify}.py`, `dmf/logging_setup.py`,
  `dmf/opcua/ingest.py`, `dmf/persistence/{db,queries,recovery,schema,writer}.py`,
  `dmf/services/{export,tracker_service}.py`, `dmf/settings.py`, `dmf/tracker/{engine,status}.py`,
  `dmf_mock/{control,server,simulator}.py` (20 files with changes in content); 8 more files changed
  only by `ruff format` (A-07).
* **Backend tests:** changed `tests/integration/{conftest,test_api,test_opcua_ingest,test_persistence}.py`,
  `tests/unit/{test_engine,test_engine_concurrent,test_plant_model,test_readonly_guard,test_replay_recording,test_simulator_vs_tracker}.py`;
  new `tests/integration/{test_cli_tools,test_mock_server,test_persistence_history,test_platform_robustness}.py`,
  `tests/unit/{test_engine_robustness,test_export,test_ingest_backoff,test_logging_setup,test_platform_units,test_tracker_service,test_writer_buffer}.py`.
* **Frontend:** `Dockerfile`, `eslint.config.js`, `package.json`, `package-lock.json`,
  `e2e/dashboard.spec.ts`, `src/App.tsx`, `src/main.tsx`, `src/api/{client.ts,live.tsx,types.ts,live.test.tsx}`,
  `src/components/{Banners,DownloadDialog,Header}.tsx`, new `src/components/{ErrorBoundary,StaleNote}.tsx`,
  `src/lib/{format,text}.ts`, new `src/lib/errors.ts`, `src/pages/{FactoryMap,History,PiecesNow}.tsx`,
  `src/styles/{app,tokens}.css`, `src/test/fixtures.tsx`, the test files `src/**/*.test.ts(x)` (updated or added).
* **Docs:** `docs/01`, `02`, `03`, `04`, `05`, `06`, `07`, `08`, new `docs/09` (this file).

## 9 · Architecture evaluation

**Kept, because it fits the problem:** one backend process owns the tracker state (single uvicorn
worker), fed by one ordered queue of OPC UA messages and timer ticks; events go to PostgreSQL through
a buffering writer; the API and WebSocket read the in-memory state. This makes ordering trivial (no
distributed state) and keeps live tracking independent of the database. The plant is configuration
(`plant.yaml`, `opcua_nodes.yaml`), so a moved cell is a config change, not a code change.

**Weak points found and fixed:** blocking work on the shared loop (B-02), unsupervised tasks (T-06),
unspecified lifecycle order (B-05, T-04), at-least-once writes without idempotency (B-03).

**Remaining architectural limits (documented, not defects):**

* One process = one point of failure. Recovery is designed (snapshot + adoption + replay protection),
  horizontal scaling is not (and not needed for one plant and a few dozen browsers, §12).
* Piece identity is inferred from state changes; where the plant gives no evidence (robot reordering,
  missed short states) the tracker marks pieces *uncertain* instead of guessing. Only the lab can
  show how often that happens.
* The WebSocket sends the full live state (≈ 35 kB raw, ≈ 2.8 kB compressed) instead of deltas —
  simpler and robust against lost messages; measured cost is low (§12).

## 10 · Security and industrial safety evaluation

**Industrial safety (read-only):**

* Only `dmf/opcua/client.py` imports the asyncua client; its facade has connect, read, browse and
  subscribe only. `test_readonly_guard` scans all backend code for every mutating asyncua API
  (derived from asyncua itself), dynamic `getattr` access and raw UA request types — verified.
* The mock refuses and counts Write, Call, AddNodes, DeleteNodes, AddReferences, DeleteReferences and
  HistoryUpdate; every integration test, the endurance run and both e2e runs ended with **0** — verified.
* `verify` reads `UserAccessLevel` and reports (exit 3) if the dashboard's user *could* write; docs/08
  §8.1 describes a server-side read-only user. Note: the PLCs themselves write into this server, so
  the restriction must apply to the dashboard's user only — **requires lab**.
* Load on the plant's server: one session, one subscription (sampling 50 ms, publishing 100 ms,
  queue 10), reconnect back-off 1 → 30 s with jitter, no longer reset by flapping (T-15d).
* The backend was never connected to `192.168.0.10` during development or the audit.

**Security:**

* No login by scope (R4): bind to `127.0.0.1` or the lab network (`HTTP_BIND`), documented.
* nginx allows only GET/HEAD on `/api`, 16 kB bodies, CSP and security headers; containers non-root,
  read-only, no capabilities, `no-new-privileges`; database on an internal network — **static only**.
* WebSocket Origin check, client limit; credentials redacted; CSV formula injection defused; inputs
  bounded (422); no 500 on database outage — verified by tests.
* Secrets: `.env` and keys git- and docker-ignored; Compose refuses to start without
  `POSTGRES_PASSWORD`; no secret found in the repository (the 139 files a first commit would contain were
  listed and reviewed).
* Dependencies (2026-11-26): pip-audit over all 53 pins → 1 advisory (pytest, test-only, A-10);
  npm production dependencies → 2 moderate (react-router-dom, A-09); dev tooling → remaining
  advisories need major upgrades (A-08). CI job `deps:audit` repeats both audits on every pipeline.

## 11 · Final test results

Environment: Windows 11, Intel Core i5-1035G1, 7.7 GB RAM, Python **3.13.2** with the exact versions
of `backend/constraints.txt` (FastAPI 0.162.5, Starlette 1.10.0, asyncua 1.1.5, SQLAlchemy 2.0.36),
PostgreSQL 16 (local, port 55432), Node 22 with `package-lock.json`.

### 11.1 Backend

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean / 62 files formatted |
| `pytest` (unit + integration with PostgreSQL) | **197 passed, 0 failed** in 610 s (last complete run, 2026-11-27 00:01–00:11). Final re-run with the 198th test: **stopped by the development tool at 177/198** because the machine was critically low on memory; 177 passed and 0 failed up to then, including every integration test (§11.5) |
| Coverage (`--cov`, statements + branches) | 87 % overall: statements 3 052/3 416 (89.3 %), branches 824/1 038 (79.4 %), from the last complete run |
| Alembic | `upgrade head` on an empty database, `check` (no drift), `downgrade base`, `upgrade head`; 0001→0002 with existing data keeps all rows |
| `pip check` | no broken requirements |

Lowest coverage: `dmf/main.py` (2 statements) and `dmf_mock/__main__.py` 0 % (process entry
points, run only as processes); `dmf_mock/control.py` 58.5 % (HTTP control API, used mainly by the
e2e tests, which are not measured); `dmf/cli/replay_check.py` 63.9 % and `dmf/cli/record.py` 67.6 %
(the tests call the functions, not the command-line wrappers); `dmf/logging_setup.py` 64.3 % (the
JSON formatter is untested, Q-14); `dmf/api/routes.py` 71.7 % (not analysed further).

### 11.2 Frontend

| Check | Result |
|---|---|
| `npm run lint` | 0 problems (also after A-13) |
| `npm run typecheck` | exit 0 (also after A-13) |
| `npx vitest run --coverage` | **79 passed, 0 failed** in 27.2 s (last full run, 2026-11-26 23:43); 95.29 % of lines (1 781/1 869), 83.44 % of branches (630/755). After A-13: `StatusBar.test.tsx` 9/9 passed; the full suite was not re-run (§11.5) |
| `npm run build` | JS 212.24 kB (68.76 kB gzip), CSS 28 kB (5.3 kB gzip); build of 2026-11-26 23:41, not rebuilt after A-13 (§11.5) |

### 11.3 End-to-end (full local stack: PostgreSQL + backend + mock with control API + built frontend via `vite preview`)

| Run | Result |
|---|---|
| 1 | 5/5 passed; the mock counted 0 modifying requests after every test |
| 2 (directly after, same database) | 5/5 passed; 0 modifying requests; data left by run 1 did not make a test pass (each test uses its own piece number) |
| Code under test | backend as of 2026-11-26 23:54, frontend build of 23:41; the two later changes (§11.5) were not covered by e2e |

### 11.4 Do the tests detect the old bugs?

* 13 of 15 new engine robustness tests fail on the pre-audit code (2 are controls that must pass on both).
* Run against a copy of the pre-audit backend code: `test_logging_setup` 2/2 fail (uvicorn logger
  level and handlers), `test_ingest_backoff` 1/2 fails (waits stay at the minimum; the other test is
  a control), `test_cli_tools` 2 of 4 `verify` tests fail (exit 0 instead of 3 for a writable user;
  credentials printed); the other 3 CLI tests pass on both, as they should.
* `test_start_up_waits_for_the_database_and_then_runs` passes on the pre-audit code (it already
  waited) and fails on mutant M23 ("start-up gives up instead of waiting for the database"), which
  had no killing test before.
* Backend mutation testing (seeded mutants from the test audit): all 23 previously surviving mutants
  that the test audit listed are now killed, plus new mutants for the guard, ingest, export and
  service (see §4.4). Mutation testing was targeted, not exhaustive.
* Frontend: 34 seeded mutants, 34 detected.

### 11.5 Changes after the last complete runs

| Change | Checked by |
|---|---|
| `dmf/api/routes.py`: 422 messages use the field names of the UI ("Day", "From", "To") and dd.mm.yyyy dates (B-12) | `ruff check` and `ruff format --check`; no test asserts this wording; the integration tests that send invalid dates (`test_validation_errors`, `test_out_of_range_dates_and_offsets_are_rejected_with_422`) passed in the stopped final run |
| New test `test_start_up_waits_for_the_database_and_then_runs` | passed alone (14 s) and in the stopped final run; fails on mutant M23 (§11.4) |
| A-13: header "Tracking stopped" and alert banner for the `failed` phase (frontend) | `npm run lint`, `npm run typecheck`, `StatusBar.test.tsx` 9/9 |

**Not run on the final code:** the backend suite to the end (21 unit tests were still to come when
it was stopped), the full frontend suite with coverage, the production build and the e2e suite. The
final backend run was stopped by the development tool because the machine was critically low on
memory (under 100 MB of 7.9 GB free afterwards), and no further runs were started while memory was
short. Before submission, run once: `pytest -q` in `backend/` with `TEST_DATABASE_URL` set,
`npx vitest run --coverage && npm run build` in `frontend/`, and the end-to-end run described in
docs/06 — or let the first GitLab pipeline do it (§17).

## 12 · Performance measurements

All numbers were measured on the laptop above, against the mock (never the plant). Scripts are kept
outside the repository (they are one-off measurements); each figure below is the actual output.

| What | Setup | Result |
|---|---|---|
| Tracker throughput | 60 simulated minutes, up to 40 pieces: 111 146 value changes + 36 000 ticks → 17 752 events, replayed 5× | median 24.8 s (min 17.4, max 41.4 — laptop power management) ≈ **4 490 messages/s** (value changes incl. ticks), 223 µs each. The dense scenario produces 31 changes/s, i.e. > 100× headroom |
| Live message | 92 pieces | 50 447 B, built and serialised in 3.4 ms |
| Endurance + 50 WebSocket clients | mock at 4× speed, 509 s, 50 clients | 82 300 messages, 0 errors, 0 server-side closes; 3.22 msg/s per client (limit 4 Hz), median interval 268 ms, max 1.6 s; backend CPU **36 %** of one core (10 % with no clients → ≈ 0.5 % per client); working set 30–47 MiB, 43 → 33 MiB start→end, handles stable at 272 after warm-up; writer backlog 0–2, 0 dropped; client 51 → close 1013; foreign Origin → 403 |
| Network | average raw message 34 940 B with ~11 pieces | ≈ 2 793 B per message with permessage-deflate (negotiated by default; estimated with zlib incl. context takeover, not captured on the wire) → ≈ 3.6 Mbit/s for 50 clients at 3.2 msg/s |
| HTTP latency | kept-alive connection, 40 requests each, mock at 4× | `/api/health` median 2.2 ms; `/api/live` 7.1 ms (p90 14.0); `/api/history/passes` 22.9 ms (p90 37.2); `/api/cells/10/events` 12.1 ms. During the 50-client endurance run (sampled every ~30 s): `/api/live` median 97.9 ms (max 390), `/api/history/passes` median 64.6 ms (max 318) |
| Large history | 20 000 pieces, 200 000 passes, 1 000 000 events (90 days) | start-up query: window function **2 994 ms** (old) → LATERAL **17 ms** (new); with one cell without any events 11–41 ms (137 ms cold); history page (one day, 50 rows + total) 18 ms; offset 100 000 over all days 572 ms; 31-day export query 68 889 rows in 2.3 s |
| Exports | | CSV 100 000 rows 6.2 s (8.0 MB); PDF 5 000 rows **9.25 s → 5.38 s** after A-03 (median of 3 runs, min 5.24, max 8.16; 157 pages both) |
| Event loop during a 5 000-row PDF | probe task every 10 ms | on the loop (old) **9 790 ms** stall; in a worker thread (now) max 43 ms, p99 38 ms |
| Tracking correctness under load | 30 simulated minutes | normal traffic (10 pieces): 0 anomalies, 0 lost, tracker count ≤ simulator + 1, 87 robot transfers (longest 93 s). 4× overload (40 pieces): count still ≤ simulator + 1; 23 pieces *lost* — all after robot transfers longer than `max_transit_s` (the simulator's robot has unlimited capacity and held pieces up to 536 s), i.e. the configured behaviour |
| Frontend bundle | `npm run build` | JS 212 kB (68.8 kB gzip), CSS 28 kB (5.3 kB gzip). Render time not measured; clock re-renders limited (U-20, tested) |
| Reconnect | | back-off 1 s doubling to 30 s, ±20 % jitter, reset only after a session lasted 30 s (tested) |

Not measured: multi-hour/multi-day memory behaviour (the endurance run was 8.5 minutes), browser
memory over a long session, behaviour against the Siemens server.

## 13 · Before-and-after evaluation (internal assessment)

**Method.** Each category is scored 0–10: 10 = verified in the target environment with no known
issue; 8–9 = verified here, only minor known issues, target-environment proof missing; 6–7 = works
with significant gaps or unverified critical parts; 4–5 = major defects; ≤ 3 = missing or broken.
Categories whose essential proof needs infrastructure that was not available are **capped**:
requirements compliance and reliability at 8 (behaviour on the real plant unproven), OPC UA
integration at 7 (the node map itself is unverified), Docker deployment and GitLab CI/CD at 6 (never
executed). Industrial safety is not capped: the read-only property is a property of the code and was
verified by the static guard and by the mock's counters; the missing point is the server-side
read-only account. "Before" is the state at the start of the audit, judged with the issues of §4.
Overall = Σ score × weight / 10.

| Evaluation category | Weight | Before | After | Evidence | Remaining issues |
|---|---|---|---|---|---|
| Requirements compliance | 12 | 7 | 8 | §3: all R1–R31 implemented; mock-verified | Plant-dependent rows need the lab |
| Software architecture | 6 | 7 | 8 | §9 | Single process by design |
| Backend quality | 8 | 6 | 8 | P0/P1 fixed, 197 backend tests | Full run on the last two small changes not completed (§11.5) |
| Frontend quality | 8 | 6 | 8 | U-01…U-29, A-13, 34/34 mutants | No usability test with operators |
| Database design | 5 | 6 | 8 | LATERAL, replay protection, migrations round trip, backup docs | No automatic retention (deliberately: nothing deletes production data) |
| OPC UA integration | 8 | 7 | 7 | strict values, untrusted nodes, timestamps, back-off | Node map `derived`, never checked on the real server |
| Industrial safety | 8 | 7 | 9 | §10: guard, 0 modifying requests everywhere, verify exit 3 | Server-side read-only account not yet set up |
| Security | 5 | 6 | 8 | §10 | No login (scope), dev-tool advisories needing majors |
| Testing quality | 8 | 5 | 8 | §11 | No Docker/real-plant tests; targeted mutation testing; JSON formatter untested (Q-14); final full run on the last changes incomplete (§11.5) |
| Performance | 4 | 5 | 8 | §12 | Long-duration memory not measured |
| Reliability | 8 | 4 | 8 | T-01…T-04, B-01…B-07 | Real outages not reproduced on the plant |
| Docker deployment | 5 | 5 | 6 | static review, A-01, A-11, graceful stop | Never built or run (cap) |
| GitLab CI/CD | 4 | 5 | 6 | schema-valid, all commands run locally | Pipeline never run (cap) |
| Documentation | 6 | 6 | 8 | §4.5, this report | — |
| Maintainability | 5 | 6 | 8 | formatting, constraints, settings instead of constants | Toolchain majors pending (A-08) |
| **Overall /100** | **100** | **59.7** | **78.2** | | |

Before: (7·12 + 7·6 + 6·8 + 6·8 + 6·5 + 7·8 + 7·8 + 6·5 + 5·8 + 5·4 + 4·8 + 5·5 + 5·4 + 6·6 + 6·5) / 10 = 59.7.
After: (8·12 + 8·6 + 8·8 + 8·8 + 8·5 + 7·8 + 9·8 + 8·5 + 8·8 + 8·4 + 8·8 + 6·5 + 6·4 + 8·6 + 8·5) / 10 = 78.2.
The remaining gap to 100 is mostly proof that this environment cannot provide (plant, Docker, GitLab).

## 14 · Outstanding blockers

| Blocker | Consequence | What unblocks it |
|---|---|---|
| No access to the real factory (and it must not be connected without the lab) | Node map, wiring (incl. 14→15), timings, sampling, offline behaviour unverified | docs/08 with lab staff |
| No Docker on the audit machine | Images, Compose stack, health checks, restart persistence, nginx config never executed | §16 on a machine with Docker |
| nginx binary not available (download not allowed) | `nginx -t` not run; A-01 and the CSP are static | `docker compose build frontend` runs the config in the real image |
| No GitLab project (the code is on GitHub, which does not run `.gitlab-ci.yml`) | Pipeline never run | Push to a GitLab project (§17) |
| Python 3.12 not installed locally | Tests ran on 3.13.2; image and CI use 3.12 (constraints resolved for Linux/py3.12) | First CI run |
| Final full test runs on the last two small changes not completed (machine out of memory) | Backend suite stopped at 177/198 (0 failures); frontend suite, build and e2e not re-run after A-13 and the 422 wording | Re-run once (§11.5) or first CI run |
| Read-only OPC UA user must be created by the lab's PLC administrator | Server-side enforcement missing | docs/08 §8.1 |

## 15 · Real factory validation checklist

The complete procedure with expected observations, acceptance criteria and failure handling is
[docs/08-lab-validation.md](08-lab-validation.md); this is the summary to tick off.

- [ ] 8.1 Read-only OPC UA user for the dashboard; `python -m dmf.cli.verify` exit **0** (3 = may write)
- [ ] Step 1 Server reachable, namespace `http://demo-plantV2` present
- [ ] Step 2 `verify`: 0 missing, 0 different browse names, 0 out of range → set `verification: verified`
- [ ] Step 3 `ZelleN` = position N; cell numbers; every connection; **14→15 is a robot/manual transfer**; state/lamp values; sampling ≤ 200 ms
- [ ] Step 4 30-minute recording; 10 hand-noted pieces match; 0 `lost`; keep one recording as regression data
- [ ] Step 5 Tune `HANDOVER_WINDOW_S`, `STUCK_AFTER_S`, `max_transit_s`
- [ ] Step 6 (optional) Offline detection via timestamps
- [ ] Step 7 Acceptance 7.1–7.10 incl. network unplug, backend restart, database stop, exports, PLC programs unchanged

## 16 · Docker validation checklist (not executed here)

```bash
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose -f docker-compose.yml -f docker-compose.demo.yml build
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d
docker compose ps                                     # db, backend, frontend, mock: healthy
curl -s localhost:8080/healthz                        # nginx
curl -s localhost:8080/api/ready                      # {"ready":true,...,"opcua":"connected"}
curl -sI localhost:8080/ | grep -i content-security    # headers present
curl -sI localhost:8080/docs                          # 301/302 to /api/docs
```

- [ ] Both images build; `docker image ls` sizes noted
- [ ] All services healthy; backend runs as uid 10001, frontend as nginx-unprivileged (`docker compose exec backend id`)
- [ ] Database initialised by the migrations (`/api/status → recovery`)
- [ ] Browser on `http://<host>:8080`: map fills, header "Live" (WebSocket through nginx — checks A-01)
- [ ] e2e against the stack: start the mock with `--control-port`, `E2E_BASE_URL=http://<host>:8080 npx playwright test`
- [ ] `docker compose restart backend`: numbering continues, History keeps its rows
- [ ] `docker compose stop backend` finishes before 30 s; log shows the final snapshot and flush
- [ ] `docker compose down` (without `-v`) and `up`: data still there (volume `pgdata`)
- [ ] Logs are JSON lines; no secrets in logs
- [ ] `docker compose down` to clean up (add `-v` only for a throw-away test database)

## 17 · GitLab validation checklist

Status: **pipeline configuration statically reviewed** (validated against the GitLab CI schema),
**every command run locally** (lint, format check, pytest with PostgreSQL and coverage, Alembic round
trip, Vitest with the same coverage flags, build, Playwright, pip-audit, npm audit),
**no pipeline has run on GitLab**.

- [x] Repository committed and pushed to GitHub (`main`, 2026-11-27)
- [ ] Push it to a GitLab project as well (the project description asks for GitLab; GitHub does not run `.gitlab-ci.yml`)
- [ ] Pipeline on a feature branch: `lint`, `test`, `e2e` green; `build:images` builds but does not push
- [ ] A deliberately failing test makes `backend:test` fail and the later stages do not run
- [ ] Coverage values appear in the merge request (regexes checked locally: frontend `Lines : 95.29%`)
- [ ] JUnit and Cobertura reports, Playwright report on failure, kept as artifacts
- [ ] `deps:audit` shows the known advisories (A-08…A-10) as a warning, not a failure
- [ ] Default branch / tag: images pushed to the GitLab registry with `CI_REGISTRY_USER`/`CI_JOB_TOKEN`; no other secret needed
- [ ] DinD runner with TLS (`DOCKER_HOST=tcp://docker:2376`) works on the institute's runners

## 18 · Final project readiness verdict

**Academic Submission Readiness: CONDITIONALLY READY.**
All functional requirements are implemented and verified against the mock; in the last complete
runs 197 backend tests, 79 frontend tests and 5 end-to-end scenarios passed (two small later changes
were only partly re-tested, §11.5); the documentation explains
installation, demonstration, tracking, database, tests, deployment, troubleshooting and what is
unverified. The conditions are the two deliverables of the project description that could not be
executed here: run the Docker Compose demo once (§16) and push to GitLab with a green pipeline (§17),
which also re-runs every test suite on the final code (§11.5).
Until then, a reviewer must be told that Docker and CI are statically reviewed only.

**Real Factory Deployment Readiness: CONDITIONAL GO.**
GO for connecting the dashboard to the plant **for the supervised, read-only validation of
docs/08** — it cannot change the plant (no write path, verified by the guard and by zero modifying
requests in every run), and the validation itself is the deployment acceptance test. **Not** for
unattended operational use until docs/08 steps 1–5 and 7 have passed: the node map is derived, not
verified; the 14→15 robot link, the timings and the sampling are assumptions; and the Docker
deployment has never been executed.
