import { daysInclusive, fmtAgo, fmtDate, fmtDateTime, fmtDuration, fmtTime, fmtWhen, isoDay, isoToInput, parseDayInput, parseIsoDay } from "./format";

describe("format", () => {
  it("formats durations like the design", () => {
    expect(fmtDuration(8)).toBe("0 min 08 s");
    expect(fmtDuration(64)).toBe("1 min 04 s");
    expect(fmtDuration(3723)).toBe("1 h 02 min 03 s");
    expect(fmtDuration(-5)).toBe("0 min 00 s");
    expect(fmtDuration(null)).toBe("–");
  });

  it("formats coarse ages", () => {
    expect(fmtAgo(42)).toBe("42 s");
    expect(fmtAgo(252)).toBe("4 min");
    expect(fmtAgo(7500)).toBe("2 h 5 min");
  });

  it("formats times and dates in the plant timezone", () => {
    const ts = Date.UTC(2026, 10, 20, 9, 42, 13) / 1000; // 10:42:13 in Berlin (CET)
    expect(fmtTime(ts, "Europe/Berlin")).toBe("10:42:13");
    expect(fmtDate(ts, "Europe/Berlin")).toBe("20.11.2026");
    expect(isoDay(Date.UTC(2026, 10, 20, 23, 30) / 1000, "Europe/Berlin")).toBe("2026-11-21"); // after midnight in Berlin
    const summer = Date.UTC(2026, 6, 1, 10, 0, 0) / 1000; // CEST
    expect(fmtTime(summer, "Europe/Berlin")).toBe("12:00:00");
  });

  it("parses DD.MM.YYYY input strictly", () => {
    expect(parseDayInput("20.11.2026")).toBe("2026-11-20");
    expect(parseDayInput(" 1.2.2026 ")).toBe("2026-02-01");
    expect(parseDayInput("31.02.2026")).toBeNull();
    expect(parseDayInput("2026-11-20")).toBeNull();
    expect(isoToInput("2026-11-20")).toBe("20.11.2026");
  });

  it("shows the date only for times on another plant day", () => {
    const tz = "Europe/Berlin";
    const noon = Date.UTC(2026, 10, 20, 11, 0, 0) / 1000; // 12:00 Berlin
    expect(fmtWhen(noon - 3600, tz, noon)).toBe("11:00:00");
    expect(fmtWhen(Date.UTC(2026, 10, 19, 22, 59, 0) / 1000, tz, noon)).toBe("19.11.2026 23:59:00"); // yesterday in Berlin
    expect(fmtWhen(Date.UTC(2026, 10, 19, 23, 1, 0) / 1000, tz, noon)).toBe("00:01:00"); // already today in Berlin
    expect(fmtWhen(null, tz, noon)).toBe("–");
  });

  it("never throws on an invalid timestamp", () => {
    expect(fmtDate(NaN, "Europe/Berlin")).toBe("–");
    expect(fmtTime(Number.POSITIVE_INFINITY, "Europe/Berlin")).toBe("–");
    expect(fmtDateTime(NaN, "Europe/Berlin")).toBe("–");
    expect(fmtWhen(NaN, "Europe/Berlin", 0)).toBe("–");
  });

  it("accepts only real ISO days from the address", () => {
    expect(parseIsoDay("2026-11-20")).toBe("2026-11-20");
    expect(parseIsoDay("2026-02-29")).toBeNull();
    expect(parseIsoDay("2024-02-29")).toBe("2024-02-29");
    expect(parseIsoDay("20.11.2026")).toBeNull();
    expect(parseIsoDay("2026-11-20x")).toBeNull();
    expect(parseIsoDay(null)).toBeNull();
  });

  it("counts calendar days including both ends, across the clock change", () => {
    expect(daysInclusive("2026-11-20", "2026-11-20")).toBe(1);
    expect(daysInclusive("2026-11-01", "2026-12-01")).toBe(31);
    expect(daysInclusive("2026-03-20", "2026-04-05")).toBe(17);
  });
});
