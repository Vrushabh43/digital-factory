"""Full backend (uvicorn + FastAPI + tracker + PostgreSQL) against the mock OPC UA server."""

from __future__ import annotations

import asyncio
import codecs
import csv
import io
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import uvicorn
import websockets

from dmf.api.app import create_app
from dmf.plant.model import k_lamp
from dmf.services import export
from dmf.testing.sequences import route, take_off, travel
from dmf_mock.server import MockPlantServer
from tests.integration.conftest import TEST_DB
from tests.integration.test_opcua_ingest import FAST, free_port, settings_for

pytestmark = [pytest.mark.db, pytest.mark.opcua, pytest.mark.timeout(90)]


@pytest.fixture
async def stack(db, plant):
    opc_port, http_port = free_port(), free_port()
    mock = MockPlantServer(plant, f"opc.tcp://127.0.0.1:{opc_port}/")
    await mock.start()
    settings = settings_for(
        opc_port,
        database_url=TEST_DB,
        handover_window_s=3,
        tick_interval_s=0.2,
        snapshot_interval_s=1,
        log_format="text",
        ws_max_rate_hz=10,
        source_label="demo",
    )
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=http_port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{http_port}", timeout=10) as client:
        for _ in range(200):
            await asyncio.sleep(0.05)
            try:
                r = await client.get("/api/ready")
                if r.status_code == 200 and (await client.get("/api/live")).json()["connection"]["connected"]:
                    break
            except httpx.TransportError:
                pass
        else:
            raise AssertionError("backend did not become ready")
        yield client, mock, http_port
    server.should_exit = True
    await asyncio.wait_for(task, 15)
    await mock.stop()


async def wait_for(client: httpx.AsyncClient, cond, timeout: float = 10.0):
    for _ in range(int(timeout / 0.1)):
        live = (await client.get("/api/live")).json()
        if cond(live):
            return live
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached")


async def wait_written(client: httpx.AsyncClient, timeout: float = 10.0) -> None:
    """Wait until the event writer has nothing pending (instead of a fixed sleep)."""
    for _ in range(int(timeout / 0.05)):
        db = (await client.get("/api/status")).json()["database"]
        if db["healthy"] and db["pending"] == 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("events not written in time")


async def test_health_status_plant_live(stack):
    client, mock, _ = stack
    assert (await client.get("/api/health")).json()["status"] == "ok"
    ready = (await client.get("/api/ready")).json()
    assert ready["ready"] and ready["database"] and ready["opcua"] == "connected"
    st = (await client.get("/api/status")).json()
    assert st["opcua"]["node_mismatches"] == [] and st["node_map_verification"] == "derived"
    assert st["recovery"]["snapshot_found"] is False
    plant = (await client.get("/api/plant")).json()
    assert len(plant["cells"]) == 18 and plant["timezone"] == "Europe/Berlin"
    assert plant["cells"][0]["layout"]["w"] > 0
    live = (await client.get("/api/live")).json()
    assert live["source"] == "demo" and live["counts"]["running"] == 18 and live["pieces"] == []
    await mock.set(k_lamp(7), 1)
    live = await wait_for(client, lambda lv: lv["counts"]["error"] == 1)
    assert next(c for c in live["cells"] if c["position"] == 7)["status"] == "error"
    assert mock.client_write_attempts == 0


