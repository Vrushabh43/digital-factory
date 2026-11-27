// Date/time formatting in the plant timezone (DD.MM.YYYY, HH:MM:SS) and durations.

const fmtCache = new Map<string, Intl.DateTimeFormat>();

function formatter(tz: string, kind: "time" | "date" | "parts"): Intl.DateTimeFormat {
  const key = `${tz}|${kind}`;
  let f = fmtCache.get(key);
  if (!f) {
    const opts: Intl.DateTimeFormatOptions =
      kind === "time"
        ? { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }
        : { year: "numeric", month: "2-digit", day: "2-digit" };
    f = new Intl.DateTimeFormat("en-GB", { ...opts, timeZone: tz });
    fmtCache.set(key, f);
  }
  return f;
}

export function fmtTime(ts: number | null | undefined, tz: string): string {
  if (ts == null || !Number.isFinite(ts)) return "–";
  return formatter(tz, "time").format(new Date(ts * 1000));
}

function dateParts(ts: number, tz: string): { d: string; m: string; y: string } {
  const parts = formatter(tz, "date").formatToParts(new Date(ts * 1000));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return { d: get("day"), m: get("month"), y: get("year") };
}

/** DD.MM.YYYY */
export function fmtDate(ts: number, tz: string): string {
  if (!Number.isFinite(ts)) return "–";
  const { d, m, y } = dateParts(ts, tz);
  return `${d}.${m}.${y}`;
}

export function fmtDateTime(ts: number | null | undefined, tz: string): string {
  if (ts == null || !Number.isFinite(ts)) return "–";
  return `${fmtDate(ts, tz)} ${fmtTime(ts, tz)}`;
}

/** ISO date (YYYY-MM-DD) of a timestamp in the plant timezone. */
export function isoDay(ts: number, tz: string): string {
  const { d, m, y } = dateParts(ts, tz);
  return `${y}-${m}-${d}`;
}

/** Time only when `ts` is on the same plant day as `ref`, otherwise date and time. */
export function fmtWhen(ts: number | null | undefined, tz: string, ref: number): string {
  if (ts == null || !Number.isFinite(ts)) return "–";
  return Number.isFinite(ref) && isoDay(ts, tz) === isoDay(ref, tz) ? fmtTime(ts, tz) : fmtDateTime(ts, tz);
}

/** "DD.MM.YYYY" -> "YYYY-MM-DD", or null if invalid. */
export function parseDayInput(text: string): string | null {
  const m = /^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$/.exec(text);
  if (!m) return null;
  const [d, mo, y] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) return null;
  return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

/** "YYYY-MM-DD" that is a real calendar day -> the same string, otherwise null. */
export function parseIsoDay(text: string | null | undefined): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text ?? "");
  if (!m) return null;
  return parseDayInput(`${m[3]}.${m[2]}.${m[1]}`);
}

/** Calendar days from `from` to `to` (both YYYY-MM-DD), counting both ends. */
export function daysInclusive(from: string, to: string): number {
  return Math.round((Date.parse(to) - Date.parse(from)) / 86_400_000) + 1;
}

/** "YYYY-MM-DD" -> "DD.MM.YYYY" */
export function isoToInput(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

/** "1 min 04 s", "0 min 08 s", "1 h 02 min 03 s" */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "–";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const ss = String(sec).padStart(2, "0");
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min ${ss} s`;
  return `${m} min ${ss} s`;
}

/** Coarse duration for sentences: "4 min", "30 min", "2 h 5 min", "12 s". */
export function fmtAgo(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}
