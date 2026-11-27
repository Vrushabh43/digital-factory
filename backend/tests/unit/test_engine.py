"""Scenario tests for the tracking engine (pure, no I/O)."""

import pytest

from dmf.plant.model import k_lamp, k_port, k_sensor, k_state
from dmf.testing.sequences import handover, put_on, route, take_off, travel
from dmf.tracker import events as ev
from dmf.tracker.engine import EMPTY, OCCUPIED, TrackerEngine, occupancy
from tests.conftest import Driver


def cells_entered(d: Driver, code: str) -> list[int]:
    return [e.pos for e in d.of(ev.CellEntered) if e.code == code]


# ------------------------------------------------------------------ start-up
def test_startup_numbers_pieces_found_on_belts(plant):
    d = Driver(plant)
    d.sync({k_state(4, 2): 3, k_state(9, 2): 5})
    appeared = d.of(ev.PieceAppeared)
    assert [(e.pos, e.conveyor, e.origin) for e in appeared] == [(4, 2, "startup"), (9, 2, "startup")]
    assert [e.code for e in appeared] == ["P-0001", "P-0002"]
    assert all(not e.uncertain for e in appeared)
    statuses = {e.pos: e.status for e in d.of(ev.CellStatusChanged)}
    assert set(statuses.values()) == {"running"} and len(statuses) == 18


def test_numbering_continues_from_given_sequence(plant):
    d = Driver(plant, next_seq=151)
    d.sync({k_state(1, 1): 2})
    assert d.of(ev.PieceAppeared)[0].code == "P-0151"


# ------------------------------------------------------------------ appear / move / leave
def test_piece_from_storage_flows_through_merger_into_next_cell(driver, plant):
    path = route(plant, (1, 1), cells=2)  # pos1 -> pos2 (merger via port 3) -> pos3
    assert path[:4] == [(1, 1), (1, 2), (1, 3), (2, 5)]
    driver.run(travel(plant, path))
    (appeared,) = driver.of(ev.PieceAppeared)
    assert appeared.origin == "source:High-bay storage" and appeared.code == "P-0001"
    assert cells_entered(driver, "P-0001") == [1, 2, 3]
    left = driver.of(ev.CellLeft)
    assert [(e.pos, e.result, e.to_pos) for e in left] == [(1, "passed", 2), (2, "passed", 3)]
    moves = [(e.pos, e.from_conveyor, e.to_conveyor) for e in driver.of(ev.ConveyorChanged)]
    assert moves[:2] == [(1, 1, 2), (1, 2, 3)]
    assert (2, 5, 2) in moves and (2, 2, 3) in moves and (2, 3, 4) in moves
    entered_pos2 = next(e for e in driver.of(ev.CellEntered) if e.pos == 2)
    assert entered_pos2.came_from_pos == 1 and entered_pos2.came_from_cell == 1
    assert driver.piece_at(*path[-1]) == "P-0001"
    assert len(driver.engine.pieces) == 1


def test_departure_before_arrival_is_paired(driver, plant):
    path = route(plant, (1, 1), cells=1)
    driver.run(travel(plant, path, arrival_first=False))
    assert cells_entered(driver, "P-0001") == [1, 2]
    assert not driver.of(ev.PieceEnded) and len(driver.engine.pieces) == 1


def test_duplicate_notifications_are_ignored(driver, plant):
    driver.run(put_on(plant, (4, 1)))
    before = len(driver.events)
    driver.set(k_state(4, 1), 3)
    driver.set(k_state(4, 1), 3)
    assert len(driver.events) == before
    assert len(driver.engine.pieces) == 1


def test_exit_reached_reported_once(driver, plant):
    driver.run(travel(plant, [(4, 1), (4, 2), (4, 3)]))
    exits = driver.of(ev.ExitReached)
    assert [(e.code, e.pos, e.conveyor) for e in exits] == [("P-0001", 4, 3)]


def test_piece_put_on_mid_line_gets_new_number_immediately(driver, plant):
    driver.run(put_on(plant, (4, 2)))
    (appeared,) = driver.of(ev.PieceAppeared)
    assert appeared.origin == "put_on" and appeared.conveyor == 2


