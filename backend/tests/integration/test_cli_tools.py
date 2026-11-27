"""The read-only lab tools (docs/08) against the mock server: ``dmf.cli.verify`` exit codes and
report, and a ``dmf.cli.record`` recording that ``dmf.cli.replay_check`` can replay."""

import asyncio
import json
import socket

import pytest
from asyncua import ua

from dmf.cli.record import record
from dmf.cli.replay_check import replay, summarise
from dmf.cli.verify import verify
from dmf.plant.model import k_lamp, k_state
from dmf.settings import get_settings
from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine
from dmf_mock.server import MockPlantServer


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
def endpoint(monkeypatch):
    """Point the CLI tools (which read the environment via get_settings) at a given endpoint."""

    def use(url: str) -> None:
        monkeypatch.setenv("OPCUA_ENDPOINT", url)
        monkeypatch.setenv("OPCUA_TIMEOUT_S", "2")
        get_settings.cache_clear()

    yield use
    get_settings.cache_clear()


async def test_verify_passes_on_a_matching_read_only_server(plant, mock, endpoint, tmp_path, capsys):
    endpoint(f"opc.tcp://127.0.0.1:{mock.port}/")
    report = tmp_path / "verify.json"
    assert await verify(report) == 0
    out = capsys.readouterr().out
    n = len(plant.signal_nodes(include_sensors=True))
    assert f"Checked {n} nodes: 0 missing, 0 with a different browse name, 0 values out of range, 0 writable" in out
    rows = json.loads(report.read_text(encoding="utf-8"))["nodes"]
    assert len(rows) == n
    assert all(r["user_can_write"] is False and r["browse_name"] == r["expected"] for r in rows)
    assert mock.client_write_attempts == 0  # verify only reads, it never tries a write


async def test_verify_exit_code_3_when_the_user_could_write(plant, mock, endpoint, capsys):
    key = k_lamp(4)
    node = mock.server.get_node(mock.key_to_nodeid[key])
    # server-side change (not a client request): grant write access to everybody on one node
    await node.set_attr_bit(ua.AttributeIds.UserAccessLevel, ua.AccessLevel.CurrentWrite)
    await node.set_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.CurrentWrite)
    endpoint(f"opc.tcp://127.0.0.1:{mock.port}/")
    assert await verify(None) == 3
    out = capsys.readouterr().out
    assert "1 writable for this user" in out and "WRITE access" in out
    assert mock.client_write_attempts == 0


async def test_verify_exit_code_1_on_a_different_browse_name(plant, mock, endpoint, capsys):
    key = k_state(5, 2)
    await mock.server.write_attribute_value(
        mock.key_to_nodeid[key], ua.DataValue(ua.Variant(ua.QualifiedName("Other", mock.ns))), ua.AttributeIds.BrowseName
    )
    endpoint(f"opc.tcp://127.0.0.1:{mock.port}/")
    assert await verify(None) == 1
    out = capsys.readouterr().out
    assert "1 with a different browse name" in out
    assert "DIFFERENT BROWSE NAME" in out and key in out and "'Other'" in out


async def test_verify_exit_code_2_when_the_server_is_unreachable(endpoint, capsys):
    endpoint(f"opc.tcp://user:secret@127.0.0.1:{_free_port()}/")
    assert await verify(None) == 2
    err = capsys.readouterr().err
    assert "Could not connect" in err
    assert "secret" not in err  # credentials in the endpoint are never printed


async def test_recording_can_be_replayed_through_the_tracker(plant, mock, endpoint, tmp_path):
    endpoint(f"opc.tcp://127.0.0.1:{mock.port}/")
    out = tmp_path / "run.jsonl"
    rec = asyncio.create_task(record(out, minutes=4 / 60))

    async def lines() -> list[dict]:
        if not out.exists():
            return []
        return [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]

    for _ in range(100):  # wait for the initial snapshot before changing anything
        if any(r["type"] == "snapshot" for r in await lines()):
            break
        await asyncio.sleep(0.05)
    # a piece is put on at the side entry (position 2, conveyor 1) and taken in
    await mock.set(k_state(2, 1), 1)
    await asyncio.sleep(0.3)
    await mock.set(k_state(2, 1), 2)
    assert await asyncio.wait_for(rec, 30) == 0

    records = await lines()
    header = records[0]
    assert header["type"] == "header" and header["node_map_verification"] == plant.node_verification
    assert [r["type"] for r in records[1:2]] == ["snapshot"]
    changes = [(r["key"], r["value"]) for r in records if r["type"] == "change"]
    assert (k_state(2, 1), 1) in changes and (k_state(2, 1), 2) in changes

    events = list(replay(out, TrackerEngine(plant, handover_window_s=8, stuck_after_s=120)))
    appeared = [e for e in events if isinstance(e, ev.PieceAppeared)]
    assert [(e.code, e.pos, e.conveyor, e.origin) for e in appeared] == [("P-0001", 2, 1, "source:Side entry")]
    summary = summarise(events)
    assert summary["pieces_appeared"] == {"source": 1} and summary["anomalies"] == []
    assert mock.client_write_attempts == 0
