"""HTTP control API for the mock OPC UA server — TEST TOOLING ONLY (end-to-end tests).

Lets Playwright drive deterministic plant situations. It only changes the MOCK server's
own values; it has nothing to do with the real factory. Enabled with
``python -m dmf_mock --control-port 8765`` and never exposed by the production Compose
setup.

Endpoints (JSON bodies):

* ``GET  /health`` -> ``{"running", "client_write_attempts", "client_attempts": {service: n}}``
* ``POST /reset`` -- every value back to an idle, empty plant
* ``POST /set`` ``{"values": {key: value}}`` -- set signal values (keys as in the node map, e.g. ``pos4.fb1.state``)
* ``POST /travel`` ``{"start": [pos, conv], "cells": 1, "take_off": false}`` -- move a piece along the line
* ``POST /put_on`` / ``POST /take_off`` ``{"conveyor": [pos, conv]}``
* ``POST /quality`` ``{"position": 4, "good": false}`` -- all nodes of a position Bad quality
  (``good: true`` restores their last good values)
* ``POST /timestamps/freeze`` ``{"position": 4, "age_s": 120}`` -- SourceTimestamp of the position's
  nodes fixed at ``now - age_s`` (a server keeping the last value of a switched-off PLC)
* ``POST /timestamps/advance`` ``{"position": 4, "seconds": 30}`` -- move the frozen timestamp forward;
  ``"seconds": null`` refreshes it to now and stops freezing
* ``POST /monitoring/refuse`` ``{"keys": ["pos4.lamp"], "status": "BadTooManyMonitoredItems"}`` -- the
  server refuses these monitored items in subscriptions created afterwards (i.e. after the next
  (re)connect); ``"keys": []`` clears it
* ``POST /subscription/status`` ``{"status": "BadTimeout"}`` -- StatusChangeNotification on every
  client subscription
* ``POST /server/stop`` / ``POST /server/start`` -- stop / restart the OPC UA server (values are kept)

Unknown keys, positions or status names are answered with 422.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from dmf.plant.model import K_ALARM, K_ENABLE, Plant, k_lamp, k_port, k_state
from dmf.testing.sequences import Timing, put_on, route, take_off, travel
from dmf_mock.server import MockPlantServer, status_code

log = logging.getLogger(__name__)
FAST = Timing(take_in_s=0.3, travel_s=0.3, rotate_s=0.3, react_s=0.05)


def idle_values(plant: Plant) -> dict[str, Any]:
    values: dict[str, Any] = {K_ALARM: False, K_ENABLE: True}
    for pos, cell in plant.cells.items():
        values[k_lamp(pos)] = 3
        for port in cell.port_nodes:
            values[k_port(pos, port, "can_send")] = False
            values[k_port(pos, port, "can_receive")] = port in cell.input_ports
        for n in cell.conveyors:
            values[k_state(pos, n)] = 0
    return values


class SetBody(BaseModel):
    values: dict[str, int | bool]


class TravelBody(BaseModel):
    start: tuple[int, int]
    cells: int = 1
    take_off: bool = False


class ConvBody(BaseModel):
    conveyor: tuple[int, int]


class QualityBody(BaseModel):
    position: int
    good: bool


class FreezeBody(BaseModel):
    position: int
    age_s: float = 0.0


class AdvanceBody(BaseModel):
    position: int
    seconds: float | None = None


class RefuseBody(BaseModel):
    keys: list[str]
    status: str = "BadTooManyMonitoredItems"


class StatusBody(BaseModel):
    status: str = "BadTimeout"


def make_control_app(holder: dict[str, MockPlantServer], plant: Plant) -> FastAPI:
    app = FastAPI(title="DMF mock plant control (TEST ONLY)")
    lock = asyncio.Lock()

    def server() -> MockPlantServer:
        srv = holder.get("server")
        if srv is None:
            raise HTTPException(409, "mock OPC UA server is stopped")
        return srv

    def check_position(pos: int) -> None:
        if pos not in plant.cells:
            raise HTTPException(422, f"unknown position {pos}")

    def check_status(name: str) -> None:
        try:
            status_code(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    async def run(steps) -> None:
        for dt, key, value in steps:
            await asyncio.sleep(dt)
            await server().set(key, value)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        srv = holder.get("server")
        return {
            "running": srv is not None,
            "client_write_attempts": srv.client_write_attempts if srv else None,
            "client_attempts": dict(srv.client_attempts) if srv else None,
        }

    @app.post("/reset")
    async def reset() -> dict[str, str]:
        async with lock:
            vals = idle_values(plant)
            srv = server()
            for k, v in vals.items():
                await srv.set(k, v)
        return {"status": "idle"}

    @app.post("/set")
    async def set_values(body: SetBody) -> dict[str, int]:
        srv = server()
        unknown = [k for k in body.values if k not in srv.key_to_nodeid]
        if unknown:
            raise HTTPException(422, f"unknown keys: {unknown}")
        for k, v in body.values.items():
            await srv.set(k, v)
        return {"set": len(body.values)}

    @app.post("/travel")
    async def do_travel(body: TravelBody) -> dict[str, Any]:
        path = route(plant, tuple(body.start), cells=body.cells)
        async with lock:
            await run(travel(plant, path, FAST))
            if body.take_off:
                await run(take_off(path[-1], FAST))
        return {"path": path}

    @app.post("/put_on")
    async def do_put_on(body: ConvBody) -> dict[str, str]:
        async with lock:
            await run(put_on(plant, tuple(body.conveyor), FAST))
        return {"status": "ok"}

    @app.post("/take_off")
    async def do_take_off(body: ConvBody) -> dict[str, str]:
        async with lock:
            await run(take_off(tuple(body.conveyor), FAST))
        return {"status": "ok"}

    @app.post("/quality")
    async def set_quality(body: QualityBody) -> dict[str, Any]:
        check_position(body.position)
        n = await server().set_position_quality(body.position, body.good)
        return {"position": body.position, "good": body.good, "nodes": n}

    @app.post("/timestamps/freeze")
    async def freeze_timestamps(body: FreezeBody) -> dict[str, Any]:
        check_position(body.position)
        ts = await server().freeze_timestamps(body.position, body.age_s)
        return {"position": body.position, "frozen": True, "source_timestamp": ts.timestamp()}

    @app.post("/timestamps/advance")
    async def advance_timestamps(body: AdvanceBody) -> dict[str, Any]:
        check_position(body.position)
        ts = await server().advance_timestamps(body.position, body.seconds)
        return {"position": body.position, "frozen": body.seconds is not None, "source_timestamp": ts.timestamp()}

    @app.post("/monitoring/refuse")
    async def refuse_monitoring(body: RefuseBody) -> dict[str, Any]:
        srv = server()
        check_status(body.status)
        unknown = [k for k in body.keys if k not in srv.key_to_nodeid]
        if unknown:
            raise HTTPException(422, f"unknown keys: {unknown}")
        srv.refuse_monitoring(body.keys, body.status)
        return {"refused": body.keys, "status": body.status}

    @app.post("/subscription/status")
    async def subscription_status(body: StatusBody) -> dict[str, Any]:
        check_status(body.status)
        n = await server().send_subscription_status(body.status)
        return {"status": body.status, "subscriptions": n}

    @app.post("/server/stop")
    async def stop_server() -> dict[str, str]:
        srv = holder.pop("server", None)
        if srv is not None:
            holder["last_values"] = dict(srv.values)  # type: ignore[assignment]
            await srv.stop()
        return {"status": "stopped"}

    @app.post("/server/start")
    async def start_server() -> dict[str, str]:
        if holder.get("server") is None:
            srv = MockPlantServer(plant, holder["endpoint"])  # type: ignore[arg-type]
            await srv.start()
            for k, v in (holder.get("last_values") or {}).items():  # type: ignore[union-attr]
                await srv.set(k, v)
            holder["server"] = srv
        return {"status": "running"}

    return app
