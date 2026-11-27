"""Backend platform behaviour found missing in the project audit: database outages after
start-up, input bounds, WebSocket limits/Origin, supervision of background tasks, ordered
shutdown and journey look-ups while the database lags behind the tracker.

The OPC UA ingest is disabled here (no plant needed); messages are fed to the tracker
service directly where a test needs plant data.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
import websockets
from sqlalchemy import select

from dmf.api.app import create_app
from dmf.opcua.ingest import Change, Snapshot
from dmf.persistence.db import make_engine
from dmf.persistence.schema import pieces, tracker_snapshot
from dmf.plant.model import k_state
from dmf.services.tracker_service import TrackerService
from dmf.settings import Settings
from dmf.testing.sequences import put_on
from tests.conftest import idle_values
from tests.integration.conftest import TEST_DB

pytestmark = [pytest.mark.db, pytest.mark.timeout(90)]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _settings(**kw) -> Settings:
    base = dict(
        database_url=TEST_DB,
        opcua_enabled=False,
        opcua_endpoint="opc.tcp://operator:s3cret@127.0.0.1:4840",
        log_format="text",
        tick_interval_s=0.2,
        snapshot_interval_s=60,
        _env_file=None,
    )
    base.update(kw)
    return Settings(**base)


@asynccontextmanager
async def backend(**kw):
    port = _port()
    app = create_app(_settings(**kw))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
        for _ in range(200):
            await asyncio.sleep(0.05)
            try:
                if (await client.get("/api/ready")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
        else:
            raise AssertionError("backend did not become ready")
        try:
            yield client, app, port
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 20)


# ------------------------------------------------------------------ database outage
async def test_database_outage_after_start_up_answers_503_not_500(db):
    async with backend() as (client, app, _):
        svc = app.state.service
        healthy_db = svc.db
        svc.db = make_engine("postgresql+asyncpg://dmf@127.0.0.1:1/none")  # nothing listens on port 1
        try:
            for url in ("/api/history/passes", "/api/cells/4/events", "/api/pieces/P-0001/journey"):
                r = await client.get(url)
                assert r.status_code == 503, (url, r.status_code, r.text)
                assert "database" in r.json()["detail"].lower()
            r = await client.get("/api/history/export", params={"from": "2026-11-01"})
            assert r.status_code == 503
        finally:
            await svc.db.dispose()
            svc.db = healthy_db
        assert (await client.get("/api/history/passes")).status_code == 200  # recovers by itself


# ------------------------------------------------------------------ input bounds
@pytest.mark.parametrize(
    ("url", "params"),
    [
        ("/api/history/passes", {"day": "9999-12-31"}),
        ("/api/history/passes", {"day": "0001-01-01"}),
        ("/api/history/passes", {"offset": str(10**12)}),
        ("/api/history/export", {"from": "1999-12-31"}),
        ("/api/history/export", {"from": "2026-01-01", "to": "9999-12-31"}),
    ],
)
async def test_out_of_range_dates_and_offsets_are_rejected_with_422(db, url, params):
    async with backend() as (client, _, _):
        r = await client.get(url, params=params)
        assert r.status_code == 422, r.text


async def test_docs_path_from_the_project_description_redirects(db):
    async with backend() as (client, _, _):
        r = await client.get("/docs", follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/api/docs"
        assert (await client.get("/api/docs")).status_code == 200


async def test_credentials_in_the_endpoint_are_not_shown(db):
    async with backend() as (client, _, _):
        st = (await client.get("/api/status")).text
        assert "s3cret" not in st and "operator" not in st and "***@127.0.0.1:4840" in st


async def test_export_range_limit_follows_the_setting(db):
    async with backend(export_max_days=3) as (client, _, _):
        assert (await client.get("/api/plant")).json()["export_max_days"] == 3
        ok = await client.get("/api/history/export", params={"from": "2026-11-01", "to": "2026-11-03"})
        assert ok.status_code == 200
        r = await client.get("/api/history/export", params={"from": "2026-11-01", "to": "2026-11-04"})
        assert r.status_code == 422 and "3 days" in r.text


# ------------------------------------------------------------------ WebSocket
async def test_websocket_client_limit_closes_with_try_again_later(db):
    async with backend(ws_max_clients=2) as (_, _, port):
        url = f"ws://127.0.0.1:{port}/ws/live"
        a = await websockets.connect(url)
        b = await websockets.connect(url)
        await a.recv()
        await b.recv()
        c = await websockets.connect(url)
        with pytest.raises(websockets.ConnectionClosed) as closed:
            await asyncio.wait_for(c.recv(), 5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1013
        await a.close()
        await asyncio.sleep(0.2)
        d = await websockets.connect(url)  # a slot became free
        assert '"type":"live"' in await asyncio.wait_for(d.recv(), 5)
        await b.close()
        await d.close()


async def test_websocket_from_a_foreign_web_page_is_refused(db):
    async with backend() as (_, _, port):
        url = f"ws://127.0.0.1:{port}/ws/live"
        with pytest.raises(websockets.InvalidStatus) as refused:
            await websockets.connect(url, origin="http://evil.example")
        assert refused.value.response.status_code == 403
        same = await websockets.connect(url, origin=f"http://127.0.0.1:{port}")
        assert '"type":"live"' in await asyncio.wait_for(same.recv(), 5)
        await same.close()
        tool = await websockets.connect(url)  # no Origin header (not a browser)
        await asyncio.wait_for(tool.recv(), 5)
        await tool.close()


async def test_websocket_survives_a_binary_frame_from_the_client(db):
    async with backend() as (_, _, port):
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/live")
        await ws.recv()
        await ws.send(b"\x00\x01")
        msg = await asyncio.wait_for(ws.recv(), 8)  # next push / heartbeat still arrives
        assert '"type":"live"' in msg
        await ws.close()


# ------------------------------------------------------------------ supervision
async def test_a_crashed_background_task_is_reported_by_health_and_ready(db):
    async with backend() as (client, app, _):
        svc = app.state.service

        async def boom():
            raise RuntimeError("simulated bug")

        svc._spawn(boom(), "simulated")
        await asyncio.sleep(0.1)
        health = await client.get("/api/health")
        assert health.status_code == 503 and "simulated" in health.json()["reason"]
        ready = await client.get("/api/ready")
        assert ready.status_code == 503 and ready.json()["phase"] == "failed"


async def test_an_error_while_handling_one_message_does_not_stop_tracking(db, monkeypatch):
    async with backend() as (client, app, _):
        svc = app.state.service
        calls = {"n": 0}
        real = svc.engine.on_tick

        def flaky(now):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("simulated bug")
            return real(now)

        monkeypatch.setattr(svc.engine, "on_tick", flaky)
        await asyncio.sleep(1.0)  # several ticks (0.2 s)
        assert calls["n"] > 2 and svc.handler_errors == 1
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/status")).json()["handler_errors"] == 1


# ------------------------------------------------------------------ shutdown & journey
async def _running_service(plant, db) -> TrackerService:
    svc = TrackerService(plant, _settings(), db=db)
    await svc.start()
    for _ in range(200):
        if svc.phase == "running":
            return svc
        await asyncio.sleep(0.05)
    raise AssertionError(f"service phase {svc.phase}")


async def test_shutdown_handles_queued_messages_before_the_final_snapshot(db, plant):
    svc = await _running_service(plant, db)
    t = svc.clock()
    svc.queue.put_nowait(Snapshot(idle_values(plant), t, "connected"))
    for i, (_dt, key, value) in enumerate(put_on(plant, (4, 1))):
        svc.queue.put_nowait(Change(key, value, True, t + 0.1 * (i + 1)))
    await svc.stop()  # immediately: the messages above are still queued
    check = make_engine(TEST_DB)
    try:
        async with check.connect() as conn:
            codes = [r[0] for r in await conn.execute(select(pieces.c.code))]
            snap = (await conn.execute(select(tracker_snapshot.c.data))).scalar_one()
    finally:
        await check.dispose()
    assert codes == ["P-0001"]
    assert list(snap["pieces"]) == ["P-0001"] and snap["convs"]["4.1"]["piece"] == "P-0001"


async def test_journey_of_a_piece_the_database_has_not_got_yet(db, plant):
    async with backend() as (client, app, _):
        svc = app.state.service
        t = svc.clock()
        svc.engine.on_resync(idle_values(plant, {k_state(4, 1): 3}), t)  # tracker only, nothing written
        (code,) = list(svc.engine.pieces)
        r = await client.get(f"/api/pieces/{code}/journey")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pending"] is True and body["passes"] == []
        assert body["piece"]["code"] == code and body["piece"]["status"] == "active"
        assert body["now"]["position"] == 4
        assert (await client.get("/api/pieces/P-9999/journey")).status_code == 404


# ------------------------------------------------------------------ start-up before the database
async def test_start_up_waits_for_the_database_and_then_runs(db):
    """The backend must not crash while its database does not exist / is not reachable yet: it
    reports ``waiting_for_database`` (ready 503, health 200) and starts tracking once it is there."""
    import asyncpg
    from sqlalchemy.engine import make_url

    name = "dmf_test_startup_wait"
    url = make_url(TEST_DB).set(database=name)
    admin_dsn = make_url(TEST_DB).set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    await admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    port = _port()
    app = create_app(_settings(database_url=url.render_as_string(hide_password=False), db_retry_max_s=2))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:

            async def ready() -> httpx.Response | None:
                try:
                    return await client.get("/api/ready")
                except httpx.TransportError:
                    return None

            for _ in range(200):  # the server answers, but is not ready
                r = await ready()
                if r is not None and r.json().get("phase") == "waiting_for_database":
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("backend never reported waiting_for_database")
            assert r.status_code == 503 and r.json()["ready"] is False
            assert (await client.get("/api/health")).status_code == 200  # alive, not failed
            await asyncio.sleep(1.5)  # a few retries later it is still waiting, not crashed
            assert (await client.get("/api/ready")).json()["phase"] == "waiting_for_database"

            await admin(f"CREATE DATABASE {name}")
            for _ in range(300):
                r = await ready()
                if r is not None and r.status_code == 200:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("backend did not start after the database appeared")
            assert r.json()["phase"] == "running" and r.json()["database"] is True
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)
        await admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
