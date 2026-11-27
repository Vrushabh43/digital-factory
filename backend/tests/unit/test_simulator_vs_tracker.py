"""The demo simulator's output must be something the tracker can follow without anomalies."""

from collections import Counter

from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine
from dmf_mock.simulator import DemoSimulator, SimConfig
from tests.conftest import idle_values


class _NoServer:
    async def set_many(self, values):  # pragma: no cover - not used
        pass


def test_tracker_follows_thirty_simulated_minutes(plant):
    cfg = SimConfig(stuck_probability=0.05, stuck_hold_s=130, fault_probability=0.001)
    sim = DemoSimulator(plant, _NoServer(), cfg, seed=7)
    stuck_after = 120.0
    eng = TrackerEngine(plant, handover_window_s=5, stuck_after_s=stuck_after)
    t = 0.0
    events = list(eng.on_resync(idle_values(plant), t))
    last = sim.values()
    codes: dict[int, str] = {}  # simulator piece identity -> tracker number
    reached_3: dict[tuple[int, int], float] = {}  # conveyor -> time it last switched to state 3
    identity_checks = 0
    stuck_checks = 0
    for _ in range(int(30 * 60 / cfg.dt)):
        sim.step()
        t += cfg.dt
        cur = sim.values()
        out: list[ev.Event] = []
        for key, value in cur.items():
            if last.get(key) != value:
                out += eng.on_signal(key, value, True, t)
        last = cur
        out += eng.on_tick(t)
        events += out
        for cid, st in sim.state.items():
            if st == 3 and cid not in reached_3:
                reached_3[cid] = t
            elif st != 3:
                reached_3.pop(cid, None)
        for e in out:
            if isinstance(e, ev.PieceAppeared):  # bind the new number to the simulator piece on that conveyor
                (sp,) = [p for p in sim.pieces if p.conv == (e.pos, e.conveyor)]
                assert sp.ident not in codes, (sp.ident, e.code)
                codes[sp.ident] = e.code
            elif isinstance(e, ev.StuckChanged) and e.stuck:
                piece = eng.pieces[e.code]
                assert plant.is_exit(piece.conv), e
                assert e.since == reached_3[piece.conv], e  # waiting started when the exit was reached
                assert stuck_after <= e.ts - e.since < stuck_after + cfg.dt + 1e-6, e
        # the tracker's number on a conveyor is always the number of the simulator's piece there
        for sp in sim.pieces:
            code = eng.convs[sp.conv].piece
            if code is not None and sim.state[sp.conv] in (2, 3, 5):
                assert code == codes[sp.ident], (t, sp, code)
                identity_checks += 1
                waited = t - reached_3.get(sp.conv, t)
                if plant.is_exit(sp.conv) and waited >= stuck_after + cfg.dt:
                    assert eng.pieces[code].stuck, (t, sp, code)
                    stuck_checks += 1
    kinds = Counter(e.kind for e in events)
    ended = Counter(e.status for e in events if isinstance(e, ev.PieceEnded))
    anomalies = [e.message for e in events if isinstance(e, ev.Anomaly)]
    assert kinds["piece_appeared"] >= 20
    assert kinds["cell_entered"] > 5 * kinds["piece_appeared"]
    assert ended["lost"] == 0, anomalies
    assert all(e.origin.startswith(("source:", "robot:")) for e in events if isinstance(e, ev.PieceAppeared)), Counter(
        e.origin for e in events if isinstance(e, ev.PieceAppeared)
    )
    assert not anomalies
    assert any(isinstance(e, ev.StuckChanged) and e.stuck for e in events)
    # every piece the simulator still has is known to the tracker on the same conveyor
    assert {p.conv for p in sim.pieces} == {c for c, r in eng.convs.items() if r.piece}
    # ... under the same number: compare piece numbers per conveyor, not just occupied conveyors
    assert {p.conv: codes[p.ident] for p in sim.pieces} == {c: r.piece for c, r in eng.convs.items() if r.piece}
    # pieces with the robot (or just picked up by it) in the same order as in the simulator
    with_robot = {lk.dst: [codes[i] for _, dst, i in sim.robot if dst == lk.dst] for lk in plant.robot_links}
    tracked = {
        lk.dst: [c for c, _ in eng.robot_queues[lk.id]] + ([eng.departures[lk.src].code] if lk.src in eng.departures else [])
        for lk in plant.robot_links
    }
    assert with_robot == tracked
    # every simulator piece got exactly one number (no renumbering after robot transfers)
    assert len(codes) == sim.spawned == kinds["piece_appeared"]
    assert identity_checks > 5000 and stuck_checks > 0, (identity_checks, stuck_checks)
