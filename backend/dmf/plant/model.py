"""Plant model: positions, cells, conveyors, ports, and the directed conveyor graph.

Loaded from ``config/plant.yaml`` (topology, layout) and ``config/opcua_nodes.yaml``
(OPC UA node ids). Everything the tracker and the API need to know about the line is
derived here, so no other module hard-codes plant facts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

log = logging.getLogger(__name__)

CellType = Literal["linear", "splitter", "merger"]
ConvId = tuple[int, int]  # (position, conveyor number)
PortRef = tuple[int, int]  # (position, port number)

TURNTABLE = 2  # conveyor number of the turntable in a cross-conveyor cell (Fb2)
LINEAR_EDGE = {1: 1, 2: 3}  # port -> edge conveyor
CROSS_EDGE = {1: 1, 2: 4, 3: 5}
# Conveyor chain from a port towards the cell centre (cross cells end at the turntable).
LINEAR_CHAIN = {1: [1, 2, 3], 2: [3, 2, 1]}
CROSS_CHAIN = {1: [1], 2: [4, 3], 3: [5]}


class PlantConfigError(ValueError):
    """Raised when the plant/node configuration is inconsistent."""


# --------------------------------------------------------------------------- signal keys
# Signals are addressed with readable string keys; they also appear in recordings.
def k_state(pos: int, conv: int) -> str:
    return f"pos{pos}.fb{conv}.state"


def k_sensor(pos: int, conv: int, n: int) -> str:
    return f"pos{pos}.fb{conv}.sensor{n}"


def k_lamp(pos: int) -> str:
    return f"pos{pos}.lamp"


def k_port(pos: int, port: int, flag: str) -> str:
    return f"pos{pos}.port{port}.{flag}"


K_ALARM = "general.alarm"
K_ENABLE = "general.enable"


@dataclass(frozen=True)
class SignalNode:
    key: str
    node_id: int
    browse_name: str
    required: bool  # required for tracking (states, lamps, ports) vs. optional (sensors)


@dataclass
class Conveyor:
    position: int
    number: int
    turntable: bool
    role: str  # entry | middle | exit | turntable
    state_node: int
    sensor_nodes: tuple[int, int]

    @property
    def id(self) -> ConvId:
        return (self.position, self.number)


@dataclass
class Cell:
    position: int
    cell: int
    retrofit: bool
    type: CellType
    input_ports: tuple[int, ...]  # priority order
    output_ports: tuple[int, ...]  # priority order
    conveyors: dict[int, Conveyor]
    edge: dict[int, int]  # port -> conveyor number
    lamp_node: int
    port_nodes: dict[int, dict[str, int]]
    layout: dict[str, Any] | None = None

    @property
    def is_cross(self) -> bool:
        return self.type != "linear"

    @property
    def entry_conveyors(self) -> set[int]:
        return {self.edge[p] for p in self.input_ports}

    @property
    def exit_conveyors(self) -> set[int]:
        return {self.edge[p] for p in self.output_ports}

    def label(self) -> str:
        return f"Cell {self.cell}"


@dataclass(frozen=True)
class RobotLink:
    id: str
    src: ConvId
    dst: ConvId
    name: str
    max_transit_s: float


@dataclass
class Plant:
    name: str
    namespace_uri: str
    node_verification: str
    cells: dict[int, Cell]  # by position
    general_nodes: dict[str, int]
    # directed graph between conveyors: conv -> [(pred, priority)], conv -> [succ]
    predecessors: dict[ConvId, list[tuple[ConvId, int]]] = field(default_factory=dict)
    successors: dict[ConvId, list[ConvId]] = field(default_factory=dict)
    robot_links: list[RobotLink] = field(default_factory=list)
    sources: dict[ConvId, str] = field(default_factory=dict)  # entry conveyor -> external source name
    sinks: dict[ConvId, str] = field(default_factory=dict)  # exit conveyor -> external sink name
    next_positions: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ queries
    def conveyor(self, cid: ConvId) -> Conveyor:
        return self.cells[cid[0]].conveyors[cid[1]]

    def all_conveyors(self) -> list[Conveyor]:
        return [c for cell in self.cells.values() for c in cell.conveyors.values()]

    def link_from(self, cid: ConvId) -> RobotLink | None:
        return next((lk for lk in self.robot_links if lk.src == cid), None)

    def links_to(self, cid: ConvId) -> list[RobotLink]:
        return [lk for lk in self.robot_links if lk.dst == cid]

    def is_exit(self, cid: ConvId) -> bool:
        return cid[1] in self.cells[cid[0]].exit_conveyors

    def is_entry(self, cid: ConvId) -> bool:
        return cid[1] in self.cells[cid[0]].entry_conveyors

    def downstream(self, cid: ConvId, max_hops: int) -> list[tuple[ConvId, int]]:
        """Breadth-first list of (conveyor, hops) reachable from ``cid`` (excluding itself)."""
        seen = {cid}
        frontier = [cid]
        out: list[tuple[ConvId, int]] = []
        for hop in range(1, max_hops + 1):
            nxt: list[ConvId] = []
            for c in frontier:
                targets = list(self.successors.get(c, []))
                link = self.link_from(c)
                if link:
                    targets.append(link.dst)
                for s in targets:
                    if s not in seen:
                        seen.add(s)
                        nxt.append(s)
                        out.append((s, hop))
            frontier = nxt
        return out

    def signal_nodes(self, include_sensors: bool = True) -> list[SignalNode]:
        """Every OPC UA value the backend subscribes to (read-only)."""
        nodes = [
            SignalNode(K_ALARM, self.general_nodes["alarm"], "Alarm", True),
            SignalNode(K_ENABLE, self.general_nodes["enable"], "Freischalten", True),
        ]
        for pos, cell in sorted(self.cells.items()):
            nodes.append(SignalNode(k_lamp(pos), cell.lamp_node, "AmpelZustand", True))
            for port, flags in sorted(cell.port_nodes.items()):
                nodes.append(SignalNode(k_port(pos, port, "can_send"), flags["can_send"], f"Port{port}_KannSenden", True))
                nodes.append(
                    SignalNode(k_port(pos, port, "can_receive"), flags["can_receive"], f"Port{port}_KannEmpfangen", True)
                )
            for n, conv in sorted(cell.conveyors.items()):
                nodes.append(SignalNode(k_state(pos, n), conv.state_node, f"Fb{n}_Zustand", True))
                if include_sensors:
                    for i, nid in enumerate(conv.sensor_nodes, start=1):
                        nodes.append(SignalNode(k_sensor(pos, n, i), nid, f"Fb{n}_S{i}", False))
        return nodes


# =========================================================================== loading
def _ports_for(spec: dict[str, Any], pos: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    t = spec.get("type")
    if t == "linear":
        inp = int(spec.get("input_port", 1))
        if inp not in (1, 2):
            raise PlantConfigError(f"position {pos}: linear input_port must be 1 or 2")
        return (inp,), (3 - inp,)
    if t == "splitter":
        ports = (int(spec["input_port"]),), (int(spec["target_port"]), int(spec["alternative_port"]))
    elif t == "merger":
        ports = (int(spec["primary_port"]), int(spec["secondary_port"])), (int(spec["output_port"]),)
    else:
        raise PlantConfigError(f"position {pos}: unknown type {t!r}")
    used = ports[0] + ports[1]
    if sorted(used) != [1, 2, 3]:
        raise PlantConfigError(f"position {pos}: {t} must use ports 1, 2 and 3 exactly once, got {used}")
    return ports


def _roles(cell_type: CellType, inputs: tuple[int, ...], outputs: tuple[int, ...]) -> dict[int, str]:
    if cell_type == "linear":
        chain = LINEAR_CHAIN[inputs[0]]
        return {chain[0]: "entry", chain[1]: "middle", chain[2]: "exit"}
    roles = {TURNTABLE: "turntable", 3: "middle"}
    for p in inputs:
        roles[CROSS_EDGE[p]] = "entry"
    for p in outputs:
        roles[CROSS_EDGE[p]] = "exit"
    return roles


def _routes(cell: Cell) -> list[list[int]]:
    if cell.type == "linear":
        return [LINEAR_CHAIN[cell.input_ports[0]]]
    return [CROSS_CHAIN[i] + [TURNTABLE] + list(reversed(CROSS_CHAIN[o])) for i in cell.input_ports for o in cell.output_ports]


def load_plant(plant_path: Path, nodes_path: Path) -> Plant:
    plant_cfg = yaml.safe_load(Path(plant_path).read_text(encoding="utf-8"))
    nodes_cfg = yaml.safe_load(Path(nodes_path).read_text(encoding="utf-8"))
    return build_plant(plant_cfg, nodes_cfg)


def build_plant(plant_cfg: dict[str, Any], nodes_cfg: dict[str, Any]) -> Plant:  # noqa: C901 - linear validation
    layout = plant_cfg.get("layout") or {}
    layout_cells = layout.get("cells") or {}
    node_positions = {int(k): v for k, v in (nodes_cfg.get("positions") or {}).items()}
    cells: dict[int, Cell] = {}
    seen_cell_numbers: dict[int, int] = {}

    for raw_pos, spec in (plant_cfg.get("positions") or {}).items():
        pos = int(raw_pos)
        if pos not in node_positions:
            raise PlantConfigError(f"position {pos} has no OPC UA nodes in the node map")
        nspec = node_positions[pos]
        cell_type: CellType = spec.get("type")
        inputs, outputs = _ports_for(spec, pos)
        expected_kind = "linear" if cell_type == "linear" else "cross"
        if nspec.get("kind") != expected_kind:
            raise PlantConfigError(
                f"position {pos}: plant.yaml says {cell_type} but the OPC UA node map has a {nspec.get('kind')} cell"
            )
        cell_no = int(spec["cell"])
        if cell_no in seen_cell_numbers:
            raise PlantConfigError(f"cell {cell_no} is assigned to positions {seen_cell_numbers[cell_no]} and {pos}")
        seen_cell_numbers[cell_no] = pos

        roles = _roles(cell_type, inputs, outputs)
        conveyors: dict[int, Conveyor] = {}
        for raw_n, c in (nspec.get("conveyors") or {}).items():
            n = int(raw_n)
            conveyors[n] = Conveyor(
                position=pos,
                number=n,
                turntable=(expected_kind == "cross" and n == TURNTABLE),
                role=roles.get(n, "middle"),
                state_node=int(c["state"]),
                sensor_nodes=(int(c["sensor1"]), int(c["sensor2"])),
            )
        expected_convs = {1, 2, 3} if expected_kind == "linear" else {1, 2, 3, 4, 5}
        if set(conveyors) != expected_convs:
            raise PlantConfigError(f"position {pos}: expected conveyors {sorted(expected_convs)}, got {sorted(conveyors)}")
        port_nodes = {int(p): {k: int(v) for k, v in f.items()} for p, f in (nspec.get("ports") or {}).items()}
        cells[pos] = Cell(
            position=pos,
            cell=cell_no,
            retrofit=bool(spec.get("retrofit", False)),
            type=cell_type,
            input_ports=inputs,
            output_ports=outputs,
            conveyors=conveyors,
            edge=dict(LINEAR_EDGE if expected_kind == "linear" else CROSS_EDGE),
            lamp_node=int(nspec["lamp"]["id"] if isinstance(nspec["lamp"], dict) else nspec["lamp"]),
            port_nodes=port_nodes,
            layout=layout_cells.get(pos) or layout_cells.get(str(pos)),
        )

    general = nodes_cfg.get("general") or {}
    general_nodes = {k: int(v["id"] if isinstance(v, dict) else v) for k, v in general.items()}
    for req in ("alarm", "enable"):
        if req not in general_nodes:
            raise PlantConfigError(f"node map is missing general.{req}")

    plant = Plant(
        name=(plant_cfg.get("plant") or {}).get("name", "Digital Model Factory"),
        namespace_uri=nodes_cfg.get("namespace_uri", "http://demo-plantV2"),
        node_verification=str(nodes_cfg.get("verification", "derived")),
        cells=cells,
        general_nodes=general_nodes,
        layout=layout,
    )
    _build_graph(plant, plant_cfg)
    return plant


def _check_port(plant: Plant, ref: list[int], direction: str, what: str) -> PortRef:
    pos, port = int(ref[0]), int(ref[1])
    if pos not in plant.cells:
        raise PlantConfigError(f"{what}: unknown position {pos}")
    cell = plant.cells[pos]
    ports = cell.output_ports if direction == "out" else cell.input_ports
    if port not in ports:
        kind = "output" if direction == "out" else "input"
        raise PlantConfigError(f"{what}: port {port} is not an {kind} port of position {pos} (has {list(ports)})")
    return pos, port


def _build_graph(plant: Plant, cfg: dict[str, Any]) -> None:
    preds: dict[ConvId, list[tuple[ConvId, int]]] = {c.id: [] for c in plant.all_conveyors()}
    succs: dict[ConvId, list[ConvId]] = {c.id: [] for c in plant.all_conveyors()}

    def add_edge(a: ConvId, b: ConvId, prio: int) -> None:
        if b not in succs[a]:
            succs[a].append(b)
            preds[b].append((a, prio))

    for cell in plant.cells.values():
        for route in _routes(cell):
            # priority of an intra-cell edge into the turntable = priority of its input port
            in_prio = cell.input_ports.index(cell_port_of(cell, route[0]))
            for a, b in zip(route, route[1:], strict=False):
                add_edge((cell.position, a), (cell.position, b), in_prio if b == TURNTABLE else 0)

    feeders: dict[PortRef, str] = {}
    consumers: dict[PortRef, str] = {}

    for i, conn in enumerate(cfg.get("connections") or []):
        src = _check_port(plant, conn["from"], "out", f"connection #{i + 1}")
        dst = _check_port(plant, conn["to"], "in", f"connection #{i + 1}")
        for ref, table, what in ((src, consumers, "consumer"), (dst, feeders, "feeder")):
            if ref in table:
                raise PlantConfigError(f"port {ref} has more than one {what}")
            table[ref] = f"connection #{i + 1}"
        add_edge((src[0], plant.cells[src[0]].edge[src[1]]), (dst[0], plant.cells[dst[0]].edge[dst[1]]), 0)

    for i, rl in enumerate(cfg.get("robot_links") or []):
        src = _check_port(plant, rl["from"], "out", f"robot link #{i + 1}")
        dst = _check_port(plant, rl["to"], "in", f"robot link #{i + 1}")
        for ref, table, what in ((src, consumers, "consumer"), (dst, feeders, "feeder")):
            if ref in table:
                raise PlantConfigError(f"port {ref} has more than one {what}")
            table[ref] = f"robot link #{i + 1}"
        plant.robot_links.append(
            RobotLink(
                id=f"robot-{src[0]}-{dst[0]}",
                src=(src[0], plant.cells[src[0]].edge[src[1]]),
                dst=(dst[0], plant.cells[dst[0]].edge[dst[1]]),
                name=str(rl.get("name", "Robot")),
                max_transit_s=float(rl.get("max_transit_s", 300)),
            )
        )

    for i, ext in enumerate(cfg.get("external") or []):
        kind = ext.get("kind")
        if kind not in ("source", "sink"):
            raise PlantConfigError(f"external #{i + 1}: kind must be source or sink")
        ref = _check_port(plant, ext["at"], "in" if kind == "source" else "out", f"external #{i + 1}")
        table = feeders if kind == "source" else consumers
        if ref in table:
            raise PlantConfigError(f"port {ref} has more than one {'feeder' if kind == 'source' else 'consumer'}")
        table[ref] = str(ext.get("name", kind))
        conv = (ref[0], plant.cells[ref[0]].edge[ref[1]])
        (plant.sources if kind == "source" else plant.sinks)[conv] = str(ext.get("name", kind))

    for cell in plant.cells.values():
        for p in cell.input_ports:
            if (cell.position, p) not in feeders:
                log.warning("position %s input port %s has no feeder configured", cell.position, p)
        for p in cell.output_ports:
            if (cell.position, p) not in consumers:
                log.warning("position %s output port %s has no consumer configured", cell.position, p)

    plant.predecessors = preds
    plant.successors = succs
    plant.next_positions = _next_positions(plant, cfg)


def cell_port_of(cell: Cell, conv: int) -> int:
    """Port whose edge conveyor is ``conv`` (raises if the conveyor is not at a port)."""
    for port, edge in cell.edge.items():
        if edge == conv:
            return port
    raise PlantConfigError(f"conveyor {conv} of position {cell.position} is not at a port")


def _next_positions(plant: Plant, cfg: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {p: [] for p in plant.cells}
    targets: dict[PortRef, dict[str, Any]] = {}
    for conn in cfg.get("connections") or []:
        targets[(int(conn["from"][0]), int(conn["from"][1]))] = {"position": int(conn["to"][0]), "via": None}
    for rl in cfg.get("robot_links") or []:
        targets[(int(rl["from"][0]), int(rl["from"][1]))] = {"position": int(rl["to"][0]), "via": rl.get("name", "Robot")}
    for ext in cfg.get("external") or []:
        if ext.get("kind") == "sink":
            targets[(int(ext["at"][0]), int(ext["at"][1]))] = {"position": None, "via": None, "name": ext.get("name")}
    for pos, cell in plant.cells.items():
        for prio, port in enumerate(cell.output_ports):
            t = targets.get((pos, port))
            if t:
                entry = {"port": port, "priority": prio + 1, **t}
                if t.get("position") is not None:
                    entry["cell"] = plant.cells[t["position"]].cell
                out[pos].append(entry)
    return out
