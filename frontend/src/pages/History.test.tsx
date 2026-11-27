import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import type { Journey, Pass, PassPage } from "../api/types";
import { ctx, NOW, plant, renderWith, tree, type TestContext } from "../test/fixtures";
import { History } from "./History";

function pass(id: number, over: Partial<Pass> = {}): Pass {
  return {
    id, piece: "P-0150", position: 9, cell_number: 8, entered_at: NOW - 100, left_at: NOW - 40, result: "passed",
    result_label: "Passed", was_stuck: false, stuck_now: false, came_from_position: 8, came_from_cell: 7,
    went_to_position: 10, via: null, uncertain: false, ...over,
  };
}

const page: PassPage = {
  items: [
    pass(3, { piece: "P-0151", position: 10, cell_number: 5, left_at: null, result: "inside", result_label: "Stuck at exit", stuck_now: true }),
    pass(2, { piece: "P-0158", left_at: null, result: "inside", result_label: "Still inside" }),
    pass(1),
  ],
  total: 50,
  limit: 15,
  offset: 0,
  ts: NOW,
};

const journey: Journey = {
  piece: {
    code: "P-0151", first_seen_at: NOW - 1486, first_position: 2, first_cell: 11, origin: "source:Side entry",
    status: "active", ended_at: null, last_position: 10, cells_passed: 15, uncertain: false,
  },
  passes: [
    pass(10, { piece: "P-0151", position: 2, cell_number: 11, entered_at: NOW - 1486, left_at: NOW - 1398 }),
    pass(11, { piece: "P-0151", position: 10, cell_number: 5, entered_at: NOW - 291, left_at: null, result: "inside", stuck_now: true }),
  ],
  now: null,
  ts: NOW,
};

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

/** fetch stub: journeys, exports and pass pages; `over` answers selected URLs differently. */
function mockFetch(over: (url: string) => Response | undefined = () => undefined) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      const special = over(url);
      if (special) return special;
      return json(url.startsWith("/api/pieces/") ? journey : page);
    }),
  );
  return calls;
}

/** fetch stub whose answers the test releases one by one (and that ignores aborts, like a slow network). */
function deferredFetch() {
  const pending: { url: string; answer: (body: unknown) => void }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => new Promise<Response>((resolve) => pending.push({ url, answer: (b) => resolve(json(b)) }))),
  );
  return pending;
}

function routes() {
  return (
    <Routes>
      <Route path="/history" element={<History />} />
    </Routes>
  );
}

function renderHistory(route: string, value: TestContext = ctx()) {
  return renderWith(routes(), value, route);
}

async function settle() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

