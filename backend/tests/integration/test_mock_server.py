"""The mock OPC UA server and its control API, against a plain asyncua client (no backend).

The mock is test tooling only, but the backend's "never writes" proof and the offline /
quality / refusal tests rely on it, so its behaviour is pinned here.
"""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import UTC, datetime

import httpx
import pytest
from asyncua import Client, ua

from dmf.plant.model import k_lamp, k_state
from dmf_mock.control import make_control_app
from dmf_mock.server import MockPlantServer

pytestmark = [pytest.mark.opcua, pytest.mark.timeout(60)]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def mock(plant):
    port = _free_port()
    server = MockPlantServer(plant, f"opc.tcp://127.0.0.1:{port}/")
    await server.start()
    server.port = port
    yield server
    await server.stop()


@pytest.fixture
async def client(mock):
    c = Client(f"opc.tcp://127.0.0.1:{mock.port}/", timeout=4)
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
async def control(mock, plant):
    holder = {"server": mock, "endpoint": mock.endpoint}
    app = make_control_app(holder, plant)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock-control") as http:
        yield http


def _node(client: Client, mock: MockPlantServer, key: str):
    return client.get_node(mock.key_to_nodeid[key])


async def _read(client: Client, mock: MockPlantServer, keys: list[str]) -> list[ua.DataValue]:
    return await client.read_attributes([_node(client, mock, k) for k in keys], ua.AttributeIds.Value)


# ------------------------------------------------------------------ read-only: every modifying service
async def test_every_modifying_service_is_refused_and_counted(mock, client, control):
    lamp = _node(client, mock, k_lamp(4))
    assert await lamp.read_value() == 3
    sub = await client.create_subscription(50, None)
    await sub.subscribe_data_change([lamp])
    await sub.delete()
    assert mock.client_write_attempts == 0  # reading, browsing and subscribing are fine

    folder = client.get_node(ua.NodeId("Zelle4", mock.ns))
    attempts = {
        "Write": lambda: lamp.write_value(ua.Variant(1, ua.VariantType.Int16)),
        "Call": lambda: client.nodes.server.call_method("0:GetMonitoredItems", ua.Variant(1, ua.VariantType.UInt32)),
        "AddNodes": lambda: client.nodes.objects.add_folder(ua.NodeId("Intruder", mock.ns), "Intruder"),
        "DeleteNodes": lambda: lamp.delete(),
        "AddReferences": lambda: folder.add_reference(client.nodes.objects, ua.ObjectIds.Organizes),
        "DeleteReferences": lambda: folder.delete_reference(lamp, ua.ObjectIds.HasComponent),
        "HistoryUpdate": lambda: _history_update(client),
    }
    for service, attempt in attempts.items():
        with pytest.raises(ua.UaStatusCodeError) as err:
            await attempt()
        assert err.value.code == ua.StatusCodes.BadUserAccessDenied, service
        assert mock.client_attempts[service] == 1, (service, dict(mock.client_attempts))
    assert dict(mock.client_attempts) == dict.fromkeys(attempts, 1)
    assert mock.client_write_attempts == len(attempts) == 7
    # nothing was changed on the server
    assert await lamp.read_value() == 3
    assert {r.NodeId for r in await folder.get_references(ua.ObjectIds.HasComponent)} >= {lamp.nodeid}
    health = (await control.get("/health")).json()
    assert health["client_write_attempts"] == 7 and health["client_attempts"] == dict.fromkeys(attempts, 1)


async def _history_update(client: Client) -> None:
    req = ua.HistoryUpdateRequest()
    req.Parameters = ua.HistoryUpdateParameters(HistoryUpdateDetails=[])
    await client.uaclient.protocol.send_request(req)


async def test_server_side_changes_are_not_counted(mock, client):
    await mock.set(k_state(4, 1), 2)
    await mock.set(k_lamp(4), 0, good=False)
    assert mock.client_write_attempts == 0
    assert await _node(client, mock, k_state(4, 1)).read_value() == 2


# ------------------------------------------------------------------ control: quality of a position
async def test_quality_endpoint_makes_one_position_bad_and_restores_it(mock, client, control):
    await mock.set(k_lamp(4), 2)
    keys = mock.position_keys(4)
    assert len(keys) == 1 + 2 * 2 + 3 * 3  # lamp, 2 ports x 2 flags, 3 conveyors x (state + 2 sensors)
    r = await control.post("/quality", json={"position": 4, "good": False})
    assert r.status_code == 200 and r.json() == {"position": 4, "good": False, "nodes": len(keys)}
    dvs = await _read(client, mock, keys)
    assert all(dv.StatusCode.value == ua.StatusCodes.BadCommunicationError for dv in dvs)
    other = await _read(client, mock, mock.position_keys(5))
    assert all(dv.StatusCode.is_good() for dv in other)
    r = await control.post("/quality", json={"position": 4, "good": True})
    assert r.status_code == 200
    dvs = await _read(client, mock, keys)
    assert all(dv.StatusCode.is_good() for dv in dvs)
    assert dvs[keys.index(k_lamp(4))].Value.Value == 2  # last good value is back
    assert (await control.post("/quality", json={"position": 99, "good": False})).status_code == 422


