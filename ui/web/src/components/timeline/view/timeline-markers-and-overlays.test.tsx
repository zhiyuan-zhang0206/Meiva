import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards } from "./timeline-test-harness";
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
    expect(screen.getByText("Restarted")).toBeTruthy();
  });
  it("source='lifecycle_resurrect' → Resurrected chip", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_resurrect", payload: "[ts] resurrected" })]} />);
    expect(screen.getByText("Resurrected")).toBeTruthy();
  });
  it("source='lifecycle_fork' → Forked chip", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_fork", payload: "[ts] forked from agent:7" })]} />);
    expect(screen.getByText("Forked")).toBeTruthy();
  });
  it("source='lifecycle_unknown' → UnknownMarkerChip red alarm", () => {
    render(<TimelineView items={[makeItem({ kind: "system_marker", source: "lifecycle_future", payload: "x" })]} />);
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
  });
});

describe("ItemView: system_marker → ephemeral by payload", () => {
  it("payload='exec_start' → unrecognized (no longer a handled marker; falls to alarm)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "exec_start" })]}
      />,
    );
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
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
    expect(screen.getByText("body text")).toBeTruthy();
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
    expect(screen.getByText("please compact")).toBeTruthy();
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
  it("Scroll-to-bottom arrow button always in DOM (visibility controlled by opacity)", () => {
    render(<TimelineView items={[]} />);
    expect(screen.getByLabelText("Scroll to bottom")).toBeTruthy();
  });
});

describe("cold-load spinner", () => {
  it("loading + no items → shows the shared ring, not its own glyph and label", () => {
    render(<TimelineView items={[]} loading />);
    const spinner = screen.getByTestId("cold-load-spinner");
    expect(spinner.querySelector('[data-testid="cold-load-ring"]')).not.toBeNull();
    expect(screen.getByText(/Loading conversation…/).className).toContain("sr-only");
  });
  it("loading + items present → no spinner (keep the content on screen)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "hi" })]} loading />);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("hi");
  });
  it("empty + not loading → no spinner (steady-state empty thread)", () => {
    render(<TimelineView items={[]} />);
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
  });
});

describe("load-older circle (revealed above the messages)", () => {
  it("loadingOlder=false → spinner present but hidden (opacity-0) and hidden from assistive tech", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    const label = screen.getByText(/Loading earlier messages/);
    const overlay = label.closest("div");
    expect(overlay?.className).toContain("opacity-0");
    expect(overlay?.className).not.toContain("opacity-100");
    expect(overlay?.getAttribute("aria-hidden")).toBe("true");
  });
  it("loadingOlder=true → spinner visible (opacity-100), stays mounted at the top, exposed to assistive tech", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder />,
    );
    const label = screen.getByText(/Loading earlier messages/);
    const overlay = label.closest("div");
    expect(overlay?.className).toContain("opacity-100");
    expect(overlay?.className).not.toContain("opacity-0");
    expect(overlay?.getAttribute("aria-hidden")).toBe("false");
    expect(overlay?.querySelector(".animate-spin")).not.toBeNull();
  });

  // meiva:note
  // The pull strip is overflow-hidden and only as tall as the revealed space, so
  // a fixed-size ring is sliced into a bar for the first few pixels of a pull
  // (user report 2026-10-20). The ring scales with the strip instead.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
  it("a short pull scales the ring instead of slicing it into a bar", () => {
    render(<LoadOlderSpinner loadingOlder={false} pullProgress={0.4} pullOffset={8} />);
    const ring = screen.getByTestId("load-older-spinner").querySelector("svg");
    expect(ring?.getAttribute("style")).toContain("scale(0.4)");
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("pulling");
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
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder />,
    );
    expect(
      screen.getByText(/Loading earlier messages/).closest("div")?.className,
    ).toContain("pointer-events-none");
  });
  it("re-render with loadingOlder flipping true→false does not remount the overlay (same node, opacity only)", () => {
    const { rerender } = render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} loadingOlder />,
    );
    const nodeWhileLoading = screen.getByText(/Loading earlier messages/).closest("div");
    rerender(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
        loadingOlder={false}
      />,
    );
    const nodeAfterLoad = screen.getByText(/Loading earlier messages/).closest("div");
    expect(nodeAfterLoad).toBe(nodeWhileLoading);
    expect(nodeAfterLoad?.className).toContain("opacity-0");
  });
});
