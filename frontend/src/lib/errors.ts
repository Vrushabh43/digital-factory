// Plain-language texts for failed requests (operators never see status codes or server messages).
import { ApiError } from "../api/client";

export type Topic = "plant" | "history" | "journey" | "export";

export function isAbort(e: unknown): boolean {
  return typeof e === "object" && e !== null && (e as { name?: unknown }).name === "AbortError";
}

const NOT_FOUND: Record<Topic, string> = {
  plant: "The dashboard server does not know the factory layout. Please contact the person who runs the dashboard.",
  history: "The chosen cell does not exist. Please pick a cell from the list.",
  journey: "This piece was not found.",
  export: "The chosen cell does not exist. Please pick a cell from the list and try again.",
};

function invalidText(detail: unknown): string {
  // the backend's own messages are short sentences ("at most 31 days per export");
  // FastAPI validation errors are lists with technical field paths and are not shown
  if (typeof detail === "string" && detail.trim() && detail.length <= 120) {
    const d = detail.trim().replace(/\.$/, "");
    return `The selection was not accepted: ${d.charAt(0).toLowerCase()}${d.slice(1)}. Please check it and try again.`;
  }
  return "The selection was not accepted. Please check the day, the cell and the piece number.";
}

/** One sentence that tells an operator what went wrong and what to do. */
export function problemText(e: unknown, topic: Topic): string {
  if (e instanceof ApiError) {
    if (e.status === 404) return NOT_FOUND[topic];
    if (e.status === 422 || e.status === 400) return invalidText(e.detail);
    if (e.status === 503) {
      return topic === "plant"
        ? "The dashboard server is starting. Please wait a moment."
        : "The history is not available right now (the database is starting or cannot be reached). Please try again in a minute.";
    }
    if (e.status >= 500) return "The dashboard server had a problem. Please try again in a minute.";
    return "The dashboard server refused the request. Please reload the page.";
  }
  return "The dashboard server cannot be reached. Please check the network connection.";
}
