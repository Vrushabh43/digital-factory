import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { CellStatus, Live } from "../api/types";
import { ctx, makeLive, NOW, renderWith } from "../test/fixtures";
import { FactoryMap } from "./FactoryMap";

function withStatuses(statuses: CellStatus[]): Live {
  const base = makeLive();
  return makeLive({
    cells: base.cells.map((c, i) => ({ ...c, status: statuses[i], reason: statuses[i] === "running" ? "ok" : c.reason })),
  });
}

function panel() {
  return screen.getByRole("region", { name: "Selected cell" });
}

describe("FactoryMap", () => {
  it("shows the summary counts and the cells that need attention", () => {
    // fixture: Cell 14 running, Cell 13 stuck, Cell 6 offline, Cell 19 error, Cell 8 running
    renderWith(<FactoryMap />);
    expect(screen.getByTestId("count-running")).toHaveTextContent(/^2$/);
    expect(screen.getByTestId("count-stuck")).toHaveTextContent(/^1$/);
    expect(screen.getByTestId("count-error")).toHaveTextContent(/^1$/);
    expect(screen.getByTestId("count-offline")).toHaveTextContent(/^1$/);
    const attention = screen.getByRole("region", { name: "Needs attention" });
    const items = within(attention).getAllByRole("button");
    expect(items.map((b) => b.textContent)).toEqual([
      "Cell 13 · Piece stuckPiece P-0151 waiting 4 min at the exit (position 5)",
      "Cell 6 · OfflineNo data for 30 min (position 6)",
      "Cell 19 · ErrorAlarm is on, needs a technician (position 13)",
    ]);
  });

  it("puts every count in its own box", () => {
    // distinct numbers, so a count shown in the wrong box is noticed
    renderWith(<FactoryMap />, ctx({ live: withStatuses(["stuck", "stuck", "offline", "offline", "offline"]) }));
    expect(screen.getByTestId("count-running")).toHaveTextContent(/^0$/);
    expect(screen.getByTestId("count-stuck")).toHaveTextContent(/^2$/);
    expect(screen.getByTestId("count-error")).toHaveTextContent(/^0$/);
    expect(screen.getByTestId("count-offline")).toHaveTextContent(/^3$/);
  });

  it("selects the first attention cell by default and explains the stuck piece", () => {
    renderWith(<FactoryMap />);
    expect(screen.getByTestId("cell-explanation")).toHaveTextContent(
      "Piece P-0151 has been waiting at the exit sensor for 4 min 12 s. The next cell (Cell 15) is not ready to take it.",
    );
    // the PLC hand-over flags as reported (stuck cell: not ready to receive, ready to send)
    expect(screen.getByTestId("ready-receive")).toHaveTextContent(/^No$/);
    expect(screen.getByTestId("ready-send")).toHaveTextContent(/^Yes$/);
    expect(screen.getByText("P-0151 reached the exit sensor")).toBeInTheDocument();
  });

  it("shows the PLC's ready flag for a stuck cell instead of guessing it", () => {
    const base = makeLive();
    const live = makeLive({ cells: base.cells.map((c) => (c.position === 5 ? { ...c, ready_to_receive: true, ready_to_send: false } : c)) });
    renderWith(<FactoryMap />, ctx({ live }));
    expect(screen.getByTestId("ready-receive")).toHaveTextContent(/^Yes$/);
    expect(screen.getByTestId("ready-send")).toHaveTextContent(/^No$/);
  });

  it("shows a selected offline cell without values", async () => {
    renderWith(<FactoryMap />);
    await userEvent.click(screen.getByTestId("cell-6"));
    expect(screen.getByTestId("cell-explanation")).toHaveTextContent(
      "No data from this cell for 30 min. It may be switched off or its network cable is disconnected.",
    );
    expect(within(panel()).getAllByText("No data")).toHaveLength(3); // its three conveyors
    expect(within(panel()).getAllByText("Unknown")).toHaveLength(2);
    expect(within(screen.getByTestId("cell-6")).getByText("No data")).toBeInTheDocument();
  });

  it("draws every cell with its real number of conveyors", () => {
    renderWith(<FactoryMap />);
    const cross = screen.getByTestId("cell-5");
    expect(cross.querySelectorAll(".belt")).toHaveLength(5);
    expect(cross.querySelectorAll(".belt.full")).toHaveLength(1);
    expect(screen.getByTestId("cell-4").querySelectorAll(".belt")).toHaveLength(3);
    expect(screen.getByTestId("cell-4")).toHaveAccessibleName("Cell 14, position 4, Running, 1 piece");
  });

  it("says when nothing needs attention", () => {
    const live = makeLive({ cells: makeLive().cells.map((c) => ({ ...c, status: "running" as const, stuck_piece: null })) });
    renderWith(<FactoryMap />, ctx({ live }));
    expect(screen.getByText("Nothing needs attention right now. All cells are running.")).toBeInTheDocument();
  });

  it("explains a running cell that is waiting to hand a piece over", async () => {
    const base = makeLive();
    const live = makeLive({ cells: base.cells.map((c) => (c.position === 4 ? { ...c, detail: "waiting" } : c)) });
    renderWith(<FactoryMap />, ctx({ live }));
    await userEvent.click(screen.getByTestId("cell-4"));
    const text = screen.getByTestId("cell-explanation").textContent ?? "";
    expect(text).toBe("Running, but waiting: the cell is waiting to hand a piece over to the next cell (Cell 13). This is not yet too long.");
    expect(screen.getByTestId("cell-4")).toHaveAccessibleName("Cell 14, position 4, Running, waiting to hand a piece over, 1 piece");
  });

  it("warns that pieces may not be tracked when a conveyor of a running cell has no valid data", async () => {
    const base = makeLive();
    const live = makeLive({
      cells: base.cells.map((c) =>
        c.position === 4
          ? { ...c, reason: "partial_data", detail: "transporting", conveyors: c.conveyors.map((v) => (v.n === 3 ? { ...v, state: null } : v)) }
          : c,
      ),
    });
    renderWith(<FactoryMap />, ctx({ live }));
    await userEvent.click(screen.getByTestId("cell-4"));
    const text = screen.getByTestId("cell-explanation").textContent ?? "";
    expect(text).toBe(
      "Running, but not all data is available. Some conveyors of this cell send no valid data, so the pieces in this cell may not be tracked correctly.",
    );
    expect(text).not.toMatch(/Working normally/);
    const rows = within(panel()).getAllByText(/^Conveyor \d/).map((r) => r.closest(".conv-row")!.querySelector(".val")!.textContent);
    expect(rows).toEqual(["Empty", "P-0157", "No data"]);
    expect(screen.getByTestId("cell-4")).toHaveAccessibleName("Cell 14, position 4, Running, some data missing, 1 piece");
  });

  it("scrolls the panel into view when a cell is chosen and the panel is off-screen", async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    try {
      renderWith(<FactoryMap />);
      const p = screen.getByTestId("cell-panel");
      vi.spyOn(p, "getBoundingClientRect").mockReturnValue({ top: window.innerHeight + 200, bottom: window.innerHeight + 600 } as DOMRect);
      await userEvent.click(screen.getByTestId("cell-6"));
      expect(scroll).toHaveBeenCalledTimes(1);
      // already visible: no jump
      vi.spyOn(p, "getBoundingClientRect").mockReturnValue({ top: 100, bottom: 500 } as DOMRect);
      await userEvent.click(screen.getByTestId("cell-4"));
      expect(scroll).toHaveBeenCalledTimes(1);
    } finally {
      delete (Element.prototype as Partial<Element>).scrollIntoView;
    }
  });

  it("ignores a cell in the address that does not exist", () => {
    renderWith(<FactoryMap />, ctx(), "/?cell=99");
    expect(screen.getByText(/names a cell position that does not exist \(99\)/)).toBeInTheDocument();
    expect(screen.getByTestId("cell-explanation")).toHaveTextContent(/^Piece P-0151 has been waiting/); // default selection
  });

  it("shows the date of events that did not happen today", () => {
    const base = makeLive();
    const live = makeLive({
      cells: base.cells.map((c) =>
        c.position === 5
          ? { ...c, latest_events: [{ ts: NOW - 60, kind: "x", piece: null, message: "today" }, { ts: NOW - 86_400, kind: "x", piece: null, message: "yesterday" }] }
          : c,
      ),
    });
    renderWith(<FactoryMap />, ctx({ live }));
    const times = within(panel()).getAllByRole("listitem").map((li) => li.querySelector("time")!.textContent);
    expect(times).toEqual(["14:32:20", "25.11.2026 14:33:20"]);
  });
});

