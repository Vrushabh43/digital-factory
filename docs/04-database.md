# 4 · Database

PostgreSQL 16, accessed with SQLAlchemy Core (async, asyncpg). The schema is defined in
`backend/dmf/persistence/schema.py`, and the migrations live in `database/migrations/`
(Alembic). All timestamps are `timestamptz` and stored in UTC; the API returns epoch seconds and
the UI shows them in `TIMEZONE` (default Europe/Berlin).

## 4.1 Tables

```mermaid
erDiagram
  pieces ||--o{ cell_passes : "has"
  pieces {
    int id PK
    string code UK "P-0151"
    int seq UK "151"
    timestamptz first_seen_at
    smallint first_position
    smallint first_cell
    string origin "put_on | source:<name> | robot:<name> | startup | found"
    string status "active | removed | stored | lost"
    timestamptz ended_at
    smallint last_position
    timestamptz last_seen_at
    int cells_passed
    bool uncertain
  }
  cell_passes {
    bigint id PK
    int piece_id FK
    smallint position
    smallint cell_number
    timestamptz entered_at
    timestamptz left_at "NULL = still inside"
    string result "inside | passed | to_robot | removed | stored | lost"
    bool was_stuck
    timestamptz stuck_since
    smallint came_from_position
    smallint came_from_cell
    smallint went_to_position
    string via "robot / source name"
    smallint first_conveyor
    smallint last_conveyor
    bool uncertain
  }
  events {
    bigint id PK
    timestamptz ts
    string kind
    smallint position
    smallint cell_number
    string piece_code
    text message "plain-language text shown in the UI"
    jsonb details
  }
  cell_status_history {
    bigint id PK
    smallint position
    smallint cell_number
    string status "running | stuck | error | offline"
    string reason
    timestamptz since
    timestamptz until "NULL = current"
  }
  tracker_snapshot {
    int id PK "always 1"
    timestamptz saved_at
    jsonb data "tracker state for restart recovery"
  }
  writer_progress {
    int id PK "always 1"
    string epoch "one value per backend run"
    bigint last_seq "last committed batch"
    timestamptz updated_at
  }
```

| Table | Purpose | Important constraints / indexes |
|---|---|---|
| `pieces` | One row per numbered piece | `code` and `seq` unique; numbering continues after restarts from `max(seq)+1` |
| `cell_passes` | One row per piece per visit to a cell (the History table) | **`uq_cell_passes_open_piece`**: at most one open pass per piece (`WHERE left_at IS NULL`), so a replayed event cannot double-book; indexes on `entered_at` and `(position, entered_at)` |
| `events` | Every domain event with its plain-language message (latest events, diagnostics) | indexes on `ts`, `(position, ts)`, `piece_code` |
| `cell_status_history` | Status intervals per cell | at most one open interval per position |
| `tracker_snapshot` | Tracker state, written every `SNAPSHOT_INTERVAL_S` (10 s), whenever a piece appears or ends, and at shutdown | single row |
| `writer_progress` | Sequence number of the last committed write batch (migration `0002`) | single row; written in the same transaction as the batch |

`events.kind` values match `backend/dmf/tracker/events.py`: `piece_appeared`, `cell_entered`,
`conveyor_changed`, `exit_reached`, `cell_left`, `piece_ended`, `stuck_changed`,
`cell_status`, `plant_flag`, `connection`, `anomaly`.

`pieces.cells_passed` counts the cells a piece has **left** (passed on, handed to a robot, stored or
taken off there); the cell in which a piece was *lost* is not counted. The live tracker uses the same rule.

## 4.2 Writing

`EventWriter` (`backend/dmf/persistence/writer.py`) turns tracker events into rows:

* events are queued in memory and written in transactions of up to 500;
* while the database is unreachable, events stay queued (up to `DB_BUFFER_MAX`, default 200 000;
  beyond that the oldest are dropped and counted) and the writer retries with back-off up to
  `DB_RETRY_MAX_S`. The live dashboard keeps working;
* if a batch fails because of **bad data** (PostgreSQL SQLSTATE class 22 "data exception" or 23
  "integrity constraint violation", or an error while building the rows), it is retried row by row
  and only the offending row is skipped and logged. Every other error (lost connection,
  "database is starting up", disk full, read-only transaction, statement or pool timeout, missing
  table) is **retried**, never skipped;
* each batch also stores its last sequence number in `writer_progress` in the same transaction. If
  the COMMIT succeeded but its acknowledgement was lost, the next attempt sees this and drops the
  already-written events instead of applying them a second time (which would have duplicated events
  and closed the wrong cell pass);
* inserts are idempotent (`ON CONFLICT DO NOTHING` on the unique keys above), and a `cell_left`
  only closes the open pass in **that** cell;
* when the in-memory queue is full, the oldest events that are not part of the transaction in
  progress are dropped (and counted in `dropped`); of several pending snapshots only the newest is kept;
* the tracker snapshot goes through the same ordered queue, so a snapshot never refers to events
  that are not yet written.

Writer health (`healthy`, `pending`, `dropped`, `skipped`) is reported in `/api/status` and shown as
a banner in the UI.

## 4.3 Restart recovery

On start-up (`backend/dmf/persistence/recovery.py`):

1. the tracker snapshot is restored (an unreadable snapshot is logged and ignored);
2. it is aligned with the database: active pieces missing from the snapshot (created after it) are
   taken over from their open cell pass — or into the robot queue if they were handed to a robot —
   and marked *uncertain* (an `anomaly` event records this); only a piece that cannot be placed is
   closed as *lost*. Pieces that already ended are dropped, and pieces whose open pass is in a
   different cell are moved;
3. numbering continues at `max(snapshot, max(seq) + 1)`;
4. after connecting, the live OPC UA values are reconciled with this state. Pieces moved across the
   gap are marked *uncertain*, and unexplained new pieces get origin `found`.

## 4.4 Migrations

Migrations run automatically on backend start (`DB_RUN_MIGRATIONS=true`). To run them by hand:

```bash
cd database
DATABASE_URL=postgresql+asyncpg://dmf:...@localhost:5432/dmf alembic upgrade head
DATABASE_URL=... alembic check        # CI: models and migrations must not drift
DATABASE_URL=... alembic revision --autogenerate -m "describe change"   # after editing schema.py
```

## 4.5 Size and retention

The plant produces roughly 20–40 pieces in parallel and a few thousand cell passes per day, so the
data stays in the megabytes per month and no automatic deletion is built in. If retention is
wanted, delete old rows with SQL. Events are not referenced by other tables:

```sql
DELETE FROM events WHERE ts < now() - interval '180 days';
DELETE FROM pieces WHERE status <> 'active' AND ended_at < now() - interval '365 days';  -- cascades to cell_passes
```

Nothing in the application deletes production data; retention is a deliberate manual step.

Backup and restore (run on the Docker host; keep backups off the machine):

```bash
docker compose exec -T db pg_dump -U dmf -Fc dmf > dmf-$(date +%F).dump        # backup (custom format)
docker compose stop backend                                                    # restore: stop writers first
docker compose exec -T db pg_restore -U dmf -d dmf --clean --if-exists < dmf-2026-11-26.dump
docker compose start backend
```

A daily backup can be scheduled with cron on the host (the restore has not been rehearsed on the
lab server yet — do this once before relying on it).
