"""Record the plant's OPC UA values to a JSON-lines file (READ-ONLY).

    python -m dmf.cli.record --out recordings/lab-2026-11-30.jsonl [--minutes 30]

Uses exactly the same connection/subscription code as the backend. The file starts
with a ``header`` line, then one ``snapshot`` per (re)connect, then ``change`` lines.
Recordings can be replayed with ``python -m dmf_mock --mode replay --file ...`` and
used as regression data for the tracker (see docs/08-lab-validation.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from dmf.logging_setup import setup_logging
from dmf.opcua.ingest import Change, Lost, OpcIngest, Snapshot, Stale
from dmf.plant.model import load_plant
from dmf.settings import get_settings, redact_url

log = logging.getLogger("dmf.record")


async def record(out: Path, minutes: float | None) -> int:
    s = get_settings()
    plant = load_plant(s.plant_config_path, s.nodes_config_path)
    queue: asyncio.Queue = asyncio.Queue(maxsize=s.event_queue_max)
    ingest = OpcIngest(plant, s, queue)
    out.parent.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(ingest.run())
    deadline = time.time() + minutes * 60 if minutes else None
    count = 0
    with out.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "header",
                    "t": time.time(),
                    "endpoint": redact_url(s.opcua_endpoint),
                    "node_map_verification": plant.node_verification,
                }
            )
            + "\n"
        )
        try:
            while deadline is None or time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if isinstance(msg, Snapshot):
                    rec = {
                        "type": "snapshot",
                        "t": msg.ts,
                        "reason": msg.reason,
                        "values": {k: [v, g] for k, (v, g) in msg.values.items()},
                    }
                    log.info("snapshot with %d values (%s)", len(msg.values), msg.reason)
                elif isinstance(msg, Change):
                    rec = {"type": "change", "t": msg.ts, "key": msg.key, "value": msg.value, "good": msg.good}
                elif isinstance(msg, Lost):
                    rec = {"type": "lost", "t": msg.ts, "reason": msg.reason}
                    log.warning("connection lost: %s", msg.reason)
                elif isinstance(msg, Stale):
                    rec = {"type": "stale", "t": msg.ts, "pos": msg.pos, "stale": msg.stale}
                else:
                    continue
                fh.write(json.dumps(rec, default=str) + "\n")
                fh.flush()
                count += 1
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await ingest.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    log.info("wrote %d records to %s", count, out)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--minutes", type=float, default=None, help="stop after this many minutes (default: Ctrl+C)")
    args = ap.parse_args()
    setup_logging(get_settings().log_level, "text")
    try:
        raise SystemExit(asyncio.run(record(args.out, args.minutes)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