def test_put_on_while_predecessor_busy_waits_for_window(driver, plant):
    # P-0001 is still travelling on conveyor 1 (state 2, not yet at the end sensor)
    driver.run([(0.1, k_state(4, 1), 1), (1.0, k_state(4, 1), 2)])
    # a piece put by hand on conveyor 2 must not be taken for P-0001 straight away
    driver.set(k_state(4, 2), 1)
    driver.set(k_state(4, 2), 2)
    assert driver.piece_at(4, 2) is None  # waiting: could still be a hand-over
    driver.tick(6)
    assert driver.piece_at(4, 2) == "P-0002"
    assert driver.piece_at(4, 1) == "P-0001"


def test_taken_off_at_end_sensor(driver, plant):
    driver.run(put_on(plant, (4, 3)))
    driver.run(take_off((4, 3)))
    assert not driver.of(ev.PieceEnded)
    driver.tick(6)
    (ended,) = driver.of(ev.PieceEnded)
    assert ended.status == "removed" and ended.pos == 4
    assert [e.result for e in driver.of(ev.CellLeft)] == ["removed"]
    assert driver.engine.pieces == {}


def test_storage_sink_at_splitter_target(driver, plant):
    # position 9 (cell 8) splitter: target port 2 = storage
    path = route(plant, (9, 1), cells=0) + [(9, 2), (9, 3), (9, 4)]
    driver.run(travel(plant, path[:1]))
    for a, b in zip(path, path[1:]):
        driver.run(handover(plant, a, b))
    driver.set(k_state(9, 4), 4)
    driver.set(k_state(9, 4), 0)
    driver.tick(6)
    (ended,) = driver.of(ev.PieceEnded)
    assert ended.status == "stored"


def test_robot_transfer_keeps_number(driver, plant):
    path = route(plant, (11, 1), cells=1)
    assert path[-1] == (12, 1)
    driver.run(put_on(plant, (11, 1)))
    driver.run(handover(plant, (11, 1), (11, 2)))
    driver.run(handover(plant, (11, 2), (11, 3)))
    driver.run(take_off((11, 3)))  # robot picks
    driver.tick(6)  # window expires -> with robot
    assert driver.of(ev.CellLeft)[-1].result == "to_robot"
    driver.run(put_on(plant, (12, 1)))  # robot places the piece
    entered = driver.of(ev.CellEntered)[-1]
    assert (entered.code, entered.pos, entered.came_from_pos, entered.via) == ("P-0001", 12, 11, "Robot")
    assert len(driver.of(ev.PieceAppeared)) == 1


def test_fast_robot_within_window(driver, plant):
    driver.run(put_on(plant, (11, 3)))
    driver.run(take_off((11, 3)))
    driver.run(put_on(plant, (12, 1)))  # within the window
    entered = driver.of(ev.CellEntered)[-1]
    assert (entered.code, entered.pos, entered.via) == ("P-0001", 12, "Robot")
    assert driver.of(ev.CellLeft)[-1].result == "to_robot"


def test_robot_transit_timeout_marks_piece_lost(driver, plant):
    driver.run(put_on(plant, (11, 3)))
    driver.run(take_off((11, 3)))
    driver.tick(6)
    driver.tick(400)
    assert [e.status for e in driver.of(ev.PieceEnded)] == ["lost"]


# ------------------------------------------------------------------ stuck
def test_stuck_at_exit_and_recovery(driver, plant):
    driver.run(travel(plant, [(4, 1), (4, 2), (4, 3)]))
    driver.set(k_lamp(4), 2)
    driver.tick(30)
    assert not driver.of(ev.StuckChanged)
    driver.tick(31)
    (stuck,) = driver.of(ev.StuckChanged)
    assert stuck.stuck and stuck.code == "P-0001" and stuck.pos == 4
    assert driver.engine.status_of(4).status == "stuck"
    driver.run(handover(plant, (4, 3), (5, 1)))
    unstuck = driver.of(ev.StuckChanged)[-1]
    assert not unstuck.stuck
    assert driver.engine.status_of(4).status == "running"


def test_turntable_waiting_for_route_counts_as_stuck(driver, plant):
    driver.run(put_on(plant, (5, 1)))
    driver.run(
        [
            (0.1, k_state(5, 2), 1),
            (0.1, k_state(5, 1), 4),
            (1.0, k_state(5, 2), 2),
            (0.1, k_state(5, 1), 0),
            (1.5, k_state(5, 2), 5),
        ]
    )
    assert driver.piece_at(5, 2) == "P-0001"
    driver.tick(61)
    assert driver.of(ev.StuckChanged)[0].stuck


