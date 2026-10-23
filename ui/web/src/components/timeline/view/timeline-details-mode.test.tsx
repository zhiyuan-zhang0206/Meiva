import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useContentToggleReset } from "@/lib/layout/content-toggle-store";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, setToggleState } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { formatItemTime } from "../timestamp";
afterEach(cleanupTimelineTest);
describe("Details mode — collapse/expand", () => {
  it('detailsMode="none" → detail blocks collapsed, primary content visible, turn headers show summaries', () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "visible chat" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hidden thought",
            reasoning_ms: 8200,
            reasoning_tokens: 1234,
          }),
        ]}
      />,
    );
    expect(screen.queryByText("hidden thought")).toBeNull();
    expect(screen.getByText("visible chat")).toBeTruthy();
    expect(screen.getByText(/worked for 8s/i)).toBeTruthy();
    expect(screen.getByText(/1 turn/i)).toBeTruthy();
  });
  it('settings still loading (isLoading) → detail blocks collapsed even when the in-flight detailsMode reads "all"', () => {
    setToggleState({ detailsMode: "all", isLoading: true });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "visible chat" }),
          makeItem({
            item_id: "2.0",
            kind: "agent_reasoning",
            payload: "hidden thought",
            reasoning_ms: 8200,
            reasoning_tokens: 1234,
          }),
        ]}
      />,
    );
    expect(screen.queryByText("hidden thought")).toBeNull();
    expect(screen.getByText("visible chat")).toBeTruthy();
  });
  it("settings load completes → the real mode applies without a flash", () => {
    setToggleState({ detailsMode: "all", isLoading: true });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "chat" }),
      makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "thought" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    expect(screen.queryByText("thought")).toBeNull();
    setToggleState({ detailsMode: "all", isLoading: false });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("thought")).toBeTruthy();
  });
  it('detailsMode="last" → primary items (agent_chat) always visible', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "first chat" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "last chat" }),
        ]}
      />,
    );
    expect(screen.getByText("first chat")).toBeTruthy();
    expect(screen.getByText("last chat")).toBeTruthy();
  });
  it('detailsMode="last" → last item follows streaming content', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        items={[
          makeItem({
            item_id: "2.0",
            kind: "agent_chat",
            payload: "streaming message",
          }),
        ]}
      />,
    );
    expect(screen.getByText("streaming message")).toBeTruthy();
  });
  it('detailsMode="all" → all blocks expanded', () => {
    setToggleState({ detailsMode: "all" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "first chat" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "second chat" }),
        ]}
      />,
    );
    expect(screen.getByText("first chat")).toBeTruthy();
    expect(screen.getByText("second chat")).toBeTruthy();
  });
  it("clicking an expanded primary card header collapses it (override for 'none')", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "the visible chat" }),
        ]}
      />,
    );
    expect(screen.getByText("the visible chat")).toBeTruthy();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText("the visible chat")).toBeNull();
  });
  it("default expanded → clicking card header collapses content (override)", () => {
    setToggleState({ detailsMode: "all" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_chat", payload: "the open chat" }),
        ]}
      />,
    );
    expect(screen.getByText("the open chat")).toBeTruthy();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText("the open chat")).toBeNull();
  });
  it("detailsMode change clears per-item overrides", () => {
    setToggleState({ detailsMode: "all" });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "the chat" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText("the chat")).toBeNull();
    setToggleState({ detailsMode: "none" });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("the chat")).toBeTruthy();
    setToggleState({ detailsMode: "all" });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("the chat")).toBeTruthy();
  });
});

describe("Details mode 'none' — turn expansion keeps child blocks collapsed (#4780)", () => {
  const turnToggle = () => screen.getByTestId("turn-toggle");
  it("reveals the child rows collapsed; each opens on its own click", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[
          makeItem({
            item_id: "1.0",
            kind: "agent_reasoning",
            payload: "hidden thought",
            reasoning_ms: 8200,
            reasoning_tokens: 1234,
          }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "print('hidden code')" }),
        ]}
      />,
    );
    expect(turnToggle().getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("card-toggle")).toBeNull();
    fireEvent.click(turnToggle());
    expect(turnToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByTestId("card-toggle")).toHaveLength(2);
    expect(screen.queryByText("hidden thought")).toBeNull();
    expect(screen.queryByTestId("python-code")).toBeNull();
    fireEvent.click(screen.getAllByTestId("card-toggle")[0]);
    expect(screen.getByText("hidden thought")).toBeTruthy();
    expect(screen.queryByTestId("python-code")).toBeNull();
    fireEvent.click(screen.getAllByTestId("card-toggle")[1]);
    expect(screen.getByTestId("python-code").textContent).toContain("print('hidden code')");
  });
  it("keeps a child's manual expansion across the parent's collapse and re-expand", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "hidden thought" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "print('code')" }),
        ]}
      />,
    );
    fireEvent.click(turnToggle());
    fireEvent.click(screen.getAllByTestId("card-toggle")[0]);
    expect(screen.getByText("hidden thought")).toBeTruthy();
    fireEvent.click(turnToggle());
    expect(screen.queryByTestId("card-toggle")).toBeNull();
    fireEvent.click(turnToggle());
    expect(screen.getByText("hidden thought")).toBeTruthy();
    expect(screen.queryByTestId("python-code")).toBeNull();
  });
});

