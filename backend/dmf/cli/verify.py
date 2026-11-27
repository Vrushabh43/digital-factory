"""Check the OPC UA node map against the live server (READ-ONLY).

    python -m dmf.cli.verify [--json report.json]

For every node in config/opcua_nodes.yaml it reads (never writes) the BrowseName,
Value and UserAccessLevel and reports:

* nodes that do not exist or whose browse name differs from the expected one,
* values outside the documented ranges (states 0..6, lamp 1..4),
* whether the configured OPC UA user would be ALLOWED to write (it should not be;
  the backend never writes either way — this checks the server side of the setup).

Exit codes: 0 = everything matches and the user cannot write, 1 = mismatches found,
2 = could not connect, 3 = everything matches but the configured user is ALLOWED to write
(the dashboard still never writes; ask for a read-only user, see docs/08).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dmf.opcua.client import ReadOnlyClient
from dmf.plant.model import load_plant
from dmf.settings import get_settings, redact_url

WRITE_BIT = 0x02


def _range_ok(key: str, value) -> bool:
    if value is None:
        return False
    if key.endswith(".state"):
        return isinstance(value, int) and 0 <= value <= 6
    if key.endswith(".lamp"):
        return isinstance(value, int) and 1 <= value <= 4
    return isinstance(value, bool)


async def verify(json_out: Path | None) -> int:
    s = get_settings()
    plant = load_plant(s.plant_config_path, s.nodes_config_path)
    client = ReadOnlyClient(
        s.opcua_endpoint,
        timeout_s=s.opcua_timeout_s,
        username=s.opcua_username,
        password=s.opcua_password,
        security=s.opcua_security,
    )
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not connect to {redact_url(s.opcua_endpoint)}: {exc}", file=sys.stderr)
        return 2
    try:
        try:
            ns = await client.resolve_namespace(plant.namespace_uri)
        except Exception:  # noqa: BLE001
            print(f"Namespace {plant.namespace_uri!r} not found on the server.", file=sys.stderr)
            return 1
        signals = plant.signal_nodes(include_sensors=True)
        nodes = [client.node(sig.node_id) for sig in signals]
        names = await client.read_browse_names(nodes)
        values = await client.read(nodes)
        access = await client.read_access(nodes)
    finally:
        await client.disconnect()

    rows = []
    for sig, (name, nstatus), val, acc in zip(signals, names, values, access, strict=True):
        rows.append(
            {
                "key": sig.key,
                "node_id": f"ns={ns};i={sig.node_id}",
                "expected": sig.browse_name,
                "browse_name": name,
                "status": nstatus if nstatus != "Good" else val.status,
                "value": val.value,
                "value_in_range": _range_ok(sig.key, val.value) if val.good else None,
                "user_can_write": bool(acc & WRITE_BIT) if acc is not None else None,
            }
        )
    missing = [r for r in rows if r["browse_name"] is None]
    renamed = [r for r in rows if r["browse_name"] is not None and r["browse_name"] != r["expected"]]
    out_of_range = [r for r in rows if r["value_in_range"] is False]
    writable = [r for r in rows if r["user_can_write"]]

    print(f"Server {redact_url(s.opcua_endpoint)}  namespace {plant.namespace_uri!r} = index {ns}")
    print(
        f"Checked {len(rows)} nodes: {len(missing)} missing, {len(renamed)} with a different browse name, "
        f"{len(out_of_range)} values out of range, {len(writable)} writable for this user"
    )
    for title, items in (("MISSING", missing), ("DIFFERENT BROWSE NAME", renamed), ("VALUE OUT OF RANGE", out_of_range)):
        for r in items[:50]:
            print(
                f"  {title:22} {r['key']:24} {r['node_id']:16} expected {r['expected']!r} got {r['browse_name']!r} "
                f"value={r['value']!r} ({r['status']})"
            )
    if writable:
        print(
            "  NOTE: the configured user has WRITE access on some nodes. The dashboard never writes, but the lab "
            "should consider a read-only OPC UA user (docs/08-lab-validation.md)."
        )
    if json_out:
        json_out.write_text(json.dumps({"namespace_index": ns, "nodes": rows}, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {json_out}")
    if missing or renamed or out_of_range:
        return 1
    return 3 if writable else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, default=None)
    raise SystemExit(asyncio.run(verify(ap.parse_args().json)))


if __name__ == "__main__":
    main()