function bodyCodes(): string[] {
  return within(screen.getByRole("region", { name: "Cell passes, newest first" }))
    .getAllByRole("row")
    .slice(1)
    .map((r) => within(r).getAllByRole("cell")[0].textContent ?? "");
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("History", () => {
  it("loads today's passes and shows result labels", async () => {
    const calls = mockFetch();
    renderHistory("/history");
    expect(await screen.findByText("Stuck at exit")).toBeInTheDocument();
    expect(screen.getByText("Still inside")).toBeInTheDocument();
    expect(screen.getByText("Showing the latest 3 of 50 passes today")).toBeInTheDocument();
    expect(calls[0]).toBe("/api/history/passes?day=2026-11-26&limit=15&offset=0");
    expect(screen.getByLabelText("Day")).toHaveValue("26.11.2026");
  });

  it("opens the journey of a piece and highlights its rows", async () => {
    mockFetch();
    renderHistory("/history");
    await userEvent.click(await screen.findByRole("button", { name: "P-0151" }));
    expect(await screen.findByTestId("journey-code")).toHaveTextContent("P-0151");
    const aside = screen.getByRole("complementary", { name: "Journey of piece" });
    expect(within(aside).getByText("15")).toBeInTheDocument();
    expect(within(aside).getByText("24 min 46 s")).toBeInTheDocument();
    expect(within(aside).getByText("Stuck at exit")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "P-0151" }).closest("tr")).toHaveClass("hl");
  });

  it("searches a piece across all days and validates input", async () => {
    const calls = mockFetch();
    renderHistory("/history");
    await screen.findByText("Stuck at exit");
    await userEvent.clear(screen.getByLabelText("Piece number"));
    await userEvent.type(screen.getByLabelText("Piece number"), "abc");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Piece numbers look like P-0151.");
    await userEvent.clear(screen.getByLabelText("Day"));
    await userEvent.clear(screen.getByLabelText("Piece number"));
    await userEvent.type(screen.getByLabelText("Piece number"), "p-0151");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    await waitFor(() => expect(calls).toContain("/api/history/passes?piece=P-0151&limit=15&offset=0"));
  });

  it("searches all days for a piece even if the pre-filled day was left in the field", async () => {
    const calls = mockFetch();
    renderHistory("/history");
    await screen.findByText("Stuck at exit");
    await userEvent.type(screen.getByLabelText("Piece number"), "P-0151");
    expect(screen.getByLabelText("Day")).toHaveAccessibleDescription("A piece number is searched on all days unless you change the day.");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    await waitFor(() => expect(calls).toContain("/api/history/passes?piece=P-0151&limit=15&offset=0"));
    expect(calls.some((c) => c.includes("piece=P-0151") && c.includes("day="))).toBe(false);
    expect(screen.getByLabelText("Day")).toHaveValue("");
  });

  it("keeps a day the user typed together with a piece number", async () => {
    const calls = mockFetch();
    renderHistory("/history");
    await screen.findByText("Stuck at exit");
    await userEvent.clear(screen.getByLabelText("Day"));
    await userEvent.type(screen.getByLabelText("Day"), "20.11.2026");
    await userEvent.type(screen.getByLabelText("Piece number"), "P-0151");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    await waitFor(() => expect(calls).toContain("/api/history/passes?day=2026-11-20&piece=P-0151&limit=15&offset=0"));
  });

  it("shows each input error under its own field", async () => {
    mockFetch();
    renderHistory("/history");
    await screen.findByText("Stuck at exit");
    await userEvent.clear(screen.getByLabelText("Day"));
    await userEvent.type(screen.getByLabelText("Day"), "32.13.2026");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByLabelText("Day")).toHaveAccessibleDescription("Please enter the day as DD.MM.YYYY.");
    expect(screen.getByLabelText("Day")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Piece number")).not.toHaveAccessibleDescription();
    expect(screen.getByLabelText("Piece number")).toHaveAttribute("aria-invalid", "false");

    await userEvent.clear(screen.getByLabelText("Day"));
    await userEvent.type(screen.getByLabelText("Piece number"), "x1");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(screen.getByLabelText("Piece number")).toHaveAccessibleDescription("Piece numbers look like P-0151.");
    expect(screen.getByLabelText("Day")).not.toHaveAccessibleDescription();
  });

  it("does not crash on a broken address and says what it ignored", async () => {
    const calls = mockFetch();
    renderHistory("/history?day=garbage&position=abc&offset=-3&journey=x");
    expect(await screen.findByText("Stuck at exit")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Part of the page address was not understood and is ignored: the day “garbage”, the cell position “abc”, the piece number “x”, the page “-3”.",
    );
    expect(calls[0]).toBe("/api/history/passes?day=2026-11-26&limit=15&offset=0");
  });

  it("ignores a day that does not exist and a cell position the plant does not have", async () => {
    const calls = mockFetch();
    renderHistory("/history?day=2026-02-31&position=77");
    await screen.findByText("Stuck at exit");
    expect(screen.getByRole("status")).toHaveTextContent("the day “2026-02-31”, the cell position “77”");
    expect(calls[0]).toBe("/api/history/passes?day=2026-11-26&limit=15&offset=0");
  });

  it("explains a database outage in plain words", async () => {
    mockFetch((url) => (url.startsWith("/api/history/passes") ? json({ detail: "database not ready yet" }, 503) : undefined));
    renderHistory("/history");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "The history is not available right now (the database is starting or cannot be reached). Please try again in a minute.",
    );
    expect(alert.textContent).not.toMatch(/503|database not ready yet/);
  });

  it("clears the 'not found' message when no piece is selected any more", async () => {
    mockFetch((url) => (url.startsWith("/api/pieces/P-0999") ? json({ detail: "piece P-0999 not found" }, 404) : undefined));
    renderHistory("/history?piece=P-0999");
    const aside = screen.getByRole("complementary", { name: "Journey of piece" });
    expect(await within(aside).findByText("Piece P-0999 was not found.")).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Piece number"));
    await userEvent.type(screen.getByLabelText("Day"), "20.11.2026");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await within(aside).findByText("Click a piece number to see its whole journey.")).toBeInTheDocument();
    expect(within(aside).queryByText(/was not found/)).not.toBeInTheDocument();
  });

  it("shows the date when a piece left on another day and explains uncertain passes", async () => {
    const other: PassPage = {
      ...page,
      items: [pass(5, { entered_at: NOW - 86_400, left_at: NOW, uncertain: true })],
      total: 1,
    };
    mockFetch((url) => (url.startsWith("/api/history/passes") ? json(other) : undefined));
    renderHistory("/history?day=2026-11-25");
    const row = (await screen.findByRole("button", { name: "P-0150" })).closest("tr")!;
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent);
    expect(cells[2]).toBe("14:33:20");
    expect(cells[3]).toBe("26.11.2026 14:33:20");
    expect(within(row).getByText("path not certain")).toHaveAccessibleDescription(
      "“path not certain”: recorded after a data gap, so the exact path of the piece is not certain.",
    );
    expect(screen.getByText(/recorded after a data gap/)).toBeVisible();
  });
});

