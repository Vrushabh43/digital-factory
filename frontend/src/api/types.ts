// Types of the backend API (see backend/dmf/api/routes.py). All times are epoch seconds (UTC).

export type CellStatus = "running" | "stuck" | "error" | "offline";
export type PieceStatus = "moving" | "stuck" | "cell_error" | "cell_offline" | "uncertain" | "with_robot";

export interface Layout {
  x: number;
  y: number;
  w: number;
  h: number;
  arrow?: "left" | "right" | "up" | "down";
}

export interface NextCell {
  port: number;
  priority: number;
  position: number | null;
  cell?: number | null;
  via?: string | null;
  name?: string | null; // sink name (e.g. "Storage") when position is null
}

export interface PlantCell {
  position: number;
  cell: number;
  type: string;
  kind: "linear" | "cross";
  retrofit: boolean;
  input_ports: number[];
  output_ports: number[];
  conveyors: { n: number; role: string; turntable: boolean }[];
  next: NextCell[];
  layout: Layout;
}

export interface Decoration extends Layout {
  kind: "storage" | "robot";
  label: string;
  sublabel?: string;
}

export interface Plant {
  name: string;
  timezone: string;
  node_map_verification: string;
  /** a piece waiting longer than this (s) counts as stuck */
  stuck_after_s: number;
  /** longest download range in days (EXPORT_MAX_DAYS); older backends do not send it */
  export_max_days?: number;
  cells: PlantCell[];
  sources: { position: number; conveyor: number; name: string }[];
  sinks: { position: number; conveyor: number; name: string }[];
  robot_links: { from: number; to: number; name: string }[];
  layout: { canvas: { width: number; height: number }; decorations?: Decoration[] };
}

export interface LiveConveyor {
  n: number;
  role: string;
  turntable: boolean;
  state: number | null;
  state_name: string | null;
  piece: string | null;
}

export interface CellEvent {
  ts: number;
  kind: string;
  piece: string | null;
  message: string;
}

export interface LiveCell {
  position: number;
  cell: number;
  status: CellStatus;
  /** ok | partial_data | piece_waiting_too_long | lamp_red | plant_not_enabled | server_unreachable |
   *  no_valid_data | stale_data | unknown_lamp_value */
  reason: string;
  /** running/stuck sub-state from the lamp: waiting | idle | transporting */
  detail: string | null;
  status_since: number | null;
  lamp: number | null;
  conveyors: LiveConveyor[];
  pieces: string[];
  ready_to_receive: boolean | null;
  ready_to_send: boolean | null;
  stuck_piece: { code: string; waiting_since: number } | null;
  latest_events: CellEvent[];
}

export interface LivePiece {
  code: string;
  position: number | null;
  cell: number | null;
  conveyor: number | null;
  conveyor_role: string | null;
  entered_at: number;
  first_seen: number;
  first_position: number;
  first_cell: number | null;
  origin: string;
  came_from_position: number | null;
  came_from_cell: number | null;
  status: PieceStatus;
  stuck_since: number | null;
  waiting_since: number | null;
  uncertain: boolean;
  uncertain_reason: string | null;
  cells_passed: number;
}

export interface Live {
  ts: number;
  version: number;
  source: "live" | "demo" | "replay";
  /** "failed": a background task of the server died; the state shown is no longer updated */
  phase: "starting" | "waiting_for_database" | "running" | "stopping" | "failed";
  connection: {
    state: string;
    connected: boolean;
    since: number | null;
    last_error: string | null;
    next_retry_at: number | null;
    last_data_at: number | null;
  };
  database: { healthy: boolean; pending: number };
  plant: { alarm: boolean | null; enabled: boolean | null };
  counts: Record<CellStatus, number>;
  cells: LiveCell[];
  pieces: LivePiece[];
}

export interface Pass {
  id: number;
  piece: string;
  position: number;
  cell_number: number;
  entered_at: number;
  left_at: number | null;
  result: string;
  result_label: string;
  was_stuck: boolean;
  stuck_now: boolean;
  came_from_position: number | null;
  came_from_cell: number | null;
  went_to_position: number | null;
  via: string | null;
  uncertain: boolean;
}

export interface PassPage {
  items: Pass[];
  total: number;
  limit: number;
  offset: number;
  ts: number;
}

export interface Journey {
  piece: {
    code: string;
    first_seen_at: number;
    first_position: number;
    first_cell: number;
    origin: string;
    status: "active" | "removed" | "stored" | "lost";
    ended_at: number | null;
    last_position: number | null;
    cells_passed: number;
    uncertain: boolean;
  };
  passes: Pass[];
  now: LivePiece | null;
  ts: number;
}
