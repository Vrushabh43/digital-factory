"""Start-up recovery: restore the tracker from the last snapshot and align it with the database.

The snapshot is written through the same ordered queue as the events, so the database
is always at least as new as the snapshot. After a crash, events written after the
last snapshot are therefore applied *from the database* to the restored tracker:

* piece active in the DB but unknown to the snapshot (created after it) -> taken over
  from its open cell pass and marked *uncertain*; only if that is impossible (its
  conveyor is taken, no open pass and no robot link) it is marked "lost",
* piece ended in the DB but still in the snapshot     -> dropped from the tracker,
* piece whose open cell pass is elsewhere than the snapshot says -> moved there.

Afterwards the first OPC UA snapshot reconciles the tracker with the real plant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from dmf.persistence.db import to_dt
from dmf.persistence.schema import cell_passes, events, pieces, tracker_snapshot
from dmf.tracker.engine import TrackerEngine

log = logging.getLogger(__name__)


@dataclass
class RecoveryReport:
    snapshot_found: bool = False
    snapshot_age_s: float | None = None
    restored_pieces: int = 0
    lost: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    snapshot_error: str | None = None
    dropped: list[str] = field(default_factory=list)
    relocated: list[str] = field(default_factory=list)
    next_seq: int = 1


async def recover(db: AsyncEngine, tracker: TrackerEngine, now: float) -> RecoveryReport:
    rep = RecoveryReport()
    async with db.begin() as conn:
        snap = (await conn.execute(select(tracker_snapshot).where(tracker_snapshot.c.id == 1))).mappings().first()
        if snap is not None:
            saved_at = snap["saved_at"].timestamp()
            rep.snapshot_found, rep.snapshot_age_s = True, round(now - saved_at, 1)
            try:
                tracker.restore_state(snap["data"], saved_at)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                # a damaged snapshot must not block start-up forever: continue without it;
                # the active pieces are then taken over from the database below
                log.error("tracker snapshot is unreadable (%r) - continuing without it", exc)
                rep.snapshot_error = repr(exc)[:200]
                tracker.reset_pieces()
                tracker.disconnected_at = saved_at
        max_seq = (await conn.execute(select(func.max(pieces.c.seq)))).scalar_one()
        tracker.next_seq = max(tracker.next_seq, (max_seq or 0) + 1)
        rep.next_seq = tracker.next_seq

        rows = (
            (
                await conn.execute(
                    select(
                        pieces.c.id,
                        pieces.c.code,
                        pieces.c.status,
                        pieces.c.first_seen_at,
                        pieces.c.first_position,
                        pieces.c.origin,
                        pieces.c.last_position,
                        pieces.c.cells_passed,
                        cell_passes.c.position,
                        cell_passes.c.last_conveyor,
                        cell_passes.c.entered_at,
                        cell_passes.c.came_from_position,
                    )
                    .select_from(
                        pieces.outerjoin(
                            cell_passes, and_(cell_passes.c.piece_id == pieces.c.id, cell_passes.c.left_at.is_(None))
                        )
                    )
                    .where(pieces.c.code.in_(list(tracker.pieces)) | (pieces.c.status == "active"))
                )
            )
            .mappings()
            .all()
        )
        db_by_code = {r["code"]: r for r in rows}
        ts = to_dt(now)

        for code in list(tracker.pieces):
            r = db_by_code.get(code)
            if r is None or r["status"] != "active":
                tracker.forget_piece(code)
                rep.dropped.append(code)
        for code, r in db_by_code.items():
            if r["status"] != "active":
                continue
            p = tracker.pieces.get(code)
            if p is None and tracker.adopt_piece(
                code,
                pos=r["position"] if r["position"] is not None else r["last_position"],
                conveyor=r["last_conveyor"] if r["position"] is not None else None,
                entered_at=(r["entered_at"] or r["first_seen_at"]).timestamp(),
                first_seen=r["first_seen_at"].timestamp(),
                first_pos=r["first_position"],
                origin=r["origin"],
                came_from=r["came_from_position"],
                cells_passed=r["cells_passed"],
                now=now,
            ):
                await conn.execute(update(pieces).where(pieces.c.id == r["id"]).values(uncertain=True))
                await conn.execute(
                    events.insert().values(
                        ts=ts,
                        kind="anomaly",
                        position=r["position"] if r["position"] is not None else r["last_position"],
                        piece_code=code,
                        message=f"{code} was restored from the history after the backend restarted (position re-checked)",
                        details={"reason": "restart"},
                    )
                )
                rep.adopted.append(code)
            elif p is None:
                await conn.execute(
                    update(cell_passes)
                    .where(and_(cell_passes.c.piece_id == r["id"], cell_passes.c.left_at.is_(None)))
                    .values(left_at=ts, result="lost")
                )
                await conn.execute(
                    update(pieces).where(pieces.c.id == r["id"]).values(status="lost", ended_at=ts, last_seen_at=ts)
                )
                await conn.execute(
                    events.insert().values(
                        ts=ts,
                        kind="piece_ended",
                        position=r["position"],
                        piece_code=code,
                        message=f"{code} could not be followed after the backend restarted",
                        details={"status": "lost", "reason": "restart"},
                    )
                )
                rep.lost.append(code)
            elif r["position"] is not None and r["last_conveyor"] is not None and p.conv != (r["position"], r["last_conveyor"]):
                if tracker.relocate_piece(code, (r["position"], r["last_conveyor"])):
                    rep.relocated.append(code)
    rep.restored_pieces = len(tracker.pieces)
    log.info(
        "recovery: snapshot=%s (age %ss), %d pieces restored (%d from the history), %d lost, %d dropped, %d relocated, "
        "next number P-%04d",
        rep.snapshot_found,
        rep.snapshot_age_s,
        rep.restored_pieces,
        len(rep.adopted),
        len(rep.lost),
        len(rep.dropped),
        len(rep.relocated),
        rep.next_seq,
    )
    return rep
