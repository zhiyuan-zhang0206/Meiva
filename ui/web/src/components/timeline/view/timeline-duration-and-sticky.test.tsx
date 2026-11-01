import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { streamingParseIntervalMs } from "../item";
import { LIFECYCLE_TAGS, MEMORY_SOURCES, NOTE_SOURCES } from "../model/marker-classify";
afterEach(cleanupTimelineTest);
describe("detail-block duration display", () => {
  const runToggle = () => screen.getByTestId("turn-toggle");
  it("streaming code block inside the active last turn shows a live 'Writing code for'", () => {
    render(
      <TimelineView
        turnActive
        streamingCode
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "on it" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_code",
            payload: "x = 1",
            codeStartedAt: Date.now() - 3_000,
          }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/Writing code for/)).toBeTruthy();
  });
  it("active last turn header shows the unified 'Working for' header and bounded scrollport", () => {
    const { container } = render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "hmm" }),
        ]}
      />,
    );
    expect(container.querySelector('[data-live-activity-window="true"]')).not.toBeNull();
    expect(screen.getByTestId("activity-scroll-viewport")).toBeTruthy();
    expect(runToggle().textContent).toContain("Working for");
    expect(runToggle().textContent).toContain("1 turn");
  });
  it("folds llmWaiting keepalive into the active turn as Thinking without spawning an extra block", () => {
    const now = Date.now();
    const { container } = render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "hello" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "",
            llmWaiting: true,
            partial: true,
            reasoningStartedAt: now - 2_000,
          }),
        ]}
      />,
    );
    expect(screen.queryByTestId("pending-live-activity-window")).toBeNull();
    expect(container.querySelectorAll("[data-turn-id]")).toHaveLength(1);
    expect(runToggle().textContent).toContain("Working for");
    expect(screen.getByTestId("card-toggle").textContent).toContain("Thinking for");
    expect(container.textContent).not.toContain("Waiting for model");
  });
  it("a system-only active turn with zero elapsed work renders 'Working…'", () => {
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "schedule:1", payload: "wake" }),
        ]}
      />,
    );
    expect(runToggle().textContent).toContain("Working…");
  });
  it("completed single-code-block turn shows 'Worked for' and the rounds count", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_code",
            payload: "x = 1",
            code_elapsed_ms: 5_000,
          }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    const header = runToggle();
    expect(header.textContent).toContain("Worked for 5s");
    expect(header.textContent).toContain("1 turn");
  });
  it("system-note-only turn shows '1 system note', not a blank header and no 'Worked for'", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({ item_id: "2.0", kind: "system_marker", source: "sdk_hint", payload: "note text" }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    const header = runToggle();
    expect(header.textContent).toContain("1 system note");
    expect(header.textContent).not.toContain("Worked for");
  });
  it("live timer excludes the idle gap the turn spans, so it does not drop at turn end", () => {
    const now = Date.now();
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
      makeItem({
        item_id: "2.0",
        kind: "inbound_chat",
        source: "schedule:1",
        payload: "wake",
        created_at: new Date(now - 6 * 60_000).toISOString(),
      }),
      makeItem({
        item_id: "3.0",
        kind: "agent_code",
        payload: "x = 1",
        code_elapsed_ms: 17_000,
        created_at: new Date(now - 1_000).toISOString(),
      }),
    ];
    const { container, rerender } = render(<TimelineView turnActive items={items} />);
    expect(runToggle().textContent).toContain("Working for 17s");
    expect(runToggle().textContent).not.toContain("6m");
    rerender(<TimelineView items={items} />);
    expect(container.querySelector('[data-live-activity-window="true"]')).toBeNull();
    expect(runToggle().textContent).toContain("Worked for 17s");
  });
  it("live timer adds the streaming block's elapsed to the committed total", () => {
    const now = Date.now();
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hmm",
            reasoning_ms: 10_000,
            created_at: new Date(now - 3_500).toISOString(),
          }),
          makeItem({
            item_id: "3.0",
            kind: "agent_code",
            payload: "x = 1",
            codeStartedAt: now - 3_000,
            created_at: new Date(now - 3_000).toISOString(),
          }),
        ]}
      />,
    );
    const header = runToggle().textContent;
    expect(header).toContain("Working for 13s");
    expect(header).toContain("1 turn");
  });
  it("live timer shows on a turn with no stamped member at all", () => {
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hmm",
            reasoning_ms: 4_000,
            created_at: null,
          }),
        ]}
      />,
    );
    expect(runToggle().textContent).toContain("Working for 4s");
  });
  it("renders PendingLiveActivityWindow when turnActive is true right after a human message", () => {
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "do it" }),
        ]}
      />,
    );
    const pending = screen.getByTestId("pending-live-activity-window");
    expect(pending.textContent).toContain("Working…");
  });
  it("scrolling up inside the Live Activity Window reveals ScrollToBottomButton and clicking re-attaches", () => {
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "do it" }),
          makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "step 1" }),
          makeItem({ item_id: "2.1", kind: "agent_code", payload: "print(1)" }),
        ]}
      />,
    );
    const inner = screen.getByTestId("activity-scroll-viewport");
    Object.defineProperty(inner, "scrollHeight", { value: 600, configurable: true });
    Object.defineProperty(inner, "clientHeight", { value: 224, configurable: true });
    inner.scrollTop = 376;
    fireEvent.scroll(inner);
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();

    inner.scrollTop = 100;
    fireEvent.scroll(inner);
    const btn = screen.getByTestId("scroll-to-bottom");
    expect(btn).toBeTruthy();

    const scrollToSpy = vi.fn();
    inner.scrollTo = scrollToSpy;
    fireEvent.click(btn);
    expect(scrollToSpy).toHaveBeenCalledWith({ top: 600, behavior: "smooth" });
    expect(screen.queryByTestId("scroll-to-bottom")).toBeNull();
  });
  it("active last turn stays expanded across streaming commits; collapses when a reply lands below", () => {
    const base = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
      makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "hmm" }),
    ];
    const { rerender } = render(<TimelineView turnActive items={base} />);
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    rerender(
      <TimelineView
        turnActive
        items={[...base, makeItem({ item_id: "2.1", kind: "agent_code", payload: "x=1" })]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    rerender(
      <TimelineView
        turnActive
        items={[
          ...base,
          makeItem({ item_id: "2.1", kind: "agent_code", payload: "x=1" }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("false");
  });
});

describe("Marker contract: every dispatch-set source renders without the red alarm", () => {
  const knownSources = [
    ...LIFECYCLE_TAGS,
    ...Array.from(MEMORY_SOURCES),
    ...Array.from(NOTE_SOURCES),
  ];
  it.each(knownSources)("source=%s renders its chip, never the alarm", (source) => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source,
            payload: "[system] contract-test payload",
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Unrecognized system_marker/)).toBeNull();
  });
  it("positive control: an unknown source still renders the red alarm", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "lifecycle_future_tag",
            payload: "x",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
  });
});

