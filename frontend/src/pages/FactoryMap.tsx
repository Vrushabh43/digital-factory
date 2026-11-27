import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { isStale, useDisplayNow, useLive } from "../api/live";
import type { CellStatus, Live, LiveCell, Plant, PlantCell } from "../api/types";
import { RobotIcon, StorageIcon } from "../components/Icons";
import { StaleNote } from "../components/StaleNote";
import { fmtWhen, isoDay } from "../lib/format";
import {
  attentionText,
  cellExplanation,
  conveyorName,
  nextCellText,
  piecesText,
  runningNote,
  STATUS_LABEL,
  SUMMARY,
  yesNo,
} from "../lib/text";

const ORDER: CellStatus[] = ["running", "stuck", "error", "offline"];
const ATTENTION_ORDER: Record<CellStatus, number> = { stuck: 0, offline: 1, error: 2, running: 3 };
const ARROWS = { left: "←", right: "→", up: "↑", down: "↓" } as const;

export function FactoryMap() {
  const ctx = useLive();
  const { plant, live, cellByPosition } = ctx;
  const stale = isStale(ctx);
  const [params, setParams] = useSearchParams();
  const liveByPos = useMemo(() => new Map((live?.cells ?? []).map((c) => [c.position, c])), [live]);
  const attention = useMemo(
    () =>
      (live?.cells ?? [])
        .filter((c) => c.status !== "running")
        .sort((a, b) => ATTENTION_ORDER[a.status] - ATTENTION_ORDER[b.status] || a.cell - b.cell),
    [live],
  );
  const factoryDown = !!live && live.phase === "running" && !live.connection.connected;

  // ?cell=<position> from the address; anything that is not a cell of the plant is ignored
  const cellParam = params.get("cell");
  const parsed = cellParam != null && /^\d{1,3}$/.test(cellParam) ? Number(cellParam) : null;
  const validParam = parsed != null && (!plant || cellByPosition.has(parsed)) ? parsed : null;
  const badParam = cellParam != null && validParam == null;
  const selected = validParam ?? attention[0]?.position ?? null;

  // a cell chosen by the user scrolls the panel into view when it is off-screen (narrow screens)
  const setParamsRef = useRef(setParams);
  setParamsRef.current = setParams;
  const [scrollRequest, setScrollRequest] = useState(0);
  const select = useCallback((pos: number) => {
    setParamsRef.current({ cell: String(pos) }, { replace: true });
    setScrollRequest((n) => n + 1);
  }, []);
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = panelRef.current;
    if (!scrollRequest || !el) return;
    const r = el.getBoundingClientRect();
    const viewport = window.innerHeight || document.documentElement.clientHeight;
    // the panel starts in the lower part of the screen (or is above it): bring it into view;
    // a panel that fits is scrolled just enough, so the map stays visible above it
    if (r.top > viewport * 0.6 || r.bottom < 60) {
      el.scrollIntoView?.({ behavior: "smooth", block: r.height > viewport * 0.8 ? "start" : "nearest" });
    }
  }, [scrollRequest]);

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1>Factory map</h1>
          <p>
            The line is drawn like the factory blueprint. Each coloured box is one cell; its colour shows how it is doing
            {stale ? " at the last known state" : " right now"}. Click a cell to see what is happening inside it.
          </p>
          <StaleNote />
        </div>
      </div>

      <section className="grid-4" aria-label="Cell summary">
        {ORDER.map((st) => (
          <div key={st} className={`card summary st-${st}`}>
            <div className="swatch" aria-hidden="true" />
            <div>
              <div className="num" data-testid={`count-${st}`}>
                {live ? live.counts[st] : "–"}
              </div>
              <div className="title">{SUMMARY[st].title}</div>
              <div className="hint">{SUMMARY[st].hint}</div>
            </div>
          </div>
        ))}
      </section>

      <section className="card section" aria-labelledby="attention-h">
        <h2 id="attention-h" style={{ marginBottom: 12 }}>
          Needs attention
        </h2>
        <AttentionList live={live} plant={plant} attention={attention} factoryDown={factoryDown} stale={stale} onSelect={select} />
      </section>

      <section className="card section" aria-labelledby="line-h">
        <div className="line-head">
          <h2 id="line-h">Production line</h2>
          <small>Arrows show the direction pieces move. Big number = cell, small text = its position in the blueprint.</small>
        </div>
        {plant ? (
          <ProductionLine plant={plant} liveByPos={liveByPos} selected={selected} onSelect={select} />
        ) : (
          <p className="empty-note">Loading the factory layout…</p>
        )}
        <p className="scroll-hint small-note">The drawing is wider than the screen: scroll it sideways to see the whole line.</p>
        <div className="legend">
          {ORDER.map((st) => (
            <span key={st}>
              <i className={`sw st-${st}`} />
              {st === "stuck" ? "Piece stuck" : STATUS_LABEL[st]}
            </span>
          ))}
          <span>
            <i className="sw dashed" />
            Robot or storage (not tracked)
          </span>
          <span>
            <i className="sw selected-sw" />
            Selected cell
          </span>
          <span>
            <i className="belt full st-offline" style={{ marginRight: -3 }} />
            <i className="belt st-offline" />
            The conveyors of a cell, filled = a piece is on it
          </span>
        </div>
      </section>

      <section className="card section cell-panel" aria-label="Selected cell" ref={panelRef} data-testid="cell-panel">
        {badParam && <p className="small-note" style={{ marginTop: 0 }}>The address names a cell position that does not exist ({cellParam}).</p>}
        {selected && liveByPos.get(selected) ? (
          <CellPanel cell={liveByPos.get(selected)!} plantCell={cellByPosition.get(selected)} />
        ) : (
          <p className="empty-note">Click a cell on the map to see what is happening inside it.</p>
        )}
      </section>
    </main>
  );
}

