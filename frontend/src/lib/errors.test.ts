import { ApiError, apiErrorFrom } from "../api/client";
import { isAbort, problemText } from "./errors";

describe("problemText", () => {
  it("maps HTTP errors to plain words without codes or server jargon", () => {
    const cases: [unknown, string][] = [
      [new ApiError(404, "unknown position 99"), "The chosen cell does not exist. Please pick a cell from the list."],
      [new ApiError(422, "at most 31 days per export", "at most 31 days per export"), "The selection was not accepted: at most 31 days per export. Please check it and try again."],
      [
        new ApiError(422, "Unprocessable Entity", [{ loc: ["query", "day"], msg: "Input should be a valid date", type: "date_from_datetime_parsing" }]),
        "The selection was not accepted. Please check the day, the cell and the piece number.",
      ],
      [new ApiError(503, "database not ready yet", "database not ready yet"), "The history is not available right now (the database is starting or cannot be reached). Please try again in a minute."],
      [new ApiError(500, "Internal Server Error"), "The dashboard server had a problem. Please try again in a minute."],
      [new ApiError(502, "Bad Gateway"), "The dashboard server had a problem. Please try again in a minute."],
      [new ApiError(403, "Forbidden"), "The dashboard server refused the request. Please reload the page."],
      [new TypeError("Failed to fetch"), "The dashboard server cannot be reached. Please check the network connection."],
    ];
    for (const [err, text] of cases) expect(problemText(err, "history")).toBe(text);
  });

  it("words 'not found' and 'starting' for what was asked", () => {
    expect(problemText(new ApiError(404, "x"), "journey")).toBe("This piece was not found.");
    expect(problemText(new ApiError(503, "x"), "plant")).toBe("The dashboard server is starting. Please wait a moment.");
  });

  it("recognises aborted requests", () => {
    expect(isAbort(new DOMException("aborted", "AbortError"))).toBe(true);
    expect(isAbort(new ApiError(500, "x"))).toBe(false);
    expect(isAbort(null)).toBe(false);
  });
});

describe("apiErrorFrom", () => {
  it("keeps the FastAPI detail, whether text or a validation list", async () => {
    const e1 = await apiErrorFrom(new Response(JSON.stringify({ detail: "piece P-0999 not found" }), { status: 404 }));
    expect([e1.status, e1.message, e1.detail]).toEqual([404, "piece P-0999 not found", "piece P-0999 not found"]);
    const list = [{ loc: ["query", "limit"], msg: "too big" }];
    const e2 = await apiErrorFrom(new Response(JSON.stringify({ detail: list }), { status: 422, statusText: "Unprocessable Entity" }));
    expect([e2.status, e2.message, e2.detail]).toEqual([422, "Unprocessable Entity", list]);
    const e3 = await apiErrorFrom(new Response("<html>bad gateway</html>", { status: 502 }));
    expect([e3.status, e3.message, e3.detail]).toEqual([502, "HTTP 502", null]);
  });
});
