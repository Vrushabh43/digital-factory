// Plain-language texts used across the dashboard (kept in one place for consistency).
import type { CellStatus, LiveCell, LivePiece, NextCell, PieceStatus, PlantCell } from "../api/types";
import { fmtAgo, fmtDuration } from "./format";

export const STATUS_LABEL: Record<CellStatus, string> = {
  running: "Running",
  stuck: "Stuck",
  error: "Error",
  offline: "Offline",
};

export const SUMMARY: Record<CellStatus, { title: string; hint: string }> = {
  running: { title: "Running", hint: "cells working normally" },
  stuck: { title: "Piece stuck", hint: "a piece is waiting too long" },
  error: { title: "Error", hint: "technical fault, needs a technician" },
  offline: { title: "Offline", hint: "no data from the cell" },
};

export const PIECE_STATUS_LABEL: Record<PieceStatus, string> = {
  moving: "Moving normally",
  stuck: "Waiting too long",
  cell_error: "Cell has an error",
  cell_offline: "Cell is offline",
  uncertain: "Position not certain",
  with_robot: "With the robot",
};

/** Fallback when the backend does not send `stuck_after_s`. */
export const DEFAULT_STUCK_AFTER_S = 120;
/** Fallback when the backend does not send `export_max_days`. */
export const DEFAULT_EXPORT_MAX_DAYS = 31;

export function needsAttention(p: LivePiece): boolean {
  return p.status !== "moving" && p.status !== "with_robot";
}

export function roleLabel(n: number, role: string, turntable: boolean): string {
  if (turntable) return `${n} · turntable`;
  if (role === "entry") return `${n} · entry`;
  if (role === "exit") return `${n} · exit`;
  return `${n} · middle`;
}

export function conveyorName(n: number, role: string, turntable: boolean): string {
  if (turntable) return `Conveyor ${n} · turntable`;
  if (role === "entry") return `Conveyor ${n} · entry sensor`;
  if (role === "exit") return `Conveyor ${n} · exit sensor`;
  return `Conveyor ${n}`;
}

export function cellTitle(cell: number): string {
  return `Cell ${cell}`;
}

export function nextCellText(next: NextCell[]): string {
  if (!next.length) return "–";
  return next
    .map((n) => (n.position == null ? n.name ?? "Storage" : `Cell ${n.cell} (position ${n.position})${n.via ? ` via ${n.via.toLowerCase()}` : ""}`))
    .join(" or ");
}

export function errorText(reason: string): string {
  if (reason === "plant_not_enabled") return "The plant is not enabled (Freischalten is off), so the cell is stopped.";
  return "The cell reports a technical fault (alarm is on). A technician should check the control panel of this cell.";
}

/**
 * Why a cell is offline. `connectionState` is `live.connection.state` ("disabled" when the
 * factory connection is switched off in the configuration).
 */
export function offlineText(cell: LiveCell, now: number, connectionState?: string): string {
  const since = cell.status_since ? ` for ${fmtAgo(now - cell.status_since)}` : "";
  switch (cell.reason) {
    case "server_unreachable":
      if (connectionState === "disabled") {
        return "The factory connection is switched off in the configuration, so there is no live data from this cell.";
      }
      return `No connection to the factory${since}. The current state of this cell is unknown.`;
    case "unknown_lamp_value":
      return `The cell sends an unknown status value (${cell.lamp ?? "none"}), so its state is unknown.`;
    case "stale_data":
      return `The values from this cell have not been updated${since}. It may be switched off or its connection may be interrupted.`;
    default:
      return `No data from this cell${since}. It may be switched off or its network cable is disconnected.`;
  }
}

/** " The next cell (Cell 15)" / " The storage" … for sentences about handing a piece over. */
function nextTarget(plantCell: PlantCell | undefined, cellByPos: Map<number, PlantCell>): string | null {
  const next = plantCell?.next[0];
  if (!next) return null;
  if (next.position != null) {
    const nextCell = next.cell ?? cellByPos.get(next.position)?.cell;
    return nextCell != null ? `the next cell (Cell ${nextCell})` : "the next cell";
  }
  return next.name ?? "the storage";
}

const PARTIAL_NOTE = "Some conveyors of this cell send no valid data, so the pieces in this cell may not be tracked correctly.";

