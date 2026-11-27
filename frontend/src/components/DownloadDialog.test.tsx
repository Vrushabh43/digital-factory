import { screen } from "@testing-library/react";
import { renderWith, tree } from "../test/fixtures";
import { DownloadDialog } from "./DownloadDialog";

describe("DownloadDialog", () => {
  it("keeps the focus where the user put it when the parent re-renders with a new onClose", () => {
    // parents re-render every second and often pass a new inline function each time
    const { rerender } = renderWith(<DownloadDialog day="2026-11-20" onClose={() => undefined} />);
    expect(screen.getByLabelText("From")).toHaveFocus();
    screen.getByLabelText("To").focus();
    for (let i = 0; i < 3; i++) rerender(tree(<DownloadDialog day="2026-11-20" onClose={() => undefined} />));
    expect(screen.getByLabelText("To")).toHaveFocus();
  });

  it("calls the latest onClose", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderWith(<DownloadDialog day="2026-11-20" onClose={first} />);
    rerender(tree(<DownloadDialog day="2026-11-20" onClose={second} />));
    screen.getByRole("button", { name: "Cancel" }).click();
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
  });
});
