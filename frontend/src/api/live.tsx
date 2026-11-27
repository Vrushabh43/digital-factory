// Live data: plant model (once) + live state via WebSocket, with REST bootstrap,
// automatic reconnect (exponential back-off) and stale-connection detection.
//
// The ticking clock lives in its own context (useNow), so the components that only need
// the live data do not re-render every second.
import { createContext, useContext, useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { problemText } from "../lib/errors";
import { api, wsUrl } from "./client";
import type { Live, Plant, PlantCell } from "./types";

/** backend sends at least a heartbeat every 5 s; a socket silent for longer is dropped */
export const STALE_AFTER_MS = 15_000;
export const FIRST_RETRY_MS = 500;
export const MAX_RETRY_MS = 10_000;

export interface LiveContextValue {
  plant: Plant | null;
  live: Live | null;
  /** WebSocket to the dashboard backend is open and has delivered data within the last 15 s */
  online: boolean;
  /** the page was just opened and the first WebSocket data is still on its way */
  connecting: boolean;
  /** plain-language error while loading the plant model (backend unreachable) */
  error: string | null;
  cellByPosition: Map<number, PlantCell>;
}

const LiveContext = createContext<LiveContextValue | null>(null);
const NowContext = createContext<number | null>(null);

export function LiveProvider({ children, socketFactory }: { children: ReactNode; socketFactory?: (url: string) => WebSocket }) {
  const [plant, setPlant] = useState<Plant | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [online, setOnline] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const offset = useRef(0); // server time - client time (s)

  // plant model, retried until the backend answers
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async (delay: number) => {
      try {
        const p = await api.plant();
        if (!stop) {
          setPlant(p);
          setError(null);
        }
      } catch (e) {
        if (stop) return;
        setError(problemText(e, "plant"));
        timer = setTimeout(() => load(Math.min(delay * 2, 15000)), delay);
      }
    };
    load(1000);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, []);

  // WebSocket with reconnect and watchdog
  useEffect(() => {
    const factory = socketFactory ?? ((u: string) => new WebSocket(u));
    let stop = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let delay = FIRST_RETRY_MS;
    let lastActivity = 0; // ms: when the current socket was created or last delivered data
    let socketData = false; // any WebSocket data received since the page was opened

    const accept = (data: Live) => {
      offset.current = data.ts - Date.now() / 1000;
      setLive(data);
    };

    // forget a socket without waiting for its closing handshake (it may hang)
    const drop = (sock: WebSocket) => {
      sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
      try {
        sock.close();
      } catch {
        /* already closed */
      }
    };

    const scheduleReconnect = () => {
      setOnline(false);
      setConnecting(false);
      if (stop) return;
      clearTimeout(retry);
      retry = setTimeout(connect, delay);
      delay = Math.min(delay * 2, MAX_RETRY_MS);
    };

    const connect = () => {
      if (stop) return;
      lastActivity = Date.now();
      let sock: WebSocket;
      try {
        sock = factory(wsUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      ws = sock;
      sock.onmessage = (msg) => {
        let parsed: { type?: string; data?: Live } | null = null;
        try {
          parsed = JSON.parse(msg.data);
        } catch {
          return; // ignore malformed message
        }
        if (parsed?.type !== "live" || !parsed.data) return;
        lastActivity = Date.now();
        socketData = true;
        // back-off starts again only once the server really delivers data: a server that
        // accepts and then closes at once (1013, too many clients) keeps the growing delay
        delay = FIRST_RETRY_MS;
        accept(parsed.data);
        setOnline(true);
        setConnecting(false);
      };
      sock.onclose = () => {
        if (ws !== sock) return;
        ws = null;
        scheduleReconnect();
      };
      sock.onerror = () => sock.close(); // the close event follows
    };

    const watchdog = setInterval(() => {
      if (!ws || Date.now() - lastActivity <= STALE_AFTER_MS) return;
      const sock = ws;
      ws = null;
      drop(sock);
      scheduleReconnect();
    }, 1000);

    // REST bootstrap so the page has data before the socket delivers; it never marks the page live
    const ctrl = new AbortController();
    api
      .live(ctrl.signal)
      .then((d) => {
        if (!stop && !socketData) accept(d);
      })
      .catch(() => undefined);
    connect();
    return () => {
      stop = true;
      ctrl.abort();
      clearTimeout(retry);
      clearInterval(watchdog);
      if (ws) drop(ws);
      ws = null;
    };
  }, [socketFactory]);

  const cellByPosition = useMemo(() => new Map((plant?.cells ?? []).map((c) => [c.position, c])), [plant]);
  const value = useMemo(
    () => ({ plant, live, online, connecting, error, cellByPosition }),
    [plant, live, online, connecting, error, cellByPosition],
  );
  return (
    <LiveContext.Provider value={value}>
      <Clock offset={offset}>{children}</Clock>
    </LiveContext.Provider>
  );
}

/** Ticks every second; only components that call useNow() re-render. */
function Clock({ offset, children }: { offset: MutableRefObject<number>; children: ReactNode }) {
  const [now, setNow] = useState(() => Date.now() / 1000 + offset.current);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000 + offset.current), 1000);
    return () => clearInterval(id);
  }, [offset]);
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>;
}

export function useLive(): LiveContextValue {
  const ctx = useContext(LiveContext);
  if (!ctx) throw new Error("useLive must be used inside <LiveProvider>");
  return ctx;
}

/** Estimated server time in epoch seconds, ticking every second. */
export function useNow(): number {
  const now = useContext(NowContext);
  if (now == null) throw new Error("useNow must be used inside <LiveProvider>");
  return now;
}

/** True when the page shows data that is no longer updated (connection to the dashboard server lost). */
export function isStale(v: Pick<LiveContextValue, "live" | "online" | "connecting">): boolean {
  return !!v.live && !v.online && !v.connecting;
}

/**
 * The time durations are measured against: the clock while the data is live, frozen at the
 * time of the last known state when it is not (so nothing keeps "ticking" on old data).
 */
export function useDisplayNow(): number {
  const now = useNow();
  const { live, online } = useLive();
  if (!live) return now;
  return online ? Math.max(now, live.ts) : live.ts;
}

/** Static context for tests and storybook-like rendering. */
export function StaticLiveProvider({ value, now, children }: { value: LiveContextValue; now?: number; children: ReactNode }) {
  return (
    <LiveContext.Provider value={value}>
      <NowContext.Provider value={now ?? value.live?.ts ?? Date.now() / 1000}>{children}</NowContext.Provider>
    </LiveContext.Provider>
  );
}