/** Explanation box in the selected-cell panel. */
export function cellExplanation(
  cell: LiveCell,
  plantCell: PlantCell | undefined,
  now: number,
  cellByPos: Map<number, PlantCell>,
  connectionState?: string,
): string {
  switch (cell.status) {
    case "offline":
      return offlineText(cell, now, connectionState);
    case "error":
      return errorText(cell.reason);
    case "stuck": {
      const sp = cell.stuck_piece;
      const target = nextTarget(plantCell, cellByPos);
      const nextName = target ? ` ${target.charAt(0).toUpperCase()}${target.slice(1)} is not ready to take it.` : "";
      if (!sp) return "A piece is waiting too long in this cell.";
      return `Piece ${sp.code} has been waiting at the exit sensor for ${fmtDuration(now - sp.waiting_since)}.${nextName}`;
    }
    default: {
      const partial = cell.reason === "partial_data";
      if (cell.detail === "waiting") {
        const target = nextTarget(plantCell, cellByPos) ?? "the next cell";
        return `Running, but waiting: the cell is waiting to hand a piece over to ${target}. This is not yet too long.${partial ? ` ${PARTIAL_NOTE}` : ""}`;
      }
      if (partial) return `Running, but not all data is available. ${PARTIAL_NOTE}`;
      return cell.pieces.length
        ? "Working normally. Pieces enter at the first sensor and leave at the last sensor without waiting."
        : "Working normally. No piece is in this cell at the moment.";
    }
  }
}

/** Short addition to the accessible name of a running cell on the map. */
export function runningNote(cell: LiveCell | undefined): string {
  if (!cell || cell.status !== "running") return "";
  const parts: string[] = [];
  if (cell.detail === "waiting") parts.push("waiting to hand a piece over");
  if (cell.reason === "partial_data") parts.push("some data missing");
  return parts.map((p) => `, ${p}`).join("");
}

/** One line under the title of a "Needs attention" card. `stale`: the data is the last known state. */
export function attentionText(cell: LiveCell, now: number, stale = false): string {
  const pos = `(position ${cell.position})`;
  let text: string;
  if (cell.status === "stuck" && cell.stuck_piece) {
    text = `Piece ${cell.stuck_piece.code} waiting ${fmtAgo(now - cell.stuck_piece.waiting_since)} at the exit ${pos}`;
  } else if (cell.status === "offline") {
    if (cell.reason === "server_unreachable") text = `No connection to the factory ${pos}`;
    else text = `No data${cell.status_since ? ` for ${fmtAgo(now - cell.status_since)}` : ""} ${pos}`;
  } else if (cell.status === "error") {
    text = cell.reason === "plant_not_enabled" ? `Plant not enabled ${pos}` : `Alarm is on, needs a technician ${pos}`;
  } else {
    text = pos;
  }
  return stale ? `Last known: ${text.charAt(0).toLowerCase()}${text.slice(1)}` : text;
}

/** A PLC hand-over flag exactly as the PLC reports it; "Unknown" without valid data. */
export function yesNo(v: boolean | null, cell: LiveCell): string {
  if (cell.status === "offline" || v == null) return "Unknown";
  return v ? "Yes" : "No";
}

export function piecesText(n: number, offline: boolean): string {
  if (offline) return "No data";
  if (n === 0) return "Empty";
  return n === 1 ? "1 piece" : `${n} pieces`;
}

export function originText(origin: string): string {
  if (origin.startsWith("source:")) return origin.slice(7);
  if (origin.startsWith("robot:")) return origin.slice(6);
  return { put_on: "Put on by hand", startup: "On the line at start-up", found: "Found after a data gap" }[origin] ?? origin;
}

/** "Came from" column: the previous cell, or where the piece entered the line. */
export function cameFromText(p: LivePiece, cellOf: (pos: number) => number | undefined): string {
  if (p.came_from_position != null) {
    const cell = p.came_from_cell ?? cellOf(p.came_from_position);
    return cell != null ? `Cell ${cell}` : `Position ${p.came_from_position}`;
  }
  return p.origin ? originText(p.origin) : "–";
}

/** Visible explanation for a piece whose position is not certain, or null. */
export function uncertainNote(p: LivePiece): string | null {
  if (!p.uncertain && p.status !== "uncertain") return null;
  return p.uncertain_reason ? `Not certain: ${p.uncertain_reason}` : "Not certain: tracked across a data gap";
}
