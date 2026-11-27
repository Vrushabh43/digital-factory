"""Run the mock OPC UA plant server (TEST / DEMO TOOLING ONLY).

python -m dmf_mock --mode demo                 # random plausible movement
python -m dmf_mock --mode idle                 # all empty, nothing moves
python -m dmf_mock --mode replay --file rec.jsonl [--speed 2] [--loop]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from dmf.logging_setup import setup_logging
from dmf.plant.model import load_plant
from dmf.settings import REPO_ROOT
from dmf_mock.server import MockPlantServer
from dmf_mock.simulator import DemoSimulator, SimConfig

log = logging.getLogger("dmf_mock")


async def replay(server: MockPlantServer, path: Path, speed: float, loop: bool) -> None:
    """Replay a recording made with ``python -m dmf.cli.record``."""
    while True:
        prev_t: float | None = None
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                kind = rec.get("type")
                if kind == "snapshot":
                    for key, (value, good) in rec["values"].items():
                        if key in server.key_to_nodeid:
                            await server.set(key, value, good=good)
                    prev_t = rec["t"]
                elif kind == "change" and rec["key"] in server.key_to_nodeid:
                    if prev_t is not None:
                        await asyncio.sleep(max(0.0, (rec["t"] - prev_t) / speed))
                    prev_t = rec["t"]
                    await server.set(rec["key"], rec["value"], good=rec["good"])
        log.info("replay finished: %s", path)
        if not loop:
            await asyncio.Event().wait()  # keep serving the last values
        await asyncio.sleep(2)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", default=os.getenv("MOCK_ENDPOINT", "opc.tcp://0.0.0.0:4840/"))
    ap.add_argument("--mode", choices=["demo", "idle", "replay"], default=os.getenv("MOCK_MODE", "demo"))
    ap.add_argument("--file", type=Path, default=os.getenv("MOCK_REPLAY_FILE"))
    ap.add_argument("--speed", type=float, default=float(os.getenv("MOCK_SPEED", "1")))
    ap.add_argument("--loop", action="store_true", default=os.getenv("MOCK_LOOP", "") == "1")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--control-port",
        type=int,
        default=int(os.getenv("MOCK_CONTROL_PORT", "0")),
        help="enable the HTTP control API for end-to-end tests on this port (0 = off)",
    )
    ap.add_argument("--control-host", default=os.getenv("MOCK_CONTROL_HOST", "127.0.0.1"))
    ap.add_argument("--plant", type=Path, default=Path(os.getenv("PLANT_CONFIG_PATH", REPO_ROOT / "config/plant.yaml")))
    ap.add_argument("--nodes", type=Path, default=Path(os.getenv("NODES_CONFIG_PATH", REPO_ROOT / "config/opcua_nodes.yaml")))
    args = ap.parse_args()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FORMAT", "text"))
    plant = load_plant(args.plant, args.nodes)
    server = MockPlantServer(plant, args.endpoint)
    await server.start()
    control_task = None
    if args.control_port:
        import uvicorn

        from dmf_mock.control import make_control_app

        holder = {"server": server, "endpoint": args.endpoint}
        app = make_control_app(holder, plant)
        cfg = uvicorn.Config(app, host=args.control_host, port=args.control_port, log_level="warning")
        control_task = asyncio.create_task(uvicorn.Server(cfg).serve())
        log.warning("mock control API (TEST ONLY) on http://%s:%d", args.control_host, args.control_port)
    try:
        if args.mode == "demo":
            await DemoSimulator(plant, server, SimConfig(), seed=args.seed).run(args.speed)
        elif args.mode == "replay":
            if not args.file:
                ap.error("--mode replay needs --file")
            await replay(server, args.file, args.speed, args.loop)
        else:
            await asyncio.Event().wait()
    finally:
        if control_task is not None:
            control_task.cancel()
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
