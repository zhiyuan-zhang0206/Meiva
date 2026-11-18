import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { LoadOlderSpinner } from "../overlays";
afterEach(cleanupTimelineTest);
describe("ItemView: system_marker → LifecycleChip", () => {
  it("source='lifecycle_terminate' → Terminated chip", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_terminate", payload: "[ts] You are terminated by user" })]} />);
    revealCollapsedCards();
    expect(screen.getByText("Terminated")).toBeTruthy();
    expect(screen.getByText(/terminated by user/)).toBeTruthy();
  });
  it("source='lifecycle_restart' → Restarted chip", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_restart", payload: "[ts] You have been restarted" })]} />);
    revealCollapsedCards();
    expect(screen.getByText("Restarted")).toBeTruthy();
  });
  it("source='lifecycle_resurrect' → Resurrected chip", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_resurrect", payload: "[ts] resurrected" })]} />);
    revealCollapsedCards();
    expect(screen.getByText("Resurrected")).toBeTruthy();
  });
  it("source='lifecycle_interrupt' → Interrupted chip naming who interrupted", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_interrupt", payload: "[ts] Your running turn was interrupted by agent:7. Their message follows." })]} />);
    revealCollapsedCards();
    expect(screen.getByText("Interrupted")).toBeTruthy();
    expect(screen.getByText(/interrupted by agent:7/)).toBeTruthy();
  });
  it("source='lifecycle_fork' → Forked chip", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_fork", payload: "[ts] forked from agent:7" })]} />);
    revealCollapsedCards();
    expect(screen.getByText("Forked")).toBeTruthy();
  });
  it("source='lifecycle_unknown' → UnknownMarkerChip red alarm asking for a reload", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_future", payload: "x" })]} />);
    revealCollapsedCards();
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
    expect(screen.getByTestId("marker-unrecognized-reload").textContent).toMatch(/Reload/);
  });
});

describe("ItemView: system_marker → ephemeral by payload", () => {
  it("payload='exec_start' → unrecognized alarm without the reload hint (untagged, not a stale page)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "exec_start" })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
    expect(screen.queryByTestId("marker-unrecognized-reload")).toBeNull();
  });
  it("payload='compact_done' → no visible marker (internal event)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "compact_done" })]}
      />,
    );
    expect(screen.queryByText(/compact complete/)).toBeNull();
  });
  it("payload='cancelled' → silent, not rendered (stop button has no UI noise)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "cancelled" })]}
      />,
    );
    expect(screen.queryByText(/cancelled/)).toBeNull();
  });
  it("payload='status:Cancellation requested.' → silent, not rendered and does not split TurnBlocks", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "step 1", reasoning_ms: 100 }),
          makeItem({ item_id: "2.0", kind: "system_marker", payload: "status:Cancellation requested." }),
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "step 2", reasoning_ms: 100 }),
        ]}
      />,
    );
    expect(screen.queryByText(/Cancellation requested/)).toBeNull();
    expect(screen.queryByTestId("marker-unrecognized")).toBeNull();
    expect(screen.getAllByText(/Worked for/)).toHaveLength(1);
  });
  it("payload='compact_request:<content>' → no visible marker (internal event)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "system_marker", payload: "compact_request:context too long" }),
        ]}
      />,
    );
    expect(screen.queryByText(/compact request/)).toBeNull();
  });
  it("payload='error:<content>' → '[error] <content>' (text-destructive)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "system_marker", payload: "error:redis disconnected" }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(/error.*redis disconnected/)).toBeTruthy();
  });
  it("unrecognized payload goes to UnknownMarkerChip red alarm", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "system_marker", payload: "future_kind_not_adapted" }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
  });
});

