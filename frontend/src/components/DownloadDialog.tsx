import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { api, apiErrorFrom } from "../api/client";
import { useLive } from "../api/live";
import { isAbort, problemText } from "../lib/errors";
import { daysInclusive, isoToInput, parseDayInput } from "../lib/format";
import { DEFAULT_EXPORT_MAX_DAYS } from "../lib/text";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** File name from `Content-Disposition: attachment; filename="..."`, or null. */
function fileNameFrom(header: string | null): string | null {
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header ?? "");
  return m ? decodeURIComponent(m[1]) : null;
}

export function DownloadDialog({
  day,
  position,
  piece,
  onClose,
}: {
  day: string; // YYYY-MM-DD
  position?: number;
  piece?: string;
  onClose: () => void;
}) {
  const { plant } = useLive();
  const maxDays = plant?.export_max_days && plant.export_max_days > 0 ? plant.export_max_days : DEFAULT_EXPORT_MAX_DAYS;
  const [from, setFrom] = useState(isoToInput(day));
  const [to, setTo] = useState(isoToInput(day));
  const [format, setFormat] = useState<"csv" | "pdf">("csv");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  // the latest onClose without re-running the effects below when the parent re-renders
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const abortRef = useRef<AbortController | null>(null);

  // once: focus the first field, close on Escape, give the focus back when the dialog closes
  useEffect(() => {
    const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.querySelector<HTMLInputElement>("input")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      abortRef.current?.abort();
      if (returnTo?.isConnected) returnTo.focus();
    };
  }, []);

  // keep Tab / Shift+Tab inside the dialog
  const trapFocus = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Tab" || !dialogRef.current) return;
    const items = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !dialogRef.current.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !dialogRef.current.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  };

  const f = parseDayInput(from);
  const t = parseDayInput(to);
  let error: string | null = null;
  if (!f || !t) error = "Please enter dates as DD.MM.YYYY.";
  else if (t < f) error = "'To' must not be before 'From'.";
  else if (daysInclusive(f, t) > maxDays) error = `At most ${maxDays} days per download.`;
  const href = !error && f && t ? api.exportUrl(f, t, format, position, piece) : undefined;

  // Fetch the file first so a refused or failed export is explained in the dialog instead of
  // silently saving an error page; the link still works without JavaScript.
  const download = async (e: ReactMouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    if (!href || busy || !f || !t) return;
    setBusy(true);
    setFailure(null);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch(href, { signal: ctrl.signal });
      if (!res.ok) throw await apiErrorFrom(res);
      const blob = await res.blob();
      const name = fileNameFrom(res.headers.get("Content-Disposition")) ?? `dmf-cell-passes-${f}${t === f ? "" : `-to-${t}`}.${format}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      onCloseRef.current();
    } catch (err) {
      if (isAbort(err)) return;
      setFailure(`The download did not work. ${problemText(err, "export")}`);
      setBusy(false);
    }
  };

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onCloseRef.current()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="dl-title" ref={dialogRef} onKeyDown={trapFocus}>
        <div className="dialog-head">
          <h2 id="dl-title">Download history</h2>
          <button className="icon-btn" aria-label="Close" onClick={() => onCloseRef.current()}>
            ×
          </button>
        </div>
        <div className="row">
          <div className="field">
            <label htmlFor="dl-from">From</label>
            <input
              id="dl-from"
              value={from}
              onChange={(e) => {
                setFrom(e.target.value);
                setFailure(null);
              }}
              placeholder="DD.MM.YYYY"
              aria-invalid={!f}
            />
          </div>
          <div className="field">
            <label htmlFor="dl-to">To</label>
            <input
              id="dl-to"
              value={to}
              onChange={(e) => {
                setTo(e.target.value);
                setFailure(null);
              }}
              placeholder="DD.MM.YYYY"
              aria-invalid={!t}
            />
          </div>
        </div>
        <div className="field">
          <label id="dl-type">File type</label>
        </div>
        <div className="row" role="radiogroup" aria-labelledby="dl-type" style={{ marginBottom: 4 }}>
          {(["csv", "pdf"] as const).map((k) => (
            <label key={k} className={`choice${format === k ? " on" : ""}`}>
              <input
                type="radio"
                name="dl-format"
                checked={format === k}
                onChange={() => {
                  setFormat(k);
                  setFailure(null);
                }}
              />
              {k.toUpperCase()}
            </label>
          ))}
        </div>
        <p className="small-note" style={{ marginTop: 4 }}>
          {format === "csv" ? "A table for Excel, one row per cell pass." : "A printable report with a summary and the table."}
          {(position || piece) && " Only the filtered cell or piece is included."} At most {maxDays} days per download.
        </p>
        {(error || failure) && (
          <p className="small-note" style={{ color: "var(--error-text)" }} role="alert">
            {error ?? failure}
          </p>
        )}
        <div className="actions">
          <button className="btn" onClick={() => onCloseRef.current()}>
            Cancel
          </button>
          {href ? (
            <a className="btn accent" href={href} onClick={download} download aria-disabled={busy || undefined}>
              {busy ? "Preparing…" : `Download ${format.toUpperCase()}`}
            </a>
          ) : (
            <button className="btn accent" disabled>
              Download {format.toUpperCase()}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
