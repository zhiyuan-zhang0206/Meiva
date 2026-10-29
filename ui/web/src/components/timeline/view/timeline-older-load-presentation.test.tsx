import { fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OlderLoadOrigin } from "@/lib/timeline/timeline-plan";

import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);

const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];

// meiva:note
// The strip is in the DOM in both states, so a missing ring is an assertion
// about the element rather than about a null query.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/loading-ring.meiva.okf.md
function strip() {
  return screen.getByTestId("load-older-spinner");
}

// meiva:note
// The strip above the messages presents the reader's own older-page read. The
// in-flight flag alone is a concurrency guard that four callers hold, and keying
// the strip on it showed a ring nobody asked for (user report 2026-10-29).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.meiva.okf.md
describe("the older-page strip presents the reader's own request only", () => {
  it("an automatic fill in flight stays invisible", () => {
    render(<TimelineView items={items} hasMoreOlder loadingOlder onLoadOlder={vi.fn()} />);
    expect(strip().dataset.state).toBe("idle");
    expect(strip().querySelector(".animate-spin")).toBeNull();
    expect(strip().getAttribute("aria-hidden")).toBe("true");
  });

  // meiva:note
  // One request, one ring element: a CSS animation restarts only when its
  // element is created or its animation name changes, so a stable node carrying
  // the same `animate-spin` proves the spin ran on through every update. A
  // conversation that reports every 0.5 s re-renders this surface at that rate.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/loading-ring.meiva.okf.md
  it("the reader's request presents one ring that a 0.5 s update stream never rebuilds", () => {
    const requested: OlderLoadOrigin[] = [];
    function Surface() {
      const [inFlight, setInFlight] = useState(false);
      const [rows, setRows] = useState(items);
      return (
        <>
          <button type="button" onClick={() => setRows([...rows, makeItem({ item_id: `${rows.length + 11}.0`, kind: "agent_chat", payload: "tick" })])}>
            tick
          </button>
          <TimelineView
            items={rows}
            hasMoreOlder
            loadingOlder={inFlight}
            readerLoadingOlder={inFlight}
            onLoadOlder={(origin) => { requested.push(origin); setInFlight(origin === "reader"); }}
          />
        </>
      );
    }
    render(<Surface />);
    expect(strip().dataset.state).toBe("idle");

    fireEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));
    expect(requested).toEqual(["reader"]);
    expect(strip().dataset.state).toBe("loading");
    const presentedStrip = strip();
    const ring = strip().querySelector("svg");
    expect(ring).not.toBeNull();
    expect(ring?.getAttribute("class")).toContain("animate-spin");

    for (let tick = 0; tick < 6; tick++) {
      fireEvent.click(screen.getByRole("button", { name: "tick" }));
      expect(strip()).toBe(presentedStrip);
      expect(strip().querySelector("svg")).toBe(ring);
      expect(strip().dataset.state).toBe("loading");
      expect(strip().querySelector(".animate-spin")).toBe(ring);
    }
  });
});
