"""REST endpoints. All times in responses are epoch seconds (UTC); the frontend formats them
in the configured timezone (default Europe/Berlin)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from dmf import __version__
from dmf.persistence import queries
from dmf.services import export
from dmf.services.tracker_service import TrackerService
from dmf.settings import Settings

router = APIRouter(prefix="/api")

PIECE_RE = re.compile(r"^P-\d{4,}$")
# Sensible calendar range for day filters; outside it the date arithmetic would overflow.
MIN_DAY, MAX_DAY = date(2000, 1, 1), date(2100, 12, 31)
MAX_OFFSET = 10_000_000


def _svc(request: Request) -> TrackerService:
    return request.app.state.service


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _epoch(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.timestamp() if isinstance(v, datetime) else v) for k, v in row.items()}


def _check_position(svc: TrackerService, position: int | None) -> None:
    if position is not None and position not in svc.plant.cells:
        raise HTTPException(404, f"unknown position {position}")


def _check_piece(code: str | None) -> str | None:
    if code is None or code == "":
        return None
    code = code.strip().upper()
    if not PIECE_RE.match(code):
        raise HTTPException(422, "piece number must look like P-0151")
    return code


def _require_db(svc: TrackerService) -> None:
    if svc.db is None or svc.phase in ("starting", "waiting_for_database"):
        raise HTTPException(503, "database not ready yet")


_FIELD = {"day": "Day", "from": "From", "to": "To"}  # the field names the operator sees in the UI


def _check_day(d: date | None, name: str) -> None:
    if d is not None and not (MIN_DAY <= d <= MAX_DAY):
        raise HTTPException(422, f"'{_FIELD.get(name, name)}' must be between {MIN_DAY:%d.%m.%Y} and {MAX_DAY:%d.%m.%Y}")


# ------------------------------------------------------------------ health & status
@router.get("/health", summary="Liveness: the process is running and no background task has died")
async def health(request: Request) -> JSONResponse:
    svc = _svc(request)
    if svc.phase == "failed":
        return JSONResponse({"status": "failed", "reason": svc.failure, "version": __version__}, status_code=503)
    return JSONResponse({"status": "ok", "version": __version__})


@router.get("/ready", summary="Readiness: tracking started and the database is reachable")
async def ready(request: Request) -> JSONResponse:
    svc = _svc(request)
    db_ok = await svc.db_ok()
    ok = svc.phase == "running" and db_ok
    body = {"ready": ok, "phase": svc.phase, "database": db_ok, "opcua": svc.ingest.status.state, "failure": svc.failure}
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/status", summary="Diagnostics: OPC UA connection, node map check, database writer, recovery")
async def status(request: Request) -> dict[str, Any]:
    return _svc(request).status_report() | {"version": __version__}


# ------------------------------------------------------------------ plant & live
@router.get("/plant", summary="Static plant model: cells, conveyors, connections and map layout")
async def plant(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    p = svc.plant
    cells = []
    for pos, c in sorted(p.cells.items()):
        cells.append(
            {
                "position": pos,
                "cell": c.cell,
                "type": c.type,
                "retrofit": c.retrofit,
                "kind": "cross" if c.is_cross else "linear",
                "input_ports": list(c.input_ports),
                "output_ports": list(c.output_ports),
                "conveyors": [{"n": n, "role": cv.role, "turntable": cv.turntable} for n, cv in sorted(c.conveyors.items())],
                "next": p.next_positions.get(pos, []),
                "layout": c.layout,
            }
        )
    return {
        "name": p.name,
        "timezone": _settings(request).timezone,
        "node_map_verification": p.node_verification,
        "stuck_after_s": svc.s.stuck_after_s,
        "export_max_days": svc.s.export_max_days,
        "cells": cells,
        "sources": [{"position": c[0], "conveyor": c[1], "name": n} for c, n in p.sources.items()],
        "sinks": [{"position": c[0], "conveyor": c[1], "name": n} for c, n in p.sinks.items()],
        "robot_links": [{"from": lk.src[0], "to": lk.dst[0], "name": lk.name} for lk in p.robot_links],
        "layout": p.layout,
    }


@router.get("/live", summary="Current state of every cell and every piece (same payload as the WebSocket)")
async def live(request: Request) -> dict[str, Any]:
    return _svc(request).live_state()


@router.get("/pieces/now", summary="Pieces currently in the factory")
async def pieces_now(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    return {"ts": svc.clock(), "items": svc.pieces_now()}


@router.get("/cells/{position}/events", summary="Latest events of one cell, newest first")
async def cell_events(request: Request, position: int, limit: Annotated[int, Query(ge=1, le=200)] = 20) -> dict[str, Any]:
    svc = _svc(request)
    _check_position(svc, position)
    _require_db(svc)
    async with svc.db.connect() as conn:
        rows = await queries.cell_events(conn, position, limit)
    return {"position": position, "items": [_epoch(r) for r in rows]}


# ------------------------------------------------------------------ history
@router.get("/history/passes", summary="Cell passes, newest first (filter by day, cell position, piece)")
async def history_passes(
    request: Request,
    day: date | None = None,
    position: int | None = None,
    piece: str | None = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> dict[str, Any]:
    svc, s = _svc(request), _settings(request)
    _check_position(svc, position)
    _check_day(day, "day")
    code = _check_piece(piece)
    _require_db(svc)
    limit = min(limit, s.history_page_max)
    start = end = None
    if day is not None:
        start, end = queries.day_bounds(day, s.timezone)
    async with svc.db.connect() as conn:
        rows, total = await queries.history_passes(
            conn, start=start, end=end, position=position, piece=code, limit=limit, offset=offset
        )
    stuck = {c for c, p in svc.engine.pieces.items() if p.stuck}
    items = []
    for r in rows:
        item = _epoch(r)
        item["stuck_now"] = r["left_at"] is None and r["piece"] in stuck
        item["result_label"] = export.result_label(r, item["stuck_now"])
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset, "ts": svc.clock()}


@router.get("/pieces/{code}/journey", summary="Every cell a piece has passed, in order")
async def journey(request: Request, code: str) -> dict[str, Any]:
    svc = _svc(request)
    code = _check_piece(code)
    _require_db(svc)
    async with svc.db.connect() as conn:
        j = await queries.journey(conn, code)
    live_piece = next((p for p in svc.pieces_now() if p["code"] == code), None)
    if j is None and live_piece is not None:
        # known to the tracker but not written yet (database slow or briefly unavailable)
        return {"piece": _live_piece_row(live_piece), "passes": [], "now": live_piece, "pending": True, "ts": svc.clock()}
    if j is None:
        raise HTTPException(404, f"piece {code} not found")
    stuck = live_piece is not None and live_piece["status"] == "stuck"
    passes = []
    for r in j["passes"]:
        item = _epoch(r)
        item["stuck_now"] = stuck and r["left_at"] is None
        item["result_label"] = export.result_label(r, item["stuck_now"])
        passes.append(item)
    piece = _epoch(j["piece"])
    piece.pop("id", None)
    return {"piece": piece, "passes": passes, "now": live_piece, "pending": False, "ts": svc.clock()}


def _live_piece_row(p: dict[str, Any]) -> dict[str, Any]:
    """The journey's ``piece`` object built from the live tracker (same fields as the database row)."""
    return {
        "code": p["code"],
        "seq": int(p["code"][2:]),
        "first_seen_at": p["first_seen"],
        "first_position": p["first_position"],
        "first_cell": p["first_cell"],
        "origin": p["origin"],
        "status": "active",
        "ended_at": None,
        "last_position": p["position"],
        "last_seen_at": p["entered_at"],
        "cells_passed": p["cells_passed"],
        "uncertain": p["uncertain"],
    }


