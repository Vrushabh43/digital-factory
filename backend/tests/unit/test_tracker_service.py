"""TrackerService read models (live payload, pieces now), driven without network or database.

Messages are fed exactly as the ingest would queue them; the service clock is the time of
the last message, so every value is deterministic.
"""

from __future__ import annotations

from dmf.opcua.ingest import Change, Lost, Snapshot, Stale, Tick
from dmf.plant.model import Plant, k_lamp, k_port, k_state
from dmf.services.tracker_service import EVENTS_PER_CELL, TrackerService
from dmf.settings import Settings
from dmf.testing.sequences import Timing, take_off, travel
from dmf.tracker import events as ev
from tests.conftest import idle_values

T0 = 1_000_000.0
FAST = Timing(take_in_s=0.4, travel_s=0.4, rotate_s=0.4, react_s=0.1)


class Feed:
    """Feeds a TrackerService through ``_apply`` (what its queue consumer does)."""

    def __init__(self, plant: Plant):
        self.plant = plant
        self.t = T0
        settings = Settings(_env_file=None, handover_window_s=5.0, stuck_after_s=60.0, tick_interval_s=1.0)
        self.svc = TrackerService(plant, settings, db=None, clock=lambda: self.t)
        self.events: list[ev.Event] = []
        self.svc.add_event_listener(self.events.extend)

    def snapshot(self, overrides=None, reason="connected", dt=0.0, values=None) -> None:
        self.t += dt
        self.svc._apply(Snapshot(values if values is not None else idle_values(self.plant, overrides), self.t, reason))

    def set(self, key, value, dt=0.2, good=True) -> None:
        self.t += dt
        self.svc._apply(Change(key, value, good, self.t))

    def run(self, steps) -> None:
        for dt, key, value in steps:
            self.set(key, value, dt)
            self.tick(0)

    def tick(self, dt=1.0) -> None:
        self.t += dt
        self.svc._apply(Tick(self.t))

    def ticks_until(self, t: float) -> None:
        while self.t + 1.0 <= t:
            self.tick(1.0)

    def cell(self, pos: int) -> dict:
        return next(c for c in self.svc.live_state()["cells"] if c["position"] == pos)

    def statuses(self) -> dict[str, str]:
        return {p["code"]: p["status"] for p in self.svc.pieces_now()}


def test_pieces_now_reports_every_kind_of_status(plant):
    f = Feed(plant)
    f.snapshot(
        {
            k_state(4, 1): 2,  # P-0001 moving (in a cell that will be "stuck" because of P-0002)
            k_state(4, 3): 3,  # P-0002 waits at the exit -> stuck after 60 s
            k_state(8, 2): 2,  # P-0003 -> uncertain after the data gap
            k_state(9, 1): 2,  # P-0004 in a cell with a red lamp
            k_state(10, 1): 2,  # P-0005 in a cell that goes offline
            k_state(11, 3): 3,  # P-0006 handed to the robot
            k_lamp(9): 1,
        }
    )
    assert f.statuses() == dict.fromkeys(["P-0001", "P-0002", "P-0003", "P-0004", "P-0005", "P-0006"], "moving") | {
        "P-0004": "cell_error"
    }
    # P-0006: to the robot
    f.set(k_port(11, 2, "can_send"), True)
    f.set(k_state(11, 3), 4)
    f.set(k_state(11, 3), 0)
    f.ticks_until(T0 + 8)
    # data gap of 30 s; afterwards P-0003's conveyor shows another state
    f.svc._apply(Lost("test", f.t))
    f.snapshot(
        {k_state(4, 1): 2, k_state(4, 3): 3, k_state(8, 2): 3, k_state(9, 1): 2, k_state(10, 1): 2, k_lamp(9): 1},
        reason="reconnected",
        dt=30.0,
    )
    f.svc._apply(Stale(10, True, f.t))
    f.ticks_until(T0 + 61)

    assert f.statuses() == {
        "P-0001": "moving",
        "P-0002": "stuck",
        "P-0003": "uncertain",
        "P-0004": "cell_error",
        "P-0005": "cell_offline",
        "P-0006": "with_robot",
    }
    pieces = {p["code"]: p for p in f.svc.pieces_now()}
    assert pieces["P-0002"]["stuck_since"] == T0 and pieces["P-0002"]["waiting_since"] == T0
    assert [c for c, p in pieces.items() if p["stuck_since"] is not None] == ["P-0002"]
    assert pieces["P-0003"]["uncertain"] is True and pieces["P-0003"]["uncertain_reason"]
    assert [c for c, p in pieces.items() if p["uncertain"]] == ["P-0003"]
    assert pieces["P-0006"]["position"] is None and pieces["P-0006"]["cell"] is None
    assert (pieces["P-0002"]["position"], pieces["P-0002"]["cell"], pieces["P-0002"]["conveyor"]) == (4, 14, 3)
    assert pieces["P-0002"]["conveyor_role"] == "exit"

    live = f.svc.live_state()
    assert live["pieces"] == f.svc.pieces_now()
    assert f.cell(4)["status"] == "stuck" and f.cell(4)["stuck_piece"] == {"code": "P-0002", "waiting_since": T0}
    assert f.cell(4)["pieces"] == ["P-0001", "P-0002"]
    assert f.cell(9)["status"] == "error" and f.cell(9)["stuck_piece"] is None
    assert f.cell(10)["status"] == "offline"
    assert live["counts"] == {"running": len(plant.cells) - 3, "stuck": 1, "error": 1, "offline": 1}