# ------------------------------------------------------------------ merger ambiguity
def test_missed_handing_state_pairs_with_the_input_that_left(driver, plant):
    # position 3 merger: primary = port 3 (conveyor 5), secondary = port 1 (conveyor 1)
    driver.run(put_on(plant, (3, 1)))  # P-0001 secondary
    driver.run(put_on(plant, (3, 5)))  # P-0002 primary
    # the turntable takes a piece, but state 4 of the feeding conveyor was never seen
    driver.set(k_state(3, 2), 1)
    driver.set(k_state(3, 2), 2)
    assert driver.piece_at(3, 2) is None  # both inputs still wait: no guess yet
    driver.set(k_state(3, 1), 0, dt=0.5)  # the SECONDARY input reports 3 -> 0
    assert driver.piece_at(3, 2) == "P-0001" and driver.piece_at(3, 5) == "P-0002"
    p = driver.engine.pieces["P-0001"]
    assert p.uncertain and p.uncertain_reason == "hand-over seen without the handing-over state"
    assert len(driver.of(ev.PieceAppeared)) == 2


def test_merger_prefers_primary_port_when_both_inputs_hand_over(driver, plant):
    driver.run(put_on(plant, (3, 1)))  # P-0001 secondary
    driver.run(put_on(plant, (3, 5)))  # P-0002 primary
    driver.set(k_state(3, 1), 4)
    driver.set(k_state(3, 5), 4)
    driver.set(k_state(3, 2), 1)
    driver.set(k_state(3, 2), 2)
    assert driver.piece_at(3, 2) == "P-0002"
    assert driver.engine.pieces["P-0002"].uncertain is False  # different ports: priority decides


def test_piece_put_on_next_to_a_waiting_piece_gets_its_own_number(driver, plant):
    # P-0001 waits at the exit of (4,1) because the operator holds its successor (4,2)
    # busy; then a piece is put straight onto (4,2). It must get a number of its own.
    driver.run(put_on(plant, (4, 1)))
    assert driver.engine.convs[(4, 1)].state == 3
    driver.set(k_state(4, 2), 2)
    driver.tick(6)  # longer than the hand-over window
    assert driver.piece_at(4, 1) == "P-0001"
    assert driver.piece_at(4, 2) == "P-0002"
    assert [e.origin for e in driver.of(ev.PieceAppeared)] == ["put_on", "put_on"]


# ------------------------------------------------------------------ cell status
def test_lamp_error_and_plant_enable(driver):
    driver.set(k_lamp(7), 1)
    assert driver.engine.status_of(7).status == "error"
    driver.set("general.enable", False)
    assert driver.engine.status_of(7).reason == "plant_not_enabled"
    assert any(isinstance(e, ev.PlantFlagChanged) and e.flag == "enable" for e in driver.events)


def test_bad_quality_lamp_is_offline(driver):
    driver.set(k_lamp(6), 0, good=False)
    assert driver.engine.status_of(6).status == "offline"


def test_bad_quality_state_keeps_piece(driver, plant):
    driver.run(put_on(plant, (4, 2)))
    driver.set(k_state(4, 2), 0, good=False)
    assert driver.piece_at(4, 2) == "P-0001"
    driver.set(k_state(4, 2), 3)
    assert driver.piece_at(4, 2) == "P-0001"
    assert len(driver.of(ev.PieceAppeared)) == 1


