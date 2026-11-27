"""Several pieces moving at the same time along a long route, with invariants checked after every change."""

import heapq

from dmf.testing.sequences import Timing, route, take_off, travel
from dmf.tracker import events as ev
from dmf.tracker.engine import occupancy
from tests.conftest import Driver


def merged_timeline(streams: list[tuple[float, list]]) -> list[tuple[float, str, int]]:
    """Turn per-piece relative step lists into one absolute, time-ordered timeline."""
    heap = []
    for start, steps in streams:
        t = start
        for dt, key, value in steps:
            t += dt
            heapq.heappush(heap, (t, key, value))
    return [heapq.heappop(heap) for _ in range(len(heap))]


PLC_OCCUPIED = {2, 3, 4, 5}  # SCL state machines: a conveyor holds a piece in these states


def check_invariants(d: Driver) -> None:
    eng = d.engine
    seen: dict[str, tuple] = {}
    for cid, conv in eng.convs.items():
        if conv.piece is not None:
            assert conv.piece not in seen, f"{conv.piece} on {seen[conv.piece]} and {cid}"
            seen[conv.piece] = cid
            assert eng.pieces[conv.piece].conv == cid
            # a conveyor that holds a piece shows an occupied (or unknown) state and has not handed it on
            assert occupancy(conv.state) is not False and not conv.released, (cid, conv)
            assert conv.state in PLC_OCCUPIED, (cid, conv.state)  # all values are valid in this test
        if conv.released:  # "already handed on" only makes sense while the conveyor still shows the piece
            assert conv.piece is None and conv.state in PLC_OCCUPIED, (cid, conv)
    for code, p in eng.pieces.items():
        if p.conv is not None and p.departing_since is None:
            assert eng.convs[p.conv].piece == code


def test_three_pieces_follow_the_long_route(plant):
    d = Driver(plant, handover_window_s=5)
    d.sync()
    path = route(plant, (1, 1), cells=14, prefer={5: 2, 9: 3})  # through the robot at 11->12
    positions = []
    for c in path:
        if not positions or positions[-1] != c[0]:
            positions.append(c[0])
    assert positions[:6] == [1, 2, 3, 4, 5, 11] and 12 in positions
    steps = list(travel(plant, path, Timing())) + [(3.0, *take_off(path[-1]).__next__()[1:])]
    total = sum(s[0] for s in steps)
    timeline = merged_timeline([(0, steps), (25, steps), (50, steps)])
    t0 = d.t
    for t, key, value in timeline:
        d.events += d.engine.on_signal(key, value, True, t0 + t)
        d.events += d.engine.on_tick(t0 + t)
        check_invariants(d)
    d.t = t0 + 50 + total + 10
    d.events += d.engine.on_tick(d.t)
    codes = [e.code for e in d.of(ev.PieceAppeared)]
    assert codes == ["P-0001", "P-0002", "P-0003"]
    for code in codes:
        entered = [e.pos for e in d.of(ev.CellEntered) if e.code == code]
        assert entered == positions, (code, entered)
    ended = d.of(ev.PieceEnded)
    assert [(e.code, e.status, e.pos) for e in ended] == [(c, "removed", path[-1][0]) for c in codes]
    assert d.engine.pieces == {}
    assert not [e for e in d.of(ev.CellEntered) if e.uncertain]
