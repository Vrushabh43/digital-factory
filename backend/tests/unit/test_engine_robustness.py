"""Tracker engine: hand-overs that are on hold, data gaps, restarts and invalid values.

Each test reproduces a failure found in the project audit (docs/09-final-project-audit.md)
and fails on the code before the fix.
"""

from __future__ import annotations

from dmf.plant.model import k_lamp, k_state
from dmf.testing.sequences import put_on
from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine
from tests.conftest import Driver, idle_values


def _waiting_at_exit_of_4_1(d: Driver, plant) -> str:
    d.sync()
    d.run(put_on(plant, (4, 1)))
    assert d.engine.convs[(4, 1)].state == 3
    return d.piece_at(4, 1)


def _depart_4_1_into_slow_take_in(d: Driver) -> None:
    d.set(k_state(4, 2), 1)  # successor accepts and starts taking the piece in
    d.set(k_state(4, 1), 4)
    d.set(k_state(4, 1), 0)  # piece left the end sensor of (4,1)


def _ended(d: Driver) -> list[ev.PieceEnded]:
    return d.of(ev.PieceEnded)


# --------------------------------------------------------------- hand-over on hold
def test_slow_take_in_does_not_remove_the_piece(plant):
    d = Driver(plant)  # window 5 s
    code = _waiting_at_exit_of_4_1(d, plant)
    _depart_4_1_into_slow_take_in(d)
    for _ in range(20):  # 20 s in state 1 (window is 5 s)
        d.tick(1)
    assert _ended(d) == []
    d.set(k_state(4, 2), 2)
    assert d.piece_at(4, 2) == code and len(d.of(ev.PieceAppeared)) == 1


def test_hand_over_interrupted_by_a_cell_error_is_completed_afterwards(plant):
    d = Driver(plant)
    code = _waiting_at_exit_of_4_1(d, plant)
    d.set(k_state(4, 1), 4)
    d.set(k_state(4, 1), 0)  # departure, successor not yet reacting
    d.set(k_lamp(4), 1)  # emergency stop / error in the cell: nothing moves
    for _ in range(30):
        d.tick(1)
    assert _ended(d) == []
    d.set(k_lamp(4), 3)
    d.set(k_state(4, 2), 1)
    d.set(k_state(4, 2), 2)
    assert d.piece_at(4, 2) == code and _ended(d) == []


def test_departure_without_arrival_still_ends_after_the_window(plant):
    d = Driver(plant)
    code = _waiting_at_exit_of_4_1(d, plant)
    d.set(k_state(4, 1), 4)
    d.set(k_state(4, 1), 0)
    d.tick(6)
    assert [(e.code, e.status) for e in _ended(d)] == [(code, "removed")]


def test_taking_in_does_not_hold_forever(plant):
    d = Driver(plant, stuck_after_s=30)
    _waiting_at_exit_of_4_1(d, plant)
    _depart_4_1_into_slow_take_in(d)
    for _ in range(40):
        d.tick(1)
    assert len(_ended(d)) == 1  # the successor never took it: give up after stuck_after_s


# --------------------------------------------------------------- data gaps & restarts
def test_blip_during_slow_take_in_keeps_the_piece(plant):
    d = Driver(plant)
    code = _waiting_at_exit_of_4_1(d, plant)
    _depart_4_1_into_slow_take_in(d)
    d.events += d.engine.on_connection_lost(d.t + 0.5, "blip")
    d.sync({k_state(4, 2): 1}, dt=1.0)  # back after 1 s, successor still taking in
    assert _ended(d) == []
    d.set(k_state(4, 2), 2)
    assert d.piece_at(4, 2) == code and len(d.of(ev.PieceAppeared)) == 1


def _handover_paired_while_source_still_in_4(d: Driver) -> None:
    d.set(k_state(4, 1), 4)
    d.set(k_state(4, 2), 1)
    d.set(k_state(4, 2), 2)  # arrival paired with the conveyor that is handing over


def test_no_phantom_piece_after_a_blip_while_the_source_is_still_handing_over(plant):
    d = Driver(plant)
    code = _waiting_at_exit_of_4_1(d, plant)
    _handover_paired_while_source_still_in_4(d)
    assert d.piece_at(4, 2) == code and d.engine.convs[(4, 1)].released
    d.events += d.engine.on_connection_lost(d.t + 0.2, "blip")
    d.sync({k_state(4, 1): 4, k_state(4, 2): 2}, dt=0.5)
    d.set(k_state(4, 1), 0)
    d.tick(6)
    assert len(d.of(ev.PieceAppeared)) == 1 and _ended(d) == []
    assert d.piece_at(4, 2) == code


def test_no_phantom_piece_after_a_restart_while_the_source_is_still_handing_over(plant):
    d = Driver(plant)
    code = _waiting_at_exit_of_4_1(d, plant)
    _handover_paired_while_source_still_in_4(d)
    fresh = TrackerEngine(plant, handover_window_s=5.0)
    fresh.restore_state(d.engine.export_state(), d.t)
    out = fresh.on_resync(idle_values(plant, {k_state(4, 1): 4, k_state(4, 2): 2}), d.t + 2)
    assert [e for e in out if isinstance(e, ev.PieceAppeared)] == []
    assert fresh.convs[(4, 2)].piece == code


