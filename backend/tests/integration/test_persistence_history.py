"""Plant-day boundaries (Europe/Berlin, incl. the DST change days) and persisted stuck info.

The day tests compare UTC instants, so they do not depend on the time zone of the
machine running the tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from dmf.persistence import queries
from dmf.persistence.writer import EventWriter
from dmf.plant.model import k_state
from dmf.services import export
from dmf.testing.sequences import handover, put_on, take_off
from dmf.tracker import events as ev
from tests.conftest import Driver

BERLIN = "Europe/Berlin"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ------------------------------------------------------------------ day_bounds (no database)
@pytest.mark.parametrize(
    ("day", "start", "end", "hours"),
    [
        (date(2026, 1, 15), utc(2026, 1, 14, 23), utc(2026, 1, 15, 23), 24),  # winter, CET
        (date(2026, 7, 1), utc(2026, 6, 30, 22), utc(2026, 7, 1, 22), 24),  # summer, CEST
        (date(2026, 3, 29), utc(2026, 3, 28, 23), utc(2026, 3, 29, 22), 23),  # clocks go forward
        (date(2026, 10, 25), utc(2026, 10, 24, 22), utc(2026, 10, 25, 23), 25),  # clocks go back
    ],
)
def test_day_bounds_are_the_plant_day_as_utc_instants(day, start, end, hours):
    s, e = queries.day_bounds(day, BERLIN)
    assert s.astimezone(UTC) == start and e.astimezone(UTC) == end
    assert e.astimezone(UTC) - s.astimezone(UTC) == timedelta(hours=hours)
    # the next day starts where this one ends
    assert queries.day_bounds(day + timedelta(days=1), BERLIN)[0].astimezone(UTC) == end


def test_day_bounds_follow_the_configured_zone():
    s, e = queries.day_bounds(date(2026, 10, 25), "UTC")
    assert (s.astimezone(UTC), e.astimezone(UTC)) == (utc(2026, 10, 25), utc(2026, 10, 26))


# ------------------------------------------------------------------ history_passes with plant days
async def _passes_entered_at(db, plant, instants: list[datetime]) -> list[str]:
    """One piece per instant, put on conveyor (4, 1) so that it enters exactly then."""
    w = EventWriter(db, plant)
    task = asyncio.create_task(w.run())
    codes = []
    try:
        steps = list(put_on(plant, (4, 1)))
        probe = Driver(plant)  # when, within the steps, does the piece enter?
        probe.sync()
        t_probe = probe.t
        probe.run(steps)
        lead = probe.of(ev.CellEntered)[0].ts - t_probe
        d = Driver(plant)
        d.t = instants[0].timestamp() - lead - 60
        w.submit(d.sync())
        for at in instants:
            assert d.t < at.timestamp() - lead  # time only moves forward
            d.t = at.timestamp() - lead
            w.submit(d.run(steps))
            entered = d.of(ev.CellEntered)[-1]
            assert abs(entered.ts - at.timestamp()) < 1e-3, (entered, at)
            codes.append(entered.code)
            w.submit(d.run(take_off((4, 1))))
            w.submit(d.tick(6))
        assert await w.flush()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with db.connect() as conn:
        rows, _ = await queries.history_passes(conn, start=None, end=None, position=None, piece=None, limit=50, offset=0)
    # precondition: every pass is stored with exactly the intended instant (to the microsecond)
    assert {r["piece"]: r["entered_at"] for r in rows} == dict(zip(codes, instants, strict=True))
    return codes


async def _day(db, day: date, tz: str = BERLIN) -> list[str]:
    start, end = queries.day_bounds(day, tz)
    async with db.connect() as conn:
        rows, total = await queries.history_passes(conn, start=start, end=end, position=None, piece=None, limit=50, offset=0)
    assert total == len(rows)
    return sorted(r["piece"] for r in rows)


@pytest.mark.db
async def test_history_day_filter_around_the_october_clock_change(db, plant):
    a, b0, b, c, e0, e = await _passes_entered_at(
        db,
        plant,
        [
            utc(2026, 10, 24, 21, 59, 30),  # 23:59:30 CEST on 24 Oct
            utc(2026, 10, 24, 22, 0, 0),  # 00:00:00 CEST on 25 Oct: first instant of 25 Oct
            utc(2026, 10, 24, 22, 30),  # 00:30 CEST on 25 Oct (still 24 Oct in UTC)
            utc(2026, 10, 25, 22, 59, 30),  # 23:59:30 CET on 25 Oct
            utc(2026, 10, 25, 23, 0, 0),  # 00:00:00 CET on 26 Oct: no longer 25 Oct
            utc(2026, 10, 25, 23, 30),  # 00:30 CET on 26 Oct (still 25 Oct in UTC)
        ],
    )
    assert await _day(db, date(2026, 10, 24)) == [a]
    assert await _day(db, date(2026, 10, 25)) == [b0, b, c]
    assert await _day(db, date(2026, 10, 26)) == [e0, e]
    # the export range ends where the route computes it: start of the day after "to"
    start, _ = queries.day_bounds(date(2026, 10, 25), BERLIN)
    end = queries.day_bounds(date(2026, 10, 25), BERLIN)[0] + timedelta(days=1)
    async with db.connect() as conn:
        rows = await queries.passes_between(conn, start, end)
    assert [r["piece"] for r in rows] == [b0, b, c]  # oldest first


@pytest.mark.db
async def test_history_day_filter_around_the_march_clock_change(db, plant):
    a, b, c0, c = await _passes_entered_at(
        db,
        plant,
        [
            utc(2026, 3, 28, 23, 30),  # 00:30 CET on 29 Mar
            utc(2026, 3, 29, 21, 59, 30),  # 23:59:30 CEST on 29 Mar
            utc(2026, 3, 29, 22, 0, 0),  # 00:00:00 CEST on 30 Mar
            utc(2026, 3, 29, 22, 30),  # 00:30 CEST on 30 Mar
        ],
    )
    assert await _day(db, date(2026, 3, 28)) == []
    assert await _day(db, date(2026, 3, 29)) == [a, b]
    assert await _day(db, date(2026, 3, 30)) == [c0, c]


# ------------------------------------------------------------------ stuck info is stored with the pass
@pytest.mark.db
async def test_stuck_is_persisted_on_the_cell_pass_and_shown_in_the_export(db, plant):
    w = EventWriter(db, plant)
    task = asyncio.create_task(w.run())
    try:
        d = Driver(plant)  # stuck after 60 s
        d.t = 1_000_000.0
        w.submit(d.sync({k_state(1, 3): 3}))  # P-0001 waits at the exit of position 1 from the start
        t_wait = d.t
        w.submit(d.run(put_on(plant, (4, 1))))  # P-0002 never waits
        for _ in range(61):
            w.submit(d.tick(1.0))
        assert d.engine.pieces["P-0001"].stuck
        w.submit(d.run(handover(plant, (1, 3), (2, 5))))  # finally handed over to position 2
        w.submit(d.tick(1.0))
        assert not d.engine.pieces["P-0001"].stuck
        assert await w.flush()
        async with db.connect() as conn:
            rows, _ = await queries.history_passes(conn, start=None, end=None, position=None, piece=None, limit=50, offset=0)
        by = {(r["piece"], r["position"]): r for r in rows}
        assert set(by) == {("P-0001", 1), ("P-0001", 2), ("P-0002", 4)}
        stuck_pass = by[("P-0001", 1)]
        assert stuck_pass["result"] == "passed" and stuck_pass["was_stuck"] is True
        assert stuck_pass["stuck_since"] == datetime.fromtimestamp(t_wait, UTC)
        assert by[("P-0001", 2)]["was_stuck"] is False and by[("P-0001", 2)]["stuck_since"] is None
        assert by[("P-0002", 4)]["was_stuck"] is False and by[("P-0002", 4)]["stuck_since"] is None

        now = datetime.fromtimestamp(d.t, UTC)
        csv_rows = export.to_csv(sorted(rows, key=lambda r: r["id"]), BERLIN, now, stuck=set()).decode("utf-8-sig")
        labels = {(line.split(",")[0], line.split(",")[2]): line.split(",")[7] for line in csv_rows.split("\r\n")[1:] if line}
        assert labels == {
            ("P-0001", "1"): "Passed (was stuck)",
            ("P-0002", "4"): "Still inside",
            ("P-0001", "2"): "Still inside",
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
