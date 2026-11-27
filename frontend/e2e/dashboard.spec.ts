import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const CONTROL = process.env.MOCK_CONTROL_URL ?? "http://127.0.0.1:8765";

async function control(request: APIRequestContext, path: string, data?: unknown) {
  const res = await request.post(`${CONTROL}${path}`, { data: data ?? {} , timeout: 30_000 });
  expect(res.ok(), `${path}: ${await res.text()}`).toBeTruthy();
  return res.json();
}

/**
 * The dashboard must never try to write to the (mock) factory. The mock counts write attempts
 * per server instance and the counter starts again when a test restarts the server, so it is
 * checked after every test and right before a test stops the server. A counter that cannot be
 * read (server stopped, control API down) fails the check instead of passing as "0".
 */
async function expectNoWriteAttempts(request: APIRequestContext) {
  const res = await request.get(`${CONTROL}/health`);
  expect(res.ok(), `mock control /health: ${res.status()}`).toBeTruthy();
  const health = await res.json();
  expect(typeof health.client_write_attempts, `write-attempt counter not readable: ${JSON.stringify(health)}`).toBe("number");
  expect(health.client_write_attempts).toBe(0);
}

function todayBerlin(): string {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit" })
    .formatToParts(new Date());
  const get = (t: string) => parts.find((p) => p.type === t)!.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

interface PassRow {
  id: number;
  piece: string;
  position: number;
  result_label: string;
}

async function passes(request: APIRequestContext, query: string): Promise<PassRow[]> {
  const res = await request.get(`/api/history/passes?${query}`);
  expect(res.ok(), `passes ${query}: ${res.status()}`).toBeTruthy();
  return (await res.json()).items;
}

async function expectAllRunning(page: Page) {
  await page.goto("/");
  await expect(page.getByTestId("count-running")).toHaveText("18");
  await expect(page.getByText(/Nothing needs attention right now/)).toBeVisible();
}

test.beforeEach(async ({ request }) => {
  await control(request, "/server/start");
  await control(request, "/reset");
});

test.afterEach(async ({ request }) => {
  await expectNoWriteAttempts(request);
});

test("factory map shows all 18 cells from the blueprint", async ({ page }) => {
  await expectAllRunning(page);
  await expect(page.locator(".cellbox")).toHaveCount(18);
  await expect(page.getByTestId("cell-10")).toContainText("Cell 5");
  await expect(page.getByTestId("cell-10")).toContainText("Position 10");
  await page.getByTestId("cell-10").click();
  await expect(page.getByTestId("cell-explanation")).toHaveText("Working normally. No piece is in this cell at the moment.");
  await expect(page.getByText("Cell 12 (position 3)")).toBeVisible();
});

test("a piece is numbered once and followed through cells into the history", async ({ page, request }) => {
  await expectAllRunning(page);
  await control(request, "/travel", { start: [1, 1], cells: 2 }); // High-bay storage -> Cell 1 -> Cell 11 -> Cell 12

  await page.goto("/pieces");
  const row = page.getByRole("row").filter({ hasText: "Cell 12" });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("position 3");
  await expect(row).toContainText("Cell 11"); // came from
  await expect(row).toContainText("Moving normally");
  const code = (await row.getByRole("link").textContent())!.trim();
  expect(code).toMatch(/^P-\d{4,}$/);

  await row.getByRole("link").click();
  await expect(page).toHaveURL(new RegExp(`/history\\?piece=${code}`));
  const journey = page.getByRole("complementary", { name: "Journey of piece" });
  await expect(journey.getByTestId("journey-code")).toHaveText(code);
  await expect(journey.locator(".steps li")).toHaveCount(3);
  await expect(journey.locator(".steps li").nth(0)).toContainText("Cell 12");
  await expect(journey.locator(".steps li").nth(0)).toContainText("Still inside");
  await expect(journey.locator(".steps li").nth(2)).toContainText("Cell 1");

  await control(request, "/take_off", { conveyor: [3, 1] }); // it waits on conveyor 1 (entered via port 1)
  // the hand-over window (3 s) decides "taken off": wait until the backend has decided for THIS piece
  await expect
    .poll(async () => (await (await request.get(`/api/pieces/${code}/journey`)).json()).piece?.status, { timeout: 20_000 })
    .toBe("removed");
  await page.reload();
  await expect(journey.getByTestId("journey-code")).toHaveText(code);
  await expect(journey.locator(".steps li").nth(0)).toContainText("Cell 12");
  await expect(journey.locator(".steps li").nth(0)).toContainText("Taken off the line");
  await expect(journey.getByTestId("journey-now")).toContainText("Taken off the line");
});

test("error and stuck cells appear under Needs attention", async ({ page, request }) => {
  await expectAllRunning(page);
  await control(request, "/set", { values: { "pos7.lamp": 1 } }); // position 7 = Cell 4
  await expect(page.getByTestId("count-error")).toHaveText("1");
  await expect(page.getByRole("button", { name: /Cell 4 · Error/ })).toBeVisible();

  await control(request, "/put_on", { conveyor: [4, 3] }); // piece waits at the exit of Cell 14
  const stuck = page.getByRole("button", { name: /Cell 14 · Piece stuck/ });
  await expect(stuck).toBeVisible({ timeout: 20_000 });
  await stuck.click();
  const explanation = page.getByTestId("cell-explanation");
  await expect(explanation).toContainText("has been waiting at the exit sensor");
  await expect(explanation).toContainText("The next cell (Cell 13) is not ready to take it.");
  const stuckCode = /Piece (P-\d{4,}) has been waiting/.exec((await explanation.textContent()) ?? "")?.[1];
  expect(stuckCode, "the explanation names the stuck piece").toBeTruthy();

  await page.goto("/pieces");
  await expect(page.getByRole("button", { name: "Need attention (1)" })).toBeVisible();
  // the stuck piece of THIS test, not any piece left over from an earlier one
  const stuckRow = page.getByRole("row").filter({ has: page.getByRole("link", { name: stuckCode!, exact: true }) });
  await expect(stuckRow).toHaveCount(1);
  await expect(stuckRow).toContainText("Cell 14");
  await expect(stuckRow).toContainText("Waiting too long");
});

test("history can be downloaded as CSV and PDF", async ({ page, request }) => {
  const day = todayBerlin();
  const before = Math.max(0, ...(await passes(request, `day=${day}&limit=50`)).map((p) => p.id));
  await control(request, "/travel", { start: [4, 1], cells: 0, take_off: true });
  // wait (instead of sleeping) until the backend has recorded this test's own piece as taken off
  let code = "";
  await expect
    .poll(
      async () => {
        const mine = (await passes(request, `day=${day}&position=4&limit=50`)).find(
          (p) => p.id > before && p.result_label === "Taken off the line",
        );
        code = mine?.piece ?? "";
        return code;
      },
      { timeout: 20_000 },
    )
    .toMatch(/^P-\d{4,}$/);

  await page.goto("/history");
  await page.getByRole("button", { name: /Download CSV or PDF/ }).click();
  const dialog = page.getByRole("dialog", { name: "Download history" });
  const [download] = await Promise.all([page.waitForEvent("download"), dialog.getByRole("link", { name: "Download CSV" }).click()]);
  expect(download.suggestedFilename()).toBe(`dmf-cell-passes-${day}.csv`);
  const raw = readFileSync((await download.path())!);
  // UTF-8 BOM so Excel opens the file correctly; checked on the bytes, not hidden by trimming
  expect([...raw.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  const lines = raw.subarray(3).toString("utf-8").split("\r\n");
  expect(lines[0]).toBe(
    "Piece,Cell,Position,Entered,Left,Time in cell,Time in cell (s),Result,Came from cell,Went to position,Uncertain",
  );
  const mine = lines.filter((l) => l.startsWith(`${code},`));
  expect(mine.length, `CSV rows of ${code}`).toBeGreaterThan(0);
  expect(mine.some((l) => l.includes("Taken off the line"))).toBe(true);

  const pdf = await request.get(`/api/history/export?from=${day}&format=pdf`);
  expect(pdf.headers()["content-type"]).toBe("application/pdf");
  expect((await pdf.body()).subarray(0, 4).toString()).toBe("%PDF");
});

test("losing the factory connection is shown and recovers automatically", async ({ page, request }) => {
  await expectAllRunning(page);
  await expectNoWriteAttempts(request); // the counter of this server instance is lost when it stops
  await control(request, "/server/stop");
  const banner = page.locator(".banner", { hasText: "No connection to the factory" });
  await expect(banner).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("count-offline")).toHaveText("18");
  await expect(page.getByText(/Factory not connected/)).toBeVisible();
  // one plant-wide card instead of 18 identical ones
  await expect(page.locator(".attention-item")).toHaveCount(1);
  await expect(page.locator(".attention-item")).toContainText("All cells · Offline");

  await control(request, "/server/start");
  await expect(page.getByTestId("count-running")).toHaveText("18", { timeout: 45_000 });
  await expect(banner).toHaveCount(0);
  await expect(page.getByText(/Live · updated/)).toBeVisible();
});