def test_unexpected_state_value_is_reported(driver, plant):
    driver.set(k_state(4, 1), 9)
    assert any(isinstance(e, ev.Anomaly) for e in driver.events)
    (anomaly,) = driver.of(ev.Anomaly)
    assert (anomaly.pos, anomaly.text) == (4, "Unexpected state value 9 on conveyor 1")
    assert driver.engine.convs[(4, 1)].state is None  # unknown, not "empty"
    assert driver.engine.status_of(4).reason == "partial_data"
    # with a piece on the conveyor: the invalid value must neither move nor end it
    driver.set(k_state(4, 1), 0)
    driver.run(put_on(plant, (4, 2)))
    code = driver.piece_at(4, 2)
    out = driver.set(k_state(4, 2), 7)
    assert [(e.pos, e.text) for e in out if isinstance(e, ev.Anomaly)] == [(4, "Unexpected state value 7 on conveyor 2")]
    assert driver.engine.convs[(4, 2)].state is None
    driver.tick(6)  # longer than the hand-over window
    assert driver.piece_at(4, 2) == code and driver.engine.pieces[code].conv == (4, 2)
    assert driver.engine.departures == {} and not driver.of(ev.PieceEnded) and not driver.of(ev.CellLeft)
    driver.set(k_state(4, 2), 3)  # valid data again: still the same piece, no new one
    assert driver.piece_at(4, 2) == code and len(driver.of(ev.PieceAppeared)) == 1


def test_sensor_crosscheck(driver):
    driver.set(k_sensor(4, 1, 1), True)
    driver.tick(1)
    driver.tick(6)
    assert any(isinstance(e, ev.Anomaly) and "sensor" in e.text for e in driver.events)


# ------------------------------------------------------------------ connection loss / restart
def test_connection_loss_and_resync_moves_pieces_uncertain(driver, plant):
    driver.run(put_on(plant, (4, 1)))  # P-0001 on pos4 conv1
    driver.run(put_on(plant, (8, 3)))  # P-0002 on pos8 conv3
    driver.engine.on_connection_lost(driver.t, "test")
    assert all(driver.engine.status_of(p).status == "offline" for p in plant.cells)
    driver.t += 30
    # during the gap: P-0001 moved to conveyor 3 of pos 4; P-0002 vanished (taken off)
    out = driver.engine.on_resync(
        __import__("tests.conftest", fromlist=["idle_values"]).idle_values(plant, {k_state(4, 3): 3}), driver.t
    )
    assert driver.piece_at(4, 3) == "P-0001"
    assert driver.engine.pieces["P-0001"].uncertain
    ended = [e for e in out if isinstance(e, ev.PieceEnded)]
    assert [(e.code, e.status) for e in ended] == [("P-0002", "lost")]
    assert driver.engine.status_of(4).status == "running"


def test_resync_without_changes_keeps_pieces_certain(driver, plant):
    driver.run(put_on(plant, (4, 2)))
    driver.engine.on_connection_lost(driver.t, "test")
    driver.t += 2  # shorter than the window
    from tests.conftest import idle_values

    driver.engine.on_resync(idle_values(plant, {k_state(4, 2): 3}), driver.t)
    assert driver.piece_at(4, 2) == "P-0001"
    assert not driver.engine.pieces["P-0001"].uncertain


def test_restart_restores_snapshot_and_continues_numbering(driver, plant):
    from tests.conftest import idle_values

    driver.run(put_on(plant, (4, 2)))
    driver.run(put_on(plant, (12, 3)))
    snap = driver.engine.export_state()
    saved_at = driver.t
    fresh = TrackerEngine(plant, handover_window_s=5, stuck_after_s=60)
    fresh.restore_state(snap, saved_at)
    out = fresh.on_resync(idle_values(plant, {k_state(4, 2): 3, k_state(12, 3): 3, k_state(1, 1): 2}), saved_at + 600)
    assert fresh.convs[(4, 2)].piece == "P-0001" and fresh.convs[(12, 3)].piece == "P-0002"
    new = [e for e in out if isinstance(e, ev.PieceAppeared)]
    assert [(e.code, e.origin) for e in new] == [("P-0003", "found")]


def test_snapshot_round_trip_preserves_robot_queue(driver, plant):
    driver.run(put_on(plant, (11, 3)))
    driver.run(take_off((11, 3)))
    driver.tick(6)
    snap = driver.engine.export_state()
    fresh = TrackerEngine(plant, handover_window_s=5)
    fresh.restore_state(snap, driver.t)
    assert list(fresh.robot_queues["robot-11-12"])[0][0] == "P-0001"


# ------------------------------------------------------------------ stuck: exact limit and timer
def _wait_at_exit_at_exact_time(d: Driver, plant, cid, t3: float) -> str:
    """Put a piece on ``cid`` (an exit conveyor); state 3 is reached exactly at ``t3``."""
    *steps, (_dt, key, value) = list(put_on(plant, cid))
    d.run(steps)
    assert value == 3 and t3 > d.t
    d.t = t3  # binary-exact times, so that "limit" really means the limit
    d.set(key, value, dt=0)
    return d.piece_at(*cid)


