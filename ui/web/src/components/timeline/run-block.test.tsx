import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";

import { TurnBlock } from "./run-block";
import type { TurnSummary } from "./model/runs";

afterEach(cleanup);

const baseSummary: TurnSummary = {
  total: 120,
  thinking: 120,
  code: 0,
  output: 0,
  turns: 120,
  systemPrompts: 0,
  compactSummaries: 0,
  memories: 0,
  agentMessages: 0,
  systemNotes: 0,
  workedMs: 6000,
  thinkingMs: 6000,
  codeMs: 0,
  execMs: 0,
  sdkCalls: [],
  lastLiveKind: null,
  lastLiveStartedAt: 0,
};

describe("TurnBlock inner row windowing", () => {
  it("mounts only the bootstrap window and turn-before spacer when item count exceeds turnRows", () => {
    const memberIds = Array.from({ length: 120 }, (_, i) => `${i + 1}.0`);
    const { container } = render(
      <TurnBlock
        id="1.0"
        memberIds={memberIds}
        summary={baseSummary}
        expanded
        onToggle={vi.fn()}
        windowedCount={120}
        turnRows={20}
        renderWindowedRow={(index) => (
          <div key={memberIds[index]} data-testid="windowed-row" data-row-index={index}>
            {`Row ${index}`}
          </div>
        )}
      />,
    );

    expect(container.querySelector('[data-timeline-spacer="turn-before"]')).not.toBeNull();
    const mounted = screen.getAllByTestId("windowed-row");
    expect(mounted.length).toBe(24);
    expect(mounted[mounted.length - 1].getAttribute("data-row-index")).toBe("119");

    const innerViewport = screen.getByTestId("turn-scroll-viewport");
    Object.defineProperty(innerViewport, "clientHeight", { value: 240, configurable: true });
    innerViewport.scrollTop = 1200;
    fireEvent.scroll(innerViewport);

    expect(container.querySelector('[data-timeline-spacer="turn-before"]')).not.toBeNull();
    expect(container.querySelector('[data-timeline-spacer="turn-after"]')).not.toBeNull();
  });

  it("keeps the measured tail mounted while the live window follows its bottom", () => {
    const observers: { callback: ResizeObserverCallback; targets: Set<Element> }[] = [];
    vi.stubGlobal("ResizeObserver", class {
      readonly record: { callback: ResizeObserverCallback; targets: Set<Element> };
      constructor(callback: ResizeObserverCallback) {
        this.record = { callback, targets: new Set() };
        observers.push(this.record);
      }
      observe(target: Element) { this.record.targets.add(target); }
      unobserve(target: Element) { this.record.targets.delete(target); }
      disconnect() { this.record.targets.clear(); }
    });
    const reportRowHeights = (blockSize: number) => {
      for (const { callback, targets } of observers) {
        const rows = [...targets].filter((target) => target.hasAttribute("data-turn-row"));
        if (rows.length === 0) continue;
        callback(
          rows.map((target) => ({ target, borderBoxSize: [{ blockSize, inlineSize: 600 }] }) as unknown as ResizeObserverEntry),
          {} as ResizeObserver,
        );
      }
    };
    const memberIds = Array.from({ length: 120 }, (_, i) => `${i + 1}.0`);
    render(
      <TurnBlock
        id="1.0"
        memberIds={memberIds}
        summary={baseSummary}
        expanded
        onToggle={vi.fn()}
        turnActive
        liveWindow
        onActivityAtBottomChange={vi.fn()}
        windowedCount={120}
        turnRows={20}
        renderWindowedRow={(index) => (
          <div data-testid="windowed-row" data-row-index={index}>
            {`Row ${index}`}
          </div>
        )}
      />,
    );
    const mountedIndexes = () =>
      screen.getAllByTestId("windowed-row").map((row) => Number(row.getAttribute("data-row-index")));

    const innerViewport = screen.getByTestId("activity-scroll-viewport");
    Object.defineProperty(innerViewport, "clientHeight", { value: 390, configurable: true });
    act(() => reportRowHeights(300));
    const pinnedTopOfMeasuredRows = 120 * 304 - 390;
    innerViewport.scrollTop = pinnedTopOfMeasuredRows;
    fireEvent.scroll(innerViewport);
    expect(mountedIndexes()).toEqual([117, 118, 119]);

    innerViewport.scrollTop = 20_000;
    fireEvent.scroll(innerViewport);
    expect(mountedIndexes()).toEqual([117, 118, 119]);
    vi.unstubAllGlobals();
  });
});
