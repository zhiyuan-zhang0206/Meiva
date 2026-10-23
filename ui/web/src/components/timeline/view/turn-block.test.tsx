import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { TurnBlock } from "../run-block";
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

  it("renders collapsed turn block with data attributes and no sticky classes", () => {
    render(
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
    expect(toggle.getAttribute("data-stuck")).toBe("false");
    expect(toggle.className).not.toContain("sticky");
    expect(toggle.className).not.toContain("shadow-[");
  });

  it("renders expanded turn block with sticky classes", () => {
    render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={true}
        isStuck={false}
        onToggle={vi.fn()}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.getAttribute("data-stuck")).toBe("false");
    expect(toggle.className).toContain("sticky");
    expect(toggle.className).toContain("top-11");
    expect(toggle.className).toContain("z-10");
    expect(toggle.className).toContain("before:content-['']");
    expect(toggle.className).toContain("before:bg-transparent");
    expect(toggle.className).toContain("before:backdrop-blur-none");
  });

  it("renders stuck active styling when expanded and isStuck is true", () => {
    render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={true}
        isStuck={true}
        onToggle={vi.fn()}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    expect(toggle.getAttribute("data-expanded")).toBe("true");
    expect(toggle.getAttribute("data-stuck")).toBe("true");
    expect(toggle.className).toContain("hover:bg-transparent");
    expect(toggle.className).not.toContain("hover:bg-accent/30");
    expect(toggle.className).toContain("before:backdrop-blur-md");
    expect(toggle.className).toContain("before:bg-background/95");
    expect(toggle.className).toContain("hover:before:bg-accent/30");
    expect(toggle.className).toContain("before:bottom-0");
    expect(toggle.className).toContain("before:-z-10");
    expect(toggle.className).toContain("16px_0_0_0_var(--background)");
    expect(toggle.className).toContain("before:-top-[2px]");
    expect(toggle.className).toContain("after:bottom-0");
    expect(toggle.className).toContain("after:h-px");
    expect(toggle.className).not.toContain("after:top-full");
    expect(toggle.className).not.toContain("-mb-px");
    expect(toggle.className).toContain("motion-reduce:transition-none");
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

  it("calls onToggle when clicked in stuck state", () => {
    const onToggle = vi.fn();
    render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0", "1.1"]}
        summary={sampleSummary}
        expanded={true}
        isStuck={true}
        onToggle={onToggle}
      >
        <div data-testid="detail-content">detail rows</div>
      </TurnBlock>,
    );

    const toggle = screen.getByTestId("turn-toggle");
    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("observes the header's full border box for the nested pin offset", () => {
    const observe = vi.fn();
    class FakeResizeObserver {
      observe = observe;
      disconnect = vi.fn();
    }
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
    try {
      render(
        <TurnBlock
          id="turn-1"
          memberIds={["1.0", "1.1"]}
          summary={sampleSummary}
          expanded={true}
          isStuck={false}
          onToggle={vi.fn()}
        >
          <div data-testid="detail-content">detail rows</div>
        </TurnBlock>,
      );
      expect(observe).toHaveBeenCalledWith(
        screen.getByTestId("turn-toggle"),
        { box: "border-box" },
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("locks the collapse wrapper shrinkable on the inline axis (#3305)", () => {
    const { container } = render(
      <TurnBlock
        id="turn-1"
        memberIds={["1.0"]}
        summary={sampleSummary}
        expanded={true}
        isStuck={false}
        onToggle={vi.fn()}
      >
        <div>detail rows</div>
      </TurnBlock>,
    );
    const grid = container.querySelector('[class*="grid-template-rows"]');
    const clip = grid?.firstElementChild;
    expect(grid).not.toBeNull();
    expect(clip?.className).toContain("overflow-clip");
    expect(clip?.className).toContain("min-w-0");
  });
});
