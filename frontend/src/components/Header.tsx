import { NavLink } from "react-router-dom";
import { useLive, useNow } from "../api/live";
import { fmtWhen } from "../lib/format";

const SOURCE_BADGE: Record<string, string | null> = { live: null, demo: "Demo data", replay: "Replayed data" };

export function Header() {
  const { live, online, connecting, plant } = useLive();
  const tz = plant?.timezone ?? "Europe/Berlin";
  const now = useNow();
  const ref = live?.ts ?? now;
  // dot colour, long text (desktop), short text (phones) and a coarse state for screen readers.
  // The announced state contains no clock time, so it only changes when the situation changes.
  let dot = "bad";
  let text: string;
  let short: string;
  let announce: string;
  if (online && live && live.phase === "failed") {
    // the socket still delivers (heartbeats), but tracking has stopped: never show "Live"
    text = "Tracking stopped · server problem";
    short = "Stopped";
    announce = "Tracking has stopped because of a problem in the dashboard server";
  } else if (online && live) {
    if (live.connection.connected) {
      dot = "ok";
      text = `Live · updated ${fmtWhen(live.ts, tz, ref)}`;
      short = "Live";
      announce = "Live data";
    } else {
      dot = "warn";
      text = `Factory not connected · last data ${fmtWhen(live.connection.last_data_at, tz, ref)}`;
      short = "No factory";
      announce = "The dashboard has no connection to the factory";
    }
  } else if (connecting) {
    dot = "warn";
    text = live ? `Connecting… · showing data from ${fmtWhen(live.ts, tz, ref)}` : "Connecting to the dashboard server…";
    short = "Connecting";
    announce = "Connecting to the dashboard server";
  } else if (live) {
    text = `Not live · last known state at ${fmtWhen(live.ts, tz, now)}`;
    short = "Not live";
    announce = "Not live: the connection to the dashboard server was lost";
  } else {
    text = "Dashboard server not reachable";
    short = "No server";
    announce = "The dashboard server cannot be reached";
  }
  const badge = live ? SOURCE_BADGE[live.source] : null;
  return (
    <header className="header">
      <div className="brand">Digital Model Factory</div>
      <nav className="nav" aria-label="Main">
        <NavLink to="/" end>
          Factory map
        </NavLink>
        <NavLink to="/pieces">Pieces now</NavLink>
        <NavLink to="/history">History</NavLink>
      </nav>
      <div className="header-status" data-testid="header-status">
        <span className={`dot ${dot}`} aria-hidden="true" />
        <span className="label-long">{text}</span>
        <span className="label-short" aria-hidden="true">
          {short}
        </span>
        {badge && <span className="source-badge">{badge}</span>}
        <span className="sr-only" role="status">
          {announce}
        </span>
      </div>
    </header>
  );
}