describe("Details mode re-pick made while the timeline was unmounted", () => {
  it("collapses the manual expansions on the next mount", () => {
    setToggleState({ detailsMode: "none" });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "hidden thought" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "print('code')" }),
    ];
    const props = { items, threadKey: "reset-gap", scrollMemoryKey: "reset-gap-thread" };
    const first = render(<TimelineView {...props} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
    fireEvent.click(screen.getAllByTestId("card-toggle")[0]);
    expect(screen.getByText("hidden thought")).toBeTruthy();
    first.unmount();
    useContentToggleReset.getState().bumpReset();
    expect(useContentToggleReset.getState().resetPending).toBe(true);
    render(<TimelineView {...props} />);
    expect(screen.getByTestId("turn-toggle").getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("card-toggle")).toBeNull();
    expect(useContentToggleReset.getState().resetPending).toBe(false);
  });
});

describe("streamingCode last-item only", () => {
  it("last is agent_code + streamingCode=true → last streaming=1, previous agent_code streaming=0", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_code", payload: "first" }),
          makeItem({ item_id: "2", kind: "agent_code", payload: "last" }),
        ]}
        streamingCode
      />,
    );
    const blocks = screen.getAllByTestId("python-code");
    expect(blocks).toHaveLength(2);
    expect(blocks[0].getAttribute("data-streaming")).toBe("0");
    expect(blocks[1].getAttribute("data-streaming")).toBe("1");
  });
  it("last is agent_chat (not agent_code) + streamingCode=true → previous agent_code not marked streaming", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1", kind: "agent_code", payload: "code" }),
          makeItem({ item_id: "2", kind: "agent_chat", payload: "follow-up chat" }),
        ]}
        streamingCode
      />,
    );
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
  });
});

describe("splitEnvelope header length guard", () => {
  it("\\n\\n distance > 200 chars → entire payload is body, no header split", () => {
    const longLine = "x".repeat(300);
    const payload = `${longLine}\n\nbody after long line`;
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload,
          }),
        ]}
      />,
    );
    const preEl = screen.getByText(/body after long line/);
    expect(preEl.textContent).toContain(longLine);
  });
  it("\\n\\n at char 0 (idx=0) → entire payload is body, no empty header", () => {
    const payload = "\n\nbody with leading newlines";
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    expect(screen.getByText(/body with leading newlines/)).toBeTruthy();
  });
});

describe("UnknownMarkerChip console.warn", () => {
  it("Unrecognized system_marker → console.warn emits source + payload", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[
            makeItem({
              kind: "system_marker",
              source: "future_source",
              payload: "future_payload",
            }),
          ]}
        />,
      );
      expect(warnSpy).toHaveBeenCalled();
      const lastCall = warnSpy.mock.calls.at(-1);
      if (!lastCall) throw new Error("no warn call");
      const callJson = JSON.stringify(lastCall);
      expect(callJson).toContain("future_source");
      expect(callJson).toContain("future_payload");
    } finally {
      warnSpy.mockRestore();
    }
  });
});

describe("ItemTimestamp", () => {
  it("invalid iso → no timestamp span rendered (Number.isNaN(d.getTime()) guard)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "hello",
            created_at: "this is not a date",
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/\[\d{4}-\d{2}-\d{2}/)).toBeNull();
  });
  it("valid iso → renders YYYY-MM-DD DayOfWeek HH:MM:SS with no brackets and no hover tooltip", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "hello",
            created_at: "2026-05-15T12:00:00Z",
          }),
        ]}
      />,
    );
    const stamp = screen.getByText(/^\d{4}-\d{2}-\d{2}( [A-Z][a-z]{2})? \d{2}:\d{2}:\d{2}$/);
    expect(stamp).toBeTruthy();
    expect(stamp.getAttribute("title")).toBeNull();
  });
  it("empty iso (created_at='') → no timestamp rendered", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "hello",
            created_at: "",
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/\[\d{4}-\d{2}-\d{2}/)).toBeNull();
  });
});