describe("ItemView: system_marker → system_note chip", () => {
  it("source='sdk_hint' → neutral Note chip, not the red alarm", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "sdk_hint",
            payload: "[system] use meiva.agents.send_message",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(
      screen.getByText(/use meiva\.agents\.send_message/),
    ).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='memory' → dedicated Memory chip (distinct from a guidance note)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "memory",
            payload: "[system] your standing memory",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Memory")).toBeTruthy();
    expect(screen.queryByText("Note")).toBeNull();
    expect(screen.getByText(/your standing memory/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='compact_reminder' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "compact_reminder",
            payload: "[system] wind down soon",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='security' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "security",
            payload: "[security note] sensitive content detected",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/sensitive content detected/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='context' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "context",
            payload: "[context note] additional context injected",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/additional context injected/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='project_skills' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "project_skills",
            payload: "[project_skills note] project skills listed here",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/project skills listed here/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='new_skills' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "new_skills",
            payload: "[system] Skills installed since your index was built: web-ai",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/Skills installed since your index was built/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='skill_recall' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "skill_recall",
            payload: "[system] These skills look relevant to the current request: web-ai",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/look relevant to the current request/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("source='preloaded_skills' → neutral Note chip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "preloaded_skills",
            payload: "[system] Preloaded skills — full body of ultra_speed here",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Note")).toBeTruthy();
    expect(screen.getByText(/full body of ultra_speed/)).toBeTruthy();
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
});

describe("ItemView: inbound_compact_*", () => {
  function expectNoUnrecognizedAlarm() {
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  }
  it("inbound_compact_summary renders envelope", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_compact_summary",
            payload: "Compact summary [ts]:\n\nbody text",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    const matches = screen.getAllByText(/Compact summary/);
    expect(matches.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/body text/)).toBeTruthy();
    expectNoUnrecognizedAlarm();
  });
  it("inbound_compact_request renders envelope", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_compact_request",
            payload: "Compact request [ts]:\n\nplease compact",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(/please compact/)).toBeTruthy();
    expectNoUnrecognizedAlarm();
  });
});

describe("CopyButton", () => {
  it("partial agent_chat does not show CopyButton (only final is copyable)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "agent_chat", payload: "streaming...", partial: true }),
        ]}
      />,
    );
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
  it("final agent_chat shows CopyButton (icon-only, aria-label)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "done" })]}
      />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });
});

describe("Scroll-to-bottom button + multi-item render order", () => {
  it("multi-item renders in items order (DOM order = items order)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
          makeItem({ item_id: "2", kind: "agent_chat", payload: "second" }),
          makeItem({ item_id: "3", kind: "agent_chat", payload: "third" }),
        ]}
      />,
    );
    const markdowns = screen.getAllByTestId("chat-markdown");
    expect(markdowns.map((m) => m.textContent)).toEqual(["first", "second", "third"]);
  });
  it("ScrollArea wraps the content", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    expect(screen.getByTestId("scroll-area")).toBeTruthy();
  });
  it("timeline viewport disables scroll anchoring (overflow-anchor: none)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    expect(screen.getByTestId("scroll-viewport").className).toContain("overflow-anchor");
  });
  it("timeline viewport contains overscroll (does not chain to the page)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    expect(screen.getByTestId("scroll-viewport").className).toContain("overscroll-y-contain");
  });
  // meiva:note
  // The control is absent while the reader is at the bottom — the former rule
  // kept it in the DOM and faded, which left a hover target over a click that
  // did nothing (user report 2026-10-30).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  it("Scroll-to-bottom control is absent while the reader is at the bottom", () => {
    render(<TimelineView items={[]} />);
    expect(screen.queryByLabelText("Scroll down")).toBeNull();
  });
});

function expectTopFloatingDisc(overlay: HTMLElement | null) {
  expect(overlay).not.toBeNull();
  for (const token of [
    "absolute",
    "left-1/2",
    "-translate-x-1/2",
    "top-14",
    "z-20",
    "pointer-events-none",
    "flex",
    "size-9",
    "rounded-full",
    "bg-background",
    "border",
    "border-border",
    "shadow-md",
    "items-center",
    "justify-center",
    "text-muted-foreground",
  ]) {
    expect(overlay?.classList.contains(token)).toBe(true);
  }
  for (const staleToken of ["inset-x-0", "top-11", "py-12", "h-8", "w-8", "right-5"]) {
    expect(overlay?.classList.contains(staleToken)).toBe(false);
  }
  expect(overlay?.parentElement?.classList.contains("relative")).toBe(true);
}

