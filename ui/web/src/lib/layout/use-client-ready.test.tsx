// meiva:note
// The mount queue is the unit under test: two waiting subtrees take two idle
// slices, so a page fills in over frames instead of in one commit.
//
// lib/ may not import src/test-support/, so a component test here mounts the page's provider by
// hand, with a fresh fake browser.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md

// meiva:note
// @vitest-environment happy-dom

import { act, render as rtlRender, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeBrowser, pageWrapper } from "@/lib/browser/testing";
import { useDeferredMount } from "./use-client-ready";

function render(ui: React.ReactElement) {
  return rtlRender(ui, { wrapper: pageWrapper(fakeBrowser()) });
}

function Probe({ id }: { id: string }) {
  const mounted = useDeferredMount();
  return mounted ? <span data-testid={id}>{id}</span> : null;
}

describe("useDeferredMount", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("releases one waiting subtree per idle slice", () => {
    render(
      <>
        <Probe id="first" />
        <Probe id="second" />
      </>,
    );

    expect(screen.queryByTestId("first")).toBeNull();
    expect(screen.queryByTestId("second")).toBeNull();

    act(() => {
      vi.advanceTimersToNextTimer();
    });
    expect(screen.queryByTestId("first")).toBeNull();

    act(() => {
      vi.advanceTimersToNextTimer();
    });
    expect(screen.queryByTestId("first")).toBeTruthy();
    expect(screen.queryByTestId("second")).toBeNull();

    act(() => {
      vi.advanceTimersToNextTimer();
    });
    expect(screen.queryByTestId("second")).toBeTruthy();
  });
});
