// The shared live clock: every live timeline clock rides one interval, the
// interval exists only while a clock is live, and the busy gate stops clocks
// whose live stamp outlived the turn.

import { act, screen } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import {
  isLiveCode,
  isLiveExecution,
  isLiveReasoning,
  itemLiveStartedAt,
  LiveClockGate,
  liveClockSubscriberCount,
  useNow,
} from "./reasoning-clock";

function Clock({ id, active }: { id: string; active: boolean }) {
  const now = useNow(active);
  return <span data-testid={id}>{now}</span>;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useNow shared ticker", () => {
  it("drives every live clock from one interval and the same timestamp", () => {
    render(
      <>
        <Clock id="a" active />
        <Clock id="b" active />
        <Clock id="c" active={false} />
      </>,
    );
    expect(liveClockSubscriberCount()).toBe(2);
    expect(vi.getTimerCount()).toBe(1);

    act(() => { vi.advanceTimersByTime(300); });
    expect(screen.getByTestId("a").textContent).toBe("1000300");
    expect(screen.getByTestId("b").textContent).toBe("1000300");
    // An inactive clock never subscribes and keeps its mount value.
    expect(screen.getByTestId("c").textContent).toBe("1000000");
  });

  it("removes the interval once the last live clock stops", () => {
    const { rerender, unmount } = render(<Clock id="a" active />);
    expect(vi.getTimerCount()).toBe(1);
    rerender(<Clock id="a" active={false} />);
    expect(liveClockSubscriberCount()).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    rerender(<Clock id="a" active />);
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not tick a live clock while the agent is not busy", () => {
    const { rerender } = render(
      <LiveClockGate value={false}>
        <Clock id="a" active />
      </LiveClockGate>,
    );
    expect(vi.getTimerCount()).toBe(0);
    act(() => { vi.advanceTimersByTime(500); });
    expect(screen.getByTestId("a").textContent).toBe("1000000");

    rerender(
      <LiveClockGate value>
        <Clock id="a" active />
      </LiveClockGate>,
    );
    act(() => { vi.advanceTimersByTime(200); });
    expect(screen.getByTestId("a").textContent).toBe("1000700");
  });
});

describe("isLiveCode", () => {
  const streaming: BackendTimelineItem = {
    item_id: "4.1",
    kind: "agent_code",
    source: null,
    payload: "x = 1",
    created_at: null,
    inbound_id: null,
    show_timestamp: true,
    codeStartedAt: 999_000,
  };

  it("is live while the code block streams", () => {
    expect(isLiveCode(streaming)).toBe(true);
  });

  it("stops once the committed duration lands, even if the stamp was never cleared", () => {
    // A missed exec_start / turn-end leaves codeStartedAt on the item.
    expect(isLiveCode({ ...streaming, code_elapsed_ms: 1_200 })).toBe(false);
  });
});

describe("itemLiveStartedAt & harness items", () => {
  const harnessTime = "2026-10-25T01:02:03.000Z";
  const harnessEpoch = Date.parse(harnessTime);

  it("prioritizes frontend *StartedAt stamps", () => {
    const item: BackendTimelineItem = {
      item_id: "1.0",
      kind: "agent_reasoning",
      source: null,
      payload: "thinking",
      created_at: harnessTime,
      inbound_id: null,
      show_timestamp: true,
      reasoningStartedAt: 500_000,
      activity_revision: 1,
    };
    expect(itemLiveStartedAt(item)).toBe(500_000);
  });

  it("falls back to parsed created_at for external harness activity items", () => {
    const reasoning: BackendTimelineItem = {
      item_id: "1.0",
      kind: "agent_reasoning",
      source: null,
      payload: "thinking",
      created_at: harnessTime,
      inbound_id: null,
      show_timestamp: true,
      activity_revision: 1,
    };
    expect(itemLiveStartedAt(reasoning)).toBe(harnessEpoch);
    expect(isLiveReasoning(reasoning)).toBe(true);

    const exec: BackendTimelineItem = {
      item_id: "2.0",
      kind: "code_output",
      source: null,
      payload: "running",
      created_at: harnessTime,
      inbound_id: null,
      show_timestamp: true,
      activity_revision: 1,
    };
    expect(itemLiveStartedAt(exec)).toBe(harnessEpoch);
    expect(isLiveExecution(exec)).toBe(true);

    const code: BackendTimelineItem = {
      item_id: "3.0",
      kind: "agent_code",
      source: null,
      payload: "print(1)",
      created_at: harnessTime,
      inbound_id: null,
      show_timestamp: true,
      activity_pending: true,
    };
    expect(itemLiveStartedAt(code)).toBe(harnessEpoch);
    expect(isLiveCode(code)).toBe(true);
  });

  it("stops when duration is committed or interrupted", () => {
    const reasoning: BackendTimelineItem = {
      item_id: "1.0",
      kind: "agent_reasoning",
      source: null,
      payload: "thought",
      created_at: harnessTime,
      inbound_id: null,
      show_timestamp: true,
      activity_revision: 2,
      reasoning_ms: 3_000,
    };
    expect(isLiveReasoning(reasoning)).toBe(false);

    const interrupted: BackendTimelineItem = {
      ...reasoning,
      reasoning_ms: null,
      interrupted: true,
    };
    expect(isLiveReasoning(interrupted)).toBe(false);

    const exec: BackendTimelineItem = {
      item_id: "2.0",
      kind: "code_output",
      source: null,
      payload: "done",
      created_at: harnessTime,
      inbound_id: null,
      show_timestamp: true,
      activity_revision: 2,
      exec_ms: 1_500,
    };
    expect(isLiveExecution(exec)).toBe(false);
  });

  it("returns undefined for non-work items", () => {
    const chat: BackendTimelineItem = {
      item_id: "1.0",
      kind: "agent_chat",
      source: null,
      payload: "hello",
      created_at: harnessTime,
      inbound_id: null,
      show_timestamp: true,
      activity_revision: 1,
    };
    expect(itemLiveStartedAt(chat)).toBeUndefined();
  });
});