describe("History refresh", () => {
  const page2: PassPage = { items: [pass(40, { piece: "P-0101" }), pass(39, { piece: "P-0100" })], total: 50, limit: 15, offset: 15, ts: NOW };

  it("never lets a late refresh of page 1 replace the page the user moved to", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const pending = deferredFetch();
    renderHistory("/history");
    await waitFor(() => expect(pending).toHaveLength(1));
    pending[0].answer(page);
    expect(await screen.findByText("Showing the latest 3 of 50 passes today")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(10_000); // the automatic refresh of page 1 starts and hangs
    });
    expect(pending).toHaveLength(2);
    expect(pending[1].url).toBe("/api/history/passes?day=2026-11-26&limit=15&offset=0");

    await userEvent.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() => expect(pending).toHaveLength(3));
    expect(pending[2].url).toBe("/api/history/passes?day=2026-11-26&limit=15&offset=15");
    pending[2].answer(page2);
    expect(await screen.findByText("Showing passes 16–17 of 50 today")).toBeInTheDocument();

    pending[1].answer(page); // the old refresh answers last
    await settle();
    await settle();
    expect(bodyCodes()).toEqual(["P-0101", "P-0100"]);
    expect(screen.getByText("Showing passes 16–17 of 50 today")).toBeInTheDocument();
  });

  it("keeps the newest answer when two refreshes overlap", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const pending = deferredFetch();
    renderHistory("/history");
    await waitFor(() => expect(pending).toHaveLength(1));
    pending[0].answer(page);
    await screen.findByText("Showing the latest 3 of 50 passes today");

    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(pending).toHaveLength(3);
    pending[2].answer({ ...page, total: 52 }); // newer
    expect(await screen.findByText("Showing the latest 3 of 52 passes today")).toBeInTheDocument();
    pending[1].answer({ ...page, total: 51 }); // older, arrives later
    await settle();
    await settle();
    expect(screen.getByText("Showing the latest 3 of 52 passes today")).toBeInTheDocument();
  });
});

