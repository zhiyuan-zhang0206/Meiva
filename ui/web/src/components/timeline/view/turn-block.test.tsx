import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";

import { PendingLiveActivityWindow, TurnBlock } from "../run-block";
import type { TurnSummary } from "../model/runs";

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
    expect(container.querySelector('[data-turn-scroll="true"]')).toBeNull();
  });

  it("renders expanded turn block with unified unboxed header and max-height bounded scrollport", () => {
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
    expect(toggle.className).not.toContain("sticky");

    const viewport = screen.getByTestId("turn-scroll-viewport");
    expect(viewport.className).toContain("max-h-[var(--timeline-turn-max-h,50dvh)]");
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
    expect(screen.getByTestId("activity-scroll-viewport").className).toContain(
      "max-h-[var(--timeline-turn-max-h,50dvh)]",
    );
  });

  it("renders PendingLiveActivityWindow as an inline unboxed header row", () => {
    render(<PendingLiveActivityWindow />);
    const pending = screen.getByTestId("pending-live-activity-window");
    expect(pending.className).not.toContain("rounded-xl");
    expect(pending.textContent).toContain("Working…");
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

  it("calls onToggle with its id and current state", () => {
    const onToggle = vi.fn();
    render(
      <TurnBlock id="turn-7" memberIds={["7.0"]} summary={sampleSummary} expanded={false} onToggle={onToggle} />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(onToggle).toHaveBeenCalledWith("turn-7", false);
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
    const grid = container.querySelector('[class*="grid-template-rows"]');
    const clip = grid?.firstElementChild;
    expect(grid).not.toBeNull();
    expect(clip?.className).toContain("overflow-hidden");
    expect(clip?.className).toContain("min-w-0");
  });
});
