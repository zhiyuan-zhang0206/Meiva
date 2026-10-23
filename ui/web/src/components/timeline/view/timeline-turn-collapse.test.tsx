import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, setToggleState } from "./timeline-test-harness";

vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";

afterEach(cleanupTimelineTest);
describe("turn-collapse (always on — Turns toggle controls expand/collapse)", () => {

  const runToggle = () => screen.getByTestId("turn-toggle");

  it("drops a pinned turn expansion when the timeline thread changes", () => {
    setToggleState({ detailsMode: "none" });
    const threadAItems = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread A thinking" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread A code" }),
    ];
    const { rerender } = render(
      <TimelineView threadKey="1" items={threadAItems} />,
    );

    expect(runToggle().getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(runToggle());
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByTestId("card-toggle")).toHaveLength(2);
    fireEvent.click(screen.getAllByTestId("card-toggle")[1]);
    expect(screen.getByTestId("python-code").textContent).toBe("thread A code");

    rerender(
      <TimelineView
        threadKey="1"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread A update" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread A updated code" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("thread A updated code");

    rerender(
      <TimelineView
        threadKey="2"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread B thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread B code" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();
  });

  it("drops a pinned inner-card collapse when the timeline thread changes", () => {
    setToggleState({ detailsMode: "all" });
    const { rerender } = render(
      <TimelineView
        threadKey="1"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread A thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread A code" }),
        ]}
      />,
    );

    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("thread A code");
    fireEvent.click(screen.getAllByTestId("card-toggle")[1]);
    expect(screen.queryByTestId("python-code")).toBeNull();

    rerender(
      <TimelineView
        threadKey="2"
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thread B thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "thread B code" }),
        ]}
      />,
    );
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("thread B code");
  });

  it("folds a run of ≥2 secondary items into an expanded turn block (detailsMode='all')", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "0.0", kind: "agent_chat", payload: "hi" }),
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thinking" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
          makeItem({ item_id: "1.2", kind: "code_output", payload: "out" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "done" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.textContent).toContain("1 turn");


    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(runHeader.getAttribute("title")).toBeNull();
    expect(screen.getAllByTestId("chat-markdown").map((m) => m.textContent)).toEqual([
      "hi",
      "thinking",
      "done",
    ]);
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  it("clicking the run header toggles inner rows (expanded in 'all' mode, click collapses)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
    fireEvent.click(runToggle());
    expect(screen.queryByTestId("python-code")).toBeNull();
    fireEvent.click(runToggle());
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  it("a lone secondary item is wrapped in a work block (always collapsible)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "0.0", kind: "agent_chat", payload: "a" }),
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "agent:7", payload: "ping" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "b" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.textContent).toContain("1 agent message");
    expect(screen.getByText("Agent 7")).toBeTruthy();
  });

  it("streaming items join the same work block — first chunk lands in a wrapper immediately", () => {
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "t1-done" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "c1-live" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("t1-done")).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("c1-live");
  });

  it("streaming items are always groupable — no peeling, all in one work block", () => {
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "t1" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "c1" }),
          makeItem({ item_id: "1.2", kind: "agent_reasoning", payload: "t2-live" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("t1")).toBeTruthy();
    expect(screen.getByText("t2-live")).toBeTruthy();
  });

  it("detailsMode='none' → runs start collapsed (toggle dim, inner rows not mounted)", () => {
    setToggleState({ detailsMode: 'none' });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();
  });

  it("interrupted items stay in the work block — not peeled out", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "t1" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "c1" }),
          makeItem({
            item_id: "1.2",
            kind: "agent_reasoning",
            payload: "t2-cut",
            partial: true,
            interrupted: true,
          }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/t2-cut/)).toBeTruthy();
    expect(screen.getByText(/reconnects resume receiving/)).toBeTruthy();
  });

  it("a turn block carries a stable data-item-id (its first member id)", () => {
    const { container } = render(
      <TimelineView
        items={[
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "r" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    const wrapper = container.querySelector('[data-item-id="3.0"]');
    expect(wrapper).toBeTruthy();
    expect(wrapper?.textContent).toContain("1 turn");
    expect(wrapper?.querySelector('[data-expanded="true"]')).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  it("a turn block also stamps data-turn-member-ids for EVERY member, surviving a run-extending load-older prepend", () => {
    const { container, rerender } = render(
      <TimelineView
        items={[
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "r" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    expect(container.querySelector('[data-item-id="3.0"]')).toBeTruthy();

    rerender(
      <TimelineView
        items={[
          makeItem({ item_id: "2.0", kind: "agent_reasoning", payload: "older" }),
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "r" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    expect(container.querySelector('[data-item-id="2.0"]')).toBeTruthy();
    const run = container.querySelector('[data-turn-member-ids~="3.0"]');
    expect(run).toBeTruthy();
    expect(run?.getAttribute("data-turn-member-ids")).toBe("2.0 3.0 3.1");
  });

  it('detailsMode="last" — clicking the last turn header toggles expansion', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");

    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();

    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");
  });

  it('detailsMode="last" — clicking the last turn header after streaming completes expands it', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("python-code")).toBeNull();

    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("python-code").textContent).toBe("x=1");

    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");
  });

  it('detailsMode="last" — collapse pinned mid-stream survives the turn going idle, then a click expands', () => {
    setToggleState({ detailsMode: "last" });
    const items = [
      makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think" }),
      makeItem({ item_id: "1.1", kind: "agent_code", payload: "x=1" }),
    ];
    const { rerender } = render(<TimelineView turnActive items={items} />);
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(runToggle());
    expect(runHeader.getAttribute("aria-expanded")).toBe("false");

    rerender(<TimelineView items={items} />);
    expect(runToggle().getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(runToggle());
    expect(runToggle().getAttribute("aria-expanded")).toBe("true");
  });

  it('detailsMode="last" — within expanded turn, all sub-items are visible (not just last)', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "think-first" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "code-mid" }),
          makeItem({ item_id: "1.2", kind: "agent_reasoning", payload: "think-last" }),
        ]}
      />,
    );
    const runHeader = runToggle();
    expect(runHeader.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("think-first")).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("code-mid");
    expect(screen.getByText("think-last")).toBeTruthy();
  });

  it('detailsMode="last" — clicking a middle turn opens it with ALL inner blocks expanded', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "older-think" }),
          makeItem({ item_id: "1.1", kind: "agent_code", payload: "older-code" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "chat" }),
          makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "newer-think" }),
          makeItem({ item_id: "3.1", kind: "agent_code", payload: "newer-code" }),
        ]}
      />,
    );
    const runHeaders = screen.getAllByTestId("turn-toggle");
    expect(runHeaders).toHaveLength(2);

    expect(runHeaders[0].getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("older-think")).toBeNull();
    expect(runHeaders[1].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("newer-think")).toBeTruthy();
    expect(screen.getByTestId("python-code").textContent).toBe("newer-code");

    fireEvent.click(runHeaders[0]);
    expect(runHeaders[0].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("older-think")).toBeTruthy();
    expect(screen.getAllByTestId("python-code")).toHaveLength(2);
    expect(runHeaders[1].getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(screen.getAllByTestId("card-toggle")[0]);
    expect(screen.queryByText("older-think")).toBeNull();
    expect(screen.getAllByTestId("python-code")).toHaveLength(2);
  });
});
