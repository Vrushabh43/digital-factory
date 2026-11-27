"""Run a recording through the tracker offline and summarise what it tracked (no OPC UA, no database).

    python -m dmf.cli.replay_check recordings/lab-2026-11-30.jsonl [--events]

Use it in the laboratory to check the tracker against real plant data: every piece
should appear once, move cell by cell and leave; "lost"/"found" pieces and anomalies
point at wrong node ids, wrong connections or tracker rules that need adjusting.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from dmf.plant.model import load_plant
from dmf.settings import get_settings
from dmf.tracker import events as ev
from dmf.tracker.engine import TrackerEngine


def replay(path: Path, engine: TrackerEngine, tick_s: float = 1.0) -> Iterator[ev.Event]:
    last_tick: float | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            kind, t = rec.get("type"), rec.get("t")
            if t is None:
                continue
            while last_tick is not None and t - last_tick >= tick_s:
                last_tick += tick_s
                yield from engine.on_tick(last_tick)
            if kind == "snapshot":
                yield from engine.on_resync({k: (v, g) for k, (v, g) in rec["values"].items()}, t, rec.get("reason", ""))
                last_tick = t
            elif kind == "change":
                yield from engine.on_signal(rec["key"], rec["value"], rec["good"], t)
            elif kind == "lost":
                yield from engine.on_connection_lost(t, rec.get("reason", ""))
            elif kind == "stale":
                yield from engine.set_stale(rec["pos"], rec["stale"], t)
    if last_tick is not None:
        yield from engine.on_tick(last_tick + tick_s)


def summarise(events: list[ev.Event]) -> dict:
    return {
        "events": dict(Counter(e.kind for e in events)),
        "pieces_appeared": dict(Counter(e.origin.split(":")[0] for e in events if isinstance(e, ev.PieceAppeared))),
        "pieces_ended": dict(Counter(e.status for e in events if isinstance(e, ev.PieceEnded))),
        "uncertain_entries": sum(1 for e in events if isinstance(e, ev.CellEntered) and e.uncertain),
        "anomalies": [e.message for e in events if isinstance(e, ev.Anomaly)][:100],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", type=Path)
    ap.add_argument("--events", action="store_true", help="print every event")
    args = ap.parse_args()
    s = get_settings()
    plant = load_plant(s.plant_config_path, s.nodes_config_path)
    engine = TrackerEngine(
        plant, handover_window_s=s.handover_window_s, stuck_after_s=s.stuck_after_s, reconcile_max_hops=s.reconcile_max_hops
    )
    events = []
    for e in replay(args.recording, engine):
        events.append(e)
        if args.events and e.kind != "cell_status":
            print(f"{e.ts:.3f} {e.kind:16} pos={e.position} {e.message}")
    print(json.dumps(summarise(events), indent=2))


if __name__ == "__main__":
    main()
