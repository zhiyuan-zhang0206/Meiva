import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";

import { PendingLiveActivityWindow, TurnBlock } from "../run-block";
import { summarizeTurn, type TurnSummary } from "../model/runs";
import type { BackendTimelineItem } from "@/lib/contracts/types";

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
  startedAtMs: SAMPLE_START,
  endedAtMs: SAMPLE_START + 12_000,
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

    rerender(block(["1.0", "1.1"], makeSummary({ endedAtMs: SAMPLE_START + 13_000 })));
    expect(renders).toBe(2);
    expect(screen.getByTestId("turn-toggle").textContent).toContain("Worked for 13s");
  });

  it("ticks the live clock only while its turn is active", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 14_000);
      const live = { ...sampleSummary, startedAtMs: SAMPLE_START, endedAtMs: SAMPLE_START + 12_000 };
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

  it("keeps ticking between blocks from the work block's first item start", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 64_000);
      const betweenBlocks: TurnSummary = {
        ...sampleSummary,
        startedAtMs: SAMPLE_START,
        endedAtMs: SAMPLE_START + 62_000,
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

  it("stops the live clock on settle and reports the block's wall-clock span", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(SAMPLE_START + 64_000);
      const betweenBlocks: TurnSummary = {
        ...sampleSummary,
        startedAtMs: SAMPLE_START,
        endedAtMs: SAMPLE_START + 62_000,
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
        startedAtMs: SAMPLE_START,
        endedAtMs: SAMPLE_START + 3_000,
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
        startedAtMs: null,
        endedAtMs: null,
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

describe("Work block wall-clock span semantics (#7509)", () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  // meiva:note
  // User's verbatim example (2026-11-21): several turns ended without display to user,
  // then a new working turn begins in the same work block. Working = now - first item's
  // start (including intervening wall-clock time), and Worked = last item's end - first
  // item's start — identical live, on settle, across reload and across bridge restarts.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  it("counts Working from the first item's start across earlier hidden turns and settles to last end minus first start", () => {
    vi.useFakeTimers();
    const t0 = Date.parse("2026-11-21T10:00:00.000Z");
    vi.setSystemTime(t0 + 135_000);

    const priorHiddenTurns = [
      blockItem("agent_reasoning", "h10.0", {
        created_at: new Date(t0 + 5_000).toISOString(),
        reasoning_ms: 5_000,
      }),
      blockItem("agent_code", "h20.0", {
        created_at: new Date(t0 + 6_000).toISOString(),
      }),
      blockItem("code_output", "h30.0", {
        created_at: new Date(t0 + 20_000).toISOString(),
        exec_ms: 14_000,
      }),
      blockItem("agent_chat", "h40.0", {
        user_facing: false,
        created_at: new Date(t0 + 25_000).toISOString(),
      }),
      blockItem("inbound_chat", "h50.0", {
        source: "agent:228",
        created_at: new Date(t0 + 120_000).toISOString(),
      }),
      blockItem("agent_code", "h60.0", {
        created_at: new Date(t0 + 125_000).toISOString(),
      }),
      blockItem("code_output", "h70.0", {
        created_at: new Date(t0 + 135_000).toISOString(),
        exec_ms: 10_000,
      }),
    ];

    const { rerender } = render(
      <TurnBlock
        id="h10.0"
        memberIds={priorHiddenTurns.map((it) => it.item_id)}
        summary={summarizeTurn(priorHiddenTurns)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={false}
      />,
    );
    expect(screen.getByText("Worked for 2m 15s")).toBeTruthy();

    vi.setSystemTime(t0 + 310_000);
    rerender(
      <TurnBlock
        id="h10.0"
        memberIds={priorHiddenTurns.map((it) => it.item_id)}
        summary={summarizeTurn(priorHiddenTurns)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
      />,
    );
    expect(screen.getByText("Working for 5m 10s")).toBeTruthy();

    const withTurn3Live = [
      ...priorHiddenTurns,
      blockItem("agent_code", "h80.0", {
        created_at: new Date(t0 + 312_000).toISOString(),
        activity_pending: true,
      }),
    ];
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    rerender(
      <TurnBlock
        id="h10.0"
        memberIds={withTurn3Live.map((it) => it.item_id)}
        summary={summarizeTurn(withTurn3Live)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={true}
      />,
    );
    expect(screen.getByText("Working for 5m 15s")).toBeTruthy();

    const settled = [
      ...priorHiddenTurns,
      blockItem("agent_code", "h80.0", {
        created_at: new Date(t0 + 312_000).toISOString(),
      }),
      blockItem("code_output", "h90.0", {
        created_at: new Date(t0 + 330_000).toISOString(),
        exec_ms: 18_000,
      }),
    ];
    rerender(
      <TurnBlock
        id="h10.0"
        memberIds={settled.map((it) => it.item_id)}
        summary={summarizeTurn(settled)}
        expanded={false}
        onToggle={vi.fn()}
        turnActive={false}
      />,
    );
    expect(screen.getByText("Worked for 5m 30s")).toBeTruthy();
  });

  it("shows plain Worked with no warning icon when block timestamps are truly missing", () => {
    const legacy = [blockItem("agent_code", "h0.0"), blockItem("code_output", "h1.0")];
    const summary = summarizeTurn(legacy);
    expect(summary.startedAtMs).toBeNull();
    expect(summary.endedAtMs).toBeNull();

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
    expect(screen.getByText("Working…")).toBeTruthy();

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
      startedAtMs: null,
      endedAtMs: null,
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
      expect(shimmerEl?.textContent).toBe("Working…");
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
        startedAtMs: SAMPLE_START,
        endedAtMs: SAMPLE_START + 850,
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
          summary={{ ...subSecondSummary, endedAtMs: SAMPLE_START + 1_900 }}
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
