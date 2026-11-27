import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { useDisplayNow, useLive } from "../api/live";
import type { Journey, Pass, PassPage } from "../api/types";
import { DownloadDialog } from "../components/DownloadDialog";
import { DownloadIcon } from "../components/Icons";
import { StaleNote } from "../components/StaleNote";
import { isAbort, problemText } from "../lib/errors";
import { fmtDateTime, fmtDuration, fmtTime, fmtWhen, isoDay, isoToInput, parseDayInput, parseIsoDay } from "../lib/format";

const PAGE = 15;
const REFRESH_MS = 10_000;
const PIECE_RE = /^P-\d{4,}$/;

function resultTag(p: Pass): string {
  if (p.result === "inside") return p.stuck_now ? "amber" : "blue";
  if (p.result === "passed" || p.result === "to_robot" || p.result === "stored") return p.was_stuck ? "amber" : "green";
  if (p.result === "lost") return "red";
  return "grey";
}

function shorten(v: string): string {
  return v.length > 30 ? `${v.slice(0, 30)}…` : v;
}

/** Reads the filters from the address; values that make no sense are ignored and reported. */
function readParams(params: URLSearchParams, positions: Set<number> | null) {
  const ignored: string[] = [];
  const rawDay = params.get("day");
  const day = rawDay ? parseIsoDay(rawDay) : null;
  if (rawDay && !day) ignored.push(`the day “${shorten(rawDay)}”`);

  const rawPos = params.get("position");
  let position: number | undefined;
  if (rawPos) {
    const n = /^\d{1,3}$/.test(rawPos) ? Number(rawPos) : NaN;
    if (Number.isInteger(n) && (!positions || positions.has(n))) position = n;
    else ignored.push(`the cell position “${shorten(rawPos)}”`);
  }

  const code = (raw: string | null, what: string): string => {
    if (!raw) return "";
    const c = raw.trim().toUpperCase();
    if (PIECE_RE.test(c)) return c;
    ignored.push(`the ${what} “${shorten(raw)}”`);
    return "";
  };
  const piece = code(params.get("piece"), "piece number");
  const journey = code(params.get("journey"), "piece number");

  const rawOffset = params.get("offset");
  const offset = rawOffset && /^\d{1,7}$/.test(rawOffset) ? Number(rawOffset) : 0;
  if (rawOffset && !/^\d{1,7}$/.test(rawOffset)) ignored.push(`the page “${shorten(rawOffset)}”`);
  return { explicitDay: day, position, piece, journey, offset, ignored };
}

