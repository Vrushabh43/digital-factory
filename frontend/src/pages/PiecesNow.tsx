import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { isStale, useDisplayNow, useLive } from "../api/live";
import type { LivePiece, PieceStatus } from "../api/types";
import { StaleNote } from "../components/StaleNote";
import { fmtDuration, fmtWhen } from "../lib/format";
import { cameFromText, DEFAULT_STUCK_AFTER_S, needsAttention, PIECE_STATUS_LABEL, roleLabel, uncertainNote } from "../lib/text";

type Filter = "all" | "moving" | "attention";

const TAG: Record<PieceStatus, string> = {
  moving: "green",
  stuck: "amber",
  cell_error: "red",
  cell_offline: "grey",
  uncertain: "blue",
  with_robot: "blue",
};

export function PiecesNow() {
  const ctx = useLive();
  const { live, plant, cellByPosition } = ctx;
  const stale = isStale(ctx);
  const [filter, setFilter] = useState<Filter>("all");
  const tz = plant?.timezone ?? "Europe/Berlin";
  const longAfter = plant?.stuck_after_s && plant.stuck_after_s > 0 ? plant.stuck_after_s : DEFAULT_STUCK_AFTER_S;
  const pieces = useMemo(
    () => [...(live?.pieces ?? [])].sort((a, b) => b.code.localeCompare(a.code, "en", { numeric: true })),
    [live],
  );
  const attention = pieces.filter(needsAttention);
  const moving = pieces.filter((p) => !needsAttention(p));
  const shown = filter === "all" ? pieces : filter === "moving" ? moving : attention;
  const cellsWithPieces = new Set(pieces.filter((p) => p.position != null).map((p) => p.position)).size;

  const perCell = useMemo(() => {
    const rows = (live?.cells ?? [])
      .filter((c) => c.pieces.length > 0)
      .map((c) => ({ cell: c, capacity: c.conveyors.length }))
      .sort((a, b) => b.cell.pieces.length - a.cell.pieces.length || a.cell.cell - b.cell.cell);
    return rows;
  }, [live]);

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1>Pieces now</h1>
          <p>
            {stale
              ? "Every piece that was in the factory at the last known state, and which cell it was in."
              : "Every piece that is in the factory right now, and which cell it is in."}
          </p>
          <StaleNote />
        </div>
      </div>

      <section className="grid-4" aria-label="Piece summary">
        <Kpi label="Pieces in the factory" value={live ? pieces.length : null} />
        <Kpi label="Cells with pieces" value={live ? cellsWithPieces : null} />
        <Kpi label="Moving normally" value={live ? moving.length : null} />
        <Kpi label="Need attention" value={live ? attention.length : null} highlight={attention.length > 0} />
      </section>

      <div className="two-col">
        <section className="card" aria-labelledby="where-h">
          <div className="card-head">
            <h2 id="where-h">Where each piece is</h2>
            <div className="filters" role="group" aria-label="Filter pieces">
              {(
                [
                  ["all", `All (${pieces.length})`],
                  ["moving", `Moving (${moving.length})`],
                  ["attention", `Need attention (${attention.length})`],
                ] as [Filter, string][]
              ).map(([f, label]) => (
                <button key={f} className={`filter-btn${filter === f ? " on" : ""}`} aria-pressed={filter === f} onClick={() => setFilter(f)}>
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Piece</th>
                  <th>Where now</th>
                  <th>On conveyor</th>
                  <th>In this cell since</th>
                  <th>Time in cell</th>
                  <th>Came from</th>
                  <th>Status</th>
                </tr>
              </thead>
              <PieceRows
                pieces={shown}
                tz={tz}
                longAfter={longAfter}
                cellOf={(pos) => cellByPosition.get(pos)?.cell}
                empty={
                  live
                    ? filter === "all"
                      ? stale
                        ? "No pieces were in the factory at the last known state."
                        : "No pieces in the factory right now."
                      : "No pieces in this group."
                    : "Waiting for data…"
                }
              />
            </table>
          </div>
          <p className="small-note">
            A piece gets its number (P-0150, P-0151 …) the first time a sensor sees it. The number then follows the piece from
            cell to cell. Time in cell is shown in bold once it is longer than {fmtDuration(longAfter)}.
          </p>
        </section>

        <aside className="card" aria-labelledby="ppc-h">
          <h2 id="ppc-h">Pieces per cell</h2>
          <p className="small-note" style={{ marginTop: 6, marginBottom: 14 }}>
            Each conveyor holds one piece: a cell with 3 conveyors holds up to 3 pieces, a cross cell with 5 conveyors up to 5.
          </p>
          <div className="ppc">
            {perCell.length === 0 && <p className="empty-note">No cell holds a piece right now.</p>}
            {perCell.map(({ cell, capacity }) => (
              <div key={cell.position} className={`st-${cell.status === "running" ? "running" : cell.status}`}>
                <div className="row-top">
                  <span>
                    <b>Cell {cell.cell}</b> <span className="muted">· position {cell.position}</span>
                  </span>
                  <b>
                    {cell.pieces.length} of {capacity}
                  </b>
                </div>
                <div className="bars" aria-hidden="true">
                  {Array.from({ length: capacity }, (_, i) => (
                    <i key={i} className={i < cell.pieces.length ? "on" : ""} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </aside>
      </div>
    </main>
  );
}

function Kpi({ label, value, highlight }: { label: string; value: number | null; highlight?: boolean }) {
  return (
    <div className={`card kpi${highlight ? " attention" : ""}`}>
      <div className="label">{label}</div>
      <div className="num">{value ?? "–"}</div>
    </div>
  );
}

/** Table body; the only part of the page that follows the ticking clock. */
function PieceRows({
  pieces,
  tz,
  longAfter,
  cellOf,
  empty,
}: {
  pieces: LivePiece[];
  tz: string;
  longAfter: number;
  cellOf: (pos: number) => number | undefined;
  empty: string;
}) {
  const now = useDisplayNow();
  return (
    <tbody>
      {pieces.map((p) => (
        <PieceRow key={p.code} p={p} now={now} tz={tz} longAfter={longAfter} cellOf={cellOf} />
      ))}
      {pieces.length === 0 && (
        <tr>
          <td colSpan={7} className="muted">
            {empty}
          </td>
        </tr>
      )}
    </tbody>
  );
}

function PieceRow({
  p,
  now,
  tz,
  longAfter,
  cellOf,
}: {
  p: LivePiece;
  now: number;
  tz: string;
  longAfter: number;
  cellOf: (pos: number) => number | undefined;
}) {
  const inCell = now - p.entered_at;
  const long = p.status === "stuck" || inCell > longAfter;
  const note = uncertainNote(p);
  return (
    <tr>
      <td>
        <Link className="piece" to={`/history?piece=${p.code}`}>
          {p.code}
        </Link>
      </td>
      <td>
        {p.position == null ? (
          <b>With the robot</b>
        ) : (
          <>
            <b>Cell {p.cell}</b> <span className="muted">· position {p.position}</span>
          </>
        )}
      </td>
      <td>{p.conveyor != null && p.conveyor_role ? roleLabel(p.conveyor, p.conveyor_role, p.conveyor_role === "turntable") : "–"}</td>
      <td className="time">{fmtWhen(p.entered_at, tz, now)}</td>
      <td className={long ? "long" : undefined} style={{ fontWeight: long ? 700 : 400 }}>
        {fmtDuration(inCell)}
      </td>
      <td>{cameFromText(p, cellOf)}</td>
      <td>
        <span className={`tag ${TAG[p.status]}`}>{PIECE_STATUS_LABEL[p.status]}</span>
        {note && <div className="tag-note">{note}</div>}
      </td>
    </tr>
  );
}