def test_waiting_time_and_stuck_survive_a_restart(plant):
    d = Driver(plant, stuck_after_s=60)
    d.sync()
    d.run(put_on(plant, (11, 3)))  # waits at the exit conveyor (robot pick-up)
    code = d.piece_at(11, 3)
    waiting_since = d.engine.pieces[code].waiting_since
    assert waiting_since is not None
    d.tick(50)
    fresh = TrackerEngine(plant, handover_window_s=5.0, stuck_after_s=60)
    fresh.restore_state(d.engine.export_state(), d.t)
    fresh.on_resync(idle_values(plant, {k_state(11, 3): 3}), d.t + 5)
    assert fresh.pieces[code].waiting_since == waiting_since
    out = fresh.on_tick(d.t + 11)  # 61 s after it started waiting
    assert [(e.code, e.stuck) for e in out if isinstance(e, ev.StuckChanged)] == [(code, True)]
    again = TrackerEngine(plant, handover_window_s=5.0, stuck_after_s=60)
    again.restore_state(fresh.export_state(), d.t + 12)
    again.on_resync(idle_values(plant, {k_state(11, 3): 3}), d.t + 13)
    assert again.pieces[code].stuck and again.status_of(11).status == "stuck"


def test_restoring_twice_does_not_duplicate_robot_queue_entries(plant):
    d = Driver(plant)
    d.sync()
    d.run(put_on(plant, (11, 3)))
    d.set(k_state(11, 3), 0)  # robot lifts the piece
    d.tick(6)
    snap = d.engine.export_state()
    assert sum(len(q) for q in snap["robot_queues"].values()) == 1
    fresh = TrackerEngine(plant)
    fresh.restore_state(snap, d.t)
    fresh.restore_state(snap, d.t)  # recovery retried after a database error
    assert sum(len(q) for q in fresh.robot_queues.values()) == 1


# --------------------------------------------------------------- robot FIFO
def test_robot_places_pieces_first_in_first_out(plant):
    d = Driver(plant)
    d.sync()
    d.run(put_on(plant, (11, 3)))
    first = d.piece_at(11, 3)
    d.set(k_state(11, 3), 0)  # robot lifts the first piece
    d.tick(6)  # -> robot queue
    d.run(put_on(plant, (11, 3)))
    second = d.piece_at(11, 3)
    d.set(k_state(11, 3), 0)  # robot lifts the second piece (departure still pending)
    d.set(k_state(12, 1), 1)
    d.set(k_state(12, 1), 2)  # robot puts down a piece: the first one
    assert d.piece_at(12, 1) == first
    assert second in d.engine.pieces


# --------------------------------------------------------------- invalid values
def test_values_of_the_wrong_type_are_not_converted(plant):
    d = Driver(plant)
    code = _waiting_at_exit_of_4_1(d, plant)
    for bad in (3.7, True, "0"):
        out = d.set(k_state(4, 1), bad)
        assert any(isinstance(e, ev.Anomaly) for e in out), bad
        assert d.engine.convs[(4, 1)].state is None and d.piece_at(4, 1) == code
    d.set(k_lamp(5), "3")
    assert d.engine.status_of(5).status == "offline"
    d.set(k_lamp(5), 3)
    assert d.engine.status_of(5).status != "offline"


# --------------------------------------------------------------- stalls & clock steps
def test_a_stalled_tracker_does_not_expire_hand_overs(plant):
    d = Driver(plant, max_tick_gap_s=3.0)
    code = _waiting_at_exit_of_4_1(d, plant)
    d.tick(1)
    d.set(k_state(4, 1), 4)
    d.set(k_state(4, 1), 0)
    d.tick(15)  # event loop blocked for 15 s; the arrival is still queued behind it
    d.set(k_state(4, 2), 1, dt=0.01)
    d.set(k_state(4, 2), 2, dt=0.01)
    assert d.piece_at(4, 2) == code and _ended(d) == []


def test_clock_stepped_back_does_not_freeze_hand_over_windows(plant):
    d = Driver(plant, max_tick_gap_s=3.0)
    code = _waiting_at_exit_of_4_1(d, plant)
    d.tick(1)
    d.set(k_state(4, 1), 4)
    d.set(k_state(4, 1), 0)
    d.tick(-3600)  # NTP stepped the clock back an hour
    for _ in range(6):
        d.tick(1)
    assert [(e.code, e.status) for e in _ended(d)] == [(code, "removed")]


# --------------------------------------------------------------- counters
def test_cells_passed_counts_cells_left_but_not_the_cell_a_piece_was_lost_in(plant):
    d = Driver(plant)
    d.sync()
    d.run(put_on(plant, (11, 3)))
    code = d.piece_at(11, 3)
    d.set(k_state(11, 3), 0)
    d.tick(6)  # to the robot: cell 11 counts
    assert d.engine.pieces[code].cells_passed == 1
    d.run(put_on(plant, (4, 1)))
    other = d.engine.pieces[d.piece_at(4, 1)]
    d.events += d.engine.on_connection_lost(d.t, "cable unplugged")
    d.sync(dt=60)  # long gap, (4,1) and everything downstream now empty: lost in cell 4
    assert other.code not in d.engine.pieces
    assert [e.result for e in d.of(ev.CellLeft) if e.code == other.code] == ["lost"]
    assert other.cells_passed == 0


def test_a_conveyor_without_valid_data_is_reported_on_the_cell(plant):
    d = Driver(plant)
    d.sync()
    assert d.engine.status_of(4).reason == "ok"
    d.set(k_state(4, 2), 0, good=False)
    st = d.engine.status_of(4)
    assert (st.status, st.reason) == ("running", "partial_data")
    d.set(k_state(4, 2), 0)
    assert d.engine.status_of(4).reason == "ok"