export function History() {
  const { plant } = useLive();
  const now = useDisplayNow();
  const tz = plant?.timezone ?? "Europe/Berlin";
  const [params, setParams] = useSearchParams();
  const today = isoDay(now, tz);
  const positions = useMemo(() => (plant ? new Set(plant.cells.map((c) => c.position)) : null), [plant]);
  const { explicitDay, position, piece: pieceParam, journey: journeyParam, offset, ignored } = readParams(params, positions);
  // a piece search covers all days unless a day was chosen explicitly
  const day = explicitDay ?? (pieceParam ? "" : today);
  const selectedPiece = journeyParam || pieceParam || null;

  const [dayText, setDayText] = useState(day ? isoToInput(day) : "");
  const [dayTouched, setDayTouched] = useState(false);
  const [cellSel, setCellSel] = useState(position ? String(position) : "");
  const [pieceText, setPieceText] = useState(pieceParam);
  const [dayError, setDayError] = useState<string | null>(null);
  const [pieceError, setPieceError] = useState<string | null>(null);
  const [page, setPage] = useState<{ key: string; data: PassPage } | null>(null);
  const [loadError, setLoadError] = useState<{ key: string; text: string } | null>(null);
  const [journey, setJourney] = useState<Journey | null>(null);
  const [journeyError, setJourneyError] = useState<string | null>(null);
  const [showDownload, setShowDownload] = useState(false);
  const closeDownload = useCallback(() => setShowDownload(false), []);

  useEffect(() => {
    setDayText(day ? isoToInput(day) : "");
    setDayTouched(false);
    setCellSel(position ? String(position) : "");
    setPieceText(pieceParam);
  }, [day, position, pieceParam]);

  // the table: loaded for the current filters, today's first page refreshed every 10 s.
  // Every request is numbered; an older answer never replaces a newer one, and answers for
  // other filters (aborted requests) are dropped.
  const key = `${day}|${position ?? ""}|${pieceParam}|${offset}`;
  const autoRefresh = (day === today || !day) && offset === 0;
  const seq = useRef(0);
  const applied = useRef(0);
  useEffect(() => {
    const ctrl = new AbortController();
    const run = async () => {
      const id = ++seq.current;
      try {
        const res = await api.passes({ day: day || undefined, position, piece: pieceParam || undefined, limit: PAGE, offset }, ctrl.signal);
        if (ctrl.signal.aborted || id < applied.current) return;
        applied.current = id;
        setPage({ key, data: res });
        setLoadError(null);
      } catch (e) {
        if (ctrl.signal.aborted || id < applied.current || isAbort(e)) return;
        applied.current = id;
        setLoadError({ key, text: problemText(e, "history") });
      }
    };
    run();
    const timer = autoRefresh ? setInterval(run, REFRESH_MS) : undefined;
    return () => {
      ctrl.abort();
      clearInterval(timer);
    };
  }, [key, day, position, pieceParam, offset, autoRefresh]);

  useEffect(() => {
    // a new selection starts empty: no journey or error of the previous piece is shown
    setJourney(null);
    setJourneyError(null);
    if (!selectedPiece) return;
    const ctrl = new AbortController();
    let n = 0;
    let done = 0;
    const fetchJourney = async () => {
      const id = ++n;
      try {
        const j = await api.journey(selectedPiece, ctrl.signal);
        if (ctrl.signal.aborted || id < done) return;
        done = id;
        setJourney(j);
        setJourneyError(null);
      } catch (e) {
        if (ctrl.signal.aborted || id < done || isAbort(e)) return;
        done = id;
        setJourney(null);
        setJourneyError(e instanceof ApiError && e.status === 404 ? `Piece ${selectedPiece} was not found.` : problemText(e, "journey"));
      }
    };
    fetchJourney();
    const id = setInterval(fetchJourney, REFRESH_MS);
    return () => {
      ctrl.abort();
      clearInterval(id);
    };
  }, [selectedPiece]);

  const pieceInput = pieceText.trim().toUpperCase();
  // a new piece number searches all days unless the user also changed the day
  const newPieceSearch = !!pieceInput && pieceInput !== pieceParam;
  const useDay = !!dayText.trim() && (!newPieceSearch || dayTouched);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const next = new URLSearchParams();
    let iso: string | null = null;
    let bad = false;
    if (useDay) {
      iso = parseDayInput(dayText);
      setDayError(iso ? null : "Please enter the day as DD.MM.YYYY.");
      bad ||= !iso;
    } else setDayError(null);
    const pieceOk = !pieceInput || PIECE_RE.test(pieceInput);
    setPieceError(pieceOk ? null : "Piece numbers look like P-0151.");
    bad ||= !pieceOk;
    if (bad) return;
    if (iso) next.set("day", iso);
    if (cellSel) next.set("position", cellSel);
    if (pieceInput) next.set("piece", pieceInput);
    if (!iso && !pieceInput) next.set("day", today);
    setParams(next);
  };

  const go = (newOffset: number) => {
    const next = new URLSearchParams(params);
    if (newOffset) next.set("offset", String(newOffset));
    else next.delete("offset");
    setParams(next);
  };

  const openJourney = (code: string) => {
    const next = new URLSearchParams(params);
    next.set("journey", code);
    setParams(next, { replace: true });
  };

  const cellOptions = useMemo(() => [...(plant?.cells ?? [])].sort((a, b) => a.cell - b.cell), [plant]);
  const shownPage = page?.key === key ? page.data : null;
  const shownError = loadError?.key === key ? loadError.text : null;
  const items = shownPage?.items ?? [];
  const isToday = day === today;
  const scope = day ? (isToday ? "today" : `on ${isoToInput(day)}`) : "in total";
  const anyUncertain = items.some((p) => p.uncertain);

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1>History</h1>
          <p>A record is saved when a piece passes the first sensor of a cell (entered) and the last sensor (left).</p>
          <StaleNote />
        </div>
        <button className="btn" onClick={() => setShowDownload(true)}>
          <DownloadIcon /> Download CSV or PDF
        </button>
      </div>

      {ignored.length > 0 && (
        <div className="banner info" role="status" style={{ marginBottom: 14 }}>
          Part of the page address was not understood and is ignored: {ignored.join(", ")}.
        </div>
      )}

      <form className="card filter-bar" onSubmit={submit} aria-label="History filters" noValidate>
        <div className="field">
          <label htmlFor="f-day">Day</label>
          <input
            id="f-day"
            value={dayText}
            onChange={(e) => {
              setDayText(e.target.value);
              setDayTouched(true);
            }}
            placeholder="DD.MM.YYYY"
            aria-invalid={!!dayError}
            aria-describedby={dayError ? "f-day-err" : newPieceSearch && !dayTouched && dayText.trim() ? "f-day-hint" : undefined}
          />
          {dayError ? (
            <div className="err" id="f-day-err" role="alert">
              {dayError}
            </div>
          ) : (
            newPieceSearch &&
            !dayTouched &&
            dayText.trim() && (
              <div className="hint" id="f-day-hint">
                A piece number is searched on all days unless you change the day.
              </div>
            )
          )}
        </div>
        <div className="field">
          <label htmlFor="f-cell">Cell</label>
          <select id="f-cell" value={cellSel} onChange={(e) => setCellSel(e.target.value)}>
            <option value="">All cells</option>
            {cellOptions.map((c) => (
              <option key={c.position} value={c.position}>
                Cell {c.cell} · position {c.position}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="f-piece">Piece number</label>
          <input
            id="f-piece"
            value={pieceText}
            onChange={(e) => setPieceText(e.target.value)}
            placeholder="e.g. P-0151"
            aria-invalid={!!pieceError}
            aria-describedby={pieceError ? "f-piece-err" : undefined}
          />
          {pieceError && (
            <div className="err" id="f-piece-err" role="alert">
              {pieceError}
            </div>
          )}
        </div>
        <button className="btn primary" type="submit" style={{ height: 38 }}>
          Show
        </button>
      </form>

      <div className="two-col wide-side">
        <section className="card" aria-labelledby="passes-h">
          <div className="card-head">
            <h2 id="passes-h">Cell passes, newest first</h2>
            <span className="small-note" style={{ margin: 0 }}>
              Click a piece number to see its whole journey.
            </span>
          </div>
          {shownError && (
            <div className="banner error" role="alert" style={{ marginBottom: 10 }}>
              {shownError}
            </div>
          )}
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Piece</th>
                  <th>Cell</th>
                  <th>Entered</th>
                  <th>Left</th>
                  <th>Time in cell</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.id} className={p.piece === selectedPiece ? "hl" : undefined}>
                    <td>
                      <button className="piece" onClick={() => openJourney(p.piece)}>
                        {p.piece}
                      </button>
                    </td>
                    <td>
                      <b>Cell {p.cell_number}</b> <span className="muted">· position {p.position}</span>
                    </td>
                    <td className="time">{day ? fmtTime(p.entered_at, tz) : fmtDateTime(p.entered_at, tz)}</td>
                    <td className="time">
                      {p.left_at == null
                        ? "–"
                        : isoDay(p.left_at, tz) === isoDay(p.entered_at, tz)
                          ? fmtTime(p.left_at, tz)
                          : fmtDateTime(p.left_at, tz)}
                    </td>
                    <td>
                      {fmtDuration((p.left_at ?? now) - p.entered_at)}
                      {p.left_at ? "" : " so far"}
                    </td>
                    <td>
                      <span className={`tag ${resultTag(p)}`}>{p.result_label}</span>
                      {p.uncertain && (
                        <span className="tag grey" style={{ marginLeft: 4 }} aria-describedby="uncertain-note">
                          path not certain
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
                {shownPage && items.length === 0 && (
                  <tr>
                    <td colSpan={6} className="muted">
                      No cell passes found for this selection.
                    </td>
                  </tr>
                )}
                {!shownPage && !shownError && (
                  <tr>
                    <td colSpan={6} className="muted">
                      Loading…
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          {anyUncertain && (
            <p className="small-note" id="uncertain-note">
              “path not certain”: recorded after a data gap, so the exact path of the piece is not certain.
            </p>
          )}
          <div className="table-foot">
            <span>
              {shownPage && shownPage.total > 0
                ? offset === 0
                  ? `Showing the latest ${items.length} of ${shownPage.total} passes ${scope}`
                  : `Showing passes ${offset + 1}–${offset + items.length} of ${shownPage.total} ${scope}`
                : ""}
            </span>
            <div className="btns">
              <button className="btn" disabled={offset === 0} onClick={() => go(Math.max(0, offset - PAGE))}>
                Newer
              </button>
              <button className="btn" disabled={!shownPage || offset + PAGE >= shownPage.total} onClick={() => go(offset + PAGE)}>
                Older
              </button>
            </div>
          </div>
        </section>

        <aside className="card journey" aria-label="Journey of piece">
          {journey && journey.piece.code === selectedPiece ? (
            <JourneyPanel j={journey} now={now} tz={tz} />
          ) : (
            <>
              <div className="eyebrow">Journey of piece</div>
              <p className="empty-note">
                {journeyError ?? (selectedPiece ? `Loading the journey of ${selectedPiece}…` : "Click a piece number to see its whole journey.")}
              </p>
            </>
          )}
        </aside>
      </div>

      {showDownload && <DownloadDialog day={day || today} position={position} piece={pieceParam || undefined} onClose={closeDownload} />}
    </main>
  );
}

const ENDED_TEXT: Record<string, string> = {
  removed: "Taken off the line",
  stored: "In storage",
  lost: "Lost track",
};

function JourneyPanel({ j, now, tz }: { j: Journey; now: number; tz: string }) {
  const p = j.piece;
  const end = p.ended_at ?? now;
  let nowText: string;
  if (p.status !== "active") nowText = ENDED_TEXT[p.status] ?? p.status;
  else if (j.now?.position == null) nowText = j.now ? "With the robot" : "Unknown";
  else nowText = `Cell ${j.now.cell}${j.now.status === "stuck" ? " (stuck)" : ""}`;
  const steps = [...j.passes].reverse();
  return (
    <>
      <div className="eyebrow">Journey of piece</div>
      <div className="code" data-testid="journey-code">
        {p.code}
      </div>
      <div className="facts">
        <div className="fact">
          <small>First seen</small>
          <b>
            {fmtWhen(p.first_seen_at, tz, now)} · Cell {p.first_cell}
          </b>
        </div>
        <div className="fact" data-testid="journey-now">
          <small>Now</small>
          <b>{nowText}</b>
        </div>
        <div className="fact">
          <small>Cells passed</small>
          <b>{p.cells_passed}</b>
        </div>
        <div className="fact">
          <small>Time in factory</small>
          <b>{fmtDuration(end - p.first_seen_at)}</b>
        </div>
      </div>
      {p.uncertain && <p className="small-note">Part of this journey was reconstructed after a data gap and may not be exact.</p>}
      <ol className="steps">
        {steps.map((s) => {
          const open = s.left_at == null;
          const cls = open ? (s.stuck_now ? "stuck" : "inside") : s.result === "passed" || s.result === "to_robot" || s.result === "stored" ? "" : "ended";
          return (
            <li key={s.id}>
              <span className={`bullet ${cls}`} aria-hidden="true" />
              <div>
                <b>Cell {s.cell_number}</b> <span className="muted">· position {s.position}</span>
                <div className="io">
                  in {fmtWhen(s.entered_at, tz, now)} → {open ? "not left yet" : `out ${fmtWhen(s.left_at, tz, now)}`}
                </div>
              </div>
              <span className={`right ${cls}`}>
                {open ? (s.stuck_now ? "Stuck at exit" : "Still inside") : s.result === "passed" ? fmtDuration(s.left_at! - s.entered_at) : s.result_label}
              </span>
            </li>
          );
        })}
      </ol>
    </>
  );
}
