# 5 · API reference

Base path `/api`, served by the backend (port 8000) and proxied by nginx (port 8080). Every
endpoint is **GET** (nginx rejects other methods at the edge). Timestamps are **epoch seconds**
(UTC) and days are ISO dates (`2026-11-20`) interpreted in `TIMEZONE`. OpenAPI/Swagger:
`/api/docs` (`/docs`, the path named in the project description, redirects there; the Swagger page
loads its scripts from cdn.jsdelivr.net, so it needs internet access in the browser —
`/api/openapi.json` works offline).

Errors use FastAPI's format `{"detail": "..."}`:

| Code | When |
|---|---|
| 404 | unknown cell position, unknown piece |
| 422 | invalid parameter (piece number not `P-` + ≥ 4 digits, `to` before `from`, export range > `EXPORT_MAX_DAYS`, a day outside 2000-01-01…2100-12-31, `offset` > 10 000 000) |
| 503 | database not ready yet, or not reachable at the moment (history, events, journey, export) — try again; never a 500 for a database outage |

## Health and diagnostics

| Endpoint | Response |
|---|---|
| `GET /api/health` | `{"status":"ok","version":"1.0.0"}`: liveness. **503** `{"status":"failed","reason":...}` when a background task (tracker, writer, OPC UA ingest, ticker) has died — the Docker health check then marks the container unhealthy |
| `GET /api/ready` | `{"ready":true,"phase":"running","database":true,"opcua":"connected","failure":null}`: 200 when tracking runs and the database is reachable, otherwise **503** (`phase` is `starting`, `waiting_for_database`, `running`, `stopping` or `failed`). `opcua` is `starting`, `connecting`, `connected`, `disconnected` or `disabled` and does not affect readiness, because the dashboard stays useful without the factory |
| `GET /api/status` | Diagnostics: `phase`, `failure`, `handler_errors`, `opcua` (state, namespace index, connected since, last error, reconnect attempts, next retry, nodes checked, `node_mismatches`, refused items, notifications), `database` (writer health, pending, dropped, skipped), `recovery` report (incl. pieces taken over from the history), `node_map_verification`, tracker counters (pieces, next number, robot transit, pending hand-overs), effective config. Credentials in the endpoint URL are shown as `***` |

## Plant and live state

