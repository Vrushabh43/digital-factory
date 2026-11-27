import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Banners } from "../components/Banners";
import { Header } from "../components/Header";
import { makeLive, NOW, plant } from "../test/fixtures";
import { LiveProvider, useLive, useNow } from "./live";
import type { Live } from "./types";

class FakeSocket {
  static all: FakeSocket[] = [];
  static get last(): FakeSocket {
    const s = FakeSocket.all.at(-1);
    if (!s) throw new Error("no socket was opened");
    return s;
  }
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  close(code = 1000) {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.({ code });
  }
  /** the server accepts the connection */
  open() {
    this.onopen?.();
  }
  push(data: unknown) {
    this.onmessage?.({ data: JSON.stringify({ type: "live", data }) });
  }
}

const factory = (url: string) => new FakeSocket(url) as unknown as WebSocket;

type Answer = { ok: boolean; status: number; statusText: string; json: () => Promise<unknown> };
const ok = (body: unknown): Answer => ({ ok: true, status: 200, statusText: "OK", json: async () => body });
const unavailable: Answer = { ok: false, status: 503, statusText: "Service Unavailable", json: async () => ({ detail: "starting" }) };

/** `/api/live` answers with `rest` (or 503); a function lets the test decide when it answers. */
function stubFetch(rest: Live | null | (() => Promise<Answer>) = null) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/plant") return ok(plant);
      if (url === "/api/live") return typeof rest === "function" ? rest() : rest ? ok(rest) : unavailable;
      return unavailable;
    }),
  );
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function status(): string {
  return screen.getByTestId("header-status").querySelector(".label-long")!.textContent ?? "";
}

function renderApp(extra?: React.ReactNode) {
  return render(
    <MemoryRouter>
      <LiveProvider socketFactory={factory}>
        <Header />
        <Banners />
        {extra}
      </LiveProvider>
    </MemoryRouter>,
  );
}

