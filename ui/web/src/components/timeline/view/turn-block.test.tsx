import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";

import { PendingLiveActivityWindow, TurnBlock } from "../run-block";
import { summarizeTurn, type TurnSummary } from "../model/runs";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { useTimelineStore } from "@/lib/timeline/timeline-store";

afterEach(cleanup);

const SAMPLE_START = 1_700_000_000_000;

const sampleSummary: TurnSummary = {
  total: 3,
  thinking: 1,
  code: 1,
  output: 1,
  turns: 1,
  systemPrompts: 0,
  compactSummaries: 0,
  memories: 0,
  agentMessages: 0,
  systemNotes: 0,
  settledActiveMs: 12_000,
  priorActiveMs: 0,
  liveSegmentStartMs: SAMPLE_START,
  sdkCalls: [{ method: "files.read", count: 2 }],
};

describe("TurnBlock component", () => {
  it("renders aggregate SDK chips in namespace and count order once expanded", () => {
    const summary = {
      ...sampleSummary,
      sdkCalls: [
        { method: "agents.spawn", count: 1 },
        { method: "files.read", count: 3 },
        { method: "files.write", count: 2 },
        { method: "shell.run", count: 10 },
      ],
    };
    render(
      <TurnBlock id="turn-1" memberIds={["1.0", "1.1"]} summary={summary} expanded={false} onToggle={vi.fn()}>
        <div>detail rows</div>
      </TurnBlock>,
    );
    expect(screen.getByTestId("turn-toggle").querySelectorAll('span[class="text-foreground/80"]').length).toBe(0);

    render(
      <TurnBlock id="turn-1" memberIds={["1.0", "1.1"]} summary={summary} expanded onToggle={vi.fn()}>
        <div>detail rows</div>
      </TurnBlock>,
    );
    const toggles = screen.getAllByTestId("turn-toggle");
    const methods = Array.from(toggles[toggles.length - 1]
      .querySelectorAll('span[class="text-foreground/80"]'))
      .map((chip) => chip.textContent);
    expect(methods).toEqual(["agents.spawn", "files.read", "files.write", "shell.run"]);
  });

  it("renders collapsed turn block without sticky classes or inner scrollport", () => {
    const { container } = render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={false}
        onToggle={vi.fn()}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("false");
    expect(toggle.className).not.toContain("sticky");
    expect(toggle.className).toContain("w-[calc(100%+0.5rem)]");
    expect(container.querySelector('[data-turn-scroll="true"]')).toBeNull();
  });

  it("renders an expanded turn with a sticky header and a natural-height body", () => {
    const { container } = render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={true}
        onToggle={vi.fn()}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const root = container.firstElementChild!;
    expect(root.className).not.toContain("rounded-xl");
    expect(root.className).not.toContain("border");

    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.classList).toContain("sticky");
    expect(toggle.classList).toContain("top-11");
    expect(toggle.classList).toContain("bg-background");
    expect(toggle.className).toContain("w-[calc(100%+0.5rem)]");

    // meiva:note
    // No inner scrollport and no height cap: the body is part of the timeline's
    // own flow, so the block is exactly as tall as its content (user ruling
    // 2026-11-09).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
    expect(screen.queryByTestId("turn-scroll-viewport")).toBeNull();
    const rail = screen.getByTestId("detail-content").parentElement;
    expect(rail?.className).toContain("ml-1.5");
    expect(rail?.className).toContain("pl-2");
    expect(rail?.className).not.toContain("pl-3");
    expect(rail?.className).not.toContain("border");
  });

  it("shares the identical unboxed chrome when liveWindow is true", () => {
    const { container } = render(
      <TurnBlock
        id="turn-live"
        memberIds={["1.0"]}
        summary={sampleSummary}
        expanded={true}
        turnActive={true}
        liveWindow={true}
        onToggle={vi.fn()}
      >
        <div>live row</div>
      </TurnBlock>,
    );

    const root = container.firstElementChild!;
    expect(root.getAttribute("data-live-activity-window")).toBe("true");
    expect(root.className).not.toContain("rounded-xl");
    expect(screen.queryByTestId("activity-scroll-viewport")).toBeNull();
    expect(screen.getByText("live row")).toBeTruthy();
  });

  it("renders PendingLiveActivityWindow as an inline unboxed Working header row without a spinner", () => {
    render(<PendingLiveActivityWindow />);
    const pending = screen.getByTestId("pending-live-activity-window");
    expect(pending.getAttribute("data-live-activity-window")).toBe("true");
    expect(pending.getAttribute("role")).toBe("status");
    expect(pending.className).not.toContain("rounded-xl");
    expect(pending.textContent).toBe("Working for 1s");
    expect(pending.querySelector("svg")).toBeTruthy();
    expect(pending.querySelector(".animate-spin")).toBeNull();
    expect(pending.querySelector("button")).toBeNull();
  });

  it("skips re-rendering when a regroup rebuilds equal member ids and summary", () => {
    let renders = 0;
    const makeSummary = (over: Partial<TurnSummary> = {}): TurnSummary => {
      const sdkCalls = [{ method: "files.read", count: 2 }];
      const origMap = sdkCalls.map.bind(sdkCalls);
      sdkCalls.map = ((...args: Parameters<typeof origMap>) => {
        renders += 1;
        return origMap(...args);
      }) as typeof sdkCalls.map;
      return { ...sampleSummary, ...over, sdkCalls };
    };
    const onToggle = vi.fn();
    const block = (memberIds: string[], summary: TurnSummary) => (
      <TurnBlock id="turn-1" memberIds={memberIds} summary={summary} expanded onToggle={onToggle} />
    );
    const { rerender } = render(block(["1.0", "1.1"], makeSummary()));
    expect(renders).toBe(1);

    rerender(block(["1.0", "1.1"], makeSummary()));
    expect(renders).toBe(1);

    rerender(block(["1.0", "1.1"], makeSummary({ settledActiveMs: 13_000 })));
    expect(renders).toBe(2);
    expect(screen.getByTestId("turn-toggle").textContent).toContain("Worked for 13s");

    rerender(block(["1.0", "1.1"], makeSummary({ settledActiveMs: 13_000, priorActiveMs: 5_000 })));
    expect(renders).toBe(3);

    rerender(block(["1.0", "1.1"], makeSummary({ settledActiveMs: 13_000, priorActiveMs: 5_000, liveSegmentStartMs: SAMPLE_START + 1_000 })));
    expect(renders).toBe(4);
  });

  it("ticks the live clock only while its turn is active", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 14_000);
      const live = { ...sampleSummary, settledActiveMs: 12_000, priorActiveMs: 0, liveSegmentStartMs: SAMPLE_START };
      const { rerender } = render(
        <TurnBlock id="turn-1" memberIds={["1.0"]} summary={live} expanded={false} turnActive onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 14s");
      act(() => { vi.advanceTimersByTime(1_000); });
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 15s");
      expect(vi.getTimerCount()).toBe(1);

      rerender(
        <TurnBlock id="turn-1" memberIds={["1.0"]} summary={live} expanded={false} turnActive={false} onToggle={vi.fn()} />,
      );
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps ticking between blocks from the live segment start", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 64_000);
      const betweenBlocks: TurnSummary = {
        ...sampleSummary,
        settledActiveMs: 62_000,
        priorActiveMs: 0,
        liveSegmentStartMs: SAMPLE_START,
      };
      render(
        <TurnBlock id="turn-gap" memberIds={["1.0"]} summary={betweenBlocks} expanded={false} turnActive lastEventAt={SAMPLE_START + 62_000} onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 1m 4s");
      act(() => { vi.advanceTimersByTime(3_000); });
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 1m 7s");
      act(() => { vi.advanceTimersByTime(10_000); });
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 1m 17s");
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops the live clock on settle and reports the block's settled active duration", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 64_000);
      const betweenBlocks: TurnSummary = {
        ...sampleSummary,
        settledActiveMs: 62_000,
        priorActiveMs: 0,
        liveSegmentStartMs: SAMPLE_START,
      };
      const { rerender } = render(
        <TurnBlock id="turn-settle" memberIds={["1.0"]} summary={betweenBlocks} expanded={false} turnActive onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 1m 4s");
      rerender(
        <TurnBlock id="turn-settle" memberIds={["1.0"]} summary={betweenBlocks} expanded={false} onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Worked for 1m 2s");
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("ticks smoothly for an external harness active turn with an in-progress activity item", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 4_000);
      const harnessLive: TurnSummary = {
        ...sampleSummary,
        settledActiveMs: 3_000,
        priorActiveMs: 0,
        liveSegmentStartMs: SAMPLE_START,
      };
      render(
        <TurnBlock id="turn-harness" memberIds={["1.0"]} summary={harnessLive} expanded={false} turnActive onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 4s");
      act(() => { vi.advanceTimersByTime(2_000); });
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 6s");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps 'Working for 1s' steady without flickering or showing 0s when lastEventAt updates on an unstamped non-work turn", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(100_000);
      const noteOnly: TurnSummary = {
        ...sampleSummary,
        total: 1,
        thinking: 0,
        code: 0,
        output: 0,
        turns: 0,
        systemNotes: 1,
        settledActiveMs: null,
        priorActiveMs: 0,
        liveSegmentStartMs: null,
        sdkCalls: [],
      };
      const { rerender } = render(
        <TurnBlock id="turn-note" memberIds={["1.0"]} summary={noteOnly} expanded={false} turnActive lastEventAt={100_000} onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 1s");
      expect(screen.getByTestId("turn-toggle").textContent).not.toContain("0s");

      act(() => { vi.setSystemTime(100_040); });
      rerender(
        <TurnBlock id="turn-note" memberIds={["1.0"]} summary={noteOnly} expanded={false} turnActive lastEventAt={100_040} onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 1s");
      expect(screen.getByTestId("turn-toggle").textContent).not.toContain("0s");

      act(() => { vi.advanceTimersByTime(2_000); });
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 2s");
    } finally {
      vi.useRealTimers();
    }
  });

  it("calls onToggle with its id and current state", () => {
    const onToggle = vi.fn();
    render(
      <TurnBlock id="turn-7" memberIds={["7.0"]} summary={sampleSummary} expanded={false} onToggle={onToggle} />,
    );
    const button = screen.getByTestId("turn-toggle");
    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledWith("turn-7", false, button);
  });

  it("locks the collapse wrapper shrinkable on the inline axis (#3305)", () => {
    const { container } = render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0"]}
        summary={sampleSummary}
        expanded={true}
        onToggle={vi.fn()}
      >
        <div>detail rows</div>
      </TurnBlock>,
    );
    const grid = container.querySelector('[class*="grid-rows-"]');
    const clip = grid?.firstElementChild;
    expect(grid).not.toBeNull();
    expect(clip?.className).toContain("overflow-hidden");
    expect(clip?.className).toContain("min-w-0");
  });
});

function blockItem(
  kind: BackendTimelineItem["kind"],
  id: string,
  extra: Partial<BackendTimelineItem> = {},
): BackendTimelineItem {
  return {
    item_id: id,
    kind,
    source: null,
    payload: "",
    created_at: null,
    inbound_id: null,
    show_timestamp: true,
    ...extra,
  };
}

describe("Work block active-segment duration semantics (#7533)", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  // meiva:note
  // User ruling (2026-11-22): 10m work -> 1h idle -> new wake -> 10m work must show
  // 20m (not 1h 20m). Live Working = prior completed active segments + (now - live
  // segment start); settled Worked = sum of active segments with agent output, identical
  // across live, settle, and reload.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  it("excludes 1h inter-turn idle across peer inbound_chat in both live Working (13m ticking) and settled Worked (20m)", () => {
    vi.useFakeTimers();
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;

    const seg1 = [
      blockItem("agent_reasoning", "h10.0", {
        created_at: new Date(t0 + 4 * MIN).toISOString(),
        reasoning_ms: 4 * MIN,
      }),
      blockItem("agent_code", "h20.0", {
        created_at: new Date(t0 + 5 * MIN).toISOString(),
      }),
      blockItem("code_output", "h30.0", {
        created_at: new Date(t0 + 10 * MIN).toISOString(),
        exec_ms: 5 * MIN,
      }),
    ];

    const seg2Live = [
      ...seg1,
      blockItem("inbound_chat", "h40.0", {
        source: "agent:228",
        created_at: new Date(t0 + 70 * MIN).toISOString(),
      }),
      blockItem("agent_code", "h50.0", {
        created_at: new Date(t0 + 71 * MIN).toISOString(),
        activity_pending: true,
      }),
    ];

    vi.setSystemTime(t0 + 73 * MIN);
    const { unmount } = render(
      <TurnBlock
        id="h10.0"
        memberIds={seg2Live.map((it) => it.item_id)}
        summary={summarizeTurn(seg2Live)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
      />,
    );
    expect(screen.getByText("Working for 13m")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.getByText("Working for 13m 5s")).toBeTruthy();

    unmount();
    render(
      <TurnBlock
        id="h10.0"
        memberIds={seg2Live.map((it) => it.item_id)}
        summary={summarizeTurn(seg2Live)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
      />,
    );
    expect(screen.getByText("Working for 13m 5s")).toBeTruthy();

    const settled = [
      ...seg1,
      blockItem("inbound_chat", "h40.0", {
        source: "agent:228",
        created_at: new Date(t0 + 70 * MIN).toISOString(),
      }),
      blockItem("agent_code", "h50.0", {
        created_at: new Date(t0 + 71 * MIN).toISOString(),
      }),
      blockItem("code_output", "h60.0", {
        created_at: new Date(t0 + 80 * MIN).toISOString(),
        exec_ms: 9 * MIN,
      }),
    ];
    cleanup();
    render(
      <TurnBlock
        id="h10.0"
        memberIds={settled.map((it) => it.item_id)}
        summary={summarizeTurn(settled)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={false}
      />,
    );
    expect(screen.getByText("Worked for 20m")).toBeTruthy();
  });

  it("handles hidden agent_chat close -> 1h idle -> hidden heartbeat wake before first item arrives (lastEventAt or 0) and on settle", () => {
    vi.useFakeTimers();
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;

    const closedSeg1 = [
      blockItem("agent_reasoning", "h10.0", {
        created_at: new Date(t0 + 5 * MIN).toISOString(),
        reasoning_ms: 5 * MIN,
      }),
      blockItem("agent_code", "h20.0", {
        created_at: new Date(t0 + 6 * MIN).toISOString(),
      }),
      blockItem("code_output", "h30.0", {
        created_at: new Date(t0 + 9 * MIN).toISOString(),
        exec_ms: 3 * MIN,
      }),
      blockItem("agent_chat", "h40.0", {
        user_facing: false,
        created_at: new Date(t0 + 10 * MIN).toISOString(),
      }),
    ];

    vi.setSystemTime(t0 + 70 * MIN + 5_000);
    const { rerender } = render(
      <TurnBlock
        id="h10.0"
        memberIds={closedSeg1.map((it) => it.item_id)}
        summary={summarizeTurn(closedSeg1)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
        lastEventAt={0}
      />,
    );
    expect(screen.getByText("Working for 10m")).toBeTruthy();
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(screen.getByText("Working for 10m 2s")).toBeTruthy();

    rerender(
      <TurnBlock
        id="h10.0"
        memberIds={closedSeg1.map((it) => it.item_id)}
        summary={summarizeTurn(closedSeg1)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
        lastEventAt={t0 + 70 * MIN}
      />,
    );
    expect(screen.getByText("Working for 10m 7s")).toBeTruthy();

    const settledWithHeartbeat = [
      ...closedSeg1,
      blockItem("agent_reasoning", "h50.0", {
        created_at: new Date(t0 + 75 * MIN).toISOString(),
        reasoning_ms: 5 * MIN,
      }),
      blockItem("code_output", "h60.0", {
        created_at: new Date(t0 + 80 * MIN).toISOString(),
        exec_ms: 5 * MIN,
      }),
    ];
    rerender(
      <TurnBlock
        id="h10.0"
        memberIds={settledWithHeartbeat.map((it) => it.item_id)}
        summary={summarizeTurn(settledWithHeartbeat)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={false}
      />,
    );
    expect(screen.getByText("Worked for 20m")).toBeTruthy();
  });

  it("keeps same-step hidden agent_chat + agent_code joined with its long code_output (8m+)", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const sameStep = [
      blockItem("agent_chat", "h100.0", {
        user_facing: false,
        created_at: new Date(t0).toISOString(),
      }),
      blockItem("agent_code", "h100.1", {
        created_at: new Date(t0 + 1_000).toISOString(),
      }),
      blockItem("code_output", "h120.0", {
        created_at: new Date(t0 + 8 * MIN + 1_000).toISOString(),
        exec_ms: 8 * MIN,
      }),
    ];
    render(
      <TurnBlock
        id="h100.0"
        memberIds={sameStep.map((it) => it.item_id)}
        summary={summarizeTurn(sameStep)}
        expanded={false}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByText("Worked for 8m 1s")).toBeTruthy();
  });

  it("does not count queue waiting between consecutive inbound_chat messages without agent output", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const queued = [
      blockItem("inbound_chat", "1.0", {
        source: "agent:10",
        created_at: new Date(t0).toISOString(),
      }),
      blockItem("inbound_chat", "2.0", {
        source: "agent:11",
        created_at: new Date(t0 + 60 * MIN).toISOString(),
      }),
      blockItem("agent_code", "3.0", {
        created_at: new Date(t0 + 60 * MIN + 5_000).toISOString(),
      }),
      blockItem("code_output", "4.0", {
        created_at: new Date(t0 + 60 * MIN + 30_000).toISOString(),
        exec_ms: 25_000,
      }),
    ];
    render(
      <TurnBlock
        id="1.0"
        memberIds={queued.map((it) => it.item_id)}
        summary={summarizeTurn(queued)}
        expanded={false}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByText("Worked for 30s")).toBeTruthy();
  });

  it("holds Working for 10m (not 70m) in the same tab after settleTurn + trailing snapshot and 1h idle wake", () => {
    vi.useFakeTimers();
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const closedTurn1 = [
      blockItem("agent_reasoning", "h10.0", {
        created_at: new Date(t0 + 5 * MIN).toISOString(),
        reasoning_ms: 5 * MIN,
      }),
      blockItem("code_output", "h20.0", {
        created_at: new Date(t0 + 9 * MIN).toISOString(),
        exec_ms: 4 * MIN,
      }),
      blockItem("agent_chat", "h30.0", {
        user_facing: false,
        created_at: new Date(t0 + 10 * MIN).toISOString(),
      }),
    ];

    const store = useTimelineStore.getState();
    act(() => {
      store.switchThread(42, [], false);
      store.processSseEvent({ role: "reasoning_start", agent_id: 42, item_id: "h10.0" }, t0);
      store.processSseEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: closedTurn1 }, t0 + 10 * MIN);
      store.settleTurn(t0 + 10 * MIN);
      store.processSseEvent({ role: "timeline_snapshot", agent_id: 42, msg_count: 3, items: closedTurn1 }, t0 + 10 * MIN + 500);
    });
    expect(useTimelineStore.getState().liveAnchorAt).toBe(0);

    vi.setSystemTime(t0 + 70 * MIN);
    render(
      <TurnBlock
        id="h10.0"
        memberIds={closedTurn1.map((it) => it.item_id)}
        summary={summarizeTurn(closedTurn1)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
        lastEventAt={useTimelineStore.getState().liveAnchorAt}
      />,
    );
    expect(screen.getByText("Working for 10m")).toBeTruthy();
  });

  it("excludes idle gaps around trailing non-wake markers and lifecycle terminate/resurrect transitions", () => {
    const t0 = Date.parse("2026-11-22T10:00:00.000Z");
    const MIN = 60_000;
    const items = [
      blockItem("agent_code", "1.0", { created_at: new Date(t0).toISOString() }),
      blockItem("code_output", "2.0", { created_at: new Date(t0 + 10 * MIN).toISOString(), exec_ms: 10 * MIN }),
      blockItem("system_marker", "3.0", { source: "lifecycle_terminate", created_at: new Date(t0 + 10 * MIN).toISOString(), show_timestamp: true }),
      blockItem("system_marker", "4.0", { source: "task", created_at: new Date(t0 + 10 * MIN + 1_000).toISOString(), show_timestamp: true }),
      blockItem("system_marker", "5.0", { source: "lifecycle_resurrect", created_at: new Date(t0 + 130 * MIN).toISOString(), show_timestamp: true }),
      blockItem("agent_reasoning", "6.0", { created_at: new Date(t0 + 135 * MIN).toISOString(), reasoning_ms: 5 * MIN }),
    ];
    render(
      <TurnBlock
        id="1.0"
        memberIds={items.map((it) => it.item_id)}
        summary={summarizeTurn(items)}
        expanded={false}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByText("Worked for 15m")).toBeTruthy();
  });

  it("shows plain Worked with no warning icon when block timestamps are truly missing", () => {
    const legacy = [blockItem("agent_code", "h0.0"), blockItem("code_output", "h1.0")];
    const summary = summarizeTurn(legacy);
    expect(summary.settledActiveMs).toBeNull();
    expect(summary.liveSegmentStartMs).toBeNull();

    render(
      <TurnBlock
        id="h0.0"
        memberIds={["h0.0", "h1.0"]}
        summary={summary}
        expanded={false}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByText("Worked")).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("counts smoothly from PendingLiveActivityWindow through TurnBlock and settles to the same wall-clock span", () => {
    vi.useFakeTimers();
    const startMs = Date.parse("2026-11-21T10:00:00.000Z");
    vi.setSystemTime(startMs + 5_000);

    const { rerender } = render(<PendingLiveActivityWindow />);
    expect(screen.getByText("Working for 1s")).toBeTruthy();

    rerender(<PendingLiveActivityWindow startedAt={startMs} />);
    expect(screen.getByText("Working for 5s")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByText("Working for 7s")).toBeTruthy();

    const inFlightItems = [
      blockItem("agent_reasoning", "1.0", {
        created_at: new Date(startMs).toISOString(),
        reasoningStartedAt: startMs,
      }),
    ];
    rerender(
      <TurnBlock
        id="1.0"
        memberIds={["1.0"]}
        summary={summarizeTurn(inFlightItems)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
      />,
    );
    expect(screen.getByText("Working for 7s")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByText("Working for 9s")).toBeTruthy();

    const settledItems = [
      blockItem("agent_reasoning", "1.0", {
        created_at: new Date(startMs + 9_000).toISOString(),
        reasoning_ms: 9_000,
      }),
    ];
    rerender(
      <TurnBlock
        id="1.0"
        memberIds={["1.0"]}
        summary={summarizeTurn(settledItems)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={false}
      />,
    );
    expect(screen.getByText("Worked for 9s")).toBeTruthy();
  });
});

describe("Working header always counts seconds while the turn is active", () => {
  const MIN = 60_000;
  const t0 = Date.parse("2026-11-25T10:00:00.000Z");
  const iso = (ms: number) => new Date(ms).toISOString();

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  function renderLive(items: BackendTimelineItem[], lastEventAt = 0) {
    return render(
      <TurnBlock
        id={items[0].item_id}
        memberIds={items.map((it) => it.item_id)}
        summary={summarizeTurn(items)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive
        lastEventAt={lastEventAt}
      />,
    );
  }

  function headerText(): string {
    return screen.getByTestId("turn-toggle").textContent;
  }

  it("native: counts when an earlier one-item active segment has no measurable duration", () => {
    vi.useFakeTimers();
    const items = [
      blockItem("agent_chat", "5.0", { created_at: iso(t0) }),
      blockItem("inbound_chat", "6.0", { source: "human", created_at: iso(t0 + 60 * MIN) }),
      blockItem("agent_reasoning", "7.0", {
        created_at: iso(t0 + 60 * MIN + 1_000),
        reasoningStartedAt: t0 + 60 * MIN + 1_000,
      }),
    ];
    expect(summarizeTurn(items).liveSegmentStartMs).toBe(t0 + 60 * MIN);
    vi.setSystemTime(t0 + 60 * MIN + 5_000);
    renderLive(items);
    expect(headerText()).toBe("Working for 5s");
    act(() => { vi.advanceTimersByTime(3_000); });
    expect(headerText()).toBe("Working for 8s");
  });

  it("external harness: counts across unmeasured tool-call segments without adding the idle gap", () => {
    vi.useFakeTimers();
    const items = [
      blockItem("agent_code", "h10.0", { created_at: iso(t0) }),
      blockItem("agent_chat", "h10.1", { created_at: iso(t0) }),
      blockItem("inbound_chat", "h20.0", { source: "human", created_at: iso(t0 + 60 * MIN) }),
      blockItem("agent_code", "h30.0", { created_at: iso(t0 + 60 * MIN + 2_000), activity_pending: true }),
    ];
    expect(summarizeTurn(items).settledActiveMs).toBeNull();
    vi.setSystemTime(t0 + 60 * MIN + 7_000);
    renderLive(items);
    expect(headerText()).toBe("Working for 7s");
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(headerText()).toBe("Working for 9s");
  });

  it("counts from the moment it went live when the open segment has no timestamps and no event anchor exists", () => {
    vi.useFakeTimers();
    vi.setSystemTime(t0);
    const items = [blockItem("agent_code", "h0.0"), blockItem("code_output", "h1.0")];
    expect(summarizeTurn(items).liveSegmentStartMs).toBeNull();
    renderLive(items);
    expect(headerText()).toBe("Working for 1s");
    act(() => { vi.advanceTimersByTime(4_000); });
    expect(headerText()).toBe("Working for 4s");
  });

  it("keeps counting from the prior total when the trailing segment is already closed", () => {
    vi.useFakeTimers();
    const items = [
      blockItem("agent_reasoning", "1.0", { created_at: iso(t0 + 10 * MIN), reasoning_ms: 10 * MIN }),
      blockItem("agent_chat", "1.1", { created_at: iso(t0 + 10 * MIN) }),
    ];
    expect(summarizeTurn(items).liveSegmentStartMs).toBeNull();
    vi.setSystemTime(t0 + 90 * MIN);
    renderLive(items);
    expect(headerText()).toBe("Working for 10m");
    act(() => { vi.advanceTimersByTime(3_000); });
    expect(headerText()).toBe("Working for 10m 3s");
  });

  it("pending row counts when neither the prompt timestamp nor an event anchor is known", () => {
    vi.useFakeTimers();
    vi.setSystemTime(t0);
    render(<PendingLiveActivityWindow />);
    expect(screen.getByTestId("pending-live-activity-window").textContent).toBe("Working for 1s");
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByTestId("pending-live-activity-window").textContent).toBe("Working for 5s");
  });
});

describe("Working title Silver Glint shimmer", () => {
  afterEach(cleanup);

  it("applies timeline-title-working only to the active TurnBlock title span, never to icons, chevron, or summary", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 5_000);
      const { container, rerender } = render(
        <TurnBlock
          id="turn-shimmer"
          memberIds={["1.0"]}
          summary={sampleSummary}
          expanded={true}
          turnActive={true}
          onToggle={vi.fn()}
        >
          <div>detail</div>
        </TurnBlock>,
      );

      const toggle = screen.getByTestId("turn-toggle");
      const shimmerEl = toggle.querySelector(".timeline-title-working");
      expect(shimmerEl).not.toBeNull();
      expect(shimmerEl?.tagName).toBe("SPAN");
      expect(shimmerEl?.textContent).toContain("Working for");
      expect(toggle.querySelectorAll(".timeline-title-working")).toHaveLength(1);
      for (const svg of container.querySelectorAll("svg")) {
        expect(svg.classList.contains("timeline-title-working")).toBe(false);
      }

      rerender(
        <TurnBlock
          id="turn-shimmer"
          memberIds={["1.0"]}
          summary={sampleSummary}
          expanded={true}
          turnActive={false}
          onToggle={vi.fn()}
        >
          <div>detail</div>
        </TurnBlock>,
      );
      expect(toggle.querySelector(".timeline-title-working")).toBeNull();
      expect(toggle.textContent).toContain("Worked for");
    } finally {
      vi.useRealTimers();
    }
  });

  it("omits timeline-title-working on settled TurnBlocks (Worked for / Work activity)", () => {
    const activityOnly: TurnSummary = {
      ...sampleSummary,
      thinking: 0,
      code: 0,
      output: 0,
      settledActiveMs: null,
      priorActiveMs: 0,
      liveSegmentStartMs: null,
    };
    const { container } = render(
      <TurnBlock
        id="turn-settled"
        memberIds={["1.0"]}
        summary={activityOnly}
        expanded={false}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByTestId("turn-toggle").textContent).toContain("Context");
    expect(container.querySelector(".timeline-title-working")).toBeNull();
  });

  it("applies timeline-title-working to PendingLiveActivityWindow title span only and formats whole seconds", () => {
    vi.useFakeTimers();
    try {
      const startMs = 1_700_000_000_000;
      vi.setSystemTime(startMs + 450);

      const { rerender } = render(<PendingLiveActivityWindow />);
      const pending = screen.getByTestId("pending-live-activity-window");
      const shimmerEl = pending.querySelector(".timeline-title-working");
      expect(shimmerEl).not.toBeNull();
      expect(shimmerEl?.tagName).toBe("SPAN");
      expect(shimmerEl?.textContent).toBe("Working for 1s");
      expect(pending.querySelectorAll(".timeline-title-working")).toHaveLength(1);
      expect(pending.querySelector("svg")?.classList.contains("timeline-title-working")).toBe(false);

      rerender(<PendingLiveActivityWindow startedAt={startMs} />);
      const timedEl = pending.querySelector(".timeline-title-working");
      expect(timedEl?.textContent).toBe("Working for 1s");
      expect(timedEl?.textContent).not.toContain("0s");
      expect(timedEl?.textContent).not.toContain(".");

      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(pending.querySelector(".timeline-title-working")?.textContent).toBe("Working for 1s");

      act(() => {
        vi.advanceTimersByTime(1_500);
      });
      expect(pending.querySelector(".timeline-title-working")?.textContent).toBe("Working for 2s");
    } finally {
      vi.useRealTimers();
    }
  });

  it("displays sub-1s durations as 1s on TurnBlock headers for both active and settled states", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 850);
      const subSecondSummary: TurnSummary = {
        ...sampleSummary,
        settledActiveMs: 850,
        priorActiveMs: 0,
        liveSegmentStartMs: SAMPLE_START,
      };
      const { rerender } = render(
        <TurnBlock
          id="turn-subsec"
          memberIds={["s1"]}
          summary={subSecondSummary}
          expanded={false}
          turnActive
          onToggle={vi.fn()}
        />,
      );
      expect(screen.getByTestId("turn-toggle").querySelector(".timeline-title-working")?.textContent).toBe("Working for 1s");

      rerender(
        <TurnBlock
          id="turn-subsec"
          memberIds={["s1"]}
          summary={subSecondSummary}
          expanded={false}
          turnActive={false}
          onToggle={vi.fn()}
        />,
      );
      expect(screen.getByTestId("turn-toggle").querySelector(".timeline-title-working")).toBeNull();
      expect(screen.getByTestId("turn-toggle").textContent).toBe("Worked for 1s");
      expect(screen.getByTestId("turn-toggle").textContent).not.toContain("0s");
      expect(screen.getByTestId("turn-toggle").textContent).not.toContain("0.9s");

      rerender(
        <TurnBlock
          id="turn-subsec"
          memberIds={["s1"]}
          summary={{ ...subSecondSummary, settledActiveMs: 1_900 }}
          expanded={false}
          turnActive={false}
          onToggle={vi.fn()}
        />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Worked for 1s");
    } finally {
      vi.useRealTimers();
    }
  });

  it("pins active shimmer animation startTime to 0 in both TurnBlock and PendingLiveActivityWindow", () => {
    const prevDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, "getAnimations");
    const activeTurnAnim = { startTime: 2345 } as unknown as Animation;
    const inactiveTurnAnim = { startTime: 6789 } as unknown as Animation;
    const pendingAnim = { startTime: 4567 } as unknown as Animation;
    let currentAnim = inactiveTurnAnim;
    Element.prototype.getAnimations = () => [currentAnim];

    try {
      const { rerender } = render(
        <TurnBlock
          id="turn-sync"
          memberIds={["1.0"]}
          summary={sampleSummary}
          expanded={false}
          turnActive={false}
          onToggle={vi.fn()}
        />,
      );
      expect(inactiveTurnAnim.startTime).toBe(6789);

      currentAnim = activeTurnAnim;
      rerender(
        <TurnBlock
          id="turn-sync"
          memberIds={["1.0"]}
          summary={sampleSummary}
          expanded={false}
          turnActive={true}
          lastEventAt={1}
          onToggle={vi.fn()}
        />,
      );
      expect(activeTurnAnim.startTime).toBe(0);

      currentAnim = pendingAnim;
      render(<PendingLiveActivityWindow />);
      expect(pendingAnim.startTime).toBe(0);
    } finally {
      if (prevDescriptor) {
        Object.defineProperty(Element.prototype, "getAnimations", prevDescriptor);
      } else {
        delete (Element.prototype as { getAnimations?: unknown }).getAnimations;
      }
    }
  });
});
