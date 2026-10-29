import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);

const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];

function geometry(viewport: HTMLElement) {
  Object.defineProperty(viewport, "scrollHeight", { value: 5000, configurable: true });
  Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
}

// meiva:note
// Nothing to scroll to means nothing to offer: at the bottom the control is not
// in the DOM, so a hover cannot turn it into an arrow over a click that does
// nothing (user report 2026-10-30). A refresh in flight does not override it.
// The state is the measured distance to the bottom, so appended content
// re-decides it.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
describe("scroll-to-bottom control presence", () => {
  it("is absent at the bottom, including while a refresh is in flight", () => {
    const { rerender } = render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport);
    viewport.scrollTop = 4400;
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();

    rerender(<TimelineView items={items} loadingNew />);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
    expect(screen.queryByTestId("latest-loading-ring")).toBeNull();
  });

  it("appears away from the bottom and is gone again once the click returns the reader", () => {
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    geometry(viewport);
    const scrollTo = vi.spyOn(viewport, "scrollTo").mockImplementation(() => undefined);
    viewport.scrollTop = 700;
    act(() => { viewport.dispatchEvent(new Event("scroll")); });
    const button = screen.getByTestId("scroll-to-bottom");
    expect(button.getAttribute("aria-label")).toBe("Scroll to bottom");
    expect(button.className).not.toContain("opacity-0");
    expect(button.className).not.toContain("pointer-events-none");

    fireEvent.click(button);
    expect(scrollTo).toHaveBeenCalledWith({ top: 5000, behavior: "smooth" });
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });
});
