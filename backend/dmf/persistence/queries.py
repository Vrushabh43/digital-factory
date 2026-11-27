"""Read queries for the REST API and exports."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import SmallInteger, and_, column, func, select, true, values
from sqlalchemy.ext.asyncio import AsyncConnection

from dmf.persistence.schema import cell_passes, events, pieces

PASS_COLUMNS = [
    cell_passes.c.id,
    pieces.c.code.label("piece"),
    cell_passes.c.position,
    cell_passes.c.cell_number,
    cell_passes.c.entered_at,
    cell_passes.c.left_at,
    cell_passes.c.result,
    cell_passes.c.was_stuck,
    cell_passes.c.stuck_since,
    cell_passes.c.came_from_position,
    cell_passes.c.came_from_cell,
    cell_passes.c.went_to_position,
    cell_passes.c.via,
    cell_passes.c.first_conveyor,
    cell_passes.c.last_conveyor,
    cell_passes.c.uncertain,
]


def day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time.min, zone)
    return start, start + timedelta(days=1)


async def history_passes(
    conn: AsyncConnection,
    *,
    start: datetime | None,
    end: datetime | None,
    position: int | None,
    piece: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    conds = []
    if start is not None:
        conds.append(cell_passes.c.entered_at >= start)
    if end is not None:
        conds.append(cell_passes.c.entered_at < end)
    if position is not None:
        conds.append(cell_passes.c.position == position)
    if piece:
        conds.append(pieces.c.code == piece)
    where = and_(*conds) if conds else True
    base = cell_passes.join(pieces, pieces.c.id == cell_passes.c.piece_id)
    total = (await conn.execute(select(func.count()).select_from(base).where(where))).scalar_one()
    rows = await conn.execute(
        select(*PASS_COLUMNS)
        .select_from(base)
        .where(where)
        .order_by(cell_passes.c.entered_at.desc(), cell_passes.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [dict(r._mapping) for r in rows], total


async def passes_between(
    conn: AsyncConnection, start: datetime, end: datetime, position: int | None = None, piece: str | None = None
) -> list[dict[str, Any]]:
    rows, _ = await history_passes(conn, start=start, end=end, position=position, piece=piece, limit=1_000_000, offset=0)
    rows.reverse()  # oldest first for exports
    return rows


async def journey(conn: AsyncConnection, code: str) -> dict[str, Any] | None:
    p = (await conn.execute(select(pieces).where(pieces.c.code == code))).mappings().first()
    if p is None:
        return None
    rows = await conn.execute(
        select(*PASS_COLUMNS)
        .select_from(cell_passes.join(pieces, pieces.c.id == cell_passes.c.piece_id))
        .where(cell_passes.c.piece_id == p["id"])
        .order_by(cell_passes.c.entered_at, cell_passes.c.id)
    )
    return {"piece": dict(p), "passes": [dict(r._mapping) for r in rows]}


async def cell_events(conn: AsyncConnection, position: int, limit: int) -> list[dict[str, Any]]:
    rows = await conn.execute(
        select(events.c.ts, events.c.kind, events.c.piece_code, events.c.message)
        .where(and_(events.c.position == position, events.c.kind != "cell_status"))
        .order_by(events.c.ts.desc(), events.c.id.desc())
        .limit(limit)
    )
    return [dict(r._mapping) for r in rows]


async def recent_events_all_cells(conn: AsyncConnection, per_cell: int, positions: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Newest ``per_cell`` events of each position, newest first.

    One short index scan per position (LATERAL ... LIMIT on ix_events_position_ts) instead
    of a window function over the whole table, so start-up stays fast with millions of rows."""
    if not positions:
        return {}
    pos = values(column("p", SmallInteger), name="pos").data([(p,) for p in positions])
    latest = (
        select(events.c.id, events.c.ts, events.c.kind, events.c.piece_code, events.c.message)
        .where(and_(events.c.position == pos.c.p, events.c.kind != "cell_status"))
        .order_by(events.c.ts.desc(), events.c.id.desc())
        .limit(per_cell)
        .lateral("latest")
    )
    rows = await conn.execute(
        select(pos.c.p, latest).select_from(pos.join(latest, true())).order_by(pos.c.p, latest.c.ts.desc(), latest.c.id.desc())
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        m = dict(r._mapping)
        m.pop("id")
        out.setdefault(m.pop("p"), []).append(m)
    return out


async def pieces_seen_on(conn: AsyncConnection, start: datetime, end: datetime) -> int:
    return (
        await conn.execute(
            select(func.count()).select_from(pieces).where(and_(pieces.c.first_seen_at >= start, pieces.c.first_seen_at < end))
        )
    ).scalar_one()
