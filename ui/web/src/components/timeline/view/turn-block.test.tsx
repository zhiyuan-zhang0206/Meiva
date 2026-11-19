import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";

import { PendingLiveActivityWindow, TurnBlock } from "../run-block";
import { summarizeTurn, type TurnSummary } from "../model/runs";
import type { BackendTimelineItem } from "@/lib/contracts/types";

afterEach(cleanup);

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
  missingTiming: false,
  workedMs: 12000,
  thinkingMs: 5000,
  codeMs: 4000,
  execMs: 3000,
  sdkCalls: [{ method: "files.read", count: 2 }],
  lastLiveKind: null,
  lastLiveStartedAt: 0,
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
    expect(toggle.className).toContain("sticky");
    expect(toggle.className).toContain("top-11");
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
    expect(pending.textContent).toBe("Working…");
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

    rerender(block(["1.0", "1.1"], makeSummary({ workedMs: 13000 })));
    expect(renders).toBe(2);
    expect(screen.getByTestId("turn-toggle").textContent).toContain("Worked for 13s");
  });

  it("ticks the live clock only while its turn is active", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(100_000);
      const live = { ...sampleSummary, lastLiveKind: "code" as const, lastLiveStartedAt: 98_000 };
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

  // meiva:note
  // The regression this pins (user report 2026-11-08): between two blocks of a turn — a tool
  // result committed, the model thinking before its next token, a harness step transition — no
  // block is in flight, so the old live delta keyed only on `lastLiveStartedAt` was zero and the
  // header froze at its last number while the turn kept running. The clock now falls back to the
  // turn's last observed activity, so it advances through the gap.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  it("keeps ticking between blocks, when no block is in flight", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(100_000);
      const betweenBlocks: TurnSummary = {
        ...sampleSummary,
        workedMs: 62_000,
        lastLiveKind: null,
        lastLiveStartedAt: 0,

      };
      render(
        <TurnBlock id="turn-gap" memberIds={["1.0"]} summary={betweenBlocks} expanded={false} turnActive lastEventAt={98_000} onToggle={vi.fn()} />,
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

  it("stops the gap clock on settle and reports the measured work", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(100_000);
      const betweenBlocks: TurnSummary = {
        ...sampleSummary,
        workedMs: 62_000,
        lastLiveKind: null,
        lastLiveStartedAt: 0,

      };
      const { rerender } = render(
        <TurnBlock id="turn-settle" memberIds={["1.0"]} summary={betweenBlocks} expanded={false} turnActive lastEventAt={98_000} onToggle={vi.fn()} />,
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
      vi.setSystemTime(100_000);
      const harnessLive: TurnSummary = {
        ...sampleSummary,
        workedMs: 3_000,
        lastLiveKind: "output",
        lastLiveStartedAt: 99_000,
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

  it("never flickers between 'Working…' and 'Working for' when lastEventAt updates on a non-work turn", () => {
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
        workedMs: 0,
        thinkingMs: 0,
        codeMs: 0,
        execMs: 0,
        sdkCalls: [],
        lastLiveKind: null,
        lastLiveStartedAt: 0,
      };
      const { rerender } = render(
        <TurnBlock id="turn-note" memberIds={["1.0"]} summary={noteOnly} expanded={false} turnActive lastEventAt={100_000} onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 0.1s");
      expect(screen.getByTestId("turn-toggle").textContent).not.toContain("Working…");

      act(() => { vi.setSystemTime(100_040); });
      rerender(
        <TurnBlock id="turn-note" memberIds={["1.0"]} summary={noteOnly} expanded={false} turnActive lastEventAt={100_040} onToggle={vi.fn()} />,
      );
      expect(screen.getByTestId("turn-toggle").textContent).toContain("Working for 0.1s");
      expect(screen.getByTestId("turn-toggle").textContent).not.toContain("Working…");
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

function planBItem(
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
    show_timestamp: false,
    ...extra,
  };
}

describe("Plan B Turn-level timing semantics", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("sums closed Turn durations once per turn_id and ignores missing child step timings", () => {
    const items = [
      planBItem("agent_reasoning", "1.0", { turn_id: "t1", turn_duration_ms: 4200 }),
      planBItem("agent_code", "1.1", { turn_id: "t1", turn_duration_ms: 4200, code_elapsed_ms: null }),
      planBItem("code_output", "2.0", { turn_id: "t1", turn_duration_ms: 4200, exec_ms: null }),
      planBItem("agent_code", "3.0", { turn_id: "t2", turn_duration_ms: 8100 }),
      planBItem("code_output", "4.0", { turn_id: "t2", turn_duration_ms: 8100 }),
    ];
    const summary = summarizeTurn(items);
    expect(summary.closedTurnsWorkedMs).toBe(12_300);
    expect(summary.workedMs).toBe(12_300);
    expect(summary.closedTurnIds).toEqual(["t1", "t2"]);
    expect(summary.missingTiming).toBe(false);
  });

  it("retains missingTiming=true on legacy items lacking both turn_duration_ms and item duration", () => {
    const legacy = [planBItem("agent_code", "h0.0"), planBItem("code_output", "h1.0")];
    const summary = summarizeTurn(legacy);
    expect(summary.workedMs).toBe(0);
    expect(summary.missingTiming).toBe(true);

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
  });

  it("counts smoothly from PendingLiveActivityWindow through TurnBlock without jumping or regressing", () => {
    vi.useFakeTimers();
    const startMs = 1_700_000_000_000;
    vi.setSystemTime(startMs + 5_000);

    const { rerender } = render(<PendingLiveActivityWindow />);
    expect(screen.getByText("Working…")).toBeTruthy();

    rerender(<PendingLiveActivityWindow startedAt={startMs} />);
    expect(screen.getByText("Working for 5s")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByText("Working for 7s")).toBeTruthy();

    const inFlightItems = [
      planBItem("agent_code", "h100.0", { turn_id: "t1", turn_duration_ms: null }),
      planBItem("code_output", "h200.0", { turn_id: "t1", turn_duration_ms: null }),
    ];
    rerender(
      <TurnBlock
        id="h100.0"
        memberIds={["h100.0", "h200.0"]}
        summary={summarizeTurn(inFlightItems)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
        activeTurnId="t1"
        activeTurnStartedAt={startMs}
      />,
    );
    expect(screen.getByText("Working for 7s")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByText("Working for 9s")).toBeTruthy();

    const settledItems = inFlightItems.map((it) => ({ ...it, turn_duration_ms: 9_000 }));
    rerender(
      <TurnBlock
        id="h100.0"
        memberIds={["h100.0", "h200.0"]}
        summary={summarizeTurn(settledItems)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={false}
      />,
    );
    expect(screen.getByText("Worked for 9s")).toBeTruthy();
  });

  it("accumulates prior closed Turn + active Turn in one Work Block while excluding sleep gaps", () => {
    vi.useFakeTimers();
    const t2StartMs = 1_700_001_800_000;
    vi.setSystemTime(t2StartMs + 4_000);

    const items = [
      planBItem("agent_code", "h10.0", { turn_id: "t1", turn_duration_ms: 5_000 }),
      planBItem("code_output", "h20.0", { turn_id: "t1", turn_duration_ms: 5_000 }),
      planBItem("agent_code", "h30.0", { turn_id: "t2", turn_duration_ms: null }),
    ];
    const { rerender } = render(
      <TurnBlock
        id="h10.0"
        memberIds={["h10.0", "h20.0", "h30.0"]}
        summary={summarizeTurn(items)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
        activeTurnId="t2"
        activeTurnStartedAt={t2StartMs}
      />,
    );
    expect(screen.getByText("Working for 9s")).toBeTruthy();

    const settled = [
      planBItem("agent_code", "h10.0", { turn_id: "t1", turn_duration_ms: 5_000 }),
      planBItem("code_output", "h20.0", { turn_id: "t1", turn_duration_ms: 5_000 }),
      planBItem("agent_code", "h30.0", { turn_id: "t2", turn_duration_ms: 4_000 }),
      planBItem("code_output", "h40.0", { turn_id: "t2", turn_duration_ms: 4_000 }),
    ];
    rerender(
      <TurnBlock
        id="h10.0"
        memberIds={["h10.0", "h20.0", "h30.0", "h40.0"]}
        summary={summarizeTurn(settled)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={false}
      />,
    );
    expect(screen.getByText("Worked for 9s")).toBeTruthy();
  });
});

describe("Working title Silver Glint shimmer", () => {
  afterEach(cleanup);

  it("applies timeline-title-working only to the active TurnBlock title span, never to icons, chevron, or summary", () => {
    const { container, rerender } = render(
      <TurnBlock
        id="turn-shimmer"
        memberIds={["1.0"]}
        summary={sampleSummary}
        expanded={true}
        turnActive={true}
        lastEventAt={1}
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
  });

  it("omits timeline-title-working on settled TurnBlocks (Worked for / Work activity)", () => {
    const activityOnly: TurnSummary = {
      ...sampleSummary,
      thinking: 0,
      code: 0,
      output: 0,
      workedMs: 0,
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

  it("applies timeline-title-working to PendingLiveActivityWindow title span only", () => {
    render(<PendingLiveActivityWindow />);
    const pending = screen.getByTestId("pending-live-activity-window");
    const shimmerEl = pending.querySelector(".timeline-title-working");
    expect(shimmerEl).not.toBeNull();
    expect(shimmerEl?.tagName).toBe("SPAN");
    expect(shimmerEl?.textContent).toBe("Working…");
    expect(pending.querySelectorAll(".timeline-title-working")).toHaveLength(1);
    expect(pending.querySelector("svg")?.classList.contains("timeline-title-working")).toBe(false);
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
