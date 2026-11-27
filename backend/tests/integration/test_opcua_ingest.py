"""OPC UA ingest against the in-process mock server (no factory needed)."""

from __future__ import annotations

import asyncio
import socket

import pytest

from dmf.opcua.ingest import Change, Lost, OpcIngest, Snapshot
from dmf.plant.model import k_lamp, k_state
from dmf.settings import Settings
from dmf.testing.sequences import Timing, route, travel
from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine
from dmf_mock.server import MockPlantServer

pytestmark = [pytest.mark.opcua, pytest.mark.timeout(60)]

FAST = Timing(take_in_s=0.15, travel_s=0.15, rotate_s=0.15, react_s=0.05)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def settings_for(port: int, **kw) -> Settings:
    base = dict(
        opcua_endpoint=f"opc.tcp://127.0.0.1:{port}/",
        opcua_reconnect_min_s=0.2,
        opcua_reconnect_max_s=0.5,
        opcua_publishing_interval_ms=50,
        opcua_sampling_interval_ms=0,
        opcua_timeout_s=2,
        _env_file=None,
    )
    base.update(kw)
    return Settings(**base)


async def next_of(queue: asyncio.Queue, kind: type, timeout: float = 10.0):
    async def _get():
        while True:
            msg = await queue.get()
            if isinstance(msg, kind):
                return msg

    return await asyncio.wait_for(_get(), timeout)


