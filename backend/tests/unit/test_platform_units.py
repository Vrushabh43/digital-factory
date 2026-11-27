"""Settings validation, export hardening and OPC UA ingest details (no network, no database)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from asyncua import ua
from pydantic import ValidationError

from dmf.opcua.ingest import Change, OpcIngest, Stale
from dmf.services import export
from dmf.settings import Settings, redact_url


# ------------------------------------------------------------------ settings
@pytest.mark.parametrize(
    "bad",
    [
        {"timezone": "Mars/Olympus"},
        {"database_url": "postgresql://dmf@db/dmf"},  # sync driver: would fail only at runtime
        {"opcua_endpoint": "http://192.168.0.10:4840"},
        {"db_buffer_max": 100},  # smaller than one write batch
        {"event_queue_max": 0},
        {"opcua_reconnect_min_s": 0},
        {"opcua_reconnect_min_s": 10, "opcua_reconnect_max_s": 5},
        {"offline_check_interval_s": 0},
        {"offline_detection": "timestamps", "offline_after_s": 5, "offline_check_interval_s": 10},
        {"export_max_days": 0},
        {"history_page_max": 0},
        {"handover_window_s": 0},
        {"ws_max_clients": 0},
    ],
)
def test_invalid_settings_are_rejected_at_start_up(bad):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **bad)


def test_default_settings_are_valid():
    s = Settings(_env_file=None)
    assert s.timezone == "Europe/Berlin" and s.export_max_days == 31


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("opc.tcp://user:pa%40ss@192.168.0.10:4840", "opc.tcp://***@192.168.0.10:4840"),
        ("opc.tcp://user@192.168.0.10:4840/path", "opc.tcp://***@192.168.0.10:4840/path"),
        ("opc.tcp://192.168.0.10:4840", "opc.tcp://192.168.0.10:4840"),
    ],
)
def test_endpoint_credentials_are_redacted(url, shown):
    assert redact_url(url) == shown


# ------------------------------------------------------------------ export hardening
def test_negative_durations_are_clamped():
    assert export.fmt_duration(-5) == "0 min 00 s"
    t = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert export._duration({"entered_at": t, "left_at": t - timedelta(seconds=7)}, t) == 0.0


@pytest.mark.parametrize("value", ['=HYPERLINK("http://x")', "+1", "-1", "@SUM(A1)", "\tx"])
def test_csv_cells_that_look_like_formulas_are_defused(value):
    assert export.csv_safe(value) == "'" + value


def test_csv_export_defuses_formula_cells():
    t = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    row = {
        "piece": "=cmd|' /C calc'!A0",
        "cell_number": 4,
        "position": 7,
        "entered_at": t,
        "left_at": t + timedelta(seconds=30),
        "result": "passed",
        "was_stuck": False,
        "came_from_cell": None,
        "went_to_position": 8,
        "uncertain": False,
    }
    text = export.to_csv([row], "Europe/Berlin", t, set()).decode("utf-8-sig")
    line = text.splitlines()[1]
    assert line.startswith("'=cmd|") and ",0 min 30 s," in line


# ------------------------------------------------------------------ ingest callbacks
@dataclass
class _Node:
    nodeid: ua.NodeId


def _ingest(plant) -> tuple[OpcIngest, asyncio.Queue]:
    q: asyncio.Queue = asyncio.Queue()
    ing = OpcIngest(plant, Settings(_env_file=None), q)
    ing._collecting = None  # forwarding mode (after the initial snapshot)
    return ing, q


def _dv(value, source_ts: datetime | None) -> ua.DataValue:
    return ua.DataValue(ua.Variant(value, ua.VariantType.Int16), SourceTimestamp=source_ts)


def test_values_of_a_node_with_the_wrong_browse_name_are_treated_as_bad(plant):
    ing, q = _ingest(plant)
    node = _Node(ua.NodeId(4711, 3))
    ing._by_nodeid = {node.nodeid: "pos4.fb1.state"}
    ing._untrusted = {"pos4.fb1.state"}
    ing._on_change(node, _dv(3, datetime.now(UTC)))
    msg = q.get_nowait()
    assert isinstance(msg, Change) and msg.key == "pos4.fb1.state" and msg.good is False


def test_a_late_initial_notification_does_not_overwrite_a_newer_read(plant):
    ing, q = _ingest(plant)
    node = _Node(ua.NodeId(4712, 3))
    ing._by_nodeid = {node.nodeid: "pos4.fb1.state"}
    read_at = datetime(2026, 11, 26, 12, 0, 10, tzinfo=UTC)
    ing._floor = {"pos4.fb1.state": read_at.timestamp()}  # value 3 was read at 12:00:10
    ing._on_change(node, _dv(0, read_at - timedelta(seconds=5)))  # initial notification, older
    assert q.empty()
    ing._on_change(node, _dv(4, read_at + timedelta(seconds=1)))  # a real, newer change
    assert q.get_nowait().value == 4


@dataclass
class _Reading:
    source_ts: float | None
    value: int = 3
    good: bool = True


class _FakeClient:
    """Lamp readings whose SourceTimestamp comes from a PLC clock that is 1 hour off."""

    def __init__(self, positions: list[int]) -> None:
        self.positions = positions
        self.frozen: dict[int, float] = {}  # position -> timestamp that no longer advances
        self.plc_time = 1_000_000.0 - 3600

    def node(self, nodeid):
        return nodeid

    async def read(self, nodes):
        self.plc_time += 1.0
        return [_Reading(self.frozen.get(pos, self.plc_time)) for pos in self.positions]


async def test_offline_detection_ignores_plc_clock_offset_and_detects_frozen_timestamps(plant):
    now = {"t": 1_000_000.0}
    q: asyncio.Queue = asyncio.Queue()
    s = Settings.model_construct(
        **(Settings(_env_file=None).model_dump() | {"offline_after_s": 3.0, "offline_check_interval_s": 0.001})
    )
    ing = OpcIngest(plant, s, q, clock=lambda: now["t"])
    client = _FakeClient(sorted(plant.cells))
    task = asyncio.create_task(ing._stale_loop(client))  # type: ignore[arg-type]

    async def run_for(steps: int) -> None:
        for _ in range(steps):
            now["t"] += 1.0
            await asyncio.sleep(0.005)

    try:
        await run_for(6)
        stale = {}
        while not q.empty():
            m = q.get_nowait()
            assert isinstance(m, Stale)
            stale[m.pos] = m.stale
        assert stale and not any(stale.values())  # 1 h clock offset alone is NOT offline
        client.frozen = {7: client.plc_time}  # the PLC of cell position 7 stops updating
        await run_for(8)
        msgs = [q.get_nowait() for _ in range(q.qsize())]
        assert [(m.pos, m.stale) for m in msgs] == [(7, True)]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
