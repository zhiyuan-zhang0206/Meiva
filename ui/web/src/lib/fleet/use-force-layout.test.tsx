// Warm-up behavior of the shared force layout (task #4008): with
// `prewarmTicks` the layout advances in manual, time-budgeted slices on rAF
// and commits sparsely — never a render per tick — while the default path
// keeps the timer-driven settle.

// meiva:note
// @vitest-environment happy-dom

import { act, render } from "@testing-library/react";
import { useEffect, useRef } from "react";
import { describe, expect, it, vi } from "vitest";

import { useForceLayout, type ForceParams, type SimLink, type SimNode } from "./use-force-layout";

const PARAMS: ForceParams = {
  nodeSizeMin: 4,
  nodeSizeMax: 6,
  linkDistance: 40,
  linkStrength: 0.3,
  repulsion: 40,
  centerStrength: 0.5,
  centerForceX: 0.1,
  centerForceY: 0.1,
  collidePadding: 2,
  alphaDecay: 0.025,
  zoomPadding: 10,
  zoomFitRatio: 1,
};

// Stable references — a new array identity would rebuild the simulation.
const N = 250;
const NODES: SimNode[] = Array.from({ length: N }, (_, i) => ({ id: `n${i}`, r: 5 }));
const LINKS: SimLink[] = Array.from({ length: N - 1 }, (_, i) => ({
  source: `n${i}`,
  target: `n${i + 1}`,
}));

function Harness({
  prewarmTicks,
  onCommit,
  requestAnimationFrame,
  cancelAnimationFrame,
}: {
  prewarmTicks?: number;
  onCommit: () => void;
  requestAnimationFrame: (cb: FrameRequestCallback) => number;
  cancelAnimationFrame: (id: number) => void;
}) {
  const { positions, layout } = useForceLayout(NODES, LINKS, PARAMS, {
    prewarmTicks,
    requestAnimationFrame,
    cancelAnimationFrame,
  });
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    onCommit();
  }, [positions, onCommit]);
  return <div data-testid="placed">{layout ? layout.placed.length : 0}</div>;
}

describe("useForceLayout warm-up (task #4008)", () => {
  it("settles in sparse rAF slices — commits stay far below the tick count", async () => {
    const rafCbs: FrameRequestCallback[] = [];
    let nextId = 0;
    const scheduleRaf = (cb: FrameRequestCallback): number => {
      rafCbs.push(cb);
      return ++nextId;
    };
    const cancelRaf = (): void => undefined;

    const onCommit = vi.fn();
    const { getByTestId } = render(
      <Harness
        prewarmTicks={320}
        onCommit={onCommit}
        requestAnimationFrame={scheduleRaf}
        cancelAnimationFrame={cancelRaf}
      />,
    );

    // The slice loop owns the simulation until it drains: no timer-driven
    // "tick" may fire before we pump. d3-timer captured the native rAF at
    // import time, so the stub above cannot intercept it — wait real time and
    // require silence. A setup-time sim.restart() would tick at ~60fps here,
    // re-entering the every-tick render path this warm-up exists to avoid.
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(onCommit).not.toHaveBeenCalled();

    // Pump frames until the warm-up drains its scheduled slices (the ~270
    // manual ticks are bounded by 320; a slice budget always makes progress).
    for (let i = 0; i < 400 && rafCbs.length > 0; i += 1) {
      const cb = rafCbs.shift();
      if (!cb) break;
      act(() => {
        cb(performance.now());
      });
    }

    // Sparse commits: a per-tick render would produce ~270 position changes;
    // the sliced warm-up commits at slices 1-2 and then every 32nd slice
    // (~13 with one tick per slice of this size). The bound keeps headroom
    // for environment timing differences while staying far below per-tick.
    expect(onCommit.mock.calls.length).toBeGreaterThan(0);
    expect(onCommit.mock.calls.length).toBeLessThanOrEqual(24);
    // The warmed layout is committed and complete.
    expect(Number(getByTestId("placed").textContent)).toBe(N);
  });
});
