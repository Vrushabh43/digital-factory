from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from dmf.plant.model import K_ALARM, K_ENABLE, Plant, k_lamp, k_port, k_state, load_plant
from dmf.testing.sequences import Step
from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def plant() -> Plant:
    return load_plant(REPO / "config" / "plant.yaml", REPO / "config" / "opcua_nodes.yaml")


def idle_values(plant: Plant, overrides: dict[str, int] | None = None) -> dict[str, tuple[object, bool]]:
    """A complete, healthy, empty plant: lamps green-blinking, all conveyors 0."""
    values: dict[str, tuple[object, bool]] = {K_ALARM: (False, True), K_ENABLE: (True, True)}
    for pos, cell in plant.cells.items():
        values[k_lamp(pos)] = (3, True)
        for port in cell.port_nodes:
            values[k_port(pos, port, "can_send")] = (False, True)
            values[k_port(pos, port, "can_receive")] = (port in cell.input_ports, True)
        for n in cell.conveyors:
            values[k_state(pos, n)] = (0, True)
    for k, v in (overrides or {}).items():
        values[k] = (v, True)
    return values


class Driver:
    """Feeds an engine with timed signal changes and collects the events."""

    def __init__(self, plant: Plant, **engine_kw):
        engine_kw.setdefault("handover_window_s", 5.0)
        engine_kw.setdefault("stuck_after_s", 60.0)
        self.plant = plant
        self.engine = TrackerEngine(plant, **engine_kw)
        self.t = 1_000_000.0
        self.events: list[ev.Event] = []

    def sync(self, overrides: dict[str, int] | None = None, dt: float = 0.0) -> list[ev.Event]:
        self.t += dt
        out = self.engine.on_resync(idle_values(self.plant, overrides), self.t)
        self.events += out
        return out

    def set(self, key: str, value, dt: float = 0.2, good: bool = True) -> list[ev.Event]:
        self.t += dt
        out = self.engine.on_signal(key, value, good, self.t)
        self.events += out
        return out

    def run(self, steps: Iterable[Step], tick: bool = True) -> list[ev.Event]:
        out: list[ev.Event] = []
        for dt, key, value in steps:
            out += self.set(key, value, dt)
            if tick:
                out += self.tick(0)
        return out

    def tick(self, dt: float = 1.0) -> list[ev.Event]:
        self.t += dt
        out = self.engine.on_tick(self.t)
        self.events += out
        return out

    def of(self, kind: type[ev.Event]) -> list:
        return [e for e in self.events if isinstance(e, kind)]

    def piece_at(self, pos: int, conv: int) -> str | None:
        return self.engine.convs[(pos, conv)].piece


@pytest.fixture
def driver(plant: Plant) -> Driver:
    d = Driver(plant)
    d.sync()
    return d
