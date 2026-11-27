"""Asynchronous, ordered, batched writer: tracker events -> PostgreSQL.

* Never blocks the tracker: ``submit`` only appends to an in-memory buffer.
* Writes in order, one transaction per batch.
* **Nothing is thrown away because the database is unavailable.** Every error is
  retried with back-off (and the writer reports itself unhealthy) unless PostgreSQL says
  the *data* is wrong (SQLSTATE class 22 "data exception" or 23 "integrity violation"),
  or converting the event to rows failed in Python. Only then is the batch split and the
  offending item skipped (logged and counted), so it cannot block everything behind it.
  "Database starting up", disk full, read-only, timeouts, pool exhaustion, lost
  connections and unknown errors are all *retried*.
* **Replay-safe.** Every item gets a sequence number; each transaction records the last
  sequence number it contains in ``writer_progress`` (per process "epoch"). If a commit
  succeeded but its reply was lost, the retry sees that and skips the committed items
  instead of applying them twice.
* Bounded memory: up to ``buffer_max`` items. When full, the oldest item that is not
  currently being written is dropped and counted. Only the newest pending tracker
  snapshot is kept (an older one is useless once a newer one is queued).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from dmf.persistence.db import to_dt
from dmf.persistence.schema import cell_passes, cell_status_history, events, pieces, tracker_snapshot, writer_progress
from dmf.plant.model import Plant
from dmf.tracker import events as ev

log = logging.getLogger(__name__)

BATCH_MAX = 500
PIECE_ID_CACHE = 10_000  # code -> id cache; misses fall back to a (indexed) lookup


@dataclass
class _Snapshot:
    data: dict[str, Any]
    saved_at: float


Item = ev.Event | _Snapshot


@dataclass
class _Entry:
    seq: int
    item: Item


@dataclass
class WriterStatus:
    healthy: bool = True
    last_error: str | None = None
    last_error_at: float | None = None
    last_write_at: float | None = None
    written: int = 0
    dropped: int = 0
    skipped: int = 0
    pending: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _sqlstate(exc: BaseException) -> str | None:
    e: BaseException | None = getattr(exc, "orig", None)
    for _ in range(5):
        if e is None:
            return None
        state = getattr(e, "sqlstate", None)
        if isinstance(state, str) and state:
            return state
        e = e.__cause__
    return None


def is_data_error(exc: BaseException) -> bool:
    """True if retrying cannot help because the item itself is bad; False = retry later."""
    if isinstance(exc, DBAPIError):
        if exc.connection_invalidated:
            return False
        state = _sqlstate(exc)
        if state is not None:
            return state[:2] in ("22", "23")
        orig = exc.orig
        return isinstance(orig, (ValueError, TypeError)) or isinstance(getattr(orig, "__cause__", None), (ValueError, TypeError))
    if isinstance(exc, (OSError, TimeoutError, SQLAlchemyError)):
        return False  # network, timeouts, pool exhaustion, closed connections, ...
    return True  # a Python error while turning this item into rows


@dataclass
class EventWriter:
    engine: AsyncEngine
    plant: Plant
    buffer_max: int = 200_000
    retry_max_s: float = 30.0
    status: WriterStatus = field(default_factory=WriterStatus)

    def __post_init__(self) -> None:
        self._buf: deque[_Entry] = deque()
        self._seq = 0
        self._epoch = str(uuid.uuid4())
        self._inflight = 0  # items at the head of the buffer that a transaction is writing
        self._snapshots = 0  # snapshots in the buffer
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._piece_ids: OrderedDict[str, int] = OrderedDict()

    # ------------------------------------------------------------------ producer side
    def submit(self, items: list[ev.Event]) -> None:
        for e in items:
            self._push(e)

    def submit_snapshot(self, data: dict[str, Any], saved_at: float) -> None:
        if self._snapshots:
            self._drop_pending_snapshots()
        self._push(_Snapshot(data, saved_at))

    def _drop_pending_snapshots(self) -> None:
        keep: deque[_Entry] = deque()
        for i, entry in enumerate(self._buf):
            if i >= self._inflight and isinstance(entry.item, _Snapshot):
                self._snapshots -= 1
                continue
            keep.append(entry)
        self._buf = keep

    def _push(self, item: Item) -> None:
        if len(self._buf) >= self.buffer_max:
            if len(self._buf) > self._inflight:
                victim = self._buf[self._inflight]
                del self._buf[self._inflight]
                if isinstance(victim.item, _Snapshot):
                    self._snapshots -= 1
            else:  # everything buffered is being written right now: drop the newcomer
                item = None  # type: ignore[assignment]
            self.status.dropped += 1
            if self.status.dropped in (1, 100, 10_000) or self.status.dropped % 100_000 == 0:
                log.error("database buffer full: %d events dropped so far (history gap)", self.status.dropped)
            if item is None:
                return
        self._seq += 1
        self._buf.append(_Entry(self._seq, item))
        if isinstance(item, _Snapshot):
            self._snapshots += 1
        self.status.pending = len(self._buf)
        self._idle.clear()
        self._wake.set()

    async def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything submitted so far is written (False on timeout)."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # ------------------------------------------------------------------ consumer side
    async def run(self) -> None:
        delay = 1.0
        check_progress = False
        while True:
            if not self._buf:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            batch: list[_Entry] = []
            try:
                if check_progress:
                    await self._drop_committed()
                    check_progress = False
                    if not self._buf:
                        continue
                batch = [self._buf[i] for i in range(min(BATCH_MAX, len(self._buf)))]
                self._inflight = len(batch)
                await self._write_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._inflight = 0
                if batch and is_data_error(exc):
                    log.exception("batch of %d items failed because of its data, writing them one by one", len(batch))
                    await self._write_one_by_one(len(batch))
                    delay = 1.0
                    continue
                self._mark_unhealthy(exc)
                check_progress = True  # the commit may have succeeded before the error
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max_s)
                continue
            self._inflight = 0
            self._done(len(batch), written=True)
            delay = 1.0

    def _done(self, n: int, *, written: bool) -> None:
        for _ in range(n):
            entry = self._buf.popleft()
            if isinstance(entry.item, _Snapshot):
                self._snapshots -= 1
        self.status.pending = len(self._buf)
        if written:
            self.status.written += n
            self.status.last_write_at = time.time()
            if not self.status.healthy:
                log.info("database writes are working again")
            self.status.healthy = True

    def _mark_unhealthy(self, exc: BaseException) -> None:
        if self.status.healthy:
            log.error("database unavailable or refusing writes, buffering events: %s", exc)
        self.status.healthy = False
        self.status.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        self.status.last_error_at = time.time()

    async def _drop_committed(self) -> None:
        """After an ambiguous failure: remove items that were committed after all."""
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(select(writer_progress.c.epoch, writer_progress.c.last_seq).where(writer_progress.c.id == 1))
            ).first()
        if row is None or row.epoch != self._epoch:
            return
        n = 0
        while n < len(self._buf) and self._buf[n].seq <= row.last_seq:
            n += 1
        if n:
            log.warning("%d items were already committed before the error; not writing them again", n)
            self._done(n, written=True)

    async def _write_one_by_one(self, n: int) -> None:
        for _ in range(n):
            if not self._buf:
                return
            entry = self._buf[0]
            delay = 1.0
            while True:
                self._inflight = 1
                try:
                    await self._write_batch([entry])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._inflight = 0
                    if is_data_error(exc):
                        log.error("skipping event that cannot be stored: %r (%s)", entry.item, exc)
                        self.status.skipped += 1
                        self._done(1, written=False)
                        break
                    self._mark_unhealthy(exc)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.retry_max_s)
                    try:
                        await self._drop_committed()
                    except Exception as check_exc:  # noqa: BLE001 - still unavailable; retry the write
                        log.debug("progress check failed: %s", check_exc)
                    if not self._buf or self._buf[0] is not entry:
                        break  # it was committed after all
                    continue
                self._inflight = 0
                self._done(1, written=True)
                break

    async def _write_batch(self, batch: list[_Entry]) -> None:
        new_ids: dict[str, int] = {}
        async with self.engine.begin() as conn:
            for entry in batch:
                if isinstance(entry.item, _Snapshot):
                    await self._write_snapshot(conn, entry.item)
                else:
                    await self._apply(conn, entry.item, new_ids)
            stmt = pg_insert(writer_progress).values(
                id=1, epoch=self._epoch, last_seq=batch[-1].seq, updated_at=to_dt(time.time())
            )
            ex = stmt.excluded
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[writer_progress.c.id],
                    set_={"epoch": ex.epoch, "last_seq": ex.last_seq, "updated_at": ex.updated_at},
                )
            )
        for code, pid in new_ids.items():  # only after commit
            self._remember(code, pid)

    def _remember(self, code: str, pid: int) -> None:
        self._piece_ids[code] = pid
        self._piece_ids.move_to_end(code)
        while len(self._piece_ids) > PIECE_ID_CACHE:
            self._piece_ids.popitem(last=False)

    # ------------------------------------------------------------------ event -> rows
    async def _piece_id(self, conn: AsyncConnection, code: str, new_ids: dict[str, int]) -> int | None:
        pid = new_ids.get(code) or self._piece_ids.get(code)
        if pid is None:
            pid = (await conn.execute(select(pieces.c.id).where(pieces.c.code == code))).scalar_one_or_none()
            if pid is not None:
                new_ids[code] = pid
        return pid

    async def _apply(self, conn: AsyncConnection, e: ev.Event, new_ids: dict[str, int]) -> None:
        ts = to_dt(e.ts)
        code = getattr(e, "code", None)
        pos = e.position
        cell = self.plant.cells[pos].cell if pos in self.plant.cells else None
        details = {k: v for k, v in e.to_dict().items() if k not in ("ts", "kind", "message", "pos", "code")}
        await conn.execute(
            events.insert().values(
                ts=ts, kind=e.kind, position=pos, cell_number=cell, piece_code=code, message=e.message, details=details
            )
        )
        if isinstance(e, ev.PieceAppeared):
            stmt = (
                pg_insert(pieces)
                .values(
                    code=e.code,
                    seq=int(e.code.split("-")[1]),
                    first_seen_at=ts,
                    first_position=e.pos,
                    first_cell=e.cell,
                    origin=e.origin,
                    status="active",
                    last_position=e.pos,
                    last_seen_at=ts,
                    uncertain=e.uncertain,
                )
                .on_conflict_do_nothing(index_elements=[pieces.c.code])
                .returning(pieces.c.id)
            )
            pid = (await conn.execute(stmt)).scalar_one_or_none()
            if pid is not None:
                new_ids[e.code] = pid
            return
        if isinstance(e, ev.CellStatusChanged):
            await conn.execute(
                update(cell_status_history)
                .where(and_(cell_status_history.c.position == e.pos, cell_status_history.c.until.is_(None)))
                .values(until=ts)
            )
            await conn.execute(
                cell_status_history.insert().values(
                    position=e.pos, cell_number=e.cell, status=e.status, reason=e.reason, since=ts
                )
            )
            return
        if code is None:
            return
        pid = await self._piece_id(conn, code, new_ids)
        if pid is None:
            log.warning("event for unknown piece %s ignored: %s", code, e.kind)
            return
        open_pass = and_(cell_passes.c.piece_id == pid, cell_passes.c.left_at.is_(None))
        if isinstance(e, ev.CellEntered):
            await conn.execute(
                pg_insert(cell_passes)
                .values(
                    piece_id=pid,
                    position=e.pos,
                    cell_number=e.cell,
                    entered_at=ts,
                    came_from_position=e.came_from_pos,
                    came_from_cell=e.came_from_cell,
                    via=e.via,
                    first_conveyor=e.conveyor,
                    last_conveyor=e.conveyor,
                    uncertain=e.uncertain,
                )
                .on_conflict_do_nothing(index_elements=[cell_passes.c.piece_id], index_where=cell_passes.c.left_at.is_(None))
            )
            values: dict[str, Any] = {"last_position": e.pos, "last_seen_at": ts}
            if e.uncertain:
                values["uncertain"] = True
            await conn.execute(update(pieces).where(pieces.c.id == pid).values(**values))
        elif isinstance(e, ev.ConveyorChanged):
            await conn.execute(update(cell_passes).where(open_pass).values(last_conveyor=e.to_conveyor))
            await conn.execute(update(pieces).where(pieces.c.id == pid).values(last_seen_at=ts))
        elif isinstance(e, ev.CellLeft):
            values = {"left_at": ts, "result": e.result, "went_to_position": e.to_pos}
            if e.uncertain:
                values["uncertain"] = True
            # only the pass in THIS cell (guards against closing a later pass by mistake)
            await conn.execute(update(cell_passes).where(and_(open_pass, cell_passes.c.position == e.pos)).values(**values))
            passed = pieces.c.cells_passed + (0 if e.result == "lost" else 1)  # same rule as the tracker
            await conn.execute(update(pieces).where(pieces.c.id == pid).values(cells_passed=passed, last_seen_at=ts))
        elif isinstance(e, ev.StuckChanged):
            if e.stuck:
                await conn.execute(update(cell_passes).where(open_pass).values(was_stuck=True, stuck_since=to_dt(e.since)))
        elif isinstance(e, ev.PieceEnded):
            await conn.execute(update(cell_passes).where(open_pass).values(left_at=ts, result=e.status))
            await conn.execute(update(pieces).where(pieces.c.id == pid).values(status=e.status, ended_at=ts, last_seen_at=ts))
        elif isinstance(e, ev.ExitReached):
            await conn.execute(update(pieces).where(pieces.c.id == pid).values(last_seen_at=ts))

    async def _write_snapshot(self, conn: AsyncConnection, s: _Snapshot) -> None:
        stmt = pg_insert(tracker_snapshot).values(id=1, saved_at=to_dt(s.saved_at), data=s.data)
        stmt = stmt.on_conflict_do_update(
            index_elements=[tracker_snapshot.c.id], set_={"saved_at": stmt.excluded.saved_at, "data": stmt.excluded.data}
        )
        await conn.execute(stmt)