| Endpoint | Response |
|---|---|
| `GET /api/plant` | Static model: `cells[]` (position, cell number, kind, conveyors with role, ports, `next` cells, map layout), sources, sinks, robot links, decorations, `timezone`, `stuck_after_s`, `export_max_days`, `node_map_verification` |
| `GET /api/live` | Current state; same payload as the WebSocket (below) |
| `GET /api/pieces/now` | `{"items":[LivePiece...], "ts":...}` |
| `GET /api/cells/{position}/events?limit=20` | Latest events of one cell (1–200), newest first: `{"position", "items": [{ts, kind, piece_code, message}]}` (in the live payload's `latest_events` the same field is called `piece`) |

`Live` (abridged):

```json
{
  "ts": 1795700000.0, "version": 7, "source": "live", "phase": "running",
  "connection": {"state": "connected", "connected": true, "since": 1795696000.0,
                 "last_error": null, "next_retry_at": null, "last_data_at": 1795700000.0},
  "database": {"healthy": true, "pending": 0},
  "plant": {"alarm": false, "enabled": true},
  "counts": {"running": 15, "stuck": 1, "error": 1, "offline": 1},
  "cells": [{
    "position": 10, "cell": 5, "status": "stuck", "reason": "piece_waiting_too_long",
    "detail": null, "status_since": 1795699748.0, "lamp": 2,
    "conveyors": [{"n": 1, "role": "entry", "turntable": false, "state": 0, "state_name": "empty", "piece": null}],
    "pieces": ["P-0151"], "ready_to_receive": false, "ready_to_send": true,
    "stuck_piece": {"code": "P-0151", "waiting_since": 1795699748.0},
    "latest_events": [{"ts": 1795699748.0, "kind": "exit_reached", "piece": "P-0151",
                       "message": "P-0151 reached the exit sensor"}]
  }],
  "pieces": [{
    "code": "P-0151", "position": 10, "cell": 5, "conveyor": 4, "conveyor_role": "exit",
    "entered_at": 1795699709.0, "first_seen": 1795698514.0, "first_position": 2, "first_cell": 11,
    "origin": "source:Side entry", "came_from_position": 9, "came_from_cell": 8,
    "status": "stuck", "stuck_since": 1795699748.0, "waiting_since": null,
    "uncertain": false, "uncertain_reason": null, "cells_passed": 15
  }]
}
```

* Cell `status`: `running | stuck | error | offline`. `reason` is one of `ok`, `partial_data`
  (running, but a conveyor of the cell has no valid state: pieces there may not be tracked),
  `piece_waiting_too_long`, `lamp_red`, `plant_not_enabled`, `server_unreachable`,
  `no_valid_data`, `stale_data`, `unknown_lamp_value`. `detail` for running is
  `waiting | idle | transporting`.
* Piece `status`: `moving | stuck | cell_error | cell_offline | uncertain | with_robot` (`with_robot`: between two cells on a robot, `position` is `null`).
* `ready_to_receive` / `ready_to_send` are `null` when the values are unknown.

## History

| Endpoint | Parameters | Response |
|---|---|---|
| `GET /api/history/passes` | `day` (ISO date), `position`, `piece`, `limit` (default 50, capped at `HISTORY_PAGE_MAX`), `offset` | `{"items":[Pass...], "total", "limit", "offset", "ts"}`, newest first |
| `GET /api/pieces/{code}/journey` | | `{"piece":{...}, "passes":[Pass...] (oldest first), "now": LivePiece or null, "pending": false, "ts"}`. A piece the tracker knows but the database has not received yet is answered from the tracker with `"pending": true` and no passes (instead of 404) |
| `GET /api/history/export` | `from` (required), `to` (default `from`), `format=csv\|pdf`, `position`, `piece` | File download `dmf-cell-passes-<from>[-to-<to>].csv\|pdf` |

`Pass`:

```json
{"id": 812, "piece": "P-0151", "position": 10, "cell_number": 5,
 "entered_at": 1795699709.0, "left_at": null, "result": "inside", "result_label": "Stuck at exit",
 "was_stuck": true, "stuck_now": true, "came_from_position": 9, "came_from_cell": 8,
 "went_to_position": null, "via": null, "uncertain": false}
```

`result_label` is the text shown in the UI: Passed, Still inside, Stuck at exit, Taken off the line,
Passed to robot, Went into storage, Lost track. Closed passes of a piece that was stuck get
"(was stuck)" appended.

CSV: UTF-8 with BOM (opens correctly in Excel), CRLF, columns `Piece, Cell, Position, Entered,
Left, Time in cell, Time in cell (s), Result, Came from cell, Went to position, Uncertain`. Times are
local (`TIMEZONE`) `DD.MM.YYYY HH:MM:SS`; "Came from cell" is a cell number, "Went to position" a
position. A cell that would start with `=`, `+`, `-` or `@` is prefixed with `'` (spreadsheet formula
injection; all values are generated by the backend, this is defence in depth). Durations are never negative.
PDF: A4 landscape with a summary and the table (at most 5 000 rows; the PDF says when it is cut).
Both are rendered in a worker thread, so a long export does not delay tracking.

## WebSocket `/ws/live`

* The server sends `{"type":"live","data": Live}` right after connecting, then whenever the state
  changes (coalesced to at most `WS_MAX_RATE_HZ` messages per second), and at least every 5 s as a
  heartbeat.
* Clients never send anything; incoming messages (text or binary) are ignored.
* A browser connection whose `Origin` is neither this host nor listed in `CORS_ORIGINS` is refused
  during the handshake (HTTP 403). Clients without an `Origin` header (tools) are accepted.
* At most `WS_MAX_CLIENTS` (default 50) concurrent clients; a further connection is accepted and
  immediately closed with code **1013** (try again later).
* The frontend reconnects with back-off (0.5 → 10 s), loads `/api/live` once at start-up so the page fills before the first message, and treats data older than 15 s as stale.
