import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards, setUserSettings } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { streamingParseIntervalMs } from "../item";
import { NOTE_TAGS } from "@/lib/contracts/constants-generated";
afterEach(cleanupTimelineTest);
describe("detail-block duration display", () => {
  const runToggle = () => screen.getByTestId("turn-toggle");
  it("streaming code block inside the active last turn shows a live 'Writing code for'", () => {
    setUserSettings({ "display.expand_working_block": true });
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
  it("active last turn header shows the unified 'Working for' header and no inner scrollport", () => {
    setUserSettings({ "display.expand_working_block": true });
    const { container } = render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "hmm" }),
        ]}
      />,
    );
    expect(container.querySelector('[data-live-activity-window="true"]')).not.toBeNull();
    expect(screen.queryByTestId("activity-scroll-viewport")).toBeNull();
    expect(runToggle().textContent).toContain("Working for");
    expect(runToggle().textContent).toContain("1 turn");
  });
  it("folds llmWaiting keepalive into the active turn as Thinking without spawning an extra block", () => {
    setUserSettings({ "display.expand_working_block": true });
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
    expect(header.textContent).toBe("Worked for 5s");
    fireEvent.click(header);
    expect(screen.getByText("1 turn")).toBeTruthy();
  });
  it("system-note-only turn has a Context label and reveals its note count on expansion", () => {
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
    expect(header.textContent).toBe("Context");
    fireEvent.click(header);
    expect(screen.getByText("1 system note")).toBeTruthy();
  });
  it("completed work turn with zero measured duration still shows 'Worked for' instead of Context", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "hi" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "quick thought",
            activity_revision: 1,
            created_at: new Date(Date.now() - 112_000).toISOString(),
          }),
          makeItem({ item_id: "3.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    const header = runToggle();
    expect(header.textContent).toBe("Worked for 0.1s");
    fireEvent.click(header);
    expect(screen.getByTestId("card-toggle").textContent).toBe("Thinking");
    expect(screen.getByTestId("card-toggle").textContent).not.toContain("Thinking for");
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
    setUserSettings({ "display.expand_working_block": true });
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
  it("active last turn stays expanded across streaming commits; collapses when a reply lands below", () => {
    setUserSettings({ "display.expand_working_block": true });
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

describe("Marker contract: every NoteTag renders without the red alarm", () => {
  it.each(NOTE_TAGS)("source=%s renders its chip, never the alarm", (source) => {
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

describe("TimelineView expanded work block height", () => {
  // meiva:note
  // An expanded work block flows at its natural height in the timeline: no
  // inner scrollport, no viewport-relative cap, whatever its position. A
  // finished block is as tall as the work really was and the reader scrolls the
  // timeline itself (user ruling 2026-11-09).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  it("keeps historical turns collapsed until opened, then renders them at their natural height", () => {
    const { container } = render(<TimelineView items={[
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "Earlier thought" }),
      makeItem({ item_id: "1.1", kind: "agent_chat", user_facing: false, payload: "Earlier progress" }),
      makeItem({ item_id: "2.0", kind: "agent_chat", payload: "Reply" }),
      makeItem({ item_id: "3.0", kind: "agent_code", payload: "latest()" }),
    ]} />);
    const [historical, trailing] = screen.getAllByTestId("turn-toggle");
    expect(historical.getAttribute("aria-expanded")).toBe("false");
    expect(trailing.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(historical);
    const earlier = container.querySelector<HTMLElement>('[data-turn-id="1.0"]')!;
    expect(earlier.getAttribute("data-turn-expanded")).toBe("true");
    expect(earlier.querySelector('[data-turn-scroll="true"]')).toBeNull();
    expect(earlier.querySelector('[class*="max-h-"]')).toBeNull();
    expect(screen.getByText("Earlier progress")).toBeTruthy();
    expect(historical.className).toContain("sticky");
    fireEvent.click(trailing);
    expect(trailing.getAttribute("aria-expanded")).toBe("true");
    expect(screen.queryByTestId("turn-scroll-viewport")).toBeNull();
    expect(container.querySelector('[data-turn-id="3.0"]')).not.toBeNull();
    fireEvent.click(historical);
    expect(historical.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Earlier progress")).toBeNull();
  });

  it("keeps an opened trailing turn expanded at natural height once it becomes historical", () => {
    const work = [makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "Thought" })];
    const { container, rerender } = render(<TimelineView items={work} />);
    const header = screen.getByTestId("turn-toggle");
    fireEvent.click(header);
    expect(container.querySelectorAll("[data-virtual-row]")).toHaveLength(1);
    rerender(<TimelineView items={[...work, makeItem({ item_id: "2.0", kind: "agent_chat", payload: "Done" })]} />);
    expect(screen.getByTestId("turn-toggle")).toBe(header);
    expect(header.getAttribute("aria-expanded")).toBe("true");
    expect(screen.queryByTestId("turn-scroll-viewport")).toBeNull();
    expect(container.querySelectorAll("[data-virtual-row]")).toHaveLength(1);
  });

  it("renders the expanded trailing work block with a sticky header and no height cap", () => {
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
      makeItem({ item_id: "1.2", kind: "code_output", payload: "output" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    const toggle = screen.getByTestId("turn-toggle");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.className).toContain("sticky");
    const turn = container.querySelector<HTMLElement>('[data-turn-id="1.0"]')!;
    expect(turn.querySelector('[class*="max-h-"]')).toBeNull();
    expect(container.querySelectorAll("[data-virtual-row]")).toHaveLength(3);
  });

  it("renders a work block under a human message at its natural height too", () => {
    const items = [
      makeItem({ item_id: "0.0", kind: "inbound_chat", payload: "envelope:go", source: "user" }),
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "code" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const turn = container.querySelector<HTMLElement>('[data-turn-id="1.0"]')!;
    expect(turn.getAttribute("data-turn-expanded")).toBe("true");
    expect(turn.querySelector('[class*="max-h-"]')).toBeNull();
    expect(screen.queryByTestId("turn-scroll-viewport")).toBeNull();
  });

  it("preserves a collapsed tail header when the browser would clamp its position", () => {
    const items = [makeItem({ item_id: "1.0", kind: "agent_code", payload: "code" })];
    const { container } = render(<TimelineView items={items} />);
    const viewport = screen.getByTestId("scroll-viewport");
    const toggle = screen.getByTestId("turn-toggle");
    fireEvent.click(toggle);
    let top = 500;
    const reserve = container.querySelector<HTMLElement>("[data-turn-collapse-reserve]")!;
    const naturalHeight = () => toggle.getAttribute("aria-expanded") === "true" ? 1100 : 760;
    const extra = () => Number.parseFloat(reserve.style.height) || 0;
    const maximum = () => naturalHeight() + extra() - 600;
    Object.defineProperties(viewport, {
      clientHeight: { value: 600 },
      scrollHeight: { get: () => naturalHeight() + extra() },
      scrollTop: {
        get: () => Math.min(top, maximum()),
        set: (value: number) => { top = Math.min(value, maximum()); },
      },
    });
    Object.defineProperty(reserve, "offsetHeight", { get: extra });
    vi.spyOn(reserve, "getBoundingClientRect").mockImplementation(() =>
      ({ top: naturalHeight() - viewport.scrollTop }) as DOMRect);
    vi.spyOn(toggle, "getBoundingClientRect").mockImplementation(() =>
      ({ top: 570 - viewport.scrollTop }) as DOMRect);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getBoundingClientRect().top).toBe(70);
    expect(extra()).toBe(340);

    act(() => { viewport.scrollTop = 120; fireEvent.scroll(viewport); });
    expect(extra()).toBe(0);
    expect(viewport.scrollTop).toBe(120);
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
});

describe("TimelineView header scrolling scope", () => {
  const items = [
    makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "Human message" }),
    makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "Thinking body" }),
    makeItem({ item_id: "2.1", kind: "agent_code", payload: "print('result')" }),
    makeItem({ item_id: "2.2", kind: "code_output", payload: "result" }),
    makeItem({ item_id: "3.0", kind: "agent_chat", payload: "Agent reply" }),
  ];
  it("keeps primary messages headerless and only pins the expanded work header", () => {
    const { container } = render(<TimelineView items={items} />);
    revealCollapsedCards();
    const titles = [...screen.getAllByTestId("card-toggle"), ...screen.getAllByTestId("turn-toggle")];
    expect(titles).toHaveLength(4);
    for (const id of ["1.0", "3.0"]) {
      const row = container.querySelector<HTMLElement>(`.timeline-item[data-item-id="${id}"]`)!;
      expect(row.querySelector('[data-testid="card-toggle"]')).toBeNull();
    }
    for (const title of titles) {
      expect(title.className.includes("sticky")).toBe(title.dataset.testid === "turn-toggle");
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
