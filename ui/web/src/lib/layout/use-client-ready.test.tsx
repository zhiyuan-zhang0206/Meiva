// meiva:note
// The mount queue is the unit under test: two waiting subtrees take two idle
// slices, so a page fills in over frames instead of in one commit.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md

// meiva:note
// @vitest-environment happy-dom

import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useDeferredMount, type DeferredMountScheduler } from "./use-client-ready";

const idle: (() => void)[] = [];

const testScheduler: DeferredMountScheduler = {
  requestFrame: (cb) => {
    cb(0);
    return 1;
  },
  cancelFrame: () => undefined,
  scheduleIdle: (cb) => {
    idle.push(cb);
  },
};

function Probe({ id }: { id: string }) {
  const mounted = useDeferredMount(testScheduler);
  return mounted ? <span data-testid={id}>{id}</span> : null;
}

async function slice() {
  await act(async () => {
    const next = idle.shift();
    next?.();
    await Promise.resolve();
  });
}

describe("useDeferredMount", () => {
  beforeEach(() => {
    idle.length = 0;
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