@pytest.mark.parametrize("limit", [60.0, 45.0])
def test_stuck_exactly_at_the_configured_limit_since_the_exit_was_reached(plant, limit):
    d = Driver(plant, stuck_after_s=limit)
    d.sync()
    t3 = 1_000_100.0
    code = _wait_at_exit_at_exact_time(d, plant, (4, 3), t3)
    assert d.engine.pieces[code].waiting_since == t3
    out = d.engine.on_tick(t3 + limit - 0.1)
    assert not [e for e in out if isinstance(e, ev.StuckChanged)] and not d.engine.pieces[code].stuck
    assert d.engine.status_of(4).status == "running"
    out = d.engine.on_tick(t3 + limit)
    (stuck,) = [e for e in out if isinstance(e, ev.StuckChanged)]
    assert (stuck.code, stuck.pos, stuck.stuck, stuck.since, stuck.ts) == (code, 4, True, t3, t3 + limit)
    p = d.engine.pieces[code]
    assert p.stuck and p.stuck_since == t3 and p.was_stuck
    assert d.engine.status_of(4).status == "stuck" and d.engine.stuck_cells() == [4]
    assert not [e for e in d.engine.on_tick(t3 + limit + 5) if isinstance(e, ev.StuckChanged)]  # reported once


@pytest.mark.parametrize("cid", [(4, 1), (4, 2), (5, 3)])  # linear entry, linear middle, cross-cell middle
def test_a_piece_in_state_3_on_a_conveyor_that_is_not_an_exit_never_becomes_stuck(plant, cid):
    d = Driver(plant, stuck_after_s=60.0)
    d.sync()
    d.run(put_on(plant, cid))
    code = d.piece_at(*cid)
    assert code is not None and d.engine.convs[cid].state == 3 and not plant.is_exit(cid)
    assert d.engine.pieces[code].waiting_since is None
    for _ in range(5):
        d.tick(60)
    assert not d.of(ev.StuckChanged) and not d.engine.pieces[code].stuck
    assert d.engine.status_of(cid[0]).status == "running"


def test_waiting_timer_is_not_restarted_by_repeated_or_unrelated_notifications(plant):
    d = Driver(plant, stuck_after_s=60.0)
    d.sync()
    t3 = 1_000_100.0
    code = _wait_at_exit_at_exact_time(d, plant, (4, 3), t3)
    d.set(k_state(4, 3), 3, dt=5)  # duplicate
    d.set(k_lamp(4), 2, dt=5)  # the PLC shows "waiting"
    d.set(k_port(4, 2, "can_send"), True)
    d.run(put_on(plant, (6, 1)))  # activity elsewhere
    d.set(k_state(4, 3), 3, dt=1, good=False)  # Bad-quality blip on the waiting conveyor itself
    d.set(k_state(4, 3), 3, dt=1)
    d.tick(1)
    assert d.engine.pieces[code].waiting_since == t3
    d.events += d.engine.on_connection_lost(d.t, "blip")  # short reconnect (shorter than the window)
    d.sync({k_state(4, 3): 3, k_state(6, 1): 3, k_lamp(4): 2}, dt=1)
    assert d.engine.pieces[code].waiting_since == t3 and d.piece_at(4, 3) == code
    assert d.t < t3 + 60
    out = d.engine.on_tick(t3 + 60.0)
    assert [(e.code, e.stuck, e.since) for e in out if isinstance(e, ev.StuckChanged)] == [(code, True, t3)]