@router.get("/history/export", summary="Download cell passes as CSV or PDF")
async def history_export(
    request: Request,
    from_: Annotated[date, Query(alias="from")],
    to: date | None = None,
    format: Literal["csv", "pdf"] = "csv",  # noqa: A002 - public query parameter name
    position: int | None = None,
    piece: str | None = None,
) -> Response:
    svc, s = _svc(request), _settings(request)
    _check_position(svc, position)
    _check_day(from_, "from")
    _check_day(to, "to")
    code = _check_piece(piece)
    _require_db(svc)
    to = to or from_
    if to < from_:
        raise HTTPException(422, "'To' must not be before 'From'")
    if (to - from_).days + 1 > s.export_max_days:
        raise HTTPException(422, f"at most {s.export_max_days} days per export")
    start, _ = queries.day_bounds(from_, s.timezone)
    end = queries.day_bounds(to, s.timezone)[0] + timedelta(days=1)
    async with svc.db.connect() as conn:
        rows = await queries.passes_between(conn, start, end, position, code)
    now = datetime.now(UTC)
    stuck = {c for c, p in svc.engine.pieces.items() if p.stuck}
    span = from_.strftime("%d.%m.%Y") + ("" if to == from_ else " – " + to.strftime("%d.%m.%Y"))
    name = f"dmf-cell-passes-{from_.isoformat()}" + ("" if to == from_ else f"-to-{to.isoformat()}")
    filters = []
    if position is not None:
        filters.append(f"cell {svc.plant.cells[position].cell} (position {position})")
    if code:
        filters.append(f"piece {code}")
    # rendering is CPU work (a long PDF takes seconds): run it in a worker thread so the
    # event loop keeps receiving OPC UA notifications and serving the dashboard meanwhile
    if format == "csv":
        body = await asyncio.to_thread(export.to_csv, rows, s.timezone, now, stuck)
        media = "text/csv; charset=utf-8"
    else:
        subtitle = span + (" · " + ", ".join(filters) if filters else " · all cells")
        title = "Digital Model Factory – cell passes"
        body = await asyncio.to_thread(export.to_pdf, rows, s.timezone, now, stuck, title, subtitle)
        media = "application/pdf"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}.{format}"'})
