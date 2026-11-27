import type { Journey, Live, PassPage, Plant } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /** `detail` of the FastAPI error body: a string, a validation list, or null */
    public detail: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Builds an ApiError from a failed response (FastAPI body `{"detail": ...}` if present). */
export async function apiErrorFrom(res: Response): Promise<ApiError> {
  let detail: unknown = null;
  try {
    const body = await res.json();
    detail = body?.detail ?? null;
  } catch {
    /* not JSON */
  }
  const message = typeof detail === "string" ? detail : res.statusText || `HTTP ${res.status}`;
  return new ApiError(res.status, message, detail);
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!res.ok) throw await apiErrorFrom(res);
  return res.json() as Promise<T>;
}

export interface HistoryQuery {
  day?: string; // YYYY-MM-DD
  position?: number;
  piece?: string;
  limit: number;
  offset: number;
}

function qs(params: Record<string, string | number | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  plant: (signal?: AbortSignal) => getJson<Plant>("/api/plant", signal),
  live: (signal?: AbortSignal) => getJson<Live>("/api/live", signal),
  passes: (q: HistoryQuery, signal?: AbortSignal) =>
    getJson<PassPage>(`/api/history/passes${qs({ ...q })}`, signal),
  journey: (code: string, signal?: AbortSignal) =>
    getJson<Journey>(`/api/pieces/${encodeURIComponent(code)}/journey`, signal),
  exportUrl: (from: string, to: string, format: "csv" | "pdf", position?: number, piece?: string) =>
    `/api/history/export${qs({ from, to, format, position, piece })}`,
};

export function wsUrl(path = "/ws/live"): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}`;
}