describe("FactoryMap without factory connection", () => {
  const base = makeLive();
  const down = (state: string) =>
    makeLive({
      connection: { ...base.connection, connected: false, state, last_data_at: base.ts - 60 },
      cells: base.cells.map((c) => ({ ...c, status: "offline" as const, reason: "server_unreachable", stuck_piece: null, status_since: base.ts - 60 })),
    });

  it("shows one plant-wide card instead of one per cell", async () => {
    renderWith(<FactoryMap />, ctx({ live: down("disconnected") }));
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(within(attention).getByText("All cells · Offline")).toBeInTheDocument();
    expect(within(attention).queryAllByRole("button")).toHaveLength(0);
    expect(attention).toHaveTextContent(
      "No connection to the factory since 14:32:20. The current state of the cells is unknown; the dashboard keeps trying to reconnect.",
    );
    await userEvent.click(screen.getByTestId("cell-5"));
    // the panel shows no values, so the text must not claim that a last known state is shown
    expect(screen.getByTestId("cell-explanation")).toHaveTextContent(
      /^No connection to the factory for 1 min\. The current state of this cell is unknown\.$/,
    );
  });

  it("does not promise retries when the factory connection is switched off", async () => {
    renderWith(<FactoryMap />, ctx({ live: down("disabled") }));
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(attention).toHaveTextContent("The factory connection is switched off in the configuration");
    expect(attention.textContent).not.toMatch(/retr|trying/i);
    await userEvent.click(screen.getByTestId("cell-5"));
    expect(screen.getByTestId("cell-explanation")).toHaveTextContent(
      "The factory connection is switched off in the configuration, so there is no live data from this cell.",
    );
  });
});

