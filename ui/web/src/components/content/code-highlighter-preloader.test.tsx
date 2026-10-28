import { render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { createLazyChunk } from "@/components/content/lazy-chunk";
import { PythonCodeHighlighterProvider } from "@/components/content/python-code";
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

it("prefetches the highlighter once requestIdleCallback fires, and cancels it on unmount", () => {
  const { importModule, chunk } = pendingChunk();
  let idleCallback: (() => void) | undefined;
  const requestIdleCallback = vi.fn((cb: () => void) => {
    idleCallback = cb;
    return 7;
  });
  const cancelIdleCallback = vi.fn();
  vi.stubGlobal("requestIdleCallback", requestIdleCallback);
  vi.stubGlobal("cancelIdleCallback", cancelIdleCallback);

  const { unmount } = render(
    <PythonCodeHighlighterProvider value={chunk}>
      <CodeHighlighterPreloader />
    </PythonCodeHighlighterProvider>,
  );
  expect(requestIdleCallback).toHaveBeenCalledTimes(1);
  expect(importModule).not.toHaveBeenCalled();

  // Idle callback fires only once the browser actually goes idle — not at
  // mount time.
  idleCallback?.();
  expect(importModule).toHaveBeenCalledTimes(1);

  unmount();
  expect(cancelIdleCallback).toHaveBeenCalledWith(7);
});

it("falls back to a setTimeout delay when requestIdleCallback doesn't exist", () => {
  vi.useFakeTimers();
  vi.stubGlobal("requestIdleCallback", undefined);
  const { importModule, chunk } = pendingChunk();

  render(
    <PythonCodeHighlighterProvider value={chunk}>
      <CodeHighlighterPreloader />
    </PythonCodeHighlighterProvider>,
  );
  expect(importModule).not.toHaveBeenCalled();

  vi.advanceTimersByTime(2000);
  expect(importModule).toHaveBeenCalledTimes(1);
});