function AttentionList({
  live,
  plant,
  attention,
  factoryDown,
  stale,
  onSelect,
}: {
  live: Live | null;
  plant: Plant | null;
  attention: LiveCell[];
  factoryDown: boolean;
  stale: boolean;
  onSelect: (pos: number) => void;
}) {
  const now = useDisplayNow();
  const tz = plant?.timezone ?? "Europe/Berlin";
  if (factoryDown && live) {
    const disabled = live.connection.state === "disabled";
    const since = live.connection.last_data_at ? ` since ${fmtWhen(live.connection.last_data_at, tz, now)}` : "";
    const text = disabled
      ? "The factory connection is switched off in the configuration, so there is no live data from the cells. The history is still available."
      : `No connection to the factory${since}. The current state of the cells is unknown; the dashboard keeps trying to reconnect.`;
    return (
      <div className="attention-list">
        <div className="attention-item st-offline" style={{ cursor: "default" }}>
          <strong>All cells · Offline</strong>
          <span>{stale ? `Last known: ${text.charAt(0).toLowerCase()}${text.slice(1)}` : text}</span>
        </div>
      </div>
    );
  }
  if (attention.length === 0) {
    return (
      <p className="empty-note">
        {!live
          ? "Waiting for data…"
          : stale
            ? "Nothing needed attention at the last known state."
            : "Nothing needs attention right now. All cells are running."}
      </p>
    );
  }
  return (
    <div className="attention-list">
      {attention.map((c) => (
        <button key={c.position} className={`attention-item st-${c.status}`} onClick={() => onSelect(c.position)}>
          <strong>
            Cell {c.cell} · {c.status === "stuck" ? "Piece stuck" : STATUS_LABEL[c.status]}
          </strong>
          <span>{attentionText(c, now, stale)}</span>
        </button>
      ))}
    </div>
  );
}