async def test_piece_flow_history_journey_exports(stack, plant):
    client, mock, _ = stack
    path = route(plant, (1, 1), cells=2)
    for dt, key, value in travel(plant, path, FAST):
        await asyncio.sleep(dt)
        await mock.set(key, value)
    live = await wait_for(client, lambda lv: any(p["position"] == 3 for p in lv["pieces"]))
    (piece,) = live["pieces"]
    assert piece["code"] == "P-0001" and piece["cell"] == 12 and piece["came_from_cell"] == 11
    assert piece["origin"] == "source:High-bay storage" and piece["status"] == "moving"
    cell3 = next(c for c in live["cells"] if c["position"] == 3)
    assert cell3["pieces"] == ["P-0001"] and cell3["latest_events"][0]["piece"] == "P-0001"
    now = (await client.get("/api/pieces/now")).json()
    assert [p["code"] for p in now["items"]] == ["P-0001"]

    await wait_written(client)
    day = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    hist = (await client.get("/api/history/passes", params={"day": day})).json()
    assert hist["total"] == 3
    assert [(i["position"], i["result_label"]) for i in hist["items"]] == [(3, "Still inside"), (2, "Passed"), (1, "Passed")]
    hist = (await client.get("/api/history/passes", params={"position": 2})).json()
    assert hist["total"] == 1 and hist["items"][0]["came_from_cell"] == 1
    j = (await client.get("/api/pieces/P-0001/journey")).json()
    assert [p["position"] for p in j["passes"]] == [1, 2, 3] and j["now"]["position"] == 3
    assert j["piece"]["first_cell"] == 1 and isinstance(j["piece"]["first_seen_at"], float)
    evs = (await client.get("/api/cells/1/events")).json()["items"]
    assert evs and evs[0]["piece_code"] == "P-0001"

    # piece is taken off -> history shows it
    for dt, key, value in take_off(path[-1]):
        await asyncio.sleep(dt)
        await mock.set(key, value)
    await wait_for(client, lambda lv: lv["pieces"] == [], timeout=10)
    await wait_written(client)
    j = (await client.get("/api/pieces/P-0001/journey")).json()
    assert j["piece"]["status"] == "removed" and j["passes"][-1]["result_label"] == "Taken off the line"

    r = await client.get("/api/history/export", params={"from": day, "format": "csv"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][0] == "Piece" and len(rows) == 4
    assert rows[1][0] == "P-0001" and rows[1][1] == "1" and rows[3][7] == "Taken off the line"
    assert rows[0] == export.HEADER
    # piece, cell, position, result, came from CELL, went to POSITION, uncertain
    assert [(r[0], r[1], r[2], r[7], r[8], r[9], r[10]) for r in rows[1:]] == [
        ("P-0001", "1", "1", "Passed", "", "2", ""),
        ("P-0001", "11", "2", "Passed", "1", "3", ""),
        ("P-0001", "12", "3", "Taken off the line", "11", "", ""),
    ]
    berlin_now = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
    previous_left = None
    for r in rows[1:]:
        entered, left = (datetime.strptime(v, "%d.%m.%Y %H:%M:%S") for v in (r[3], r[4]))
        assert entered <= left and abs((berlin_now - entered).total_seconds()) < 120  # plant time, not UTC
        assert abs(int(r[6]) - (left - entered).total_seconds()) <= 1
        assert r[5] == f"{int(r[6]) // 60} min {int(r[6]) % 60:02d} s"
        assert previous_left is None or entered <= previous_left + timedelta(seconds=1)
        previous_left = left
    r = await client.get("/api/history/export", params={"from": day, "format": "pdf"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert mock.client_write_attempts == 0


async def test_validation_errors(stack):
    client, _, _ = stack
    assert (await client.get("/api/history/passes", params={"piece": "abc"})).status_code == 422
    assert (await client.get("/api/history/passes", params={"position": 99})).status_code == 404
    assert (await client.get("/api/cells/0/events")).status_code == 404
    assert (await client.get("/api/pieces/P-0042/journey")).status_code == 404
    r = await client.get("/api/history/export", params={"from": "2026-01-01", "to": "2026-03-01"})
    assert r.status_code == 422
    r = await client.get("/api/history/export", params={"from": "2026-01-02", "to": "2026-01-01"})
    assert r.status_code == 422
    assert (await client.get("/api/history/passes", params={"limit": 100000})).json()["limit"] == 200


async def test_export_range_limit_and_empty_range(stack):
    client, _, _ = stack
    assert (await client.get("/api/plant")).json()["export_max_days"] == 31
    r = await client.get("/api/history/export", params={"from": "2026-01-01", "to": "2026-01-31"})  # 31 days
    assert r.status_code == 200
    assert r.content == codecs.BOM_UTF8 + (",".join(export.HEADER) + "\r\n").encode("utf-8")  # nothing that month
    r = await client.get("/api/history/export", params={"from": "2026-01-01", "to": "2026-02-01"})  # 32 days
    assert r.status_code == 422
    r = await client.get("/api/history/export", params={"from": "2026-01-01", "to": "2026-01-31", "format": "pdf"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


async def test_websocket_pushes_changes(stack):
    client, mock, port = stack
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/live") as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert first["type"] == "live" and len(first["data"]["cells"]) == 18
        await mock.set(k_lamp(5), 1)
        for _ in range(50):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg["data"]["counts"]["error"] == 1:
                break
        else:
            raise AssertionError("no websocket update with the error")
        assert msg["data"]["version"] > first["data"]["version"]


async def test_backend_restart_keeps_pieces_and_numbering(db, plant):
    from contextlib import asynccontextmanager

    from dmf.testing.sequences import put_on

    opc_port, port = free_port(), free_port()  # the restarted backend listens on the same port
    mock = MockPlantServer(plant, f"opc.tcp://127.0.0.1:{opc_port}/")
    await mock.start()

    @asynccontextmanager
    async def backend():
        settings = settings_for(
            opc_port, database_url=TEST_DB, handover_window_s=3, tick_interval_s=0.2, snapshot_interval_s=60, log_format="text"
        )
        server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
        task = asyncio.create_task(server.serve())
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            await wait_ready(client)
            yield client
        server.should_exit = True
        await asyncio.wait_for(task, 15)

    async def wait_ready(client):
        for _ in range(200):
            await asyncio.sleep(0.05)
            try:
                if (await client.get("/api/ready")).status_code == 200 and (await client.get("/api/live")).json()["connection"][
                    "connected"
                ]:
                    return
            except httpx.TransportError:
                pass
        raise AssertionError("backend not ready")

    try:
        async with backend() as client:
            for dt, key, value in put_on(plant, (4, 2), FAST):
                await asyncio.sleep(dt)
                await mock.set(key, value)
            await wait_for(client, lambda lv: [p["code"] for p in lv["pieces"]] == ["P-0001"])
        # backend is down (graceful stop wrote a final snapshot); the plant keeps running
        async with backend() as client:
            st = (await client.get("/api/status")).json()
            assert st["recovery"]["snapshot_found"] and st["recovery"]["restored_pieces"] == 1
            live = await wait_for(client, lambda lv: lv["connection"]["connected"])
            assert [(p["code"], p["position"], p["conveyor"]) for p in live["pieces"]] == [("P-0001", 4, 2)]
            for dt, key, value in put_on(plant, (8, 1), FAST):
                await asyncio.sleep(dt)
                await mock.set(key, value)
            live = await wait_for(client, lambda lv: len(lv["pieces"]) == 2)
            assert sorted(p["code"] for p in live["pieces"]) == ["P-0001", "P-0002"]
    finally:
        await mock.stop()
    assert mock.client_write_attempts == 0