# ------------------------------------------------------------------ reconnect: reconciliation
def test_reconnect_with_two_vanished_pieces_keeps_their_order(plant):
    # B waits on (3,5) (merger input from position 10), A on (10,3) right behind it. While
    # the connection is down both move on: A onto the turntable (3,2), B further to (3,3).
    # Pieces cannot overtake each other, so B must take the downstream conveyor - although
    # (3,5) comes before (10,3) in the conveyor list.
    d = Driver(plant)
    d.sync()
    d.run(put_on(plant, (3, 5)))
    d.run(put_on(plant, (10, 3)))
    d.tick(6)
    b, a = d.piece_at(3, 5), d.piece_at(10, 3)
    assert (b, a) == ("P-0001", "P-0002")
    d.events += d.engine.on_connection_lost(d.t, "cable unplugged")
    out = d.sync({k_state(3, 2): 5, k_state(3, 3): 3}, dt=30)  # (10,3) and (3,5) are empty now
    assert d.piece_at(3, 2) == a and d.piece_at(3, 3) == b
    assert d.piece_at(3, 5) is None and d.piece_at(10, 3) is None
    for code in (a, b):
        p = d.engine.pieces[code]
        assert p.uncertain and p.uncertain_reason == "moved while no data was received"
    assert not [e for e in out if isinstance(e, (ev.PieceAppeared, ev.PieceEnded))]
    assert [(e.code, e.pos, e.result, e.to_pos) for e in out if isinstance(e, ev.CellLeft)] == [(a, 10, "passed", 3)]
    assert [(e.code, e.pos, e.came_from_pos) for e in out if isinstance(e, ev.CellEntered)] == [(a, 3, 10)]
    assert [(e.code, e.from_conveyor, e.to_conveyor) for e in out if isinstance(e, ev.ConveyorChanged)] == [(b, 5, 3)]


@pytest.mark.parametrize(
    "gap,state_after,uncertain",
    [
        (30.0, 2, True),  # long gap and the state changed: it may be another piece now
        (30.0, 3, False),  # long gap, same state: still the same piece
        (2.0, 2, False),  # gap shorter than the window: nothing could have happened
    ],
)
def test_resync_marks_a_piece_uncertain_only_after_a_long_gap_with_a_state_change(plant, gap, state_after, uncertain):
    d = Driver(plant)
    d.sync()
    d.run(put_on(plant, (4, 2)))
    code = d.piece_at(4, 2)
    d.events += d.engine.on_connection_lost(d.t, "test")
    out = d.sync({k_state(4, 2): state_after}, dt=gap)
    assert d.piece_at(4, 2) == code and len(d.engine.pieces) == 1
    p = d.engine.pieces[code]
    assert p.uncertain is uncertain
    assert p.uncertain_reason == ("position re-checked after a data gap" if uncertain else None)
    assert not [e for e in out if isinstance(e, (ev.PieceAppeared, ev.PieceEnded, ev.CellLeft))]


# ------------------------------------------------------------------ turntable state 6
def test_turntable_state_6_counts_as_empty(plant):
    """Deliberate deviation from the project description (p. 4: "5, 6 on a turntable hold a
    piece"): the PLC SCL code enters 6 AFTER the piece has left (4 -> 6 -> 0) and uses 6 as the
    start state, see docs/01-project-analysis.md section 1.2 / 1.3 (I-1). Changing this rule
    must be a deliberate decision, so it is pinned here."""
    assert occupancy(6) is False and 6 in EMPTY and 6 not in OCCUPIED
    assert OCCUPIED == {2, 3, 4, 5} and EMPTY == {0, 1, 6}
    # start state after a PLC start: turntables in 6 hold no piece
    d = Driver(plant)
    d.sync({k_state(3, 2): 6, k_state(5, 2): 6})
    assert not d.of(ev.PieceAppeared)
    # a turntable hands a piece over: 4 -> 6 is the departure (the successor reports later)
    d.run(put_on(plant, (3, 1)))
    d.run(handover(plant, (3, 1), (3, 2)))
    code = d.piece_at(3, 2)
    assert code == "P-0001" and d.engine.convs[(3, 2)].state == 3
    d.set(k_state(3, 3), 1)
    d.set(k_state(3, 2), 4)
    d.set(k_state(3, 2), 6)
    assert d.piece_at(3, 2) is None
    dep = d.engine.departures[(3, 2)]
    assert (dep.code, dep.from_state) == (code, 4)
    d.set(k_state(3, 3), 2)  # arrival paired with the departure from state 4: a certain hand-over
    assert d.piece_at(3, 3) == code and not d.engine.pieces[code].uncertain
    d.set(k_state(3, 2), 0, dt=1.2)  # rotating back finished: nothing happens
    d.tick(6)
    assert d.piece_at(3, 2) is None and d.piece_at(3, 3) == code
    assert len(d.of(ev.PieceAppeared)) == 1 and not d.of(ev.PieceEnded)