describe("cold-load spinner", () => {
  it("loading + no items → shows the shared ring inside the floating disc below the header bar, not its own glyph and label", () => {
    render(<TimelineView items={[]} loading />);
    const spinner = screen.getByTestId("cold-load-spinner");
    expectTopFloatingDisc(spinner);
    expect(spinner.getAttribute("role")).toBe("status");
    expect(spinner.querySelector('[data-testid="cold-load-ring"]')).not.toBeNull();
    expect(screen.getByText(/Loading conversation…/).className).toContain("sr-only");
  });
  it("retained content stays readable under the qualification disc until loading ends", () => {
    const items = [makeItem({ kind: "agent_chat", payload: "hi" })];
    const { rerender } = render(<TimelineView items={items} loading />);
    expectTopFloatingDisc(screen.getByTestId("cold-load-spinner"));
    expect(screen.getByTestId("cold-load-ring")).toBeTruthy();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("hi");
    rerender(<TimelineView items={items} />);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("hi");
  });
  it("empty + not loading → no spinner (steady-state empty thread)", () => {
    render(<TimelineView items={[]} />);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
  });
});

// meiva:note
// One motion contract for the one ring: it turns while a load is in flight, and a
// reader who asked for reduced motion gets the same ring breathing (opacity
// only) rather than a frozen frame. `motion-reduce:animate-none` is the
// regression this pins — a still ring reads as stuck, not busy (user report
// 2026-10-29: "it doesn't spin any more, it's a static animation").
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
describe("loading ring motion (cold load, pull strip, scroll-to-bottom)", () => {
  const expectRingMoves = (ring: Element | null) => {
    expect(ring).not.toBeNull();
    const classes = ring?.getAttribute("class") ?? "";
    expect(classes).toContain("animate-spin");
    expect(classes).toContain("motion-reduce:animate-pulse");
    expect(classes).not.toContain("motion-reduce:animate-none");
    expect((ring as SVGElement).style.transform).toBe("");
  };

  it("cold load: the ring turns", () => {
    render(<TimelineView items={[]} loading />);
    expectRingMoves(screen.getByTestId("cold-load-ring"));
  });
  it("pull strip: the ring turns while an older page loads", () => {
    render(<LoadOlderSpinner loadingOlder />);
    expectRingMoves(screen.getByTestId("load-older-spinner").querySelector("svg"));
  });

});

describe("load-older circle (revealed above the messages)", () => {
  it("loadingOlder=false → spinner present in floating disc but hidden (opacity-0) and hidden from assistive tech", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    const label = screen.getByText(/Loading earlier messages/);
    const overlay = label.closest("div");
    expectTopFloatingDisc(overlay);
    expect(overlay?.className).toContain("opacity-0");
    expect(overlay?.className).not.toContain("opacity-100");
    expect(overlay?.getAttribute("aria-hidden")).toBe("true");
  });
  it("readerLoadingOlder=true → spinner visible (opacity-100) inside the floating disc, stays mounted at the top, exposed to assistive tech", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder readerLoadingOlder />,
    );
    const label = screen.getByText(/Loading earlier messages/);
    const overlay = label.closest("div");
    expectTopFloatingDisc(overlay);
    expect(overlay?.className).toContain("opacity-100");
    expect(overlay?.className).not.toContain("opacity-0");
    expect(overlay?.getAttribute("aria-hidden")).toBe("false");
    expect(overlay?.querySelector(".animate-spin")).not.toBeNull();
  });

  it("loadingOlder=false → the hidden overlay runs no spin animation", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    const overlay = screen.getByTestId("load-older-spinner");
    expect(overlay.className).toContain("opacity-0");
    expect(overlay.querySelector(".animate-spin")).toBeNull();
  });
  it("pointer-events-none in BOTH states — the overlay never captures the scroll gesture that triggered it", () => {
    const { rerender } = render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    expect(
      screen.getByText(/Loading earlier messages/).closest("div")?.className,
    ).toContain("pointer-events-none");
    rerender(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder readerLoadingOlder />,
    );
    expect(
      screen.getByText(/Loading earlier messages/).closest("div")?.className,
    ).toContain("pointer-events-none");
  });
  it("re-render with the load flipping true→false does not remount the overlay (same node, opacity only)", () => {
    const { rerender } = render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder readerLoadingOlder />,
    );
    const nodeWhileLoading = screen.getByText(/Loading earlier messages/).closest("div");
    rerender(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
        loadingOlder={false}
        readerLoadingOlder={false}
      />,
    );
    const nodeAfterLoad = screen.getByText(/Loading earlier messages/).closest("div");
    expect(nodeAfterLoad).toBe(nodeWhileLoading);
    expect(nodeAfterLoad?.className).toContain("opacity-0");
  });
});
