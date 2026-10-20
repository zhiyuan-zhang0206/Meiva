// meiva:note
// The mount queue is the unit under test: two waiting subtrees take two idle
// slices, so a page fills in over frames instead of in one commit.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md

import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useDeferredMount } from "./use-client-ready";

const idle: IdleRequestCallback[] = [];

function Probe({ id }: { id: string }) {
  const mounted = useDeferredMount();
  return mounted ? <span data-testid={id}>{id}</span> : null;
}

async function slice() {
  await act(async () => {
    const next = idle.shift();
    next?.({ didTimeout: false, timeRemaining: () => 5 });
    await Promise.resolve();
  });
}

describe("useDeferredMount", () => {
  beforeEach(() => {
    idle.length = 0;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      cb(0);
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => undefined);
    vi.stubGlobal("requestIdleCallback", (cb: IdleRequestCallback) => {
      idle.push(cb);
      return idle.length;
    });
    vi.stubGlobal("cancelIdleCallback", () => undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("releases one waiting subtree per idle slice", async () => {
    render(
      <>
        <Probe id="first" />
        <Probe id="second" />
      </>,
    );

    expect(idle).toHaveLength(1);
    await slice();
    expect(screen.queryByTestId("first")).toBeTruthy();
    expect(screen.queryByTestId("second")).toBeNull();

    await slice();
    expect(screen.queryByTestId("second")).toBeTruthy();
  });
});
