"""Workpiece tracking engine — pure, synchronous, deterministic.

Inputs are individual OPC UA value changes, periodic ticks, connection loss and
resynchronisation. Outputs are domain events (``dmf.tracker.events``). The engine
never performs I/O and takes the current time as an argument, so it can be unit
tested and fed with recorded lab data.

Key rules (derived from the PLC SCL code, see docs/01-project-analysis.md):

* A conveyor holds a piece in states {2,3,4,5}; it is empty in {0,1,6}.
  (Turntable state 6 = rotating back AFTER the piece left.)
* A hand-over is a departure from state 4 (``4->0``, on a turntable ``4->6``) on one
  conveyor paired with an arrival (empty -> occupied) on a successor conveyor.
  The two notifications may come in either order; they are paired within
  ``handover_window_s``. ``3->0`` normally is a piece taken off at the end sensor; it is
  only paired as a fallback (state 4 not seen, e.g. a coarse server sampling rate), with
  lower priority and marked *uncertain*. A piece still waiting in state 3/5 is never
  paired with an arrival: the arrival waits for the window instead, because a piece put
  on by hand next to a waiting piece would otherwise take over its number.
* The window does not run while a hand-over is on hold: while the cells involved are
  halted (lamp 1, plant not enabled, stale or unknown data) or while the successor is
  taking the piece in (state 1, up to ``stuck_after_s``). Time during which the
  tracker itself did not run (stalled loop, clock step) is not counted either.
* An arrival without a matching departure is a piece put on by a person/robot
  (new number). A departure without a matching arrival is a piece taken off,
  handed to a robot (FIFO bridge) or sent to storage.
* After a data gap (reconnect, restart) the live states are reconciled with the
  tracked pieces; moves inferred across the gap are marked *uncertain*.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from dmf.plant.model import K_ALARM, K_ENABLE, ConvId, Plant
from dmf.tracker import events as ev
from dmf.tracker.status import STUCK, StatusResult, compute_status

log = logging.getLogger(__name__)

OCCUPIED = frozenset({2, 3, 4, 5})
AMBIGUOUS_HANDOVER = "two pieces could have made this hand-over"
NO_HANDING_STATE = "hand-over seen without the handing-over state"
NOT_PASSED = "lost"  # a cell counts as passed when the piece left it, not when it was lost there
EMPTY = frozenset({0, 1, 6})
VALID_STATES = OCCUPIED | EMPTY


def occupancy(state: int | None) -> bool | None:
    if state is None or state not in VALID_STATES:
        return None
    return state in OCCUPIED


# --------------------------------------------------------------------------- runtime state
@dataclass
class ConvRuntime:
    state: int | None = None  # None = unknown (no data / bad quality)
    last_good_state: int | None = None
    since: float = 0.0
    piece: str | None = None
    released: bool = False  # piece already moved on while this conveyor still shows "occupied"
    pending_arrival_since: float | None = None
    sensors: list[bool | None] = field(default_factory=lambda: [None, None])


@dataclass
class PieceRuntime:
    code: str
    conv: ConvId | None  # current conveyor; None while with a robot
    pos: int | None
    entered_at: float
    first_seen: float
    first_pos: int
    origin: str
    came_from: int | None = None
    waiting_since: float | None = None
    stuck: bool = False
    stuck_since: float | None = None
    was_stuck: bool = False
    exit_reported: bool = False
    uncertain: bool = False
    uncertain_reason: str | None = None
    departing_since: float | None = None  # left its conveyor, hand-over not yet confirmed
    cells_passed: int = 0


@dataclass
class Departure:
    code: str
    from_state: int
    ts: float
    armed: float | None = None  # start of the current window (re-armed while on hold)

    @property
    def window_start(self) -> float:
        return self.ts if self.armed is None else self.armed


@dataclass
class CellRuntime:
    lamp: int | None = None
    lamp_good: bool = False
    ports: dict[str, bool | None] = field(default_factory=dict)  # "1.can_send" -> value
    stale: bool = False
    last_data: float | None = None
    status: StatusResult | None = None
    status_since: float = 0.0


class TrackerEngine:
    def __init__(
        self,
        plant: Plant,
        *,
        handover_window_s: float = 8.0,
        stuck_after_s: float = 120.0,
        reconcile_max_hops: int = 6,
        next_seq: int = 1,
        max_tick_gap_s: float | None = None,
    ) -> None:
        self.plant = plant
        self.window = handover_window_s
        self.stuck_after = stuck_after_s
        self.max_hops = reconcile_max_hops
        # Ticks normally come every second. A longer gap means the tracker did not run
        # (stalled event loop, clock step); hand-over windows are then shifted by the gap
        # so that notifications still queued behind the stall can pair. None = off.
        self.max_tick_gap = max_tick_gap_s
        self.next_seq = next_seq
        self.convs: dict[ConvId, ConvRuntime] = {c.id: ConvRuntime() for c in plant.all_conveyors()}
        self.cells: dict[int, CellRuntime] = {p: CellRuntime() for p in plant.cells}
        self.pieces: dict[str, PieceRuntime] = {}
        self.departures: dict[ConvId, Departure] = {}
        self.robot_queues: dict[str, deque[tuple[str, float]]] = {lk.id: deque() for lk in plant.robot_links}
        self.connected = False
        self.connected_since: float | None = None
        self.disconnected_at: float | None = None
        self.alarm: bool | None = None
        self.enable: bool | None = None
        self.last_update: float | None = None
        self._never_synced = True  # cleared after the first resync; a restored snapshot also clears it
        self._sensor_conflict: dict[ConvId, float] = {}
        self._last_tick: float | None = None
        self._key_index = self._build_key_index()

    # ================================================================== public API
    def on_signal(self, key: str, value: Any, good: bool, now: float) -> list[ev.Event]:
        """Apply one OPC UA value change."""
        out: list[ev.Event] = []
        self.last_update = now
        target = self._key_index.get(key)
        if target is None:
            return out
        kind = target[0]
        if kind == "state":
            state = _as_int(value) if good else None
            if good and state is None:
                out.append(ev.Anomaly(now, target[1][0], f"Invalid state value {value!r} on conveyor {target[1][1]}"))
            self._on_state(target[1], state, now, out)
        elif kind == "sensor":
            conv = self.convs[target[1]]
            conv.sensors[target[2] - 1] = _as_bool(value) if good else None
        elif kind == "lamp":
            cell = self.cells[target[1]]
            lamp = _as_int(value) if good else None
            if good and lamp is None:
                out.append(ev.Anomaly(now, target[1], f"Invalid lamp value {value!r}"))
            cell.lamp, cell.lamp_good = lamp, lamp is not None
            cell.last_data = now
        elif kind == "port":
            self.cells[target[1]].ports[target[2]] = _as_bool(value) if good else None
        elif kind == "general":
            self._on_general(target[1], _as_bool(value) if good else None, now, out)
        self._refresh_statuses(now, out)
        return out

    def on_tick(self, now: float) -> list[ev.Event]:
        """Resolve hand-over windows, robot transit timeouts and stuck pieces."""
        out: list[ev.Event] = []
        if not self.connected:
            self._last_tick = None
            return out
        if self.max_tick_gap is not None and self._last_tick is not None:
            gap = now - self._last_tick
            if gap > self.max_tick_gap or gap < 0:  # stall / clock stepped forward or back
                self._shift_windows(gap)
        self._last_tick = now
        # pending arrivals -> new pieces
        for cid, conv in self.convs.items():
            if conv.pending_arrival_since is None:
                continue
            if self._halted(cid[0]) or any(self._halted(p[0]) for p, _ in self.plant.predecessors.get(cid, [])):
                conv.pending_arrival_since = now  # nothing moves while halted
            elif now - conv.pending_arrival_since >= self.window:
                conv.pending_arrival_since = None
                if conv.piece is None and occupancy(conv.state):
                    self._new_piece(cid, "put_on", now, out)
        # pending departures -> robot / storage / removed
        for cid, dep in list(self.departures.items()):
            if self._handover_on_hold(cid, dep, now):
                dep.armed = now
            elif now - dep.window_start >= self.window:
                del self.departures[cid]
                self._resolve_departure(cid, dep, now, out)
        # robot transit timeouts
        for link in self.plant.robot_links:
            q = self.robot_queues[link.id]
            while q and now - q[0][1] > link.max_transit_s:
                code, _ = q.popleft()
                self._end_piece(code, "lost", None, now, out, note="did not arrive after the robot transfer")
        # sensor cross-check: an empty, idle conveyor whose sensor sees something
        for cid, conv in self.convs.items():
            conflict = conv.state == 0 and any(s is True for s in conv.sensors)
            if not conflict:
                self._sensor_conflict.pop(cid, None)
            elif cid not in self._sensor_conflict:
                self._sensor_conflict[cid] = now
            elif self._sensor_conflict[cid] >= 0 and now - self._sensor_conflict[cid] >= self.window:
                self._sensor_conflict[cid] = -1.0  # report once
                out.append(ev.Anomaly(now, cid[0], f"A sensor on conveyor {cid[1]} sees a piece the PLC does not report"))
        # stuck detection
        for p in self.pieces.values():
            if p.waiting_since is not None and not p.stuck and now - p.waiting_since >= self.stuck_after:
                p.stuck, p.stuck_since, p.was_stuck = True, p.waiting_since, True
                out.append(ev.StuckChanged(now, p.code, p.pos, True, p.waiting_since))
        self._refresh_statuses(now, out)
        return out

    def on_connection_lost(self, now: float, reason: str) -> list[ev.Event]:
        out: list[ev.Event] = []
        if self.connected:
            self.connected = False
            self.disconnected_at = now
            out.append(ev.ConnectionChanged(now, False, reason))
        for conv in self.convs.values():
            conv.state = None  # last_good_state is kept for reconciliation
        for cell in self.cells.values():
            cell.lamp_good = False
        self._refresh_statuses(now, out)
        return out

    def on_resync(self, values: dict[str, tuple[Any, bool]], now: float, reason: str = "connected") -> list[ev.Event]:
        """(Re)initialise from a complete set of current values and reconcile pieces.

        Used for the first connection, every reconnection and after a restart
        (with the restored snapshot as the "before" picture).
        """
        out: list[ev.Event] = []
        gap = (now - self.disconnected_at) if self.disconnected_at is not None else 0.0
        long_gap = gap > self.window
        # non-conveyor signals first (no piece logic)
        new_states: dict[ConvId, int | None] = {}
        for key, (value, good) in values.items():
            target = self._key_index.get(key)
            if target is None:
                continue
            if target[0] == "state":
                new_states[target[1]] = _as_int(value) if good else None
            elif target[0] == "general":
                self._on_general(target[1], bool(value) if good else None, now, out)
            else:
                self.on_signal(key, value, good, now)
        self.connected = True
        self.last_update = now
        out.append(ev.ConnectionChanged(now, True, reason))
        self._reconcile(new_states, now, long_gap, out)
        self.connected_since = now
        self.disconnected_at = None
        self._refresh_statuses(now, out)
        return out

    def _halted(self, pos: int) -> bool:
        """The cell's PLC is not moving pieces (or we cannot tell): hand-over timers pause."""
        cell = self.cells[pos]
        return self.enable is False or cell.stale or not cell.lamp_good or cell.lamp == 1

    def _handover_on_hold(self, cid: ConvId, dep: Departure, now: float) -> bool:
        if self._halted(cid[0]):
            return True
        if dep.from_state != 4:
            return False
        for succ in self.plant.successors.get(cid, []):
            if self._halted(succ[0]):
                return True
            sconv = self.convs[succ]
            if sconv.state == 1 and sconv.piece is None and now - dep.ts < self.stuck_after:
                return True  # the successor has accepted the piece and is taking it in
        return False

    def _shift_windows(self, gap: float) -> None:
        for dep in self.departures.values():
            dep.armed = dep.window_start + gap
        for conv in self.convs.values():
            if conv.pending_arrival_since is not None:
                conv.pending_arrival_since += gap

    @property
    def has_state(self) -> bool:
        """True once synchronised with the plant or restored from a snapshot (a snapshot of
        a never-synchronised engine would overwrite a good one with nothing)."""
        return not self._never_synced

    def set_stale(self, pos: int, stale: bool, now: float) -> list[ev.Event]:
        out: list[ev.Event] = []
        if self.cells[pos].stale != stale:
            self.cells[pos].stale = stale
            self._refresh_statuses(now, out)
        return out

    # ================================================================== state changes
    def _on_state(self, cid: ConvId, state: int | None, now: float, out: list[ev.Event]) -> None:
        conv = self.convs[cid]
        if state is not None and state not in VALID_STATES:
            out.append(ev.Anomaly(now, cid[0], f"Unexpected state value {state} on conveyor {cid[1]}"))
            state = None
        if state == conv.state:
            return  # duplicate notification
        prev_known = conv.state if conv.state is not None else conv.last_good_state
        conv.state = state
        if state is None:
            return  # unknown: keep pieces where they are until data returns
        conv.last_good_state = state
        conv.since = now
        was, now_occ = occupancy(prev_known), occupancy(state)
        if was is not True and now_occ:
            self._arrival(cid, now, out)
        elif was is True and not now_occ:
            self._departure(cid, prev_known if prev_known is not None else 4, now, out)
        elif now_occ:
            self._occupied_state_change(cid, state, now, out)

    def _arrival(self, cid: ConvId, now: float, out: list[ev.Event]) -> None:
        conv = self.convs[cid]
        conv.released = False
        if conv.piece is not None:  # e.g. data returned after a short Bad-quality blip
            self._occupied_state_change(cid, conv.state, now, out)
            return
        src = self._best_source(cid)
        if src is not None:
            source_conv, how, uncertain = src
            self._take_from(source_conv, how, cid, now, out, uncertain=uncertain)
            return
        # robot-fed entry, first in first out: pieces already with the robot come first;
        # a piece that has only just left the robot's pick-up point can also be placed
        # directly (the robot may be faster than the hand-over window)
        for link in self.plant.links_to(cid):
            q = self.robot_queues[link.id]
            if q:
                code, _t = q.popleft()
                self._place(code, cid, now, out, came_from=link.src[0], via=link.name)
                return
            dep = self.departures.pop(link.src, None)
            if dep is not None and dep.code in self.pieces:
                piece = self.pieces[dep.code]
                self._leave_cell(piece, "to_robot", now, out, to_pos=cid[0])
                self._place(dep.code, cid, now, out, came_from=link.src[0], via=link.name)
                return
        if self._predecessor_may_deliver(cid):
            conv.pending_arrival_since = now  # wait for the matching departure
            return
        origin = f"source:{self.plant.sources[cid]}" if cid in self.plant.sources else "put_on"
        if self.plant.links_to(cid):
            origin = f"robot:{self.plant.links_to(cid)[0].name}"
        self._new_piece(cid, origin, now, out)

    def _departure(self, cid: ConvId, from_state: int, now: float, out: list[ev.Event]) -> None:
        conv = self.convs[cid]
        conv.pending_arrival_since = None
        if conv.released or conv.piece is None:
            conv.released = False
            conv.piece = None
            return
        code = conv.piece
        conv.piece = None
        piece = self.pieces[code]
        piece.waiting_since = None
        # an arrival downstream may already be waiting for us (a departure that did not
        # come from state 4 is only a fallback match: the hand-over state was not seen)
        for succ in self.plant.successors.get(cid, []):
            sconv = self.convs[succ]
            if sconv.pending_arrival_since is not None and sconv.piece is None:
                sconv.pending_arrival_since = None
                self._move(code, cid, succ, now, out, uncertain=from_state != 4, reason=NO_HANDING_STATE)
                return
        piece.departing_since = now
        previous = self.departures.pop(cid, None)
        if previous is not None:  # an older departure from this conveyor was never paired
            self._resolve_departure(cid, previous, now, out)
        self.departures[cid] = Departure(code, from_state, now)

    def _occupied_state_change(self, cid: ConvId, state: int | None, now: float, out: list[ev.Event]) -> None:
        conv = self.convs[cid]
        if conv.piece is None:
            return
        piece = self.pieces[conv.piece]
        is_exit = self.plant.is_exit(cid)
        waiting = (state == 3 and is_exit) or (state == 5 and self.plant.conveyor(cid).turntable)
        if waiting:
            if piece.waiting_since is None:
                piece.waiting_since = now
        else:
            piece.waiting_since = None
            if piece.stuck:
                piece.stuck = False
                out.append(ev.StuckChanged(now, piece.code, cid[0], False, piece.stuck_since or now))
        if state == 3 and is_exit and not piece.exit_reported:
            piece.exit_reported = True
            out.append(ev.ExitReached(now, piece.code, cid[0], cid[1]))

    # ================================================================== matching helpers
    def _best_source(self, cid: ConvId) -> tuple[ConvId, str, bool] | None:
        """Find the predecessor conveyor whose piece is arriving at ``cid``."""
        # Rank 0: left from state 4; 1: handing over now (state 4); 2: left without state 4
        # being seen (fallback, uncertain). A predecessor still waiting in state 3/5 is no
        # candidate: the arrival waits for the window (see _predecessor_may_deliver).
        cands: list[tuple[int, int, float, ConvId, str]] = []
        for pred, prio in self.plant.predecessors.get(cid, []):
            dep = self.departures.get(pred)
            pconv = self.convs[pred]
            if dep is not None:
                cands.append((0 if dep.from_state == 4 else 2, prio, dep.ts, pred, "departed"))
            elif pconv.piece is not None and not pconv.released and pconv.state == 4:
                cands.append((1, prio, pconv.since, pred, "handing"))
        if not cands:
            return None
        cands.sort()
        best = cands[0]
        ambiguous = len(cands) > 1 and cands[1][0] == best[0] and cands[1][1] == best[1]
        return best[3], best[4], ambiguous or best[0] == 2

    def _predecessor_may_deliver(self, cid: ConvId) -> bool:
        """True if a predecessor holds a piece whose hand-over notification may still be on its way."""
        for pred, _ in self.plant.predecessors.get(cid, []):
            pconv = self.convs[pred]
            if pconv.piece is not None and not pconv.released:
                return True
        return False

    def _take_from(self, src: ConvId, how: str, dst: ConvId, now: float, out: list[ev.Event], uncertain: bool = False) -> None:
        reason = AMBIGUOUS_HANDOVER
        if how == "departed":
            dep = self.departures.pop(src)
            code = dep.code
            if dep.from_state != 4:
                reason = NO_HANDING_STATE
        else:
            sconv = self.convs[src]
            code = sconv.piece
            sconv.piece = None
            sconv.released = True  # its coming 4->0 / 3->0 is part of this hand-over
        assert code is not None
        self._move(code, src, dst, now, out, uncertain=uncertain, reason=reason)

    def _move(
        self,
        code: str,
        src: ConvId,
        dst: ConvId,
        now: float,
        out: list[ev.Event],
        uncertain: bool = False,
        reason: str = AMBIGUOUS_HANDOVER,
    ) -> None:
        piece = self.pieces[code]
        piece.departing_since = None
        if uncertain:
            self._mark_uncertain(piece, reason)
        if src[0] == dst[0]:
            piece.conv = dst
            self.convs[dst].piece = code
            self.convs[dst].pending_arrival_since = None
            out.append(ev.ConveyorChanged(now, code, dst[0], src[1], dst[1]))
            self._after_place(piece, dst, now, out)
            return
        self._leave_cell(piece, "passed", now, out, to_pos=dst[0])
        self._place(code, dst, now, out, came_from=src[0])

    def _place(
        self, code: str, dst: ConvId, now: float, out: list[ev.Event], came_from: int | None, via: str | None = None
    ) -> None:
        piece = self.pieces[code]
        cell = self.plant.cells[dst[0]]
        piece.conv, piece.pos = dst, dst[0]
        piece.entered_at, piece.came_from = now, came_from
        piece.exit_reported, piece.departing_since = False, None
        self.convs[dst].piece = code
        self.convs[dst].pending_arrival_since = None
        came_cell = self.plant.cells[came_from].cell if came_from in self.plant.cells else None
        out.append(ev.CellEntered(now, code, dst[0], cell.cell, dst[1], came_from, came_cell, via, piece.uncertain))
        self._after_place(piece, dst, now, out)

    def _after_place(self, piece: PieceRuntime, cid: ConvId, now: float, out: list[ev.Event]) -> None:
        piece.waiting_since = None
        if piece.stuck:
            piece.stuck = False
            out.append(ev.StuckChanged(now, piece.code, cid[0], False, piece.stuck_since or now))
        state = self.convs[cid].state
        if state in (3, 5):
            self._occupied_state_change(cid, state, now, out)

    def _new_piece(self, cid: ConvId, origin: str, now: float, out: list[ev.Event], uncertain: bool = False) -> str:
        code = f"P-{self.next_seq:04d}"
        self.next_seq += 1
        cell = self.plant.cells[cid[0]]
        self.pieces[code] = PieceRuntime(
            code=code, conv=cid, pos=cid[0], entered_at=now, first_seen=now, first_pos=cid[0], origin=origin
        )
        if uncertain:
            self._mark_uncertain(self.pieces[code], "found after a data gap")
        out.append(ev.PieceAppeared(now, code, cid[0], cell.cell, cid[1], origin, uncertain))
        self._place(code, cid, now, out, came_from=None)
        return code

    def _leave_cell(self, piece: PieceRuntime, result: str, now: float, out: list[ev.Event], to_pos: int | None) -> None:
        if piece.pos is None:
            return
        cell = self.plant.cells[piece.pos]
        if result != NOT_PASSED:
            piece.cells_passed += 1
        out.append(ev.CellLeft(now, piece.code, piece.pos, cell.cell, result, to_pos, piece.uncertain))
        if piece.stuck:
            piece.stuck = False
            out.append(ev.StuckChanged(now, piece.code, piece.pos, False, piece.stuck_since or now))
        piece.was_stuck = False
        piece.waiting_since = None

    def _end_piece(self, code: str, status: str, cid: ConvId | None, now: float, out: list[ev.Event], note: str = "") -> None:
        piece = self.pieces.pop(code, None)
        if piece is None:
            return
        if piece.pos is not None and piece.conv is not None:
            self._leave_cell(piece, status, now, out, to_pos=None)
        if cid is not None and self.convs[cid].piece == code:
            self.convs[cid].piece = None
        out.append(ev.PieceEnded(now, code, status, piece.pos))
        if note:
            out.append(ev.Anomaly(now, piece.pos, f"{code} {note}", code))

    def _resolve_departure(self, cid: ConvId, dep: Departure, now: float, out: list[ev.Event]) -> None:
        piece = self.pieces.get(dep.code)
        if piece is None:
            return
        piece.departing_since = None
        link = self.plant.link_from(cid)
        if link is not None:
            self._leave_cell(piece, "to_robot", now, out, to_pos=link.dst[0])
            piece.conv, piece.pos = None, None
            self.robot_queues[link.id].append((dep.code, now))
            return
        if cid in self.plant.sinks:
            self._end_piece(dep.code, "stored", None, now, out)
            return
        self._end_piece(dep.code, "removed", None, now, out)

    def _mark_uncertain(self, piece: PieceRuntime, reason: str) -> None:
        piece.uncertain = True
        piece.uncertain_reason = reason

    # ================================================================== reconciliation
    def _reconcile(self, new_states: dict[ConvId, int | None], now: float, long_gap: bool, out: list[ev.Event]) -> None:
        vanished: list[tuple[str, ConvId]] = []
        unexplained: list[ConvId] = []
        # A pending departure whose successor is now taking a piece in (state 1, empty)
        # is still a hand-over in progress: keep it and restart its window. Any other
        # pending departure becomes a vanished candidate again.
        kept: dict[ConvId, Departure] = {}
        for cid, dep in self.departures.items():
            if dep.code not in self.pieces:
                continue
            taking_in = any(
                new_states.get(s, self.convs[s].state) == 1 and self.convs[s].piece is None
                for s in self.plant.successors.get(cid, [])
            )
            if taking_in:
                dep.armed = now
                kept[cid] = dep
            else:
                vanished.append((dep.code, cid))
        self.departures = kept
        for cid, conv in self.convs.items():
            new = new_states.get(cid, conv.state)
            before = conv.last_good_state
            conv.state = new
            conv.pending_arrival_since = None
            if new is None:
                continue  # still unknown: leave pieces in place (and keep "released")
            # a conveyor whose piece was already handed on keeps that mark while it still
            # shows the same occupied state (otherwise a phantom piece would be created)
            released = conv.released and conv.piece is None and new == before and bool(occupancy(new))
            conv.released = released
            conv.last_good_state = new
            conv.since = now
            occ_new = occupancy(new)
            if released:
                continue
            if conv.piece is not None:
                if occ_new:
                    piece = self.pieces[conv.piece]
                    if long_gap and new != before:
                        self._mark_uncertain(piece, "position re-checked after a data gap")
                else:
                    vanished.append((conv.piece, cid))
                    conv.piece = None
            elif occ_new:
                unexplained.append(cid)
        # order pieces upstream -> downstream, each takes its nearest unexplained arrival
        # (the positions are collected BEFORE sorting: during list.sort() the list is empty)
        where = [v[1] for v in vanished]
        vanished.sort(key=lambda item: self._downstream_rank(item[1], where))
        free = set(unexplained)
        for code, cid in vanished:
            target = next((c for c, _h in self.plant.downstream(cid, self.max_hops) if c in free), None)
            if target is None:
                self._end_piece(code, "lost", None, now, out, note="was not found again after a data gap")
                continue
            free.discard(target)
            self._mark_uncertain(self.pieces[code], "moved while no data was received")
            self._move(code, cid, target, now, out)
        first_sync = self._never_synced
        for cid in sorted(free):
            self._new_piece(cid, "startup" if first_sync else "found", now, out)
        self._never_synced = False
        # re-evaluate waiting/stuck for kept pieces from their current state (keeps the
        # original waiting time if still waiting, clears it - and "stuck" - if not)
        for cid, conv in self.convs.items():
            if conv.piece is not None and conv.state in OCCUPIED:
                self._occupied_state_change(cid, conv.state, now, out)

    def _downstream_rank(self, cid: ConvId, others: list[ConvId]) -> int:
        """Number of other candidates upstream of ``cid`` (smaller = further upstream)."""
        return sum(1 for o in others if o != cid and any(c == cid for c, _ in self.plant.downstream(o, self.max_hops)))

    # ================================================================== cells & plant
    def _on_general(self, which: str, value: bool | None, now: float, out: list[ev.Event]) -> None:
        if which == "alarm" and value != self.alarm:
            self.alarm = value
            out.append(ev.PlantFlagChanged(now, "alarm", value))
        elif which == "enable" and value != self.enable:
            self.enable = value
            out.append(ev.PlantFlagChanged(now, "enable", value))

    def _refresh_statuses(self, now: float, out: list[ev.Event]) -> None:
        stuck_positions = {p.pos for p in self.pieces.values() if p.stuck}
        for pos, cell in self.cells.items():
            partial = self.connected and any(self.convs[(pos, n)].state is None for n in self.plant.cells[pos].conveyors)
            res = compute_status(
                connected=self.connected,
                lamp=cell.lamp,
                lamp_good=cell.lamp_good,
                stale=cell.stale,
                has_stuck_piece=pos in stuck_positions,
                plant_enabled=self.enable,
                partial=partial,
            )
            if cell.status is None or cell.status.status != res.status or cell.status.reason != res.reason:
                previous = cell.status.status if cell.status else None
                status_changed = previous != res.status
                cell.status = res
                if status_changed:
                    cell.status_since = now
                    out.append(ev.CellStatusChanged(now, pos, self.plant.cells[pos].cell, res.status, previous, res.reason))
            else:
                cell.status = res  # detail may change without an event

    # ================================================================== snapshot (restart)
    def export_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "next_seq": self.next_seq,
            "convs": {
                f"{c[0]}.{c[1]}": {"state": r.last_good_state, "piece": r.piece, "released": r.released}
                for c, r in self.convs.items()
            },
            "pieces": {
                code: {
                    "conv": list(p.conv) if p.conv else None,
                    "pos": p.pos,
                    "entered_at": p.entered_at,
                    "first_seen": p.first_seen,
                    "first_pos": p.first_pos,
                    "origin": p.origin,
                    "came_from": p.came_from,
                    "was_stuck": p.was_stuck,
                    "stuck": p.stuck,
                    "stuck_since": p.stuck_since,
                    "waiting_since": p.waiting_since,
                    "exit_reported": p.exit_reported,
                    "uncertain": p.uncertain,
                    "uncertain_reason": p.uncertain_reason,
                    "cells_passed": p.cells_passed,
                }
                for code, p in self.pieces.items()
            },
            "departures": {f"{c[0]}.{c[1]}": [d.code, d.from_state, d.ts, d.armed] for c, d in self.departures.items()},
            "robot_queues": {k: list(q) for k, q in self.robot_queues.items()},
        }

    def restore_state(self, data: dict[str, Any], saved_at: float) -> None:
        """Load a snapshot. Call ``on_resync`` afterwards to reconcile with live values."""
        if data.get("version") != 1:
            log.warning("ignoring tracker snapshot with unknown version %s", data.get("version"))
            return
        self.reset_pieces()  # clean slate, so that a retried recovery gives the same result
        self.next_seq = max(self.next_seq, int(data.get("next_seq", 1)))
        for code, p in (data.get("pieces") or {}).items():
            conv = tuple(p["conv"]) if p.get("conv") else None
            if conv is not None and conv not in self.convs:
                continue
            self.pieces[code] = PieceRuntime(
                code=code,
                conv=conv,  # type: ignore[arg-type]
                pos=p.get("pos"),
                entered_at=p["entered_at"],
                first_seen=p["first_seen"],
                first_pos=p["first_pos"],
                origin=p["origin"],
                came_from=p.get("came_from"),
                was_stuck=bool(p.get("was_stuck")),
                stuck=bool(p.get("stuck")),
                stuck_since=p.get("stuck_since"),
                waiting_since=p.get("waiting_since"),
                exit_reported=bool(p.get("exit_reported")),
                uncertain=bool(p.get("uncertain")),
                uncertain_reason=p.get("uncertain_reason"),
                cells_passed=int(p.get("cells_passed", 0)),
            )
        for key, c in (data.get("convs") or {}).items():
            pos, n = (int(x) for x in key.split("."))
            if (pos, n) in self.convs:
                rt = self.convs[(pos, n)]
                rt.last_good_state = c.get("state")
                rt.piece = c.get("piece") if c.get("piece") in self.pieces else None
                rt.released = bool(c.get("released")) and rt.piece is None
        for key, item in (data.get("departures") or {}).items():
            pos, n = (int(x) for x in key.split("."))
            code, st, ts = item[0], item[1], item[2]
            if code in self.pieces and (pos, n) in self.convs:
                self.departures[(pos, n)] = Departure(code, st, ts, item[3] if len(item) > 3 else None)
        for link_id, items in (data.get("robot_queues") or {}).items():
            if link_id in self.robot_queues:  # assign, not extend: restore may be retried
                self.robot_queues[link_id] = deque((c, t) for c, t in items if c in self.pieces)
        self.disconnected_at = saved_at  # treat the downtime as a data gap
        self._never_synced = False

    def forget_piece(self, code: str) -> None:
        """Drop a piece without events (startup recovery: it already ended in the database)."""
        self.pieces.pop(code, None)
        for conv in self.convs.values():
            if conv.piece == code:
                conv.piece = None
        for cid in [c for c, d in self.departures.items() if d.code == code]:
            del self.departures[cid]
        for q in self.robot_queues.values():
            for item in [i for i in q if i[0] == code]:
                q.remove(item)

    def reset_pieces(self) -> None:
        self.pieces.clear()
        self.departures.clear()
        for rt in self.convs.values():
            rt.piece, rt.released = None, False
        for q in self.robot_queues.values():
            q.clear()

    def adopt_piece(
        self,
        code: str,
        *,
        pos: int | None,
        conveyor: int | None,
        entered_at: float,
        first_seen: float,
        first_pos: int,
        origin: str,
        came_from: int | None,
        cells_passed: int,
        now: float,
    ) -> bool:
        """Take over a piece that is active in the database but missing from the snapshot
        (created after the last snapshot). Without events; marked uncertain. The following
        resync checks the position against the live plant like any other piece.

        ``pos``/``conveyor`` come from the open cell pass; no open pass means the piece
        was handed to a robot from position ``first_pos`` / its last position."""
        if code in self.pieces:
            return True
        piece = PieceRuntime(
            code=code,
            conv=None,
            pos=None,
            entered_at=entered_at,
            first_seen=first_seen,
            first_pos=first_pos,
            origin=origin,
            came_from=came_from,
            cells_passed=cells_passed,
        )
        self._mark_uncertain(piece, "restored from the history after a restart")
        if pos is not None and conveyor is not None:
            cid = (pos, conveyor)
            if cid not in self.convs or self.convs[cid].piece is not None:
                return False
            piece.conv, piece.pos = cid, pos
            self.pieces[code] = piece
            self.convs[cid].piece = code
            return True
        link = next((lk for lk in self.plant.robot_links if lk.src[0] == pos), None)
        if link is None:
            return False
        self.pieces[code] = piece
        self.robot_queues[link.id].append((code, now))  # transit timeout starts now
        return True

    def relocate_piece(self, code: str, cid: ConvId) -> bool:
        """Move a piece to ``cid`` without events (startup recovery from newer database state)."""
        piece = self.pieces.get(code)
        if piece is None or cid not in self.convs or self.convs[cid].piece not in (None, code):
            return False
        self.forget_piece(code)
        self.pieces[code] = piece
        piece.conv, piece.pos = cid, cid[0]
        self.convs[cid].piece = code
        return True

    # ================================================================== internals
    def _build_key_index(self) -> dict[str, tuple]:
        idx: dict[str, tuple] = {K_ALARM: ("general", "alarm"), K_ENABLE: ("general", "enable")}
        for pos, cell in self.plant.cells.items():
            idx[f"pos{pos}.lamp"] = ("lamp", pos)
            for port in cell.port_nodes:
                for flag in ("can_send", "can_receive"):
                    idx[f"pos{pos}.port{port}.{flag}"] = ("port", pos, f"{port}.{flag}")
            for n in cell.conveyors:
                idx[f"pos{pos}.fb{n}.state"] = ("state", (pos, n))
                idx[f"pos{pos}.fb{n}.sensor1"] = ("sensor", (pos, n), 1)
                idx[f"pos{pos}.fb{n}.sensor2"] = ("sensor", (pos, n), 2)
        return idx

    def has_stuck(self, pos: int) -> bool:
        return any(p.stuck and p.pos == pos for p in self.pieces.values())

    def status_of(self, pos: int) -> StatusResult:
        return self.cells[pos].status or StatusResult("offline", "no_valid_data")

    def stuck_cells(self) -> list[int]:
        return sorted({p.pos for p in self.pieces.values() if p.stuck and p.pos is not None})

    def is_stuck_status(self, pos: int) -> bool:
        return self.status_of(pos).status == STUCK


def _as_int(value: Any) -> int | None:
    """Strict: only real integers (an OPC UA Int16/Byte arrives as int). 3.7, True or "3"
    mean a wrong node or data type and are treated as invalid, not silently converted."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
