// meiva:note
// The preloader asks the page for an idle slice, so the suite hands in a page whose slices it fires
// by hand: "not at mount, only when the page is idle — and dropped if the mount goes first" is a
// value the test moves instead of a global it replaces. The timer a browser without
// requestIdleCallback falls back to is `idleOver`'s behavior, tested in system-browser.test.ts.
import { render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { createLazyChunk } from "@/components/content/lazy-chunk";
import { PythonCodeHighlighterProvider } from "@/components/content/python-code";
import { fakeBrowser, pageWrapper } from "@/lib/browser/testing";
import { CodeHighlighterPreloader } from "./code-highlighter-preloader";

function pendingChunk() {
  const importModule = vi.fn(() => new Promise<never>(() => undefined));
  return {
    importModule,
    chunk: createLazyChunk({
      name: "test-highlighter",
      importModule,
    }),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("prefetches the highlighter once the page goes idle, and drops it on unmount", () => {
  const { importModule, chunk } = pendingChunk();
  const page = fakeBrowser({ idle: "manual" });

  const { unmount } = render(
    <PythonCodeHighlighterProvider value={chunk}>
      <CodeHighlighterPreloader />
    </PythonCodeHighlighterProvider>,
    { wrapper: pageWrapper(page) },
  );
  expect(page.idleRequests).toHaveLength(1);
  expect(importModule).not.toHaveBeenCalled();

  // meiva:note
  // The work runs only when the page actually goes idle — not at mount.
  page.idleRequests[0].fire();
  expect(importModule).toHaveBeenCalledTimes(1);

  unmount();
  expect(page.idleRequests[0].cancelled).toBe(true);
});

it("drops the prefetch when the page leaves before the idle slice arrives", () => {
  const { importModule, chunk } = pendingChunk();
  const page = fakeBrowser({ idle: "manual" });

  const { unmount } = render(
    <PythonCodeHighlighterProvider value={chunk}>
      <CodeHighlighterPreloader />
    </PythonCodeHighlighterProvider>,
    { wrapper: pageWrapper(page) },
  );
  unmount();

  page.idleRequests[0].fire();
  expect(page.idleRequests[0].ran).toBe(false);
  expect(importModule).not.toHaveBeenCalled();
});