def test_ready_to_receive_and_ready_to_send_follow_the_port_flags(plant):
    f = Feed(plant)
    f.snapshot()
    c4 = f.cell(4)  # linear: port 1 in, port 2 out
    assert c4["ports"] == {
        "1": {"direction": "in", "can_send": False, "can_receive": True},
        "2": {"direction": "out", "can_send": False, "can_receive": False},
    }
    assert (c4["ready_to_receive"], c4["ready_to_send"]) == (True, False)
    f.set(k_port(4, 1, "can_receive"), False)
    f.set(k_port(4, 2, "can_send"), True)
    assert (f.cell(4)["ready_to_receive"], f.cell(4)["ready_to_send"]) == (False, True)
    # the flags of the other direction do not count
    f.set(k_port(4, 1, "can_send"), True)
    f.set(k_port(4, 2, "can_receive"), True)
    f.set(k_port(4, 2, "can_send"), False)
    assert (f.cell(4)["ready_to_receive"], f.cell(4)["ready_to_send"]) == (False, False)

    # merger (position 2): two input ports, one ready is enough
    assert set(plant.cells[2].input_ports) == {1, 3}
    f.set(k_port(2, 3, "can_receive"), False)
    assert f.cell(2)["ready_to_receive"] is True
    f.set(k_port(2, 1, "can_receive"), False)
    assert f.cell(2)["ready_to_receive"] is False
    # unknown (Bad quality) -> None, not False
    f.set(k_port(2, 1, "can_receive"), None, good=False)
    assert f.cell(2)["ready_to_receive"] is None and f.cell(2)["ports"]["1"]["can_receive"] is None

    # splitter (position 5): two output ports; one ready but the other unknown -> None
    f.set(k_port(5, 2, "can_send"), True)
    assert f.cell(5)["ready_to_send"] is True
    f.set(k_port(5, 3, "can_send"), None, good=False)
    assert f.cell(5)["ready_to_send"] is None


def test_ready_flags_are_none_before_any_data(plant):
    f = Feed(plant)
    values = idle_values(plant)
    for port in plant.cells[4].port_nodes:
        del values[k_port(4, port, "can_receive")], values[k_port(4, port, "can_send")]
    f.snapshot(values=values)
    assert (f.cell(4)["ready_to_receive"], f.cell(4)["ready_to_send"]) == (None, None)
    assert (f.cell(1)["ready_to_receive"], f.cell(1)["ready_to_send"]) == (True, False)


def test_latest_events_are_newest_first_without_cell_status_and_limited(plant):
    f = Feed(plant)
    f.snapshot()
    for _ in range(3):  # three pieces through cell 1 (position 1), each taken off at the exit
        f.run(travel(plant, [(1, 1), (1, 2), (1, 3)], FAST))
        f.run(take_off((1, 3), FAST))
        f.ticks_until(f.t + 6)
    n_before = len(f.events)
    f.set(k_lamp(1), 1)  # status changes of the cell itself (running -> error -> running) come last
    f.set(k_lamp(1), 3)

    at1 = [e for e in f.events if e.position == 1 and e.kind != "cell_status"]
    assert len(at1) > EVENTS_PER_CELL  # enough to see the limit
    assert [e.status for e in f.events[n_before:] if e.kind == "cell_status"] == ["error", "running"]
    expected = [(e.ts, e.kind, getattr(e, "code", None), e.message) for e in reversed(at1[-EVENTS_PER_CELL:])]
    latest = f.cell(1)["latest_events"]
    assert [(r["ts"], r["kind"], r["piece"], r["message"]) for r in latest] == expected
    assert [r["ts"] for r in latest] == sorted((r["ts"] for r in latest), reverse=True)
    assert [(r["kind"], r["piece"]) for r in latest[:3]] == [
        ("piece_ended", "P-0003"),
        ("cell_left", "P-0003"),
        ("exit_reached", "P-0003"),
    ]
    assert latest[-1]["piece"] == "P-0002"