describe("FactoryMap without connection to the dashboard server", () => {
  it("freezes durations at the last known state and says so", () => {
    // ten minutes after the last message: nothing may keep counting, nothing may claim to be live
    renderWith(<FactoryMap />, ctx({ online: false, now: NOW + 600 }));
    expect(screen.getByTestId("stale-note")).toHaveTextContent("Not live: showing the last known state from 14:33:20.");
    const attention = screen.getByRole("region", { name: "Needs attention" });
    expect(within(attention).getAllByRole("button")[0]).toHaveTextContent(
      "Cell 13 · Piece stuckLast known: piece P-0151 waiting 4 min at the exit (position 5)",
    );
    expect(screen.getByTestId("cell-explanation")).toHaveTextContent(
      "Last known state at 14:33:20: Piece P-0151 has been waiting at the exit sensor for 4 min 12 s.",
    );
    expect(screen.queryByText(/right now/)).not.toBeInTheDocument();
  });

  it("does not say that nothing needs attention right now", () => {
    const live = makeLive({ cells: makeLive().cells.map((c) => ({ ...c, status: "running" as const, stuck_piece: null })) });
    renderWith(<FactoryMap />, ctx({ live, online: false }));
    expect(screen.getByText("Nothing needed attention at the last known state.")).toBeInTheDocument();
  });

  it("keeps showing durations live while connected", () => {
    renderWith(<FactoryMap />, ctx({ now: NOW + 600 }));
    expect(screen.queryByTestId("stale-note")).not.toBeInTheDocument();
    expect(screen.getByTestId("cell-explanation")).toHaveTextContent("for 14 min 12 s");
  });
});
