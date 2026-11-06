import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards, setUserSettings } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { formatAbsolute } from "@/lib/format/time";
afterEach(cleanupTimelineTest);

describe("Settled vs live expansion behavior", () => {
  it("settled detail blocks hide the summary until expanded while primary content stays visible", () => {
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
    expect(screen.queryByText("1 turn")).toBeNull();
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByText("1 turn")).toBeTruthy();
  });

  it("primary messages stay visible in flow without a card-toggle header", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "user prompt" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "the visible chat" }),
        ]}
      />,
    );
    expect(screen.getByText("user prompt")).toBeTruthy();
    expect(screen.getByText("the visible chat")).toBeTruthy();
    expect(screen.queryByTestId("card-toggle")).toBeNull();
  });

  it("live trailing turn expands secondary items by default and clicking card header collapses content", () => {
    setUserSettings({ "display.expand_working_block": true });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "the open thought" }),
        ]}
      />,
    );
    expect(screen.getByText("the open thought")).toBeTruthy();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByText("the open thought")).toBeNull();
  });
});

describe("Settled turn expansion keeps non-chat child blocks collapsed (#4780)", () => {
  const turnToggle = () => screen.getByTestId("turn-toggle");
  it("reveals the child rows collapsed; each opens on its own click", () => {
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

describe("streamingCode last-item only", () => {
  it("last is agent_code + streamingCode=true → last streaming=1, previous agent_code streaming=0", () => {
    setUserSettings({ "display.expand_working_block": true });
    render(
      <TimelineView
        turnActive
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
    revealCollapsedCards();
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
    revealCollapsedCards();
    const preEl = screen.getByText(/body after long line/);
    expect(preEl.textContent).toContain(longLine);
  });
  it("\\n\\n at char 0 (idx=0) → entire payload is body, no empty header", () => {
    const payload = "\n\nbody with leading newlines";
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    revealCollapsedCards();
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

describe("timeline message chrome", () => {
  it("renders primary messages without a timestamp or Copy footer", () => {
    const human = makeItem({
      item_id: "1.0",
      kind: "inbound_chat",
      source: "user",
      payload: "hi",
      created_at: "2026-10-06T13:39:24+00:00",
    });
    const agent = makeItem({
      item_id: "2.0",
      kind: "agent_chat",
      payload: "hello",
      created_at: "2026-10-06T13:39:25+00:00",
    });
    render(<TimelineView items={[human, agent]} />);
    expect(screen.getByTestId("user-message-bubble").textContent).toContain("hi");
    expect(screen.getByTestId("agent-message-prose").textContent).toContain("hello");
    expect(screen.queryByTestId("card-chrome")).toBeNull();
    for (const message of [human, agent]) {
      expect(screen.queryByText(formatAbsolute(message.created_at!, { weekday: true }))).toBeNull();
    }
    expect(screen.queryByRole("button", { name: "Copy message" })).toBeNull();
  });
  it("secondary work items inside TurnBlock stamp no time even when expanded", () => {
    const item = makeItem({
      kind: "agent_reasoning",
      payload: "deep thought",
      created_at: "2026-10-06T13:39:25+00:00",
    });
    render(<TimelineView items={[item]} />);
    revealCollapsedCards();
    const stamped = formatAbsolute(item.created_at!, { weekday: true });
    expect(screen.getByText("deep thought")).toBeTruthy();
    expect(screen.queryByText(stamped)).toBeNull();
  });
  it("a streaming reply carries no timestamp or Copy footer", () => {
    const item = makeItem({
      kind: "agent_chat",
      payload: "streaming",
      partial: true,
      created_at: "2026-10-06T13:39:25+00:00",
    });
    render(<TimelineView items={[item]} />);
    expect(screen.queryByTestId("card-chrome")).toBeNull();
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
    expect(container.textContent).not.toContain(formatAbsolute(standing.created_at!, { weekday: true }));
    expect(container.querySelectorAll(".font-mono.tabular-nums")).toHaveLength(0);
  });
});

describe("agent_chat canvas prose and streaming indicators", () => {
  it("plain agent_chat renders directly on canvas with no 'Meiva ▸' header and no i18n key leak (Task #816)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x" })]}
      />,
    );
    expect(screen.getByTestId("agent-message-prose")).toBeTruthy();
    expect(screen.queryByText(/Meiva ▸/)).toBeNull();
    expect(screen.queryByText(/timeline\.agentChat/)).toBeNull();
    expect(screen.queryByText(/\{suffix\}/)).toBeNull();
  });
  it("agent_chat partial=true → renders pulsing streaming cursor without '(streaming…)' label", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x", partial: true })]}
      />,
    );
    expect(screen.getByTestId("streaming-cursor")).toBeTruthy();
    expect(screen.queryByText(/\(streaming…\)/)).toBeNull();
  });
  it("agent_chat interrupted=true → drops streaming cursor without InterruptedNotice", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "x", partial: true, interrupted: true })]}
      />,
    );
    expect(screen.queryByTestId("streaming-cursor")).toBeNull();
    expect(screen.queryByText(/streaming interrupted/i)).toBeNull();
  });
});