describe("streamingParseIntervalMs (adaptive stream-parse window, user report 2026-09-06)", () => {
  it("keeps the base 40ms window for short payloads", () => {
    expect(streamingParseIntervalMs(0)).toBe(40);
    expect(streamingParseIntervalMs(100)).toBe(40);
    expect(streamingParseIntervalMs(2400)).toBe(40);
  });
  it("widens one SSE window per 2400 bytes", () => {
    expect(streamingParseIntervalMs(2401)).toBe(80);
    expect(streamingParseIntervalMs(4800)).toBe(80);
    expect(streamingParseIntervalMs(4801)).toBe(120);
    expect(streamingParseIntervalMs(20_000)).toBe(360);
  });
  it("caps at 1000ms — a 300-line block re-parses ~1x/s", () => {
    expect(streamingParseIntervalMs(57_600)).toBe(960);
    expect(streamingParseIntervalMs(60_000)).toBe(1000);
    expect(streamingParseIntervalMs(200_000)).toBe(1000);
  });
});

describe("TimelineView bounded expanded work block", () => {
  it("renders expanded work block without sticky header classes and inside a max-height scrollport", () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    render(<TimelineView items={items} />);
    const toggle = screen.getByTestId("turn-toggle");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.className).not.toContain("sticky");
    const turnViewport = screen.getByTestId("turn-scroll-viewport");
    expect(turnViewport.className).toContain("max-h-[var(--timeline-turn-max-h,50dvh)]");
  });
  it("collapsing an expanded work block preserves outer viewport scroll position", () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 450;
    const toggle = screen.getByTestId("turn-toggle");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("data-expanded")).toBe("false");
    expect(viewport.scrollTop).toBe(450);
  });
  it("chains upward and downward wheel events to the outer message list once the expanded TurnBlock hits its scroll boundary", () => {
    const items = [
      makeItem({ item_id: "0.5", kind: "inbound_chat", source: "user", payload: "Prompt" }),
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    render(<TimelineView items={items} turnActive />);
    const outerViewport = screen.getByTestId("scroll-viewport");
    const innerViewport = screen.getByTestId("activity-scroll-viewport");
    expect(innerViewport.className).not.toContain("overscroll-y-contain");

    Object.defineProperty(outerViewport, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(outerViewport, "clientHeight", { value: 600, configurable: true });
    outerViewport.scrollTop = 300;

    Object.defineProperty(innerViewport, "scrollHeight", { value: 500, configurable: true });
    Object.defineProperty(innerViewport, "clientHeight", { value: 250, configurable: true });
    innerViewport.scrollTop = 120;

    // meiva:note
    // While inner viewport still has upward travel, wheel stays inside inner viewport.
    fireEvent.wheel(innerViewport, { deltaY: -40 });
    expect(outerViewport.scrollTop).toBe(300);

    // meiva:note
    // Once inner viewport reaches top (scrollTop = 0), upward wheel chains to outer message list.
    innerViewport.scrollTop = 0;
    fireEvent.wheel(innerViewport, { deltaY: -45 });
    expect(outerViewport.scrollTop).toBe(255);

    // meiva:note
    // Once inner viewport reaches bottom (scrollTop = 250), downward wheel chains to outer message list.
    innerViewport.scrollTop = 250;
    fireEvent.wheel(innerViewport, { deltaY: 35 });
    expect(outerViewport.scrollTop).toBe(290);
  });
});

