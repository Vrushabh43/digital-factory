import copy
from pathlib import Path

import pytest
import yaml

from dmf.plant.model import PlantConfigError, build_plant

REPO = Path(__file__).resolve().parents[3]
PLANT_CFG = yaml.safe_load((REPO / "config" / "plant.yaml").read_text(encoding="utf-8"))
NODES_CFG = yaml.safe_load((REPO / "config" / "opcua_nodes.yaml").read_text(encoding="utf-8"))


def test_counts(plant):
    assert len(plant.cells) == 18
    assert len(plant.all_conveyors()) == 72  # 9 linear x 3 + 9 cross x 5
    assert sorted(p for p, c in plant.cells.items() if c.is_cross) == [2, 3, 5, 7, 9, 14, 15, 17, 18]
    assert sorted(c.cell for c in plant.cells.values()) == [1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


def test_signal_nodes_unique_and_counted(plant):
    nodes = plant.signal_nodes(include_sensors=True)
    ids = [n.node_id for n in nodes]
    assert len(ids) == len(set(ids))
    # 2 general + 18 lamps + port flags (9*2*2 + 9*3*2) + 72 states + 144 sensors
    assert len(nodes) == 2 + 18 + 90 + 72 + 144
    assert len(plant.signal_nodes(include_sensors=False)) == 2 + 18 + 90 + 72


def test_linear_chain_and_roles(plant):
    cell = plant.cells[1]
    assert plant.successors[(1, 1)] == [(1, 2)]
    assert plant.successors[(1, 2)] == [(1, 3)]
    assert plant.successors[(1, 3)] == [(2, 5)]  # pos1 port2 -> pos2 port3 (conveyor 5)
    assert {n: c.role for n, c in cell.conveyors.items()} == {1: "entry", 2: "middle", 3: "exit"}


def test_merger_graph_priorities(plant):
    # position 3 = cell 12: merger, primary port 3 (from pos 10), secondary port 1 (from pos 2), output 2
    preds = dict(plant.predecessors[(3, 2)])
    assert preds == {(3, 5): 0, (3, 1): 1}
    assert plant.successors[(3, 2)] == [(3, 3)]
    assert plant.successors[(3, 3)] == [(3, 4)]
    assert plant.successors[(3, 4)] == [(4, 1)]
    assert plant.cells[3].entry_conveyors == {1, 5} and plant.cells[3].exit_conveyors == {4}


def test_splitter_graph(plant):
    # position 5 = cell 13: splitter in 1, target 2 (-> pos 11), alternative 3 (-> pos 6)
    assert plant.successors[(5, 2)] == [(5, 3), (5, 5)]
    assert plant.successors[(5, 4)] == [(11, 1)]
    assert plant.successors[(5, 5)] == [(6, 1)]
    nxt = plant.next_positions[5]
    assert [(n["position"], n["priority"]) for n in nxt] == [(11, 1), (6, 2)]


def test_robot_links_sources_sinks(plant):
    # 14 -> 15 is a transfer without PLC hand-over (no neighbour wiring in Konfig_DB, gap in
    # the plant drawing): modelled as a robot/FIFO link, pending lab verification
    assert {(lk.src, lk.dst) for lk in plant.robot_links} == {((11, 3), (12, 1)), ((17, 4), (18, 1)), ((14, 4), (15, 5))}
    assert plant.sinks[(9, 4)] == "Storage"
    assert plant.sources[(1, 1)] == "High-bay storage"
    assert plant.next_positions[11][0]["via"] == "Robot"
    assert plant.next_positions[9][0]["position"] is None  # storage first priority


def test_downstream_follows_robot_links(plant):
    reach = dict(plant.downstream((11, 3), 2))
    assert reach[(12, 1)] == 1 and reach[(12, 2)] == 2


def test_rejects_kind_mismatch():
    cfg = copy.deepcopy(PLANT_CFG)
    cfg["positions"][1] = {"cell": 1, "type": "merger", "primary_port": 3, "secondary_port": 1, "output_port": 2}
    with pytest.raises(PlantConfigError, match="node map has a linear cell"):
        build_plant(cfg, NODES_CFG)


def test_rejects_connection_to_output_port():
    cfg = copy.deepcopy(PLANT_CFG)
    cfg["connections"][0] = {"from": [1, 2], "to": [2, 2]}
    with pytest.raises(PlantConfigError, match="not an input port"):
        build_plant(cfg, NODES_CFG)


def test_rejects_duplicate_cell_number():
    cfg = copy.deepcopy(PLANT_CFG)
    cfg["positions"][2]["cell"] = 1
    with pytest.raises(PlantConfigError, match="assigned to positions"):
        build_plant(cfg, NODES_CFG)


def test_rejects_double_feeder():
    cfg = copy.deepcopy(PLANT_CFG)
    cfg["external"].append({"at": [2, 3], "kind": "source", "name": "x"})
    with pytest.raises(PlantConfigError, match="more than one feeder"):
        build_plant(cfg, NODES_CFG)


# ---------------------------------------------------------------------------- pinned plant facts
# Position -> cell number and the retrofit cells: requirements PDF pages 1-2 (cell table), also
# the PLC documentation table / Konfig_DB (docs/01-project-analysis.md section 1.1).
POSITION_TO_CELL = {
    1: 1,
    2: 11,
    3: 12,
    4: 14,
    5: 13,
    6: 6,
    7: 4,
    8: 7,
    9: 8,
    10: 5,
    11: 15,
    12: 18,
    13: 19,
    14: 16,
    15: 17,
    16: 20,
    17: 2,
    18: 3,
}
RETROFIT = {1, 6, 7, 8, 9, 10, 17, 18}
# Konfig_DB: merger (primary, secondary, output), splitter (input, target, alternative), linear input port
MERGERS = {2: (3, 1, 2), 3: (3, 1, 2), 7: (3, 1, 2), 14: (3, 1, 2), 15: (3, 1, 2), 17: (1, 3, 2), 18: (1, 3, 2)}
SPLITTERS = {5: (1, 2, 3), 9: (1, 2, 3)}
LINEAR = {1, 4, 6, 8, 10, 11, 12, 13, 16}
# config/plant.yaml (Figure 1 arrows + design "next cell"; lab verification pending): (pos, port) -> (pos, port)
CONNECTIONS = {
    (1, 2): (2, 3),
    (2, 2): (3, 1),
    (3, 2): (4, 1),
    (4, 2): (5, 1),
    (5, 2): (11, 1),
    (5, 3): (6, 1),
    (6, 2): (7, 3),
    (7, 2): (8, 1),
    (8, 2): (9, 1),
    (9, 3): (10, 1),
    (10, 2): (3, 3),
    (12, 2): (13, 1),
    (13, 2): (14, 3),
    (15, 2): (16, 1),
    (16, 2): (17, 1),
    (18, 2): (7, 1),
}
ROBOT_LINKS = {(11, 2): (12, 1), (17, 2): (18, 1), (14, 2): (15, 3)}
SOURCES = {
    (1, 1): "High-bay storage",
    (2, 1): "Side entry",
    (14, 1): "Side entry",
    (15, 1): "Side entry",
    (17, 3): "Small storage",
    (18, 3): "Robot",
}
SINKS = {(9, 2): "Storage"}


def _edge_conveyor(pos: int, port: int) -> tuple[int, int]:
    """Conveyor at a port (SCL wiring): linear Port1-Fb1 ... Fb3-Port2; cross Port1-Fb1, Port2-Fb4, Port3-Fb5."""
    table = {1: 1, 2: 3} if pos in LINEAR else {1: 1, 2: 4, 3: 5}
    return pos, table[port]


def test_position_to_cell_map_and_retrofit_cells(plant):
    assert {pos: c.cell for pos, c in plant.cells.items()} == POSITION_TO_CELL
    assert {pos for pos, c in plant.cells.items() if c.retrofit} == RETROFIT
    assert {pos: c.label() for pos, c in plant.cells.items()} == {p: f"Cell {n}" for p, n in POSITION_TO_CELL.items()}


def test_cell_types_and_port_roles(plant):
    assert {pos for pos, c in plant.cells.items() if c.type == "linear"} == LINEAR
    for pos, (primary, secondary, output) in MERGERS.items():
        cell = plant.cells[pos]
        assert cell.type == "merger" and cell.input_ports == (primary, secondary) and cell.output_ports == (output,), pos
        # the primary input wins at the turntable: its edge conveyor has priority 0
        prios = dict(plant.predecessors[(pos, 2)])
        assert prios == {_edge_conveyor(pos, primary): 0, _edge_conveyor(pos, secondary): 1}, pos
    for pos, (inp, target, alternative) in SPLITTERS.items():
        cell = plant.cells[pos]
        assert cell.type == "splitter" and cell.input_ports == (inp,) and cell.output_ports == (target, alternative), pos
    for pos in LINEAR:
        assert plant.cells[pos].input_ports == (1,) and plant.cells[pos].output_ports == (2,), pos


def test_all_port_connections_robot_links_and_endpoints(plant):
    between_cells = {(a, b) for a, succs in plant.successors.items() for b in succs if a[0] != b[0]}
    assert between_cells == {(_edge_conveyor(*src), _edge_conveyor(*dst)) for src, dst in CONNECTIONS.items()}
    assert {(lk.src, lk.dst) for lk in plant.robot_links} == {
        (_edge_conveyor(*src), _edge_conveyor(*dst)) for src, dst in ROBOT_LINKS.items()
    }
    assert plant.sources == {_edge_conveyor(*ref): name for ref, name in SOURCES.items()}
    assert plant.sinks == {_edge_conveyor(*ref): name for ref, name in SINKS.items()}
    # "next cell" shown on the map, in output-port priority order
    for pos, cell in plant.cells.items():
        expected = []
        for port in cell.output_ports:
            if (pos, port) in CONNECTIONS:
                expected.append((port, CONNECTIONS[(pos, port)][0], None))
            elif (pos, port) in ROBOT_LINKS:
                expected.append((port, ROBOT_LINKS[(pos, port)][0], "Robot"))
            else:
                expected.append((port, None, None))  # storage
        got = [(n["port"], n["position"], n["via"]) for n in plant.next_positions[pos]]
        assert got == expected, pos
        for n in plant.next_positions[pos]:
            if n["position"] is not None:
                assert n["cell"] == POSITION_TO_CELL[n["position"]]


def test_every_port_has_exactly_one_feeder_or_consumer():
    feeds = list(CONNECTIONS.values()) + list(ROBOT_LINKS.values()) + list(SOURCES)
    takes = list(CONNECTIONS) + list(ROBOT_LINKS) + list(SINKS)
    inputs = [(pos, p) for pos, ports in _input_ports().items() for p in ports]
    outputs = [(pos, p) for pos, ports in _output_ports().items() for p in ports]
    assert sorted(feeds) == sorted(inputs)
    assert sorted(takes) == sorted(outputs)


def _input_ports() -> dict[int, tuple[int, ...]]:
    return {**{p: (1,) for p in LINEAR}, **{p: m[:2] for p, m in MERGERS.items()}, **{p: s[:1] for p, s in SPLITTERS.items()}}


def _output_ports() -> dict[int, tuple[int, ...]]:
    return {**{p: (2,) for p in LINEAR}, **{p: m[2:] for p, m in MERGERS.items()}, **{p: s[1:] for p, s in SPLITTERS.items()}}
