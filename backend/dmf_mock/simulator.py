"""Small discrete-time demo simulator that drives ``MockPlantServer`` with PLC-like behaviour.

TEST / DEMO TOOLING ONLY — a rough imitation of the PLC state machines so that the
dashboard shows plausible movement without the factory. Timings, routing choices and
fault injection are random; nothing here describes the real plant.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

from dmf.plant.model import ConvId, Plant, k_lamp, k_port, k_sensor, k_state

log = logging.getLogger(__name__)


@dataclass
class SimPiece:
    conv: ConvId
    timer: float
    target: ConvId | None = None
    hold: float = 0.0  # extra waiting time at the exit (stuck demo)
    ident: int = 0  # simulator-side identity (kept through robot transfers) for tests


@dataclass
class SimConfig:
    dt: float = 0.1
    travel_s: float = 2.0
    take_in_s: float = 1.0
    rotate_s: float = 1.2
    robot_s: float = 6.0
    spawn_every_s: float = 12.0
    max_pieces: int = 10
    stuck_probability: float = 0.03
    stuck_hold_s: float = 150.0
    fault_probability: float = 0.002  # per second per cell, lamp red for a while
    fault_s: float = 20.0
    take_off_after_s: float = 15.0


class DemoSimulator:
    def __init__(self, plant: Plant, server, cfg: SimConfig | None = None, seed: int | None = None):
        self.plant = plant
        self.server = server
        self.cfg = cfg or SimConfig()
        self.rng = random.Random(seed)
        self.state: dict[ConvId, int] = {c.id: 0 for c in plant.all_conveyors()}
        self.pieces: list[SimPiece] = []
        self.robot: list[tuple[float, ConvId, int]] = []  # (remaining s, destination, piece ident)
        self.faults: dict[int, float] = {}
        self.rotating_back: dict[ConvId, float] = {}  # empty turntable in state 6
        self.spawn_timer = 1.0
        self.spawned = 0  # pieces put on at sources so far (= last ident)

    def _tt(self, cid: ConvId) -> bool:
        return self.plant.conveyor(cid).turntable

    def _piece_on(self, cid: ConvId) -> SimPiece | None:
        return next((p for p in self.pieces if p.conv == cid), None)

    def _free(self, cid: ConvId) -> bool:
        return self.state[cid] == 0 and self._piece_on(cid) is None

    def _choose_next(self, cid: ConvId) -> ConvId | str | None:
        succ = list(self.plant.successors.get(cid, []))
        link = self.plant.link_from(cid)
        if link is not None:
            return "robot"
        if cid in self.plant.sinks:
            return "sink"
        if not succ:
            return None
        if len(succ) > 1 and self.rng.random() < 0.4:
            succ.reverse()  # sometimes take the alternative route
        free = [s for s in succ if self._free(s)]
        return free[0] if free else succ[0]

    def step(self) -> None:
        cfg, dt = self.cfg, self.cfg.dt
        # spawn new pieces at external sources
        self.spawn_timer -= dt
        if self.spawn_timer <= 0 and len(self.pieces) < cfg.max_pieces:
            self.spawn_timer = cfg.spawn_every_s * self.rng.uniform(0.6, 1.4)
            sources = [c for c in self.plant.sources if self._free(c)]
            if sources:
                c = self.rng.choice(sources)
                self.spawned += 1
                self.pieces.append(SimPiece(c, cfg.take_in_s, ident=self.spawned))
                self.state[c] = 1
        # robot transfers
        for item in list(self.robot):
            remaining, dst, ident = item
            remaining -= dt
            self.robot.remove(item)
            if remaining <= 0 and self._free(dst):
                self.pieces.append(SimPiece(dst, cfg.take_in_s, ident=ident))
                self.state[dst] = 1
            else:
                self.robot.append((remaining, dst, ident))
        # pieces
        for p in list(self.pieces):
            p.timer -= dt
            st = self.state[p.conv]
            if st == 1 and p.timer <= 0:
                self.state[p.conv] = 2
                p.timer = cfg.travel_s
            elif st == 2 and p.timer <= 0:
                if self._tt(p.conv):
                    self.state[p.conv] = 5
                    p.timer = cfg.rotate_s
                else:
                    self.state[p.conv] = 3
                    p.timer = 0
                    if self.plant.is_exit(p.conv) and self.rng.random() < cfg.stuck_probability:
                        p.hold = cfg.stuck_hold_s
            elif st == 5 and p.timer <= 0:
                self.state[p.conv] = 3
            elif st == 3:
                if p.hold > 0:
                    p.hold -= dt
                    continue
                nxt = self._choose_next(p.conv)
                if nxt in ("robot", "sink"):
                    self.state[p.conv] = 0
                    self.pieces.remove(p)
                    if nxt == "robot":
                        link = self.plant.link_from(p.conv)
                        self.robot.append((cfg.robot_s, link.dst, p.ident))
                elif nxt is None:
                    if p.timer < -cfg.take_off_after_s:
                        self.state[p.conv] = 0
                        self.pieces.remove(p)
                elif self._free(nxt):
                    self.state[nxt] = 1
                    self.state[p.conv] = 4
                    p.target = nxt
                    p.timer = cfg.take_in_s
            elif st == 4 and p.timer <= 0 and p.target is not None:
                self.state[p.target] = 2
                self.state[p.conv] = 6 if self._tt(p.conv) else 0
                old = p.conv
                p.conv, p.target, p.timer = p.target, None, cfg.travel_s
                if self._tt(old):
                    self.rotating_back[old] = cfg.rotate_s
        # empty turntables rotating back (6 -> 0)
        for cid in list(self.rotating_back):
            self.rotating_back[cid] -= dt
            if self.rotating_back[cid] <= 0:
                del self.rotating_back[cid]
                self.state[cid] = 0
        # random lamp faults
        for pos in self.plant.cells:
            if pos in self.faults:
                self.faults[pos] -= dt
                if self.faults[pos] <= 0:
                    del self.faults[pos]
            elif self.rng.random() < cfg.fault_probability * dt:
                self.faults[pos] = cfg.fault_s

    def values(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for cid, st in self.state.items():
            out[k_state(*cid)] = st
            occupied = self._piece_on(cid) is not None
            out[k_sensor(*cid, 1)] = occupied and st in (1, 2)
            out[k_sensor(*cid, 2)] = occupied and st in (3, 4)
        for pos, cell in self.plant.cells.items():
            edge_states = [self.state[(pos, n)] for n in cell.edge.values()]
            states = [self.state[(pos, n)] for n in cell.conveyors]
            if pos in self.faults:
                lamp = 1
            elif any(s == 3 for s in edge_states):
                lamp = 2
            elif all(s == 0 for s in states):
                lamp = 3
            else:
                lamp = 4
            out[k_lamp(pos)] = lamp
            for port, n in cell.edge.items():
                st = self.state[(pos, n)]
                out[k_port(pos, port, "can_send")] = port in cell.output_ports and st in (3, 4)
                out[k_port(pos, port, "can_receive")] = port in cell.input_ports and st in (0, 1)
        return out

    async def run(self, speed: float = 1.0) -> None:
        log.info("demo simulator running (speed x%.1f)", speed)
        while True:
            self.step()
            await self.server.set_many(self.values())
            await asyncio.sleep(self.cfg.dt / speed)