async def poll(cond, timeout: float = 10.0, every: float = 0.05) -> None:
    """Wait until ``cond()`` is true (AssertionError after ``timeout``)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(every)


async def stop_ingest(ingest: OpcIngest, task: asyncio.Task) -> None:
    await ingest.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def mock(plant):
    port = free_port()
    server = MockPlantServer(plant, f"opc.tcp://127.0.0.1:{port}/")
    await server.start()
    server.port = port
    yield server
    await server.stop()


async def test_snapshot_changes_and_validation(plant, mock):
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(plant, settings_for(mock.port), queue)
    task = asyncio.create_task(ingest.run())
    try:
        snap = await next_of(queue, Snapshot)
        expected = len(plant.signal_nodes(include_sensors=True))
        assert len(snap.values) == expected and snap.reason == "connected"
        assert snap.values[k_lamp(1)] == (3, True)
        assert ingest.status.state == "connected"
        assert ingest.status.node_mismatches == [] and ingest.status.nodes_checked == expected
        await mock.set(k_state(4, 1), 1)
        await mock.set(k_state(4, 1), 2)
        got = [await next_of(queue, Change), await next_of(queue, Change)]
        assert [(c.key, c.value, c.good) for c in got] == [(k_state(4, 1), 1, True), (k_state(4, 1), 2, True)]
        await mock.set(k_lamp(6), 0, good=False)
        bad = await next_of(queue, Change)
        assert bad.key == k_lamp(6) and bad.good is False
    finally:
        await ingest.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert mock.client_write_attempts == 0


async def test_browse_name_mismatch_is_reported_not_fatal(plant, mock):
    # rename one node on the mock server to simulate a wrong entry in the node map
    from asyncua import ua

    node = mock.server.get_node(mock.key_to_nodeid[k_lamp(2)])
    await mock.server.write_attribute_value(
        node.nodeid, ua.DataValue(ua.Variant(ua.QualifiedName("Other", mock.ns))), ua.AttributeIds.BrowseName
    )
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(plant, settings_for(mock.port), queue)
    task = asyncio.create_task(ingest.run())
    try:
        await next_of(queue, Snapshot)
        assert [m["key"] for m in ingest.status.node_mismatches] == [k_lamp(2)]
        assert ingest.status.node_mismatches[0]["actual"] == "Other"
    finally:
        await ingest.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_reconnect_after_server_restart(plant):
    port = free_port()
    server = MockPlantServer(plant, f"opc.tcp://127.0.0.1:{port}/")
    await server.start()
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(plant, settings_for(port), queue)
    task = asyncio.create_task(ingest.run())
    try:
        await next_of(queue, Snapshot)
        await server.stop()
        lost = await next_of(queue, Lost, timeout=15)
        assert lost.reason
        assert ingest.status.state in ("disconnected", "connecting")
        server2 = MockPlantServer(plant, f"opc.tcp://127.0.0.1:{port}/")
        await server2.start()
        await server2.set(k_state(9, 2), 5)  # something changed while we were away
        try:
            snap = await next_of(queue, Snapshot, timeout=20)
            assert snap.reason == "reconnected"
            assert snap.values[k_state(9, 2)] == (5, True)
            assert ingest.status.state == "connected"
        finally:
            await server2.stop()
    finally:
        await ingest.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unreachable_server_retries_with_backoff(plant):
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(plant, settings_for(free_port()), queue)
    task = asyncio.create_task(ingest.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.1)
            if ingest.status.reconnect_attempts >= 2:
                break
        assert ingest.status.reconnect_attempts >= 2
        assert ingest.status.last_error
        assert queue.empty()  # never connected -> no Lost message, no Snapshot
    finally:
        await ingest.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_end_to_end_tracking_through_opcua(plant, mock):
    """Mock server -> OPC UA subscription -> tracker: a piece crosses three cells."""
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(plant, settings_for(mock.port), queue)
    engine = TrackerEngine(plant, handover_window_s=3, stuck_after_s=60)
    events: list[ev.Event] = []
    task = asyncio.create_task(ingest.run())

    async def consume():
        loop = asyncio.get_running_loop()
        while True:
            msg = await queue.get()
            now = loop.time()
            if isinstance(msg, Snapshot):
                events.extend(engine.on_resync(msg.values, now))
            elif isinstance(msg, Change):
                events.extend(engine.on_signal(msg.key, msg.value, msg.good, now))
            events.extend(engine.on_tick(now))

    consumer = asyncio.create_task(consume())
    try:
        while not engine.connected:
            await asyncio.sleep(0.05)
        path = route(plant, (1, 1), cells=2)
        steps = list(travel(plant, path, FAST))
        for dt, key, value in steps:
            await asyncio.sleep(dt)
            await mock.set(key, value)
        last_key, last_value = steps[-1][1:]
        # wait until the last change has reached the tracker (instead of a fixed sleep)
        await poll(lambda: engine.convs[path[-1]].state == last_value and engine.convs[path[-1]].piece is not None)
        assert last_key == k_state(*path[-1])
        entered = [(e.code, e.pos) for e in events if isinstance(e, ev.CellEntered)]
        assert entered == [("P-0001", 1), ("P-0001", 2), ("P-0001", 3)]
        assert engine.convs[path[-1]].piece == "P-0001"
    finally:
        consumer.cancel()
        await ingest.stop()
        task.cancel()
        await asyncio.gather(task, consumer, return_exceptions=True)
    assert mock.client_write_attempts == 0


# ------------------------------------------------------------------ node map check against a WRONG node map
def _plant_with_swapped_state_nodes(a: int, b: int):
    """The node map of position 4 with the state node ids of conveyors ``a`` and ``b`` swapped."""
    import copy

    import yaml

    from dmf.plant.model import build_plant
    from dmf.settings import REPO_ROOT

    plant_cfg = yaml.safe_load((REPO_ROOT / "config" / "plant.yaml").read_text(encoding="utf-8"))
    nodes_cfg = copy.deepcopy(yaml.safe_load((REPO_ROOT / "config" / "opcua_nodes.yaml").read_text(encoding="utf-8")))
    convs = nodes_cfg["positions"][4]["conveyors"]
    convs[a]["state"], convs[b]["state"] = convs[b]["state"], convs[a]["state"]
    return build_plant(plant_cfg, nodes_cfg)


async def test_a_wrong_entry_in_the_node_map_is_reported(plant, mock):
    """The server is right (true node map), our node map is wrong: the check must say so."""
    wrong = _plant_with_swapped_state_nodes(1, 2)
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(wrong, settings_for(mock.port), queue)
    task = asyncio.create_task(ingest.run())
    try:
        await next_of(queue, Snapshot)
        assert ingest.status.nodes_checked == len(plant.signal_nodes(include_sensors=True))
        found = {(m["key"], m["node_id"], m["expected"], m["actual"]) for m in ingest.status.node_mismatches}
        ids = {n: plant.cells[4].conveyors[n].state_node for n in (1, 2)}
        assert found == {
            (k_state(4, 1), ids[2], "Fb1_Zustand", "Fb2_Zustand"),
            (k_state(4, 2), ids[1], "Fb2_Zustand", "Fb1_Zustand"),
        }
    finally:
        await stop_ingest(ingest, task)
    assert mock.client_write_attempts == 0


# ------------------------------------------------------------------ subscription status change
async def test_subscription_status_change_leads_to_lost_and_a_resynchronisation(plant, mock):
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(plant, settings_for(mock.port), queue)
    task = asyncio.create_task(ingest.run())
    try:
        first = await next_of(queue, Snapshot)
        assert first.reason == "connected"
        await mock.set(k_state(9, 2), 5)  # changes while the subscription is being dropped ...
        assert await mock.send_subscription_status("BadTimeout") == 1
        seen = []
        while not seen or not isinstance(seen[-1], Snapshot):
            msg = await asyncio.wait_for(queue.get(), 15)
            if isinstance(msg, (Lost, Snapshot)):
                seen.append(msg)
        lost, snap = seen
        assert isinstance(lost, Lost) and "BadTimeout" in lost.reason
        assert snap.reason == "reconnected"
        assert snap.values[k_state(9, 2)] == (5, True)  # ... are in the fresh snapshot
        assert ingest.status.state == "connected"
    finally:
        await stop_ingest(ingest, task)
    assert mock.client_write_attempts == 0


# ------------------------------------------------------------------ reconnect back-off
async def test_reconnect_back_off_doubles_up_to_the_cap(plant):
    class Unreachable:
        async def connect(self):
            raise OSError("connection refused")

        async def disconnect(self):
            pass

    now = 1000.0
    waits: list[float] = []
    ingest: OpcIngest | None = None

    def factory():
        if ingest is not None and ingest.status.next_retry_at is not None:
            waits.append(ingest.status.next_retry_at - now)  # delay + jitter of the previous attempt
        return Unreachable()

    s = settings_for(1, opcua_reconnect_min_s=0.02, opcua_reconnect_max_s=0.16)
    ingest = OpcIngest(plant, s, asyncio.Queue(), clock=lambda: now, client_factory=factory)
    task = asyncio.create_task(ingest.run())
    try:
        await poll(lambda: len(waits) >= 7, timeout=15)
    finally:
        await stop_ingest(ingest, task)
    expected = [0.02, 0.04, 0.08, 0.16, 0.16, 0.16, 0.16]
    for got, base in zip(waits, expected, strict=False):
        assert base - 1e-9 <= got <= base * 1.2 + 1e-9, (waits, expected)
    assert ingest.status.state == "disconnected" and "connection refused" in (ingest.status.last_error or "")


# ------------------------------------------------------------------ refused monitored item
async def test_a_refused_monitored_item_is_reported_and_not_trusted(plant, mock):
    mock.refuse_monitoring([k_lamp(4)], "BadTooManyMonitoredItems")
    queue: asyncio.Queue = asyncio.Queue()
    ingest = OpcIngest(plant, settings_for(mock.port), queue)
    task = asyncio.create_task(ingest.run())
    try:
        snap = await next_of(queue, Snapshot)
        assert ingest.status.refused == [{"key": k_lamp(4), "status": "BadTooManyMonitoredItems"}]
        assert ingest.status.subscribed == len(plant.signal_nodes(include_sensors=True)) - 1
        assert snap.values[k_lamp(4)][1] is False  # read once, but not followed: not usable
        assert snap.values[k_lamp(5)] == (3, True)
    finally:
        await stop_ingest(ingest, task)
    assert mock.client_write_attempts == 0