describe("History downloads", () => {
  async function openDialog() {
    await userEvent.click(screen.getByRole("button", { name: /Download CSV or PDF/ }));
    return screen.getByRole("dialog", { name: "Download history" });
  }

  async function setTo(dialog: HTMLElement, value: string) {
    await userEvent.clear(within(dialog).getByLabelText("To"));
    await userEvent.type(within(dialog).getByLabelText("To"), value);
  }

  it("offers CSV and PDF downloads with the chosen range", async () => {
    mockFetch();
    renderHistory("/history?position=9&day=2026-11-20");
    const dialog = await openDialog();
    expect(within(dialog).getByRole("link", { name: "Download CSV" })).toHaveAttribute(
      "href",
      "/api/history/export?from=2026-11-20&to=2026-11-20&format=csv&position=9",
    );
    await userEvent.click(within(dialog).getByLabelText("PDF"));
    await userEvent.clear(within(dialog).getByLabelText("To"));
    await userEvent.type(within(dialog).getByLabelText("To"), "19.11.2026");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("'To' must not be before 'From'.");
    expect(within(dialog).getByRole("button", { name: "Download PDF" })).toBeDisabled();
  });

  it("allows exactly 31 days when the server does not say its limit", async () => {
    mockFetch();
    renderHistory("/history?day=2026-11-01", ctx({ plant: { ...plant, export_max_days: undefined } }));
    const dialog = await openDialog();
    await setTo(dialog, "01.12.2026"); // 31 days
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Download CSV" })).toHaveAttribute(
      "href",
      "/api/history/export?from=2026-11-01&to=2026-12-01&format=csv",
    );
    await setTo(dialog, "02.12.2026"); // 32 days
    expect(within(dialog).getByRole("alert")).toHaveTextContent("At most 31 days per download.");
    expect(within(dialog).getByRole("button", { name: "Download CSV" })).toBeDisabled();
    expect(within(dialog).queryByRole("link")).not.toBeInTheDocument();
  });

  it("uses the download limit of the server", async () => {
    mockFetch();
    renderHistory("/history?day=2026-11-20", ctx({ plant: { ...plant, export_max_days: 7 } }));
    const dialog = await openDialog();
    await setTo(dialog, "26.11.2026"); // 7 days
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Download CSV" })).toBeInTheDocument();
    await setTo(dialog, "27.11.2026"); // 8 days
    expect(within(dialog).getByRole("alert")).toHaveTextContent("At most 7 days per download.");
    expect(within(dialog).getByRole("button", { name: "Download CSV" })).toBeDisabled();
  });

  it("explains a failed download instead of saving an error page", async () => {
    mockFetch((url) => (url.startsWith("/api/history/export") ? json({ detail: "database not ready yet" }, 503) : undefined));
    renderHistory("/history?day=2026-11-20");
    const dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole("link", { name: "Download CSV" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The download did not work. The history is not available right now (the database is starting or cannot be reached). Please try again in a minute.",
    );
    expect(screen.getByRole("dialog", { name: "Download history" })).toBeInTheDocument();
  });

  it("shows the reason when the server refuses the range", async () => {
    mockFetch((url) => (url.startsWith("/api/history/export") ? json({ detail: "at most 31 days per export" }, 422) : undefined));
    renderHistory("/history?day=2026-11-20");
    const dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole("link", { name: "Download CSV" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The download did not work. The selection was not accepted: at most 31 days per export. Please check it and try again.",
    );
  });

  it("saves the file under the server's name, closes and gives the focus back", async () => {
    mockFetch((url) =>
      url.startsWith("/api/history/export")
        ? new Response("\uFEFFPiece,Cell\r\n", { status: 200, headers: { "Content-Disposition": 'attachment; filename="dmf-cell-passes-2026-11-20.csv"' } })
        : undefined,
    );
    const created = vi.fn<(b: Blob) => string>(() => "blob:test");
    URL.createObjectURL = created;
    URL.revokeObjectURL = vi.fn();
    const saved: { href: string; download: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ href: this.getAttribute("href") ?? "", download: this.download });
    });
    renderHistory("/history?day=2026-11-20");
    const trigger = screen.getByRole("button", { name: /Download CSV or PDF/ });
    const dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole("link", { name: "Download CSV" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(saved).toEqual([{ href: "blob:test", download: "dmf-cell-passes-2026-11-20.csv" }]);
    // the file is saved byte for byte, including the BOM Excel needs
    const bytes = new Uint8Array(await created.mock.calls[0][0].arrayBuffer());
    expect([...bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
    expect(new TextDecoder().decode(bytes.slice(3))).toBe("Piece,Cell\r\n");
    expect(trigger).toHaveFocus();
  });

  it("keeps the keyboard inside the dialog and returns the focus on Escape", async () => {
    mockFetch();
    renderHistory("/history?day=2026-11-20");
    const trigger = screen.getByRole("button", { name: /Download CSV or PDF/ });
    const dialog = await openDialog();
    expect(within(dialog).getByLabelText("From")).toHaveFocus();
    const close = within(dialog).getByRole("button", { name: "Close" });
    const last = within(dialog).getByRole("link", { name: "Download CSV" });
    last.focus();
    await userEvent.tab();
    expect(close).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(last).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("does not move the focus while the page updates every second", async () => {
    mockFetch();
    const { rerender } = renderHistory("/history?day=2026-11-20");
    const dialog = await openDialog();
    const to = within(dialog).getByLabelText("To");
    to.focus();
    for (let i = 1; i <= 3; i++) rerender(tree(routes(), ctx({ now: NOW + i }), "/history?day=2026-11-20"));
    expect(screen.getByRole("dialog", { name: "Download history" })).toBeInTheDocument();
    expect(to).toHaveFocus();
  });
});