# ------------------------------------------------------------------ control: source timestamps
async def test_timestamp_endpoints_freeze_and_advance_the_source_timestamp(mock, client, control):
    def ts_of(dv: ua.DataValue) -> float:
        return dv.SourceTimestamp.replace(tzinfo=UTC).timestamp()

    r = await control.post("/timestamps/freeze", json={"position": 4, "age_s": 120})
    assert r.status_code == 200 and r.json()["frozen"] is True
    frozen = r.json()["source_timestamp"]
    assert abs(frozen - (time.time() - 120)) < 5
    (lamp4, lamp5) = await _read(client, mock, [k_lamp(4), k_lamp(5)])
    assert abs(ts_of(lamp4) - frozen) < 0.01
    assert abs(ts_of(lamp5) - time.time()) < 5  # other positions are not affected
    await mock.set(k_lamp(4), 4)  # the value still changes, the timestamp does not
    (lamp4,) = await _read(client, mock, [k_lamp(4)])
    assert lamp4.Value.Value == 4 and abs(ts_of(lamp4) - frozen) < 0.01
    r = await control.post("/timestamps/advance", json={"position": 4, "seconds": 30})
    assert r.status_code == 200 and abs(r.json()["source_timestamp"] - (frozen + 30)) < 0.01
    (state,) = await _read(client, mock, [k_state(4, 2)])
    assert abs(ts_of(state) - (frozen + 30)) < 0.01  # every node of the position
    r = await control.post("/timestamps/advance", json={"position": 4, "seconds": None})
    assert r.status_code == 200 and r.json()["frozen"] is False
    (lamp4,) = await _read(client, mock, [k_lamp(4)])
    assert abs(ts_of(lamp4) - time.time()) < 5 and mock.frozen_timestamp(4) is None
    assert (await control.post("/timestamps/freeze", json={"position": 0})).status_code == 422


# ------------------------------------------------------------------ control: refused monitored items
async def test_refuse_endpoint_makes_the_server_refuse_a_monitored_item(mock, client, control):
    r = await control.post("/monitoring/refuse", json={"keys": [k_lamp(4)]})
    assert r.status_code == 200 and r.json() == {"refused": [k_lamp(4)], "status": "BadTooManyMonitoredItems"}
    sub = await client.create_subscription(50, None)
    res = await sub.subscribe_data_change([_node(client, mock, k_lamp(4)), _node(client, mock, k_lamp(5))])
    assert isinstance(res[0], ua.StatusCode) and res[0].value == ua.StatusCodes.BadTooManyMonitoredItems
    assert isinstance(res[1], int)  # the other item was created
    assert (await _node(client, mock, k_lamp(4)).read_value()) == 3  # still readable
    assert (await control.post("/monitoring/refuse", json={"keys": []})).status_code == 200
    (again,) = await sub.subscribe_data_change([_node(client, mock, k_lamp(4))])
    assert isinstance(again, int)
    await sub.delete()
    assert (await control.post("/monitoring/refuse", json={"keys": ["pos4.nope"]})).status_code == 422
    assert (await control.post("/monitoring/refuse", json={"keys": [k_lamp(4)], "status": "NoSuchCode"})).status_code == 422
    assert mock.client_write_attempts == 0


# ------------------------------------------------------------------ control: subscription status change
async def test_subscription_status_endpoint_sends_a_status_change(mock, client, control):
    got: asyncio.Queue = asyncio.Queue()

    class Handler:
        def datachange_notification(self, node, val, data):
            pass

        def status_change_notification(self, status):
            got.put_nowait(status.Status)

    sub = await client.create_subscription(50, Handler())
    await sub.subscribe_data_change([_node(client, mock, k_lamp(4))])
    r = await control.post("/subscription/status", json={"status": "BadTimeout"})
    assert r.status_code == 200 and r.json() == {"status": "BadTimeout", "subscriptions": 1}
    status = await asyncio.wait_for(got.get(), 5)
    assert status.value == ua.StatusCodes.BadTimeout
    assert (await control.post("/subscription/status", json={"status": "Nonsense"})).status_code == 422


async def test_frozen_timestamp_is_a_datetime_in_the_past(mock):
    ts = await mock.freeze_timestamps(7, age_s=60)
    assert isinstance(ts, datetime) and ts < datetime.now(UTC)
    assert mock.frozen_timestamp(7) == ts
