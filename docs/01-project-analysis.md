# 1 · Project analysis

Sources analysed (all in `Project-description/`, unchanged):

| File | Content |
|---|---|
| `DMF workpiece tracking dashboard – project description.pdf` | Goal, scope, factory rules, OPC UA model, tracking rules, pages, technology |
| `Team 11 DMF – Dashboard v2 (blueprint layout).pdf` | 27 frames of the Figma prototype: Factory map (17 selected-cell variants), Pieces now (All / Moving / Need attention), History (3 journeys), download pop-up |
| `PLC program cell.zip` → `2023_10_23_Dokumentation_SPS_Programm.docx` | PLC documentation (German): Figure 1 blueprint, cell/conveyor I/O, state machines, OPC UA model, lamp states |
| `PLC program cell.zip` → `Zellensteuerung_Zellen_Antriebe_V19.zip` | Full TIA Portal V19 project (binary) |

## 1.1 What the TIA project revealed (without TIA Portal)

The TIA project stores its OPC UA **client interface** ("OPC-Schnittstelle") and the
SCL source as XML inside `System/PEData.plf` (partly zlib-compressed). We extracted
it with `tools/tia_nodemap/extract_tia_nodemap.py` (reproducible, stdlib only):

* **Namespace URI** `http://demo-plantV2`; numeric node ids (`i=6013 … i=6586`).
* **18 read lists `Pull_ZelleN` and 18 write lists `Push_ZelleN`** with every node's
  browse name, data type and parent. Each write list was matched to its read list
  through the shared port nodes, so the assignment is confirmed by two independent sources.
* **Variables per cell** (browse names): `AmpelZustand` (INT), `PortN_KannSenden`,
  `PortN_KannEmpfangen` (BOOL), `FbN_Zustand` (INT), `FbN_S1`, `FbN_S2`, `FbN_M`, `FbN_M_r`
  (BOOL); cross-conveyor cells also `Dr_M`, `Dr_M_r`, `E1`, `E2` (turntable).
  Global: `Alarm` (i=6306), `Freischalten` (i=6307).
* **Conveyor count**: linear cells expose `Fb1…Fb3`; cross-conveyor cells expose
  `Fb1…Fb5`, where **`Fb2` is the turntable conveyor**. That gives 9 × 3 + 9 × 5 = **72 conveyor
  states**, which fits the spec's "about 60–90 values".
* **Internal wiring of a cross-conveyor cell** (SCL): `Port1—Fb1—Fb2(turntable)—Fb3—Fb4—Port2`
  and `Fb2—Fb5—Port3`.
* **Port flag semantics** (SCL): `PortN_KannSenden = edge conveyor at N in state 3 or 4`;
  `PortN_KannEmpfangen = (N is the input port) and edge conveyor at N in state 0 or 1`.
* **Lamp logic** (SCL): `1` if not enabled; `2` if an edge conveyor is in state 3; `3` if all
  conveyors are 0; else `4`.
* **Cell configuration** (`Konfig_DB`, newer program part, by *cell number*):

  | Cell | Type | Ports | Position (mapping table) |
  |---|---|---|---|
  | 1, 5, 6, 7, 14, 15, 18, 19, 20 | Linear | input port 1 | 1, 10, 6, 8, 4, 11, 12, 13, 16 |
  | 8, 13 | Splitter | input 1, target 2, alternative 3 | 9, 5 |
  | 11, 12, 4, 16, 17 | Merger | primary 3, secondary 1, output 2 | 2, 3, 7, 14, 15 |
  | 2, 3 | Merger | primary 1, secondary 3, output 2 | 17, 18 |

  Mapped to positions, these are exactly the 3-port OPC objects (positions 2, 3, 5, 7, 9,
  14, 15, 17, 18). They also match Figure 1: turntables at 3/5/7/9, corners at 2/14/15, and robot
  ports at 17/18. The "①/②" marks in Figure 1 are the routing priorities, and they agree with the
  primary/target ports.

## 1.2 Confirmed facts used by the implementation

1. `ZelleN` on the server is **position N**. Evidence: the read lists are named `Pull_ZelleposX` /
   `Pull_ZelleN`; `OPC_Write_Cell(Zellennummer := Konfig_DB.Zelle[Zellennr].Zellenposition)`;
   the 3-port objects match the cross-conveyor positions; and the retrofit check in OB100 uses positions.
   *Still listed for lab confirmation.*
2. Conveyor state machine (linear conveyor, SCL):
   `0→1` predecessor 3/4 **or start sensor**; `0→2` end sensor already active (piece left on the
   belt); `1→2` falling edge start sensor; `2→3` end sensor; `3→4` successor 0/1;
   `3→0` falling edge end sensor = **piece taken off**; `4→0` falling edge end sensor = handed over.
3. Turntable state machine: `0→1→2` as above, `2→5` end sensor (routing), `5→3` target position
   reached, `3→4` immediately, **`4→6`** falling edge end sensor (piece gone), `6→0` back at entry
   position. Start state after PLC start is `6`.

## 1.3 Inconsistencies found (and the decision taken)

