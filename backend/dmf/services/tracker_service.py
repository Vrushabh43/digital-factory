"""Runtime orchestration: OPC UA ingest -> tracker engine -> database writer + live state.

Everything runs in one asyncio event loop; the tracker engine is synchronous and only
called from this service, so there is no concurrent mutation. All engine input - OPC UA
changes, connection events and the 1 s ticks - goes through ONE ordered queue, each
message stamped when it was received, so a tick can never overtake a notification that
was received before it.

Every background task is supervised: an exception while handling one message is logged
and forces a resynchronisation; a task that ends unexpectedly puts the service into
phase ``failed`` (``/api/health`` and ``/api/ready`` then answer 503).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from dmf.opcua.ingest import Change, Lost, Message, OpcIngest, Snapshot, Stale, Tick
from dmf.persistence import queries
from dmf.persistence.db import make_engine, ping, run_migrations
from dmf.persistence.recovery import RecoveryReport, recover
from dmf.persistence.writer import EventWriter
from dmf.plant.model import Plant
from dmf.settings import Settings, redact_url
from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine

log = logging.getLogger(__name__)

EVENTS_PER_CELL = 8
STATE_NAMES = {0: "empty", 1: "taking in", 2: "moving", 3: "at exit", 4: "handing over", 5: "turning", 6: "turning back"}


class TrackerService:
    def __init__(
        self,
        plant: Plant,
        settings: Settings,
        *,
        db: AsyncEngine | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.plant = plant
        self.s = settings
        self.clock = clock
        self.db = db
        self.engine = TrackerEngine(
            plant,
            handover_window_s=settings.handover_window_s,
            stuck_after_s=settings.stuck_after_s,
            reconcile_max_hops=settings.reconcile_max_hops,
            # ticks normally arrive every tick_interval_s; a much longer gap = stalled loop
            max_tick_gap_s=max(3.0, 3 * settings.tick_interval_s),
        )
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=settings.event_queue_max)
        self.ingest = OpcIngest(plant, settings, self.queue, clock=clock)
        self.writer: EventWriter | None = None
        self.recovery: RecoveryReport | None = None
        self.phase = "starting"  # starting | waiting_for_database | running | stopping | failed
        self.failure: str | None = None
        self.handler_errors = 0  # messages whose handling raised (each forces a resync)
        self.started_at = clock()
        self.recent: dict[int, deque[dict[str, Any]]] = {p: deque(maxlen=EVENTS_PER_CELL) for p in plant.cells}
        self._listeners: list[Callable[[], None]] = []
        self._event_listeners: list[Callable[[list[ev.Event]], None]] = []
        self._tasks: list[asyncio.Task] = []
        self.version = 0  # increases with every change of the live state

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._spawn(self._startup(), "startup", critical=False)

    def _spawn(self, coro: Any, name: str, critical: bool = True) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(lambda t: self._task_done(t, critical))
        self._tasks.append(task)
        return task

    def _task_done(self, task: asyncio.Task, critical: bool) -> None:
        if task.cancelled() or self.phase == "stopping":
            return
        exc = task.exception()
        if exc is None and not critical:
            return  # e.g. startup finished
        self.phase = "failed"
        self.failure = f"background task {task.get_name()!r} " + (f"crashed: {exc!r}" if exc else "stopped unexpectedly")
        log.critical("%s - tracking has stopped; restart the backend", self.failure, exc_info=exc)
        self._changed()

    async def _startup(self) -> None:
        delay = 1.0
        while True:  # tracking only starts once history and numbering are recovered
            try:
                if self.db is None:
                    self.db = make_engine(self.s.database_url, self.s.db_pool_size)
                if self.s.db_run_migrations:
                    await run_migrations(self.s.database_url, self.s.alembic_ini_path)
                self.recovery = await recover(self.db, self.engine, self.clock())
                async with self.db.connect() as conn:
                    for pos, items in (
                        await queries.recent_events_all_cells(conn, EVENTS_PER_CELL, list(self.plant.cells))
                    ).items():
                        if pos in self.recent:
                            self.recent[pos].extend(reversed([_event_row(i) for i in items]))
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.phase = "waiting_for_database"
                # usually the database is not reachable yet; the exception type tells otherwise
                log.error(
                    "start-up not complete, database not ready? (%s: %s); retrying in %.0fs",
                    type(exc).__name__,
                    str(exc)[:200],
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.s.db_retry_max_s)
        self.writer = EventWriter(self.db, self.plant, buffer_max=self.s.db_buffer_max, retry_max_s=self.s.db_retry_max_s)
        self._writer_task = self._spawn(self.writer.run(), "db-writer")
        self._consume_task = self._spawn(self._consume(), "consume")
        self._loop_tasks = [self._spawn(self._ticker(), "ticker"), self._spawn(self._snapshotter(), "snapshots")]
        if self.s.opcua_enabled:
            self._ingest_task: asyncio.Task | None = self._spawn(self.ingest.run(), "opcua")
        else:
            self._ingest_task = None
            self.ingest.status.state = "disabled"
            log.warning("OPC UA ingest is disabled (OPCUA_ENABLED=false)")
        self.phase = "running"
        self._changed()

    async def stop(self) -> None:
        """Ordered shutdown: no new input -> handle what is queued -> final snapshot ->
        write everything -> stop. The snapshot is taken after the queue is drained, so it
        matches the events written before it."""
        was = self.phase
        self.phase = "stopping"
        await self.ingest.stop()
        ingest_task = getattr(self, "_ingest_task", None)
        if ingest_task is not None:  # let it disconnect and queue its last messages
            await asyncio.wait({ingest_task}, timeout=5)
        if was in ("running", "failed") and self.writer is not None:
            for t in getattr(self, "_loop_tasks", []):  # ticker and snapshotter
                t.cancel()
            await asyncio.gather(*getattr(self, "_loop_tasks", []), return_exceptions=True)
            await self._drain_queue(timeout=5)
            if self.engine.has_state:
                self.writer.submit_snapshot(self.engine.export_state(), self.clock())
            if not await self.writer.flush(timeout=10):
                log.warning("shutdown: %d events could not be written in time", self.writer.status.pending)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.db is not None:
            await self.db.dispose()

    async def _drain_queue(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        consumer = getattr(self, "_consume_task", None)
        while not self.queue.empty() and consumer is not None and not consumer.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if not self.queue.empty():
            log.warning("shutdown: %d queued OPC UA messages were not processed", self.queue.qsize())

    # ================================================================ loops
    async def _consume(self) -> None:
        while True:
            msg = await self.queue.get()
            try:
                self._apply(msg)
            except Exception:  # noqa: BLE001 - one bad message must not stop tracking
                self.handler_errors += 1
                log.exception("tracker failed on %s; forcing a resynchronisation", type(msg).__name__)
                self.ingest.request_resync("tracker error")

    def _apply(self, msg: Message) -> None:
        now = msg.ts  # time of receipt, not of processing
        if isinstance(msg, Tick):
            self._handle(self.engine.on_tick(now), force=True)
            return
        if isinstance(msg, Snapshot):
            out = self.engine.on_resync(msg.values, now, msg.reason)
        elif isinstance(msg, Change):
            out = self.engine.on_signal(msg.key, msg.value, msg.good, now)
        elif isinstance(msg, Lost):
            out = self.engine.on_connection_lost(now, msg.reason)
        elif isinstance(msg, Stale):
            out = self.engine.set_stale(msg.pos, msg.stale, now)
        else:  # pragma: no cover
            return
        self._handle(out)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.s.tick_interval_s)
            try:
                self.queue.put_nowait(Tick(self.clock()))
            except asyncio.QueueFull:
                pass  # the consumer is behind; the ingest forces a resync when the queue overflows

    async def _snapshotter(self) -> None:
        while True:
            await asyncio.sleep(self.s.snapshot_interval_s)
            self._snapshot()

    def _snapshot(self) -> None:
        if self.writer is not None and self.engine.connected:
            self.writer.submit_snapshot(self.engine.export_state(), self.clock())

    def _handle(self, out: list[ev.Event], force: bool = False) -> None:
        if out:
            if self.writer is not None:
                self.writer.submit(out)
                if any(isinstance(e, (ev.PieceAppeared, ev.PieceEnded)) for e in out):
                    self._snapshot()  # numbering/piece set changed: keep restart recovery exact
            for e in out:
                if e.position in self.recent and e.kind != "cell_status":
                    self.recent[e.position].append(
                        _event_row({"ts": e.ts, "kind": e.kind, "piece_code": getattr(e, "code", None), "message": e.message})
                    )
            for cb in self._event_listeners:
                cb(out)
        if out or force:
            self._changed()

    # ================================================================ listeners
    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def add_event_listener(self, cb: Callable[[list[ev.Event]], None]) -> None:
        self._event_listeners.append(cb)

    def _changed(self) -> None:
        self.version += 1
        for cb in list(self._listeners):
            cb()

    # ================================================================ read models
    async def db_ok(self) -> bool:
        return self.db is not None and await ping(self.db)

    def live_state(self) -> dict[str, Any]:
        eng, now = self.engine, self.clock()
        cells = []
        counts = {"running": 0, "stuck": 0, "error": 0, "offline": 0}
        pieces_by_pos: dict[int, list[str]] = {}
        for code, p in eng.pieces.items():
            if p.pos is not None:
                pieces_by_pos.setdefault(p.pos, []).append(code)
        for pos, cell in sorted(self.plant.cells.items()):
            rt = eng.cells[pos]
            st = eng.status_of(pos)
            counts[st.status] += 1
            stuck = next((p for p in eng.pieces.values() if p.pos == pos and p.stuck), None)
            convs = []
            for n, conv in sorted(cell.conveyors.items()):
                c = eng.convs[(pos, n)]
                convs.append(
                    {
                        "n": n,
                        "role": conv.role,
                        "turntable": conv.turntable,
                        "state": c.state,
                        "state_name": STATE_NAMES.get(c.state) if c.state is not None else None,
                        "piece": c.piece,
                        "sensors": c.sensors,
                    }
                )
            ports = {}
            for port in cell.port_nodes:
                ports[str(port)] = {
                    "direction": "in" if port in cell.input_ports else "out",
                    "can_send": rt.ports.get(f"{port}.can_send"),
                    "can_receive": rt.ports.get(f"{port}.can_receive"),
                }
            cells.append(
                {
                    "position": pos,
                    "cell": cell.cell,
                    "status": st.status,
                    "reason": st.reason,
                    "detail": st.detail,
                    "status_since": rt.status_since or None,
                    "lamp": rt.lamp,
                    "conveyors": convs,
                    "pieces": sorted(pieces_by_pos.get(pos, [])),
                    "ports": ports,
                    "ready_to_receive": _any_true(ports, "in", "can_receive"),
                    "ready_to_send": _any_true(ports, "out", "can_send"),
                    "stuck_piece": {"code": stuck.code, "waiting_since": stuck.stuck_since} if stuck else None,
                    "latest_events": list(reversed(self.recent[pos])),
                }
            )
        return {
            "ts": now,
            "version": self.version,
            "source": self.s.source_label,
            "phase": self.phase,
            "connection": {
                "state": self.ingest.status.state,
                "connected": eng.connected,
                "since": self.ingest.status.connected_since,
                "last_error": self.ingest.status.last_error,
                "next_retry_at": self.ingest.status.next_retry_at,
                "last_data_at": eng.last_update,
            },
            "database": {
                "healthy": self.writer.status.healthy if self.writer else False,
                "pending": self.writer.status.pending if self.writer else 0,
            },
            "plant": {"alarm": eng.alarm, "enabled": eng.enable},
            "counts": counts,
            "cells": cells,
            "pieces": self.pieces_now(now),
        }

    def pieces_now(self, now: float | None = None) -> list[dict[str, Any]]:
        eng = self.engine
        now = self.clock() if now is None else now
        out = []
        for code, p in sorted(eng.pieces.items()):
            cell = self.plant.cells.get(p.pos) if p.pos is not None else None
            if p.pos is None:
                status = "with_robot"
            elif p.stuck:
                status = "stuck"
            elif eng.status_of(p.pos).status in ("error", "offline"):
                status = "cell_" + eng.status_of(p.pos).status
            elif p.uncertain:
                status = "uncertain"
            else:
                status = "moving"
            came_cell = self.plant.cells[p.came_from].cell if p.came_from in self.plant.cells else None
            out.append(
                {
                    "code": code,
                    "position": p.pos,
                    "cell": cell.cell if cell else None,
                    "conveyor": p.conv[1] if p.conv else None,
                    "conveyor_role": cell.conveyors[p.conv[1]].role if cell and p.conv else None,
                    "entered_at": p.entered_at,
                    "first_seen": p.first_seen,
                    "first_position": p.first_pos,
                    "first_cell": self.plant.cells[p.first_pos].cell if p.first_pos in self.plant.cells else None,
                    "origin": p.origin,
                    "came_from_position": p.came_from,
                    "came_from_cell": came_cell,
                    "status": status,
                    "stuck_since": p.stuck_since if p.stuck else None,
                    "waiting_since": p.waiting_since,
                    "uncertain": p.uncertain,
                    "uncertain_reason": p.uncertain_reason,
                    "cells_passed": p.cells_passed,
                }
            )
        return out

    def status_report(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "failure": self.failure,
            "handler_errors": self.handler_errors,
            "started_at": self.started_at,
            "source": self.s.source_label,
            "opcua": self.ingest.status.to_dict(),
            "database": self.writer.status.to_dict() if self.writer else None,
            "recovery": asdict(self.recovery) if self.recovery else None,
            "node_map_verification": self.plant.node_verification,
            "tracker": {
                "pieces": len(self.engine.pieces),
                "next_number": f"P-{self.engine.next_seq:04d}",
                "robot_transit": {k: [c for c, _ in q] for k, q in self.engine.robot_queues.items()},
                "pending_handovers": len(self.engine.departures),
            },
            "config": {
                "handover_window_s": self.s.handover_window_s,
                "stuck_after_s": self.s.stuck_after_s,
                "offline_detection": self.s.offline_detection,
                "subscribe_sensors": self.s.opcua_subscribe_sensors,
                "endpoint": redact_url(self.s.opcua_endpoint),
                "timezone": self.s.timezone,
            },
        }


def _any_true(ports: dict[str, dict[str, Any]], direction: str, flag: str) -> bool | None:
    vals = [p[flag] for p in ports.values() if p["direction"] == direction]
    if any(v is None for v in vals):
        return None
    return any(vals)


def _event_row(item: dict[str, Any]) -> dict[str, Any]:
    ts = item["ts"]
    return {
        "ts": ts.timestamp() if hasattr(ts, "timestamp") else ts,
        "kind": item["kind"],
        "piece": item.get("piece_code"),
        "message": item["message"],
    }