describe("LiveProvider", () => {
  beforeEach(() => {
    FakeSocket.all = [];
    vi.useFakeTimers();
    vi.setSystemTime(NOW * 1000);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("shows live updates and reports a lost connection to the dashboard server", async () => {
    stubFetch();
    renderApp();
    await advance(0);
    expect(FakeSocket.all).toHaveLength(1);
    expect(FakeSocket.last.url).toMatch(/\/ws\/live$/);
    act(() => FakeSocket.last.push(makeLive()));
    expect(status()).toBe("Live · updated 14:33:20");
    expect(screen.getByText("Demo data")).toBeInTheDocument();

    act(() => FakeSocket.last.push(makeLive({ connection: { ...makeLive().connection, connected: false, state: "disconnected" } })));
    expect(screen.getByText(/^No connection to the factory/)).toBeInTheDocument();
    expect(status()).toMatch(/^Factory not connected/);

    act(() => FakeSocket.last.close());
    expect(screen.getByText(/^Connection to the dashboard server lost/)).toBeInTheDocument();
    expect(status()).toBe("Not live · last known state at 14:33:20");
    // reconnects automatically after the first back-off step (0.5 s)
    await advance(499);
    expect(FakeSocket.all).toHaveLength(1);
    await advance(1);
    expect(FakeSocket.all).toHaveLength(2);
  });

  it("waits longer after every failed attempt and starts again once data arrives", async () => {
    stubFetch();
    renderApp();
    await advance(0);
    for (const delay of [500, 1000, 2000, 4000, 8000, 10_000, 10_000]) {
      const before = FakeSocket.all.length;
      act(() => FakeSocket.last.close());
      await advance(delay - 1);
      expect(FakeSocket.all, `no new attempt before ${delay} ms`).toHaveLength(before);
      await advance(1);
      expect(FakeSocket.all, `new attempt after ${delay} ms`).toHaveLength(before + 1);
    }
    act(() => FakeSocket.last.push(makeLive()));
    act(() => FakeSocket.last.close());
    await advance(499);
    const n = FakeSocket.all.length;
    await advance(1);
    expect(FakeSocket.all).toHaveLength(n + 1);
  });

  it("keeps backing off when the server accepts and closes at once (1013, too many clients)", async () => {
    stubFetch();
    renderApp();
    await advance(0);
    for (const delay of [500, 1000, 2000, 4000]) {
      const before = FakeSocket.all.length;
      act(() => {
        FakeSocket.last.open();
        FakeSocket.last.close(1013);
      });
      await advance(delay - 1);
      expect(FakeSocket.all, `no new attempt before ${delay} ms`).toHaveLength(before);
      await advance(1);
      expect(FakeSocket.all).toHaveLength(before + 1);
    }
    expect(status()).toBe("Dashboard server not reachable");
  });

  it("closes a connection that stays silent for 15 s, shows it as not live and reconnects", async () => {
    stubFetch();
    renderApp();
    await advance(0);
    const first = FakeSocket.last;
    act(() => first.push(makeLive()));
    await advance(14_000);
    expect(first.closed).toBe(false);
    expect(status()).toBe("Live · updated 14:33:20");

    await advance(2_000); // more than 15 s without any message
    expect(first.closed).toBe(true);
    expect(status()).toBe("Not live · last known state at 14:33:20");
    expect(screen.getByText(/^Connection to the dashboard server lost/)).toBeInTheDocument();
    expect(FakeSocket.all).toHaveLength(1);
    await advance(500);
    expect(FakeSocket.all).toHaveLength(2);
    act(() => FakeSocket.last.push(makeLive()));
    expect(status()).toBe("Live · updated 14:33:20");
  });

  it("gives up on a socket that never delivers data", async () => {
    stubFetch();
    renderApp();
    await advance(0);
    const first = FakeSocket.last;
    act(() => first.open());
    await advance(16_000);
    expect(first.closed).toBe(true);
    expect(status()).toBe("Dashboard server not reachable");
  });

  it("fills the page from the REST answer before the socket, without calling it live", async () => {
    stubFetch(makeLive({ version: 3 }));
    function Probe() {
      const { live, online } = useLive();
      return <p data-testid="probe">{live ? `version ${live.version}, ${online ? "online" : "not online"}` : "no data"}</p>;
    }
    renderApp(<Probe />);
    await advance(0);
    await advance(0);
    expect(screen.getByTestId("probe")).toHaveTextContent("version 3, not online");
    expect(status()).toBe("Connecting… · showing data from 14:33:20");
    expect(status()).not.toMatch(/Live/);
    expect(screen.queryByText(/Connection to the dashboard server lost/)).not.toBeInTheDocument();

    act(() => FakeSocket.last.push(makeLive({ version: 4 })));
    expect(screen.getByTestId("probe")).toHaveTextContent("version 4, online");
    expect(status()).toBe("Live · updated 14:33:20");
  });

  it("never replaces socket data by a late REST answer", async () => {
    let answer: (a: Answer) => void = () => undefined;
    stubFetch(() => new Promise<Answer>((r) => (answer = r)));
    function Probe() {
      const { live } = useLive();
      return <p data-testid="probe">{live ? `version ${live.version}` : "no data"}</p>;
    }
    renderApp(<Probe />);
    await advance(0);
    act(() => FakeSocket.last.push(makeLive({ version: 9 })));
    answer(ok(makeLive({ version: 2 })));
    await advance(0);
    await advance(0);
    expect(screen.getByTestId("probe")).toHaveTextContent("version 9");
  });

  it("ticks the clock without re-rendering components that only read the live data", async () => {
    stubFetch();
    let liveRenders = 0;
    function LiveProbe() {
      useLive();
      liveRenders += 1;
      return null;
    }
    function ClockProbe() {
      return <p data-testid="clock">{Math.floor(useNow())}</p>;
    }
    renderApp(
      <>
        <LiveProbe />
        <ClockProbe />
      </>,
    );
    await advance(0);
    act(() => FakeSocket.last.push(makeLive()));
    const before = liveRenders;
    const t0 = Number(screen.getByTestId("clock").textContent);
    await advance(5_000);
    expect(Number(screen.getByTestId("clock").textContent)).toBe(t0 + 5);
    expect(liveRenders).toBe(before);
  });

  it("shows plant alarm and database warnings", async () => {
    stubFetch();
    renderApp();
    await advance(0);
    act(() =>
      FakeSocket.last.push(makeLive({ plant: { alarm: true, enabled: false }, database: { healthy: false, pending: 12 } })),
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Plant alarm is on");
    expect(screen.getByText(/not enabled \(Freischalten is off\)/)).toBeInTheDocument();
    expect(screen.getByText(/12 events are waiting/)).toBeInTheDocument();
  });
});
