"""Extract the OPC UA node map from the Siemens TIA Portal V19 project archive.

The PLC program (``Zellensteuerung_Zellen_Antriebe_V19``) contains an OPC UA
*client interface* ("OPC-Schnittstelle") with one read list (``Pull_ZelleN``)
and one write list (``Push_ZelleN``) per cell position. Those lists hold the
exact node ids (namespace ``http://demo-plantV2``) and browse names that the
PLCs use on the plant OPC UA server. This tool recovers them WITHOUT TIA Portal:

1. the uncompressed part of ``System/PEData.plf`` holds the read lists in
   plain text (node id, browse name, parent node id);
2. the compressed part (zlib pages) holds every node record of the client
   interface (node id, data type, browse name, parent node id) plus the write
   lists' node-id arrays;
3. each write list is assigned to a position by matching its port nodes with
   the read list of the same position (two independent sources must agree).

Usage::

    python tools/tia_nodemap/extract_tia_nodemap.py \
        "Project-description/PLC program cell.zip" config/opcua_nodes.yaml

The result is DERIVED data: it must still be verified against the live server
(see ``python -m dmf.cli.verify``) before it is trusted in production.
"""
from __future__ import annotations

import datetime as _dt
import io
import re
import sys
import zipfile
import zlib
from pathlib import Path

NS_URI = "http://demo-plantV2"
_ID = re.escape(NS_URI) + r";i=(\d+)"

FB_FIELDS = {"M": "motor", "M_r": "motor_dir", "S1": "sensor1", "S2": "sensor2", "Zustand": "state"}
TT_FIELDS = {"Dr_M": "rotate_motor", "Dr_M_r": "rotate_dir", "E1": "end_pos1", "E2": "end_pos2"}


def _load_plf(archive: Path) -> bytes:
    """Return the bytes of PEData.plf from the nested zip archives."""
    with zipfile.ZipFile(archive) as outer:
        inner_name = next(n for n in outer.namelist() if n.endswith("Zellensteuerung_Zellen_Antriebe_V19.zip"))
        with zipfile.ZipFile(io.BytesIO(outer.read(inner_name))) as inner:
            plf = next(n for n in inner.namelist() if n.endswith("System/PEData.plf"))
            return inner.read(plf)


def _inflate_pages(raw: bytes) -> str:
    """Decompress every zlib stream in the file and join them in file order."""
    parts: list[str] = []
    for m in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", raw):
        try:
            out = zlib.decompressobj().decompress(raw[m.start(): m.start() + 2_000_000])
        except zlib.error:
            continue
        if len(out) > 64:
            parts.append(out.decode("latin-1"))
    return "".join(parts)


def _ascii_strings(raw: bytes, min_len: int = 5) -> list[str]:
    return [s.decode("latin-1") for s in re.findall(rb"[\x20-\x7e]{%d,}" % min_len, raw)]


def _read_lists(strings: list[str]) -> dict[int, list[tuple[int, str, int]]]:
    """Parse plain-text NodeList_READ_Pull_ZelleN sections: (node id, name, parent)."""
    lists: dict[int, list[tuple[int, str, int]]] = {}
    cur: int | None = None
    i = 0
    while i < len(strings):
        s = strings[i]
        m = re.match(r"NodeList_READ_Pull_Zelle(\d+)#", s)
        if m:
            cur = int(m.group(1))
            lists[cur] = []
        elif s.startswith("NodeList_") or s.startswith("OpcUaGeneralInfo"):
            cur = None
        elif cur is not None and re.fullmatch(_ID, s) and i + 2 < len(strings):
            par = re.fullmatch(_ID, strings[i + 2])
            if par:
                lists[cur].append((int(re.fullmatch(_ID, s).group(1)), strings[i + 1], int(par.group(1))))
                i += 3
                continue
        i += 1
    return lists


def _node_records(text: str) -> dict[int, tuple[str, str, int]]:
    """All client-interface node records: node id -> (type, browse name, parent id)."""
    rec = re.compile(_ID + r".([A-Z]+)\x00.([A-Za-z0-9_]+)\x03\x1a" + _ID, re.S)
    return {int(m.group(1)): (m.group(2), m.group(3), int(m.group(4))) for m in rec.finditer(text)}


def _write_arrays(text: str) -> list[set[int]]:
    """Node-id arrays stored as start values of the write-list configuration DB."""
    arrays: dict[str, dict[int, int]] = {}
    for m in re.finditer(r'p="902:(\d+):52\[(\d+)\]:52" v="WSTRING#\'(\d+)\'"', text):
        arrays.setdefault(m.group(1), {})[int(m.group(2))] = int(m.group(3))
    return [set(v.values()) for v in arrays.values()]


