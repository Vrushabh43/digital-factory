"""PLC-faithful conveyor state sequences for tests and the mock OPC UA server.

These helpers reproduce the state machines of the PLC program (SCL code in the TIA
project) closely enough to exercise the tracker: a hand-over between two conveyors,
putting a piece on, taking a piece off. They are *test tooling*, not a plant
simulator for production use.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from dmf.plant.model import ConvId, Plant, k_state

Step = tuple[float, str, int]  # (delay before the change in seconds, signal key, new state)


@dataclass(frozen=True)
class Timing:
    take_in_s: float = 1.0  # start sensor -> piece fully on the conveyor
    travel_s: float = 1.5  # piece travels to the end sensor
    rotate_s: float = 1.2  # turntable rotation
    react_s: float = 0.1  # PLC reaction / OPC propagation


def _is_tt(plant: Plant, cid: ConvId) -> bool:
    return plant.conveyor(cid).turntable


def arrive_and_travel(plant: Plant, cid: ConvId, timing: Timing = Timing()) -> Iterator[Step]:
    """After a piece was taken in (state 2): move to the end sensor and get ready (state 3)."""
    if _is_tt(plant, cid):
        yield timing.travel_s, k_state(*cid), 5
        yield timing.rotate_s, k_state(*cid), 3
    else:
        yield timing.travel_s, k_state(*cid), 3


def put_on(plant: Plant, cid: ConvId, timing: Timing = Timing()) -> Iterator[Step]:
    """A person/robot puts a piece on the start sensor of an empty conveyor."""
    yield timing.react_s, k_state(*cid), 1
    yield timing.take_in_s, k_state(*cid), 2
    yield from arrive_and_travel(plant, cid, timing)


def handover(plant: Plant, src: ConvId, dst: ConvId, timing: Timing = Timing(), arrival_first: bool = True) -> Iterator[Step]:
    """Hand a piece from ``src`` (state 3) to ``dst`` (state 0). Ends with dst in state 3."""
    yield timing.react_s, k_state(*dst), 1
    yield timing.react_s, k_state(*src), 4
    release = [(timing.react_s, k_state(*src), 6 if _is_tt(plant, src) else 0)]
    if _is_tt(plant, src):
        release.append((timing.rotate_s, k_state(*src), 0))
    take = [(timing.take_in_s, k_state(*dst), 2)]
    if arrival_first:
        yield from take
        yield from release
    else:
        yield timing.take_in_s, release[0][1], release[0][2]
        yield timing.react_s, take[0][1], take[0][2]
        yield from release[1:]
    yield from arrive_and_travel(plant, dst, timing)


def take_off(cid: ConvId, timing: Timing = Timing()) -> Iterator[Step]:
    """A person/robot removes the piece waiting at the end sensor (3 -> 0)."""
    yield timing.react_s, k_state(*cid), 0


def route(plant: Plant, start: ConvId, cells: int, prefer: dict[int, int] | None = None) -> list[ConvId]:
    """Conveyors a piece passes from ``start`` until it has entered ``cells`` further cells.

    ``prefer`` maps a position to the output port to use at a splitter (default: first successor).
    Robot links are followed as well.
    """
    path = [start]
    entered = 0
    cur = start
    while entered < cells:
        succs = list(plant.successors.get(cur, []))
        link = plant.link_from(cur)
        if link:
            succs.append(link.dst)
        if not succs:
            break
        nxt = succs[0]
        if prefer and cur[0] in prefer and len(succs) > 1:
            port = prefer[cur[0]]
            want = plant.cells[cur[0]].edge[port]
            nxt = next((s for s in succs if s[0] == cur[0] and _leads_to_port(plant, s, want)), nxt)
        if nxt[0] != cur[0]:
            entered += 1
        path.append(nxt)
        cur = nxt
    return path


def _leads_to_port(plant: Plant, cid: ConvId, edge_conv: int) -> bool:
    seen = set()
    stack = [cid]
    while stack:
        c = stack.pop()
        if c[1] == edge_conv:
            return True
        seen.add(c)
        stack.extend(s for s in plant.successors.get(c, []) if s[0] == cid[0] and s not in seen)
    return False


def travel(plant: Plant, path: list[ConvId], timing: Timing = Timing(), arrival_first: bool = True) -> Iterator[Step]:
    """Full sequence: put a piece on ``path[0]`` and hand it along the path (robot links included)."""
    yield from put_on(plant, path[0], timing)
    for a, b in zip(path, path[1:], strict=False):
        link = plant.link_from(a)
        if link is not None and link.dst == b:
            yield from take_off(a, timing)  # robot picks the piece
            yield 2.0, k_state(*b), 1  # robot places it on the next cell
            yield timing.take_in_s, k_state(*b), 2
            yield from arrive_and_travel(plant, b, timing)
        else:
            yield from handover(plant, a, b, timing, arrival_first)