| # | Where | Inconsistency | Decision |
|---|---|---|---|
| I-1 | Description p. 4 | "states 5, 6 on a turntable hold a piece" — per SCL, **6 is entered after the piece has left** (`4→6→0`) and is the start state. | Occupied = {2,3,4,5}; empty = {0,1,6}. Turntable hand-over is `4→6`, not `4→0`. |
| I-2 | Design | Every cell is drawn with 3 conveyors ("holds up to 3 pieces"). Cross-conveyor cells have 5 (incl. turntable). | Render the real conveyor count from configuration (3 or 5 squares; capacity 3 or 5). |
| I-3 | Description p. 4 | "The PLC exposes input port / target / alternative port" — these are **PLC-internal constants**, not in the OPC model. | Routing ports are configuration (`config/plant.yaml`), pre-filled from `Konfig_DB`. |
| I-4 | Design "Cell 19 · Error: Alarm is on" | `Alarm` exists only globally (`Allgemein`). No per-cell alarm in the lists. | Cell *Error* = lamp 1. Global alarm → banner + "Needs attention" entry. |
| I-5 | Design "Offline: no data for 30 min" | The server keeps the last value of a switched-off PLC; a subscription cannot tell "idle" from "off". A PLC that lost its connection sets lamp 1 locally but cannot publish it. | *Offline* = server unreachable / Bad quality / stale data. Stale detection by periodic **read of source timestamps** is optional (`OFFLINE_DETECTION`), default off until verified in the lab. |
| I-6 | Design "Ready to pass a piece on: Yes" for empty cells | Real `KannSenden` is only true when a piece waits at the exit. | Show real values in plain words ("Nothing to pass on", "Waiting for next cell", "Handing over now"). |
| I-7 | Description "1 cell = 3 conveyors" on the map | See I-2. | As I-2. |
| I-8 | Description rule "handover = 4→0 paired with 1→2 at the same time" | The two changes are separate PLC/OPC updates; their order and timing vary (`4→0` is the falling edge of the source's end sensor, `1→2` the falling edge of the successor's start sensor). | Pairing within a configurable time window, in either order. The window pauses while the successor is still taking the piece in (state 1) or a cell involved is halted (lamp 1 / not enabled / no data). |
| I-9 | Linear `Port1_KannSenden` (reverse direction) uses `Fb1 = 3 OR Fb3 = 4` (likely a PLC typo) | Only matters for input port 2 (unused: every linear cell uses input port 1). | No impact; noted for the PLC team. |
| I-10 | `Konfig_DB` "Zellen verbinden: TO DO" (comment) | The comment is outdated: the PLC project **does** wire neighbours (`Nachbar_PortN_KannSenden/KannEmpfangen` of the adjacent cells). It has no 14↔15 neighbour pair. | Connections derived from Figure 1 + the PLC neighbour wiring; configuration, flagged for verification. |
| I-11 | Figure 1 vs. a direct 14→15 connection (corrected in the audit, see docs/09) | No PLC neighbour wiring between positions 14 and 15, and the drawing shows a gap there (robot/table), unlike the real conveyor joints at positions 1, 6 and 10. Without neighbour wiring the PLC of 14 cannot hand over (`3→4` needs the successor's state). | Modelled as a robot/manual transfer (FIFO, like 11→12 and 17→18). Lab check 3.4. |

## 1.4 Missing information / dependencies (lab verification list)

These are implemented as **configuration** with the derived values, never hard-coded:

1. `ZelleN` = position N (evidence strong; confirm with UaExpert by browsing `Zelle5` → 3 ports).
2. Every node id/browse name in `config/opcua_nodes.yaml` (run `python -m dmf.cli.verify`, see [08-lab-validation.md](08-lab-validation.md)).
3. Port connections between positions and to robots/storage (`config/plant.yaml → connections`,
   `robot_links`), in particular whether 14→15 is a robot/manual transfer (I-11).
4. Whether robots publish port flags (decides if a robot hand-over shows as `3→0` "taken off").
5. Whether the server refreshes `SourceTimestamp` on unchanged PLC writes (offline detection).
6. Read-only access: a dedicated read-only OPC UA user/endpoint if the server supports it.
7. Timezone of the lab (default `Europe/Berlin`), stuck threshold (default 120 s).

## 1.5 Limitations that follow from the plant itself

* Pieces between sensors or on a turntable at PLC start-up are not detected by the PLC, so the
  dashboard cannot see them either.
* Pieces have no physical ID: identity is inferred from state hand-overs. A piece removed by hand
  and put back gets a **new number**, and so does a piece coming back from the (untracked) high-bay storage.
* Robot transfers (positions 11→12, 17→18 and, pending verification, 14→15) are bridged with a
  first-in-first-out queue and a maximum transit time. If a robot reorders pieces, their numbers swap.
* Pieces are matched only by timing: a piece put on by hand right next to a conveyor that is handing
  over within the same few seconds can take over the wrong number (marked *uncertain* when ambiguous).
* After a backend restart or a connection gap, positions are reconciled from the live states. Moves
  inferred across the gap are marked **uncertain**.