// meiva:note
// The item stamp and Copy share one footer OUTSIDE the card box; the band is hover-revealed, so the card body keeps its own padding and both pieces of chrome line up under the card's bottom-right corner.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
describe("card hover footer", () => {
  it("timestamp and Copy share the one band under the block, outside the card box", () => {
    const item = makeItem({
      kind: "agent_chat",
      payload: "hello",
      created_at: "2026-10-06T13:39:25+00:00",
    });
    render(<TimelineView items={[item]} />);
    const stamp = screen.getByText(formatItemTime(item.created_at!, { showWeekday: true }));
    const band = stamp.parentElement;
    expect(band).toBe(screen.getByLabelText("Copy message").parentElement);
    expect(band?.className).toContain("justify-end");
    expect(band?.closest('[data-testid="card-toggle"]')).toBeNull();
    // meiva:note
    // The band is the row's own bottom strip, not the card's: it follows the card
    // box as a sibling inside `.timeline-item`, which pads its bottom by the gap
    // between two blocks and gives that height back with a negative margin. So the
    // chrome stands in whitespace that already existed, and never overlaps the
    // body (user 2026-10-21).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
    const cardBox = band?.previousElementSibling;
    expect(cardBox?.textContent).toContain("hello");
    expect(cardBox?.contains(band ?? null)).toBe(false);
    expect(band?.className).toContain("absolute");
    expect(band?.className).toContain("bottom-0");
    expect(band?.parentElement?.className).toContain("timeline-item");
  });
  it("a collapsed block stamps no time — the stamp rides the block that is open", () => {
    const item = makeItem({
      kind: "agent_chat",
      payload: "hello",
      created_at: "2026-10-06T13:39:25+00:00",
    });
    render(<TimelineView items={[item]} />);
    const stamped = formatItemTime(item.created_at!, { showWeekday: true });
    expect(screen.getByText(stamped)).toBeTruthy();
    // meiva:note
    // A collapsed row is a one-line header; a time under it read as chrome for a
    // message that is not showing (user 2026-10-21).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText(stamped)).toBeNull();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.getByText(stamped)).toBeTruthy();
  });
  it("a streaming reply stamps the timestamp but offers no Copy", () => {
    const item = makeItem({
      kind: "agent_chat",
      payload: "streaming",
      partial: true,
      created_at: "2026-10-06T13:39:25+00:00",
    });
    render(<TimelineView items={[item]} />);
    const stamp = screen.getByText(formatItemTime(item.created_at!, { showWeekday: true }));
    expect(stamp.parentElement?.className).toContain("justify-end");
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
  it("standing notes (show_timestamp false) and undated items stamp nothing", () => {
    const standing = makeItem({
      kind: "system_marker",
      source: "agent_id",
      payload: "note",
      show_timestamp: false,
      created_at: "2026-10-06T13:39:25+00:00",
    });
    const undated = makeItem({
      kind: "system_marker",
      source: "impersonation",
      payload: "event",
      created_at: null,
    });
    const { container } = render(<TimelineView items={[standing, undated]} />);
    expect(container.textContent).not.toContain(formatItemTime(standing.created_at!, { showWeekday: true }));
    expect(container.querySelectorAll(".font-mono.tabular-nums")).toHaveLength(0);
  });
});

describe("ItemTimestamp label content", () => {
  it("ChatMarkdown label 'Meiva ▸' appears (pins label JSX against deletion)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
      />,
    );
    expect(screen.getByText(/Meiva ▸/)).toBeTruthy();
  });
  it("plain agent_chat label is exactly 'Meiva ▸' — no i18n key leak (Task #816)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
      />,
    );
    expect(screen.getByText(/^Meiva ▸$/)).toBeTruthy();
    expect(screen.queryByText(/timeline\.agentChat/)).toBeNull();
    expect(screen.queryByText(/\{suffix\}/)).toBeNull();
  });
  it("agent_chat partial=true → label contains '(streaming…)'", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x", partial: true })]}
      />,
    );
    expect(screen.getByText(/\(streaming…\)/)).toBeTruthy();
  });
  it("agent_chat interrupted=true → label contains '(streaming interrupted)'", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x", partial: true, interrupted: true })]}
      />,
    );
    const labels = screen.getAllByText(/streaming interrupted/i);
    expect(labels.length).toBeGreaterThan(0);
  });
});
