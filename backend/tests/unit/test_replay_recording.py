"""A recording (here: made from the mock server with dmf.cli.record) replays cleanly through the tracker.

Lab recordings should be added next to it once available (docs/08-lab-validation.md), together
with their expected summary in ``GOLDEN`` (count the pieces by hand in the lab notes): a replay
that only checks "no anomalies" would also pass for a tracker that tracks nothing.
"""

from pathlib import Path

import pytest

from dmf.cli.replay_check import replay, summarise
from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine

DATA = Path(__file__).resolve().parents[1] / "data"

# Expected result per recording. Derived once from the engine and then checked against the
# recording itself (see the test below):
# * the snapshot shows pos1.fb3.state = 2 -> one piece found at start-up on (1, 3);
# * three pieces are put on at external sources during the 15 s: pos2.fb1 (side entry,
#   t+3.6 s), pos17.fb5 (small storage, t+8.2 s) and pos15.fb1 (side entry, t+12.6 s);
# * P-0001 crosses positions 1 -> 2 -> 3 -> 4, P-0002 2 -> 3; nobody leaves the factory;
# * at the end pos4.fb1, pos3.fb3 and pos15.fb2 are occupied (state 2) and pos17.fb4 has just
#   gone 3 -> 0 (robot pick-up, 0.5 s before the end: the hand-over window is still open).
GOLDEN = {
    "mock-demo-recording.jsonl": {
        "pieces_appeared": {"startup": 1, "source": 3},
        "origins": ["startup", "source:Side entry", "source:Small storage", "source:Side entry"],
        "cell_entered": 8,
        "entered": {"P-0001": [1, 2, 3, 4], "P-0002": [2, 3], "P-0003": [17], "P-0004": [15]},
        "cell_left": [("P-0001", 1, "passed"), ("P-0001", 2, "passed"), ("P-0002", 2, "passed"), ("P-0001", 3, "passed")],
        "pieces_ended": {},
        "on_conveyor": {(4, 1): "P-0001", (3, 3): "P-0002", (15, 2): "P-0004"},
        "departing": {(17, 4): "P-0003"},
    },
}


def _run(plant, name: str) -> tuple[TrackerEngine, list[ev.Event]]:
    engine = TrackerEngine(plant, handover_window_s=8, stuck_after_s=120)
    return engine, list(replay(DATA / name, engine))


@pytest.mark.parametrize("name", sorted(p.name for p in DATA.glob("*.jsonl")))
def test_recording_replays_without_anomalies(plant, name):
    engine = TrackerEngine(plant, handover_window_s=8, stuck_after_s=120)
    summary = summarise(list(replay(DATA / name, engine)))
    assert summary["events"].get("connection", 0) >= 1
    assert summary["anomalies"] == []
    assert summary["pieces_ended"].get("lost", 0) == 0
    assert name in GOLDEN, f"add the expected summary of {name} to GOLDEN"


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_recording_gives_the_expected_pieces_and_moves(plant, name):
    gold = GOLDEN[name]
    engine, events = _run(plant, name)
    summary = summarise(events)
    assert summary["pieces_appeared"] == gold["pieces_appeared"]
    assert [e.origin for e in events if isinstance(e, ev.PieceAppeared)] == gold["origins"]
    assert summary["events"].get("cell_entered", 0) == gold["cell_entered"]
    assert summary["pieces_ended"] == gold["pieces_ended"]
    assert summary["uncertain_entries"] == 0
    entered: dict[str, list[int]] = {}
    for e in events:
        if isinstance(e, ev.CellEntered):
            entered.setdefault(e.code, []).append(e.pos)
    assert entered == gold["entered"]
    assert [(e.code, e.pos, e.result) for e in events if isinstance(e, ev.CellLeft)] == gold["cell_left"]
    # final picture: which piece is on which conveyor (and which one is just being handed on)
    assert {cid: rt.piece for cid, rt in engine.convs.items() if rt.piece} == gold["on_conveyor"]
    assert {cid: d.code for cid, d in engine.departures.items()} == gold["departing"]
    expected_pieces = set(gold["on_conveyor"].values()) | set(gold["departing"].values())
    assert set(engine.pieces) == expected_pieces
    for cid, code in {**gold["on_conveyor"], **gold["departing"]}.items():
        assert engine.pieces[code].conv == cid and not engine.pieces[code].uncertain