describe("TimelineView header scrolling scope", () => {
  const items = [
    makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "Human message" }),
    makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "Thinking body" }),
    makeItem({ item_id: "2.1", kind: "agent_code", payload: "print('result')" }),
    makeItem({ item_id: "2.2", kind: "code_output", payload: "result" }),
    makeItem({ item_id: "3.0", kind: "agent_chat", payload: "Agent reply" }),
  ];
  it("keeps primary messages headerless and work/detail headers non-sticky", () => {
    const { container } = render(<TimelineView items={items} />);
    revealCollapsedCards();
    const titles = [...screen.getAllByTestId("card-toggle"), ...screen.getAllByTestId("turn-toggle")];
    expect(titles).toHaveLength(4);
    for (const id of ["1.0", "3.0"]) {
      const row = container.querySelector<HTMLElement>(`.timeline-item[data-item-id="${id}"]`)!;
      expect(row.querySelector('[data-testid="card-toggle"]')).toBeNull();
    }
    for (const title of titles) {
      expect(title.className).not.toContain("sticky");
      expect(container.contains(title)).toBe(true);
    }
    const viewport = screen.getByTestId("scroll-viewport");
    act(() => { viewport.scrollTop = 450; fireEvent.scroll(viewport); });
    for (const title of titles) expect(container.contains(title)).toBe(true);
  });
  it("collapses an expanded work block at the current reading position", () => {
    render(<TimelineView items={items} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("true");
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 450;
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("false");
    expect(viewport.scrollTop).toBe(450);
    expect(screen.getByText("Human message")).toBeDefined();
    expect(screen.getByText("Agent reply")).toBeDefined();
  });
});
