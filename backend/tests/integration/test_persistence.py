"""Event writer, queries and start-up recovery against a real PostgreSQL database."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from dmf.persistence import queries
from dmf.persistence.db import make_engine
from dmf.persistence.recovery import recover
from dmf.persistence.schema import cell_passes, cell_status_history, events, pieces
from dmf.persistence.writer import EventWriter
from dmf.plant.model import k_state
from dmf.testing.sequences import put_on, route, take_off, travel
from dmf.tracker.engine import TrackerEngine
from tests.conftest import Driver, idle_values

pytestmark = [pytest.mark.db, pytest.mark.timeout(60)]


async def start_writer(db, plant) -> tuple[EventWriter, asyncio.Task]:
    w = EventWriter(db, plant)
    return w, asyncio.create_task(w.run())


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_passes_journey_and_status_history(db, plant):
    w, task = await start_writer(db, plant)
    try:
        d = Driver(plant)
        w.submit(d.sync())
        path = route(plant, (1, 1), cells=2)
        w.submit(d.run(travel(plant, path)))
        w.submit(d.run(take_off(path[-1])))
        w.submit(d.tick(6))
        assert await w.flush()
        async with db.connect() as conn:
            rows, total = await queries.history_passes(conn, start=None, end=None, position=None, piece=None, limit=50, offset=0)
            assert total == 3
            assert [(r["piece"], r["position"], r["result"]) for r in rows] == [
                ("P-0001", 3, "removed"),
                ("P-0001", 2, "passed"),
                ("P-0001", 1, "passed"),
            ]
            assert rows[1]["came_from_position"] == 1 and rows[1]["came_from_cell"] == 1
            assert rows[1]["first_conveyor"] == 5 and rows[1]["last_conveyor"] == 4
            assert all(r["left_at"] >= r["entered_at"] for r in rows)
            j = await queries.journey(conn, "P-0001")
            assert j["piece"]["status"] == "removed" and j["piece"]["cells_passed"] == 3
            assert j["piece"]["origin"] == "source:High-bay storage"
            assert [p["position"] for p in j["passes"]] == [1, 2, 3]
            assert await queries.journey(conn, "P-9999") is None
            # 18 cells -> one open status row each
            open_rows = (
                await conn.execute(
                    select(func.count()).select_from(cell_status_history).where(cell_status_history.c.until.is_(None))
                )
            ).scalar_one()
            assert open_rows == 18
            ev3 = await queries.cell_events(conn, 3, 10)
            assert ev3[0]["message"] == "P-0001 was taken off the line"
            recent = await queries.recent_events_all_cells(conn, 2, list(plant.cells))
            assert len(recent[1]) == 2 and set(recent) == {1, 2, 3}
    finally:
        await stop(task)


async def test_filters_pagination_and_day_bounds(db, plant):
    w, task = await start_writer(db, plant)
    try:
        d = Driver(plant)
        w.submit(d.sync())
        for _ in range(3):
            w.submit(d.run(put_on(plant, (4, 1))))
            w.submit(d.run(take_off((4, 1))))
            w.submit(d.tick(6))
        w.submit(d.run(put_on(plant, (6, 1))))
        assert await w.flush()
        async with db.connect() as conn:
            rows, total = await queries.history_passes(conn, start=None, end=None, position=4, piece=None, limit=2, offset=0)
            assert total == 3 and [r["piece"] for r in rows] == ["P-0003", "P-0002"]
            rows, _ = await queries.history_passes(conn, start=None, end=None, position=4, piece=None, limit=2, offset=2)
            assert [r["piece"] for r in rows] == ["P-0001"]
            rows, total = await queries.history_passes(
                conn, start=None, end=None, position=None, piece="P-0004", limit=10, offset=0
            )
            assert total == 1 and rows[0]["result"] == "inside" and rows[0]["left_at"] is None
            day = datetime.fromtimestamp(d.t, UTC).astimezone(ZoneInfo("Europe/Berlin")).date()
            start, end = queries.day_bounds(day, "Europe/Berlin")
            assert start.utcoffset() is not None and (end - start).total_seconds() in (82800, 86400, 90000)
    finally:
        await stop(task)


async def test_buffering_while_database_is_down(db, plant):
    from tests.integration.conftest import TEST_DB

    bad = make_engine("postgresql+asyncpg://dmf@127.0.0.1:1/none")
    good = make_engine(TEST_DB)
    w = EventWriter(bad, plant, retry_max_s=0.2)
    task = asyncio.create_task(w.run())
    submitted: list = []

    def submit(out: list) -> None:
        submitted.extend(out)
        w.submit(out)

    try:
        d = Driver(plant)
        submit(d.sync())
        submit(d.run(put_on(plant, (4, 1))))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if not w.status.healthy:
                break
        assert not w.status.healthy and w.status.pending > 0 and w.status.last_error
        # the plant keeps running while the database is down
        submit(d.run(take_off((4, 1))))
        submit(d.tick(6))
        await asyncio.sleep(0.5)  # a few more failed write attempts
        assert not w.status.healthy and w.status.written == 0
        w.engine = good  # database is back
        assert await w.flush(15)
        assert w.status.healthy and w.status.pending == 0
        async with db.connect() as conn:
            assert (await conn.execute(select(func.count()).select_from(pieces))).scalar_one() == 1
            stored = (
                await conn.execute(
                    select(events.c.ts, events.c.kind, events.c.piece_code, events.c.position).order_by(events.c.id)
                )
            ).all()
            passes = (await conn.execute(select(cell_passes.c.position, cell_passes.c.result))).all()
            piece = (await conn.execute(select(pieces.c.code, pieces.c.status))).one()
        # nothing lost, nothing duplicated, nothing reordered
        assert len(stored) == len(submitted) > 20
        assert [(r.kind, r.piece_code, r.position) for r in stored] == [
            (e.kind, getattr(e, "code", None), e.position) for e in submitted
        ]
        assert all(abs(r.ts.timestamp() - e.ts) < 1e-3 for r, e in zip(stored, submitted, strict=True))
        assert [tuple(p) for p in passes] == [(4, "removed")] and tuple(piece) == ("P-0001", "removed")
    finally:
        await stop(task)
        await bad.dispose()
        await good.dispose()


async def test_lost_commit_reply_is_not_applied_twice(db, plant):
    """The COMMIT succeeds but the client sees an error (reply lost): the retry must not
    re-apply the batch (duplicate events, a CellLeft closing the wrong pass, ...)."""
    d = Driver(plant)
    w = EventWriter(db, plant)
    real = w._write_batch
    calls = {"n": 0}

    async def commit_then_fail(batch):
        await real(batch)
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("connection lost after COMMIT")

    w._write_batch = commit_then_fail  # type: ignore[method-assign]
    task = asyncio.create_task(w.run())
    try:
        submitted = d.sync() + d.run(travel(plant, route(plant, (1, 1), cells=2)))
        w.submit(submitted)
        assert await w.flush(20)
        assert w.status.pending == 0 and w.status.skipped == 0 and w.status.written == len(submitted)
        async with db.connect() as conn:
            n_events = (await conn.execute(select(func.count()).select_from(events))).scalar_one()
            rows = (await conn.execute(select(cell_passes.c.position, cell_passes.c.entered_at, cell_passes.c.left_at))).all()
            cp = (await conn.execute(select(pieces.c.cells_passed))).scalar_one()
        assert calls["n"] == 1  # the committed batch was recognised, not written again
        assert sorted(r.position for r in rows) == [1, 2, 3]
        assert all(r.left_at is None or r.left_at >= r.entered_at for r in rows)
        assert cp == 2
        assert n_events == len(submitted)  # one events row per tracker event, no duplicates
    finally:
        await stop(task)


async def test_database_refusing_writes_is_retried_not_skipped(db, plant):
    """'database is starting up' (57P03), disk full, read-only, ... are NOT bad data."""
    from sqlalchemy.exc import OperationalError

    class Refused(Exception):
        sqlstate = "57P03"

    d = Driver(plant)
    w = EventWriter(db, plant, retry_max_s=0.2)
    real = w._write_batch
    left = {"n": 3}

    async def refuse_then_work(batch):
        if left["n"] > 0:
            left["n"] -= 1
            raise OperationalError("INSERT ...", {}, Refused("the database system is starting up"))
        await real(batch)

    w._write_batch = refuse_then_work  # type: ignore[method-assign]
    task = asyncio.create_task(w.run())
    try:
        w.submit(d.sync() + d.run(put_on(plant, (4, 1))))
        await asyncio.sleep(0.3)
        assert not w.status.healthy and "starting up" in (w.status.last_error or "")
        assert await w.flush(20)
        assert w.status.healthy and w.status.skipped == 0 and w.status.dropped == 0
        async with db.connect() as conn:
            assert (await conn.execute(select(func.count()).select_from(pieces))).scalar_one() == 1
    finally:
        await stop(task)


async def test_bad_item_is_skipped_not_blocking(db, plant):
    from dmf.tracker import events as ev

    w, task = await start_writer(db, plant)
    try:
        d = Driver(plant)
        good = d.sync()
        bad = ev.Anomaly(d.t, None, "x" * 10, code="P-TOO-LONG-CODE-123")  # piece_code exceeds varchar(16)
        w.submit(good + [bad] + d.run(put_on(plant, (4, 1))))
        assert await w.flush(15)
        assert w.status.skipped == 1
        async with db.connect() as conn:
            assert (await conn.execute(select(func.count()).select_from(pieces))).scalar_one() == 1
    finally:
        await stop(task)


async def test_restart_recovery_aligns_snapshot_with_database(db, plant):
    w, task = await start_writer(db, plant)
    try:
        d = Driver(plant)
        w.submit(d.sync())
        w.submit(d.run(put_on(plant, (4, 1))))  # P-0001
        w.submit(d.run(put_on(plant, (12, 3))))  # P-0002
        w.submit(d.run(put_on(plant, (6, 2))))  # P-0003
        w.submit_snapshot(d.engine.export_state(), d.t)
        # after the snapshot (then the backend crashes):
        w.submit(
            d.run(
                [
                    (0.1, k_state(4, 2), 1),
                    (0.1, k_state(4, 1), 4),
                    (1.0, k_state(4, 2), 2),
                    (0.1, k_state(4, 1), 0),
                    (1.0, k_state(4, 2), 3),
                ]
            )
        )  # P-0001 -> conveyor 2
        w.submit(d.run(take_off((12, 3))) + d.tick(6))  # P-0002 removed
        w.submit(d.run(put_on(plant, (8, 1))))  # P-0004 appears
        assert await w.flush()
    finally:
        await stop(task)

    fresh = TrackerEngine(plant, handover_window_s=5)
    rep = await recover(db, fresh, d.t + 60)
    assert rep.snapshot_found and rep.next_seq == 5
    assert rep.relocated == ["P-0001"] and rep.dropped == ["P-0002"]
    # P-0004 was created after the snapshot: it is taken over from its open cell pass
    # (it keeps its number) instead of being marked lost and renumbered
    assert rep.adopted == ["P-0004"] and rep.lost == []
    assert fresh.convs[(4, 2)].piece == "P-0001" and fresh.convs[(4, 1)].piece is None
    assert fresh.convs[(8, 1)].piece == "P-0004"
    assert set(fresh.pieces) == {"P-0001", "P-0003", "P-0004"}
    p4 = fresh.pieces["P-0004"]
    assert p4.uncertain and p4.origin == "put_on" and p4.first_pos == 8
    out = fresh.on_resync(idle_values(plant, {k_state(4, 2): 3, k_state(6, 2): 3, k_state(8, 1): 3}), d.t + 60)
    assert [e for e in out if e.kind in ("piece_appeared", "piece_ended")] == []
    assert fresh.next_seq == 5
    async with db.connect() as conn:
        rows = (await conn.execute(select(pieces.c.code, pieces.c.status, pieces.c.uncertain))).all()
        assert {r.code: (r.status, r.uncertain) for r in rows} == {
            "P-0001": ("active", False),
            "P-0002": ("removed", False),
            "P-0003": ("active", False),
            "P-0004": ("active", True),
        }
        notes = (await conn.execute(select(events.c.kind, events.c.message).where(events.c.piece_code == "P-0004"))).all()
        assert [n.kind for n in notes].count("anomaly") == 1
        assert not any(n.kind == "piece_ended" for n in notes)


async def _insert_active_piece(conn, code: str, seq: int, last_position: int) -> int:
    now = datetime.now(UTC)
    return (
        await conn.execute(
            pieces.insert()
            .values(
                code=code,
                seq=seq,
                first_seen_at=now,
                first_position=last_position,
                first_cell=1,
                origin="put_on",
                status="active",
                last_position=last_position,
                last_seen_at=now,
            )
            .returning(pieces.c.id)
        )
    ).scalar_one()


async def test_recovery_hands_a_piece_with_the_robot_back_to_the_robot_queue(db, plant):
    # active, no open cell pass, last seen at the robot pick-up (position 11) = with the robot
    async with db.begin() as conn:
        await _insert_active_piece(conn, "P-0007", 7, 11)
    fresh = TrackerEngine(plant)
    rep = await recover(db, fresh, datetime.now(UTC).timestamp())
    assert rep.adopted == ["P-0007"] and rep.lost == [] and rep.next_seq == 8
    link = next(lk for lk in plant.robot_links if lk.src[0] == 11)
    assert [c for c, _ in fresh.robot_queues[link.id]] == ["P-0007"]


async def test_recovery_marks_a_piece_lost_only_when_it_cannot_be_placed(db, plant):
    # active, no open pass and position 4 has no robot link: nothing to continue from
    async with db.begin() as conn:
        await _insert_active_piece(conn, "P-0003", 3, 4)
    fresh = TrackerEngine(plant)
    rep = await recover(db, fresh, datetime.now(UTC).timestamp())
    assert rep.lost == ["P-0003"] and rep.adopted == [] and fresh.pieces == {}
    async with db.connect() as conn:
        assert (await conn.execute(select(pieces.c.status))).scalar_one() == "lost"