def extract(archive: Path) -> dict:
    raw = _load_plf(archive)
    text = _inflate_pages(raw)
    records = _node_records(text)
    pulls = _read_lists(_ascii_strings(raw))
    pushes = _write_arrays(text)
    if len(pulls) != 18:
        raise SystemExit(f"expected 18 read lists, found {len(pulls)}")

    general = {}
    positions: dict[int, dict] = {}
    for pos in sorted(pulls):
        port_parents = {p for (_, name, p) in pulls[pos] if name.startswith("Port")}
        for nid, name, _ in pulls[pos]:
            if name in ("Alarm", "Freischalten"):
                general[name] = nid
        matches = [s for s in pushes if port_parents <= {records[n][2] for n in s if n in records}]
        if len(matches) != 1:
            raise SystemExit(f"position {pos}: {len(matches)} write lists match its ports")
        entry: dict = {"conveyors": {}, "ports": {}, "turntable": {}}
        for nid in sorted(matches[0]):
            _typ, name, _par = records[nid]
            if name == "AmpelZustand":
                entry["lamp"] = nid
            elif name.startswith("Port"):
                key = "can_send" if name.endswith("KannSenden") else "can_receive"
                entry["ports"].setdefault(int(name[4]), {})[key] = nid
            elif name.startswith("Fb"):
                entry["conveyors"].setdefault(int(name[2]), {})[FB_FIELDS[name[4:]]] = nid
            elif name in TT_FIELDS:
                entry["turntable"][TT_FIELDS[name]] = nid
        # Cross-check: ports from the write list must equal the read list's ports.
        read_ports = {nid for (nid, name, _) in pulls[pos] if name.startswith("Port")}
        write_ports = {v for p in entry["ports"].values() for v in p.values()}
        if read_ports != write_ports:
            raise SystemExit(f"position {pos}: read/write port nodes disagree")
        entry["kind"] = "cross" if len(entry["ports"]) == 3 else "linear"
        positions[pos] = entry
    return {"general": general, "positions": positions, "browse_names": {nid: r[1] for nid, r in records.items()}}


def to_yaml(data: dict, source: str) -> str:
    """Serialise without third-party YAML libraries (keeps the tool dependency-free)."""
    bn = data["browse_names"]
    out = [
        "# OPC UA node map of the DMF plant server (opc.tcp://192.168.0.10:4840).",
        f"# GENERATED by tools/tia_nodemap/extract_tia_nodemap.py from: {source}",
        f"# Generated: {_dt.date.today().isoformat()}",
        "#",
        "# Node ids are numeric ids in namespace 'http://demo-plantV2' as configured in the",
        "# PLCs' OPC UA client lists (Pull_ZelleN / Push_ZelleN). The namespace INDEX is",
        "# resolved at runtime from the URI. Keys are POSITIONS on the line (ZelleN = position N).",
        "#",
        "# verification: derived  -> taken from the TIA project, NOT yet confirmed on the live server.",
        "# Run `python -m dmf.cli.verify` (backend) against the plant and set 'verified' once every node matches.",
        "verification: derived",
        f"namespace_uri: {NS_URI}",
        "general:",
    ]
    for k, v in data["general"].items():
        out.append(f"  {'alarm' if k == 'Alarm' else 'enable'}: {{id: {v}, browse_name: {k}}}")
    out.append("positions:")
    for pos, e in data["positions"].items():
        out.append(f"  {pos}:")
        out.append(f"    kind: {e['kind']}")
        out.append(f"    lamp: {{id: {e['lamp']}, browse_name: {bn[e['lamp']]}}}")
        out.append("    ports:")
        for pn in sorted(e["ports"]):
            p = e["ports"][pn]
            out.append(f"      {pn}: {{can_send: {p['can_send']}, can_receive: {p['can_receive']}}}")
        out.append("    conveyors:")
        for cn in sorted(e["conveyors"]):
            c = e["conveyors"][cn]
            fields = ", ".join(f"{k}: {c[k]}" for k in ("state", "sensor1", "sensor2", "motor", "motor_dir"))
            out.append(f"      {cn}: {{{fields}}}")
        if e["turntable"]:
            t = e["turntable"]
            fields = ", ".join(f"{k}: {t[k]}" for k in ("rotate_motor", "rotate_dir", "end_pos1", "end_pos2"))
            out.append(f"    turntable: {{{fields}}}")
    out.append("# Expected browse names per field (used by the verifier and at backend start-up):")
    out.append("browse_names:")
    out.append("  conveyor: {state: 'Fb{n}_Zustand', sensor1: 'Fb{n}_S1', sensor2: 'Fb{n}_S2', motor: 'Fb{n}_M', motor_dir: 'Fb{n}_M_r'}")
    out.append("  turntable: {rotate_motor: Dr_M, rotate_dir: Dr_M_r, end_pos1: E1, end_pos2: E2}")
    out.append("  port: {can_send: 'Port{n}_KannSenden', can_receive: 'Port{n}_KannEmpfangen'}")
    out.append("  lamp: AmpelZustand")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    archive, target = Path(argv[1]), Path(argv[2])
    data = extract(archive)
    target.write_text(to_yaml(data, archive.name), encoding="utf-8")
    n = sum(len(e["conveyors"]) for e in data["positions"].values())
    print(f"wrote {target}: 18 positions, {n} conveyors, general={data['general']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
