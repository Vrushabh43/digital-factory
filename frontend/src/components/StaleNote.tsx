import { isStale, useLive, useNow } from "../api/live";
import { fmtWhen } from "../lib/format";

/** Visible marker on a page while it shows the last known state instead of live data. */
export function StaleNote() {
  const ctx = useLive();
  const now = useNow();
  if (!isStale(ctx) || !ctx.live) return null;
  const tz = ctx.plant?.timezone ?? "Europe/Berlin";
  return (
    <p className="stale-note" data-testid="stale-note">
      Not live: showing the last known state from {fmtWhen(ctx.live.ts, tz, now)}. Times and durations are as of
      then.
    </p>
  );
}