const ProductionLine = memo(function ProductionLine({
  plant,
  liveByPos,
  selected,
  onSelect,
}: {
  plant: Plant;
  liveByPos: Map<number, LiveCell>;
  selected: number | null;
  onSelect: (pos: number) => void;
}) {
  const { width: W, height: H } = plant.layout.canvas;
  const pct = (v: number, total: number) => `${(v / total) * 100}%`;
  return (
    <div className="line-wrap">
      <div className="line-canvas" style={{ aspectRatio: `${W} / ${H}` }}>
        {(plant.layout.decorations ?? []).map((d, i) => (
          <div
            key={i}
            className="decor"
            style={{ left: pct(d.x, W), top: pct(d.y, H), width: pct(d.w, W), height: pct(d.h, H) }}
          >
            {d.kind === "robot" ? <RobotIcon /> : <StorageIcon />}
            {d.label}
            {d.sublabel && <small>{d.sublabel}</small>}
          </div>
        ))}
        {plant.cells.map((pc) => {
          const lc = liveByPos.get(pc.position);
          const status: CellStatus = lc?.status ?? "offline";
          const offline = status === "offline";
          const vertical = pc.layout.w < 90;
          const nPieces = lc?.pieces.length ?? 0;
          const isSelected = selected === pc.position;
          return (
            <button
              key={pc.position}
              className={`cellbox st-${status}${vertical ? " vertical" : ""}${isSelected ? " selected" : ""}`}
              style={{ left: pct(pc.layout.x, W), top: pct(pc.layout.y, H), width: pct(pc.layout.w, W), height: pct(pc.layout.h, H) }}
              onClick={() => onSelect(pc.position)}
              aria-pressed={isSelected}
              aria-label={`Cell ${pc.cell}, position ${pc.position}, ${STATUS_LABEL[status]}${runningNote(lc)}, ${piecesText(nPieces, offline)}`}
              data-testid={`cell-${pc.position}`}
            >
              <div className="top">
                <span className="name">Cell {pc.cell}</span>
                {pc.layout.arrow && <span className="arrow">{ARROWS[pc.layout.arrow]}</span>}
              </div>
              <span className="pos">Position {pc.position}</span>
              <span>
                <span className="pill">{STATUS_LABEL[status]}</span>
              </span>
              <span className="belts">
                <span className="belt-row">
                  {(lc?.conveyors ?? pc.conveyors.map((c) => ({ ...c, piece: null }))).map((c) => (
                    <i key={c.n} className={`belt${c.piece && !offline ? " full" : ""}`} />
                  ))}
                </span>
                <span className="count">{piecesText(nPieces, offline)}</span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
});

function CellPanel({ cell, plantCell }: { cell: LiveCell; plantCell: PlantCell | undefined }) {
  const ctx = useLive();
  const { plant, live, cellByPosition } = ctx;
  const now = useDisplayNow();
  const stale = isStale(ctx);
  const tz = plant?.timezone ?? "Europe/Berlin";
  const offline = cell.status === "offline";
  const today = isoDay(now, tz);
  const explanation = cellExplanation(cell, plantCell, now, cellByPosition, live?.connection.state);
  return (
    <div className={`panel st-${cell.status}`}>
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <span className="eyebrow">Selected cell</span>
          <span className="pill">{cell.status === "stuck" ? "Piece stuck" : STATUS_LABEL[cell.status]}</span>
        </div>
        <h2 className="cell-name">Cell {cell.cell}</h2>
        <div className="sub">
          Position {cell.position} · {plantCell?.retrofit ? "retrofit cell" : "new cell"}
          {plantCell?.kind === "cross" ? " · cross cell with turntable" : ""}
        </div>
        <div className="explain" data-testid="cell-explanation">
          {stale && live ? `Last known state at ${fmtWhen(live.ts, tz, now)}: ` : ""}
          {explanation}
        </div>
        <Link className="btn block" to={`/history?position=${cell.position}&day=${today}`}>
          See this cell's history
        </Link>
      </div>
      <div>
        <h3>Conveyors in this cell</h3>
        <div className="conv-list">
          {cell.conveyors.map((c) => (
            <div key={c.n} className="conv-row">
              <i className={`belt${c.piece && !offline ? " full" : ""}`} />
              {conveyorName(c.n, c.role, c.turntable)}
              <span className="val">{offline || c.state == null ? "No data" : c.piece ?? "Empty"}</span>
            </div>
          ))}
        </div>
        <h3>Handing pieces over</h3>
        <div className="kv">
          <span>Ready to take a piece</span>
          <b data-testid="ready-receive">{yesNo(cell.ready_to_receive, cell)}</b>
        </div>
        <div className="kv">
          <span>Ready to pass a piece on</span>
          <b data-testid="ready-send">{yesNo(cell.ready_to_send, cell)}</b>
        </div>
        <div className="kv">
          <span>Next cell</span>
          <b>{nextCellText(plantCell?.next ?? [])}</b>
        </div>
      </div>
      <div>
        <h3>Latest events</h3>
        {cell.latest_events.length === 0 ? (
          <p className="empty-note">No events recorded yet.</p>
        ) : (
          <ul className="events">
            {cell.latest_events.slice(0, 6).map((e, i) => (
              <li key={`${e.ts}-${i}`}>
                <time>{fmtWhen(e.ts, tz, now)}</time>
                <span>{e.message}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
