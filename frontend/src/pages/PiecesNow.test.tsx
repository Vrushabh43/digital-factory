import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { LivePiece, PieceStatus } from "../api/types";
import { ctx, makeLive, NOW, piece, plant, renderWith } from "../test/fixtures";
import { PiecesNow } from "./PiecesNow";

function codes(): string[] {
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((r) => within(r).getAllByRole("cell")[0].textContent ?? "");
}

function rowOf(code: string): HTMLElement {
  return screen.getByRole("link", { name: code }).closest("tr")!;
}

function kpi(label: string): string {
  return screen.getByText(label, { selector: ".kpi .label" }).parentElement!.querySelector(".num")!.textContent ?? "";
}

// one piece for every status the backend sends (src/api/types.ts PieceStatus)
const ALL: Record<PieceStatus, LivePiece> = {
  moving: piece("P-0201", { status: "moving" }),
  with_robot: piece("P-0202", { status: "with_robot", position: null, cell: null, conveyor: null, conveyor_role: null }),
  stuck: piece("P-0203", { status: "stuck", position: 5, cell: 13 }),
  cell_error: piece("P-0204", { status: "cell_error", position: 13, cell: 19 }),
  cell_offline: piece("P-0205", { status: "cell_offline", position: 6, cell: 6 }),
  uncertain: piece("P-0206", { status: "uncertain", uncertain: true, uncertain_reason: "found after a data gap" }),
};

describe("PiecesNow", () => {
  it("lists every piece with location, time in cell and status", () => {
    renderWith(<PiecesNow />);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    const first = within(rows[0]).getAllByRole("cell").map((c) => c.textContent);
    expect(first[0]).toBe("P-0157");
    expect(first[1]).toBe("Cell 14 · position 4");
    expect(first[2]).toBe("2 · middle");
    expect(first[3]).toBe("14:32:37");
    expect(first[4]).toBe("0 min 43 s");
    expect(first[5]).toBe("Cell 12");
    expect(first[6]).toBe("Moving normally");
    expect(screen.getByText("Waiting too long")).toBeInTheDocument();
    expect(screen.getByText("Cell has an error")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "P-0151" })).toHaveAttribute("href", "/history?piece=P-0151");
  });

  it("filters pieces that need attention", async () => {
    renderWith(<PiecesNow />);
    await userEvent.click(screen.getByRole("button", { name: "Need attention (2)" }));
    expect(codes()).toEqual(["P-0155", "P-0151"]);
  });

  it("sorts every status into exactly one filter", async () => {
    renderWith(<PiecesNow />, ctx({ live: makeLive({ pieces: Object.values(ALL) }) }));
    expect(codes()).toEqual(["P-0206", "P-0205", "P-0204", "P-0203", "P-0202", "P-0201"]);
    expect(kpi("Pieces in the factory")).toBe("6");
    expect(kpi("Moving normally")).toBe("2");
    expect(kpi("Need attention")).toBe("4");
    expect(kpi("Cells with pieces")).toBe("4"); // positions 4, 5, 13, 6; the robot is not a cell

    await userEvent.click(screen.getByRole("button", { name: "Moving (2)" }));
    expect(screen.getByRole("button", { name: "Moving (2)" })).toHaveAttribute("aria-pressed", "true");
    expect(codes()).toEqual(["P-0202", "P-0201"]); // moving + with the robot

    await userEvent.click(screen.getByRole("button", { name: "Need attention (4)" }));
    expect(codes()).toEqual(["P-0206", "P-0205", "P-0204", "P-0203"]); // uncertain, offline, error, stuck

    await userEvent.click(screen.getByRole("button", { name: "All (6)" }));
    expect(codes()).toHaveLength(6);
  });

  it("says when a group is empty", async () => {
    renderWith(<PiecesNow />, ctx({ live: makeLive({ pieces: [ALL.moving] }) }));
    await userEvent.click(screen.getByRole("button", { name: "Need attention (0)" }));
    expect(screen.getByText("No pieces in this group.")).toBeInTheDocument();
  });

  it("shows pieces per cell against the real capacity", () => {
    renderWith(<PiecesNow />);
    const side = screen.getByRole("complementary");
    expect(within(side).getByText("1 of 5")).toBeInTheDocument(); // cross cell
    expect(within(side).getAllByText("1 of 3")).toHaveLength(2);
  });

  it("shows where a piece entered the line when it did not come from a cell", () => {
    const live = makeLive({
      pieces: [
        piece("P-0301", { came_from_position: null, came_from_cell: null, origin: "source:Side entry" }),
        piece("P-0302", { came_from_position: null, came_from_cell: null, origin: "put_on" }),
        piece("P-0303", { came_from_position: 5, came_from_cell: null }), // cell number from the plant model
      ],
    });
    renderWith(<PiecesNow />, ctx({ live }));
    const cameFrom = (code: string) => within(rowOf(code)).getAllByRole("cell")[5].textContent;
    expect(cameFrom("P-0301")).toBe("Side entry");
    expect(cameFrom("P-0302")).toBe("Put on by hand");
    expect(cameFrom("P-0303")).toBe("Cell 13");
  });

  it("marks long stays using the plant's stuck threshold", () => {
    const live = makeLive({ pieces: [piece("P-0401", { entered_at: NOW - 43 })] });
    const { unmount } = renderWith(<PiecesNow />, ctx({ live, plant: { ...plant, stuck_after_s: 30 } }));
    expect(within(rowOf("P-0401")).getByText("0 min 43 s")).toHaveClass("long");
    unmount();
    renderWith(<PiecesNow />, ctx({ live, plant: { ...plant, stuck_after_s: 120 } }));
    expect(within(rowOf("P-0401")).getByText("0 min 43 s")).not.toHaveClass("long");
  });

  it("shows why a position is not certain as visible text", () => {
    renderWith(<PiecesNow />, ctx({ live: makeLive({ pieces: [ALL.uncertain] }) }));
    expect(within(rowOf("P-0206")).getByText("Not certain: found after a data gap")).toBeVisible();
  });

  it("shows the date when a piece entered its cell before today", () => {
    const live = makeLive({ pieces: [piece("P-0501", { entered_at: NOW - 86_400 })] });
    renderWith(<PiecesNow />, ctx({ live }));
    expect(within(rowOf("P-0501")).getAllByRole("cell")[3]).toHaveTextContent("25.11.2026 14:33:20");
  });

  it("stops the time in cell when the data is no longer live", () => {
    renderWith(<PiecesNow />, ctx({ online: false, now: NOW + 600 }));
    expect(within(rowOf("P-0157")).getAllByRole("cell")[4]).toHaveTextContent("0 min 43 s");
    expect(screen.getByTestId("stale-note")).toBeInTheDocument();
    expect(screen.getByText(/at the last known state, and which cell it was in/)).toBeInTheDocument();
  });
});
