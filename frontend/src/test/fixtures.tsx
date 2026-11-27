import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { StaticLiveProvider, type LiveContextValue } from "../api/live";
import type { CellStatus, Live, LiveCell, LivePiece, Plant, PlantCell } from "../api/types";

export const NOW = 1_795_700_000; // 2026-11-26 ~ 13:53 Europe/Berlin
export const TZ = "Europe/Berlin";

function plantCell(position: number, cell: number, kind: "linear" | "cross", x: number, next: PlantCell["next"]): PlantCell {
  const conveyors =
    kind === "linear"
      ? [
          { n: 1, role: "entry", turntable: false },
          { n: 2, role: "middle", turntable: false },
          { n: 3, role: "exit", turntable: false },
        ]
      : [
          { n: 1, role: "entry", turntable: false },
          { n: 2, role: "turntable", turntable: true },
          { n: 3, role: "middle", turntable: false },
          { n: 4, role: "exit", turntable: false },
          { n: 5, role: "entry", turntable: false },
        ];
  return {
    position, cell, kind, type: kind === "linear" ? "linear" : "merger", retrofit: position < 10,
    input_ports: [1], output_ports: [2], conveyors, next, layout: { x, y: 0, w: 130, h: 92, arrow: "right" },
  };
}

export const plant: Plant = {
  name: "Digital Model Factory",
  timezone: TZ,
  node_map_verification: "derived",
  stuck_after_s: 120,
  cells: [
    plantCell(4, 14, "linear", 0, [{ port: 2, priority: 1, position: 5, cell: 13 }]),
    plantCell(5, 13, "cross", 140, [{ port: 2, priority: 1, position: 11, cell: 15 }]),
    plantCell(6, 6, "linear", 280, [{ port: 2, priority: 1, position: 7, cell: 4 }]),
    plantCell(13, 19, "linear", 420, [{ port: 2, priority: 1, position: 14, cell: 16 }]),
    plantCell(9, 8, "linear", 560, [{ port: 2, priority: 1, position: 10, cell: 5 }]), // empty and running
  ],
  sources: [],
  sinks: [],
  robot_links: [],
  layout: { canvas: { width: 700, height: 100 }, decorations: [{ kind: "robot", label: "Robot", x: 0, y: 0, w: 10, h: 10 }] },
};

function liveCell(pc: PlantCell, over: Partial<LiveCell> = {}, pieces: Record<number, string> = {}): LiveCell {
  return {
    position: pc.position, cell: pc.cell, status: "running", reason: "ok", detail: "idle", status_since: NOW - 3600,
    lamp: 3,
    conveyors: pc.conveyors.map((c) => ({ ...c, state: pieces[c.n] ? 2 : 0, state_name: null, piece: pieces[c.n] ?? null })),
    pieces: Object.values(pieces), ready_to_receive: true, ready_to_send: true, stuck_piece: null, latest_events: [],
    ...over,
  };
}

export function piece(code: string, over: Partial<LivePiece> = {}): LivePiece {
  return {
    code, position: 4, cell: 14, conveyor: 2, conveyor_role: "middle", entered_at: NOW - 43, first_seen: NOW - 900,
    first_position: 2, first_cell: 11, origin: "source:Side entry", came_from_position: 3, came_from_cell: 12,
    status: "moving", stuck_since: null, waiting_since: null, uncertain: false, uncertain_reason: null, cells_passed: 3,
    ...over,
  };
}

/** Cell counts exactly as the backend computes them: one per cell status. */
export function countStatuses(cells: LiveCell[]): Record<CellStatus, number> {
  const counts: Record<CellStatus, number> = { running: 0, stuck: 0, error: 0, offline: 0 };
  for (const c of cells) counts[c.status] += 1;
  return counts;
}

export function makeLive(over: Partial<Live> = {}): Live {
  const [c4, c5, c6, c13, c9] = plant.cells;
  const live: Live = {
    ts: NOW, version: 7, source: "demo", phase: "running",
    connection: { state: "connected", connected: true, since: NOW - 4000, last_error: null, next_retry_at: null, last_data_at: NOW },
    database: { healthy: true, pending: 0 },
    plant: { alarm: false, enabled: true },
    counts: { running: 0, stuck: 0, error: 0, offline: 0 }, // replaced by countStatuses below
    cells: [
      liveCell(c4, {}, { 2: "P-0157" }),
      liveCell(c5, {
        status: "stuck", reason: "piece_waiting_too_long", detail: "waiting", ready_to_receive: false, ready_to_send: true,
        stuck_piece: { code: "P-0151", waiting_since: NOW - 252 },
        latest_events: [{ ts: NOW - 252, kind: "exit_reached", piece: "P-0151", message: "P-0151 reached the exit sensor" }],
      }, { 4: "P-0151" }),
      liveCell(c6, { status: "offline", reason: "no_valid_data", status_since: NOW - 1800, lamp: null }),
      liveCell(c13, { status: "error", reason: "lamp_red", lamp: 1 }, { 2: "P-0155" }),
      liveCell(c9),
    ],
    pieces: [
      piece("P-0157"),
      piece("P-0151", { position: 5, cell: 13, conveyor: 4, conveyor_role: "exit", status: "stuck", stuck_since: NOW - 252, entered_at: NOW - 291 }),
      piece("P-0155", { position: 13, cell: 19, status: "cell_error", entered_at: NOW - 138 }),
    ],
    ...over,
  };
  // like the backend, counts always match the cells unless a test sets them on purpose
  return over.counts ? live : { ...live, counts: countStatuses(live.cells) };
}

/** Context value for StaticLiveProvider plus the clock value (`now`). */
export type TestContext = LiveContextValue & { now: number };

export function ctx(over: Partial<TestContext> = {}): TestContext {
  const p = over.plant === undefined ? plant : over.plant;
  return {
    plant: p, live: makeLive(), online: true, connecting: false, error: null, now: NOW,
    cellByPosition: new Map((p?.cells ?? []).map((c) => [c.position, c])),
    ...over,
  };
}

export function tree(ui: ReactElement, value: TestContext = ctx(), route = "/") {
  return (
    <MemoryRouter initialEntries={[route]}>
      <StaticLiveProvider value={value} now={value.now}>
        {ui}
      </StaticLiveProvider>
    </MemoryRouter>
  );
}

export function renderWith(ui: ReactElement, value: TestContext = ctx(), route = "/") {
  return render(tree(ui, value, route));
}
