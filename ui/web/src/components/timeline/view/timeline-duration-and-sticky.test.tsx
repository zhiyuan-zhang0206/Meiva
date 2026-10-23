import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards, setToggleState } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { streamingParseIntervalMs } from "../item";
import { LIFECYCLE_TAGS, MEMORY_SOURCES, NOTE_SOURCES } from "../markers";
afterEach(cleanupTimelineTest);
describe("detail-block duration display (Last mode)", () => {
  const runToggle = () => screen.getByTestId("turn-toggle");
  it("streaming code block inside the active last turn shows a live 'Writing code for'", () => {
    setToggleState({ detailsMode: "last" });
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
  it("active last turn header shows a live 'Working for'", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "hmm" }),
        ]}
      />,
    );
    expect(runToggle().textContent).toContain("Working for");
  });
  it("live 'Working for' does not wait for a work item — a system-only active turn still ticks", () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "schedule:1", payload: "wake" }),
        ]}
      />,
    );
    expect(runToggle().textContent).toContain("Working for");
  });
  it("completed single-code-block turn shows 'Worked for' and the rounds count", () => {
    setToggleState({ detailsMode: "last" });
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
    setToggleState({ detailsMode: "last" });
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
    setToggleState({ detailsMode: "last" });
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
    const { rerender } = render(<TimelineView turnActive items={items} />);
    expect(runToggle().textContent).toContain("Working for 17s");
    expect(runToggle().textContent).not.toContain("6m");
    rerender(<TimelineView items={items} />);
    expect(runToggle().textContent).toContain("Worked for 17s");
  });
  it("live timer adds the streaming block's elapsed to the committed total", () => {
    setToggleState({ detailsMode: "last" });
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
    setToggleState({ detailsMode: "last" });
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
  it("active last turn stays expanded across streaming commits; collapses when a reply lands below", () => {
    setToggleState({ detailsMode: "last" });
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

describe("TimelineView sticky work block (task #2601)", () => {
  function makeMockRect(top: number, bottom: number, height: number): DOMRect {
    return {
      top,
      bottom,
      height,
      left: 0,
      right: 400,
      width: 400,
      x: 0,
      y: top,
      toJSON: () => undefined,
    };
  }
  it("renders expanded work block with sticky header classes", () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    render(<TimelineView items={items} />);
    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.className).toContain("sticky");
    expect(toggle.className).toContain("top-11");
    expect(toggle.className).toContain("z-10");
  });
  it("activates stuck state on scroll past top and updates data-stuck", async () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    const viewport = container.querySelector('[data-slot="scroll-area-viewport"]')!;
    vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue(makeMockRect(0, 800, 800));
    const turnEl = container.querySelector('[data-turn-expanded="true"]')!;
    vi.spyOn(turnEl, "getBoundingClientRect").mockReturnValue(makeMockRect(10, 700, 690));
    fireEvent.scroll(viewport);
    await waitFor(() => {
      const toggle = screen.getByTestId("turn-toggle");
      expect(toggle.getAttribute("data-stuck")).toBe("true");
      expect(toggle.className).toContain("backdrop-blur-md");
      expect(toggle.className).toContain("shadow-[");
      expect(toggle.className).toContain("16px_0_0_0_var(--background)");
      expect(toggle.className).toContain("after:h-px");
      expect(toggle.className).toContain("after:bg-border");
      expect(toggle.className).not.toContain("0_2px_0_0_var(--background)");
    });
  });
  it("keeps an expanded work block pinned through a rapid scroll burst", async () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    const viewport = container.querySelector('[data-slot="scroll-area-viewport"]')!;
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    Object.defineProperty(viewport, "scrollHeight", { value: 8000, configurable: true });
    vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue(makeMockRect(0, 800, 800));
    const turnEl = container.querySelector('[data-turn-expanded="true"]')!;
    vi.spyOn(turnEl, "getBoundingClientRect").mockReturnValue(makeMockRect(10, 700, 690));
    fireEvent.scroll(viewport);
    await waitFor(() => {
      expect(screen.getByTestId("turn-toggle").getAttribute("data-stuck")).toBe("true");
    });
    for (const top of [1200, 2400, 3600, 4800, 6000]) {
      act(() => {
        viewport.scrollTop = top;
        fireEvent.scroll(viewport);
      });
      await waitFor(() => {
        const toggle = screen.getByTestId("turn-toggle");
        expect(toggle.getAttribute("data-stuck")).toBe("true");
        expect(toggle.className).toContain("sticky");
        expect(toggle.className).not.toContain("group-data-[fast-scrolling");
      });
    }
    expect(container.querySelector('[data-fast-scrolling="true"]')).toBeNull();
  });
  it("only marks the closest/latest work block as stuck when multiple blocks cross top", async () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking 1" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code 1" }),
      makeItem({ item_id: "2.0", kind: "agent_chat", payload: "intermediate reply" }),
      makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "thinking 2" }),
      makeItem({ item_id: "3.1", kind: "agent_code", payload: "code 2" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    const viewport = container.querySelector('[data-slot="scroll-area-viewport"]')!;
    vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue(makeMockRect(0, 800, 800));
    const turnEls = container.querySelectorAll<HTMLElement>('[data-turn-expanded="true"]');
    expect(turnEls.length).toBe(2);
    vi.spyOn(turnEls[0], "getBoundingClientRect").mockReturnValue(makeMockRect(-300, 200, 500));
    vi.spyOn(turnEls[1], "getBoundingClientRect").mockReturnValue(makeMockRect(20, 750, 730));
    fireEvent.scroll(viewport);
    await waitFor(() => {
      const toggles = screen.getAllByTestId("turn-toggle");
      expect(toggles[0].getAttribute("data-stuck")).toBe("false");
      expect(toggles[1].getAttribute("data-stuck")).toBe("true");
    });
  });
  it("collapsing while stuck collapses in place and preserves viewport scroll position", () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    const viewport = container.querySelector('[data-slot="scroll-area-viewport"]')!;
    viewport.scrollTop = 450;
    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("data-expanded")).toBe("false");
    expect(viewport.scrollTop).toBe(450);
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
  it.each(["all", "last", "none"] as const)("keeps primary titles in flow and work/detail titles sticky in %s mode", (mode) => {
    setToggleState({ detailsMode: mode });
    const { container } = render(<TimelineView items={items} />);
    for (const toggle of screen.getAllByTestId("turn-toggle")) {
      if (toggle.getAttribute("data-expanded") === "false") fireEvent.click(toggle);
    }
    revealCollapsedCards();
    const titles = [...screen.getAllByTestId("card-toggle"), ...screen.getAllByTestId("turn-toggle")];
    expect(titles).toHaveLength(6);
    for (const id of ["1.0", "3.0"]) {
      const row = container.querySelector<HTMLElement>(`.timeline-item[data-item-id="${id}"]`)!;
      const title = row.querySelector<HTMLElement>('[data-testid="card-toggle"]')!;
      expect(row.dataset.cardSticky).toBe("false");
      expect(title.className).not.toContain("sticky");
      expect(title.dataset.stuck).toBe("false");
    }
    const workTitle = screen.getByTestId("turn-toggle");
    expect(workTitle.className).toContain("sticky");
    expect(workTitle.className).toContain("top-11");
    const detailRows = container.querySelectorAll<HTMLElement>('[data-turn-child="true"]');
    expect(detailRows).toHaveLength(3);
    for (const row of detailRows) {
      const title = row.querySelector<HTMLElement>('[data-testid="card-toggle"]')!;
      expect(row.dataset.cardSticky).toBe("true");
      expect(title.className).toContain("sticky");
      expect(title.className).toContain("var(--turn-header-h");
    }
    const viewport = screen.getByTestId("scroll-viewport");
    act(() => { viewport.scrollTop = 450; fireEvent.scroll(viewport); });
    for (const title of titles) expect(container.contains(title)).toBe(true);
    expect(container.querySelector('[style*="--turn-header-h"]')).not.toBeNull();
  });
  it("collapses an expanded work block at the current reading position", () => {
    render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 450;
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("false");
    expect(viewport.scrollTop).toBe(450);
    expect(screen.getByText("Human message")).toBeDefined();
    expect(screen.getByText("Agent reply")).toBeDefined();
  });
});
