import { render, screen } from "@testing-library/react";
import { ctx, makeLive, NOW, renderWith, tree } from "../test/fixtures";
import { App, Footer } from "../App";
import { Banners } from "./Banners";
import { ErrorBoundary } from "./ErrorBoundary";
import { Header } from "./Header";

describe("Header", () => {
  it("has a short text for phones and a screen-reader status without the ticking time", () => {
    const { rerender } = renderWith(<Header />);
    expect(screen.getByTestId("header-status").querySelector(".label-short")).toHaveTextContent("Live");
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("Live data");
    expect(region.textContent).not.toMatch(/\d\d:\d\d/);
    // a new message one second later changes the visible time, but not the announced status
    rerender(tree(<Header />, ctx({ live: makeLive({ ts: NOW + 1 }), now: NOW + 1 })));
    expect(screen.getByText("Live · updated 14:33:21")).toBeInTheDocument();
    expect(screen.getByRole("status")).toBe(region);
    expect(region).toHaveTextContent(/^Live data$/);
  });

  it("names every connection state in words", () => {
    const cases: [Parameters<typeof ctx>[0], string, string][] = [
      [{ live: makeLive({ connection: { ...makeLive().connection, connected: false } }) }, "Factory not connected · last data 14:33:20", "No factory"],
      [{ online: false, connecting: true, live: null }, "Connecting to the dashboard server…", "Connecting"],
      [{ online: false }, "Not live · last known state at 14:33:20", "Not live"],
      [{ online: false, live: null }, "Dashboard server not reachable", "No server"],
      [{ live: makeLive({ phase: "failed" }) }, "Tracking stopped · server problem", "Stopped"],
    ];
    for (const [over, long, short] of cases) {
      const { unmount } = renderWith(<Header />, ctx(over));
      const box = screen.getByTestId("header-status");
      expect(box.querySelector(".label-long")).toHaveTextContent(long);
      expect(box.querySelector(".label-short")).toHaveTextContent(short);
      unmount();
    }
  });
});

describe("Banners", () => {
  const down = () =>
    makeLive({ connection: { ...makeLive().connection, connected: false, state: "disconnected", last_data_at: NOW - 60, next_retry_at: NOW + 5 } });

  it("keeps a banner in place while its countdown runs and does not announce the countdown", () => {
    const live = down();
    const { rerender } = renderWith(<Banners />, ctx({ live }));
    const banner = screen.getByText(/^No connection to the factory since 14:32:20\./).closest(".banner")!;
    expect(banner).toHaveTextContent("Next attempt in 5 s.");
    expect(screen.getByText("Next attempt in 5 s.", { exact: false })).toHaveAttribute("aria-hidden", "true");
    rerender(tree(<Banners />, ctx({ live, now: NOW + 2 })));
    const again = screen.getByText(/^No connection to the factory since 14:32:20\./).closest(".banner")!;
    expect(again).toBe(banner); // same element: not removed and re-added every second
    expect(again).toHaveTextContent("Next attempt in 3 s.");
  });

  it("marks plant facts as last known when the dashboard server is not reachable", () => {
    renderWith(<Banners />, ctx({ live: makeLive({ plant: { alarm: true, enabled: true } }), online: false }));
    expect(screen.getByText(/^Connection to the dashboard server lost\. Reconnecting automatically…/)).toHaveTextContent(
      "The page shows the last known state from 14:33:20; the factory may have changed since.",
    );
    // the alarm stays visible (an outage must not hide it) but is not presented as live
    expect(screen.getByRole("alert")).toHaveTextContent("Last known state: plant alarm is on.");
  });

  it("says plainly when tracking has stopped on the server, although messages still arrive", () => {
    renderWith(<Banners />, ctx({ live: makeLive({ phase: "failed" }) }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Tracking has stopped because of a problem in the dashboard server. Cells and pieces shown here are no longer updated.",
    );
  });

  it("shows nothing while the page is still connecting", () => {
    renderWith(<Banners />, ctx({ online: false, connecting: true }));
    expect(screen.queryByText(/Connection to the dashboard server lost/)).not.toBeInTheDocument();
  });
});

describe("Footer", () => {
  it("explains the read-only view without jargon", () => {
    renderWith(<Footer />);
    const text = screen.getByText(/only reads data/).textContent ?? "";
    expect(text).toMatch(/never controls it/);
    expect(text).not.toMatch(/OPC|node map/i);
  });
});

describe("App", () => {
  it("shows a message instead of a blank page when a page fails", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    // a live state without counts breaks the factory map
    const broken = { ...makeLive(), counts: undefined } as unknown as ReturnType<typeof makeLive>;
    render(tree(<App />, ctx({ live: broken }), "/"));
    expect(screen.getByRole("alert")).toHaveTextContent("This page could not be shown");
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument(); // header still works
    spy.mockRestore();
  });

  it("tries again when the address changes", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    function Page({ fail }: { fail: boolean }) {
      if (fail) throw new Error("broken");
      return <p>page works</p>;
    }
    const { rerender } = render(
      <ErrorBoundary resetKey="/a">
        <Page fail />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This page could not be shown");
    rerender(
      <ErrorBoundary resetKey="/b">
        <Page fail={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("page works")).toBeInTheDocument();
    spy.mockRestore();
  });
});
