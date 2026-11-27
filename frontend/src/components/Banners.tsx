import { isStale, useLive, useNow } from "../api/live";
import { fmtAgo, fmtWhen } from "../lib/format";

interface Banner {
  /** stable key: a banner keeps its DOM node (and is not re-announced) while its text changes */
  id: string;
  kind: "error" | "warn" | "offline" | "info";
  text: string;
  /** changing part (countdown), hidden from screen readers so it is not announced every second */
  ticking?: string;
}

/** Plant-wide warnings shown above every page. */
export function Banners() {
  const ctx = useLive();
  const { live, online, connecting, error, plant } = ctx;
  const now = useNow();
  const tz = plant?.timezone ?? "Europe/Berlin";
  const stale = isStale(ctx);
  const items: Banner[] = [];
  if (error && !plant) {
    items.push({ id: "server", kind: "offline", text: "The dashboard server cannot be reached. Retrying automatically…" });
  } else if (!online && !connecting) {
    items.push({
      id: "server",
      kind: "offline",
      text: live
        ? `Connection to the dashboard server lost. Reconnecting automatically… The page shows the last known state from ${fmtWhen(live.ts, tz, now)}; the factory may have changed since.`
        : "Connection to the dashboard server lost. Reconnecting automatically…",
    });
  }
  if (live) {
    // facts from an old state are marked as such instead of being presented as live
    const known = (t: string) => (stale ? `Last known state: ${t.charAt(0).toLowerCase()}${t.slice(1)}` : t);
    if (live.phase === "failed") {
      items.push({
        id: "failed",
        kind: "error",
        text: "Tracking has stopped because of a problem in the dashboard server. Cells and pieces shown here are no longer updated. Please ask the person responsible for the dashboard to restart it.",
      });
    }
    if (live.phase === "waiting_for_database" || live.phase === "starting") {
      items.push({ id: "starting", kind: "info", text: known("The dashboard is starting and waiting for its database. Tracking begins shortly.") });
    }
    if (live.phase === "running" && !live.connection.connected && live.connection.state !== "disabled") {
      const since = live.connection.last_data_at ? ` since ${fmtWhen(live.connection.last_data_at, tz, live.ts)}` : "";
      const retry = !stale && live.connection.next_retry_at ? ` Next attempt in ${fmtAgo(Math.max(0, live.connection.next_retry_at - now))}.` : undefined;
      items.push({
        id: "factory",
        kind: "offline",
        text: known(`No connection to the factory${since}. All cells are shown as offline; pieces keep their last known position.`),
        ticking: retry,
      });
    }
    if (live.connection.state === "disabled") {
      items.push({ id: "disabled", kind: "info", text: "The factory connection is switched off in the configuration (history only)." });
    }
    if (live.plant.alarm) items.push({ id: "alarm", kind: "error", text: known("Plant alarm is on. A technician should check the factory.") });
    if (live.plant.enabled === false) {
      items.push({ id: "enabled", kind: "warn", text: known("The plant is not enabled (Freischalten is off). Cells are stopped.") });
    }
    if (live.phase === "running" && !live.database.healthy) {
      items.push({
        id: "database",
        kind: "warn",
        text: known(
          `History is not being saved right now (database unreachable). Live tracking continues; ${live.database.pending} events are waiting.`,
        ),
      });
    }
  }
  if (!items.length) return null;
  return (
    <div className="banners">
      {items.map((b) => (
        <div key={b.id} className={`banner ${b.kind}`} role={b.kind === "error" ? "alert" : "status"} data-banner={b.id}>
          <span>
            {b.text}
            {b.ticking && <span aria-hidden="true">{b.ticking}</span>}
          </span>
        </div>
      ))}
    </div>
  );
}
