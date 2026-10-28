import { fireEvent, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import type { Highlight as HighlightComponentType } from "prism-react-renderer";

import { renderWithBrowser } from "@/test-support/render";
import { createLazyChunk, type LazyChunk } from "./lazy-chunk";
import { PythonCode, PythonCodeHighlighterProvider } from "./python-code";

// meiva:note
// The highlighter chunk is a probe of the surface under test, not module state:
// every test builds its own chunk from the import it wants (a real one, a
// pending one, a rejecting one) and hands it to the tree. Nothing here resets a
// cache a later test would inherit, and no production export exists for tests.
//
// The copy button inside the block writes through the browser port, so the assertion is what the
// component asked the page to do, not a stub of `navigator.clipboard`.
// meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md

const realImport = () =>
  import("prism-react-renderer").then((module) => module.Highlight);

function realChunk(): LazyChunk<typeof HighlightComponentType> {
  return createLazyChunk({ name: "test-highlighter", importModule: realImport });
}

function renderWith(chunk: LazyChunk<typeof HighlightComponentType>, ui: ReactElement) {
  return renderWithBrowser(ui, {
    wrapper: ({ children }) => (
      <PythonCodeHighlighterProvider value={chunk}>{children}</PythonCodeHighlighterProvider>
    ),
  });
}

it("highlights immediately when the chunk was already prefetched", async () => {
  const chunk = realChunk();
  await chunk.preload();

  const first = 'print("<pending>")';
  const latest = `${first}\nprint(2)`;
  const { container, rerender, browser } = renderWith(chunk, <PythonCode code={first} streaming />);
  expect(container.querySelector("pre")?.textContent).toBe(first);
  expect(container.querySelector("pre .token.string")?.textContent).toBe('"<pending>"');
  expect(container.querySelector("pre .animate-pulse")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Copy code" })).toBeDefined();

  rerender(<PythonCode code={latest} streaming />);
  expect(container.querySelectorAll(".token-line")).toHaveLength(2);
  expect(container.querySelector("pre")?.textContent).toContain("print(2)");
  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(browser.actions.copied).toEqual([latest]));

  rerender(<PythonCode code={latest} />);
  expect(container.querySelector("pre .animate-pulse")).toBeNull();
  expect(container.querySelectorAll("pre .token.number")).toHaveLength(1);
  expect(screen.getAllByRole("button", { name: "Copied" })).toHaveLength(1);
});

it("falls back to plain text, then highlights once an un-prefetched chunk resolves", async () => {
  const importModule = vi.fn(realImport);
  const chunk = createLazyChunk({ name: "test-highlighter", importModule });
  const code = 'print("<pending>")';
  const { container, browser } = renderWith(chunk, <PythonCode code={code} streaming />);

  expect(container.querySelector("pre")?.textContent).toBe(code);
  expect(container.querySelector("pre .token")).toBeNull();
  expect(container.querySelector("pre .animate-pulse")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Copy code" })).toBeDefined();
  expect(importModule).toHaveBeenCalledTimes(1);

  await waitFor(() => {
    expect(container.querySelector("pre .token.string")?.textContent).toBe('"<pending>"');
  });
  expect(container.querySelector("pre .animate-pulse")).not.toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
  await waitFor(() => expect(browser.actions.copied).toEqual([code]));
});

it("a rejected import falls back to plain text without an unhandled rejection", async () => {
  const unhandled: unknown[] = [];
  const onUnhandledRejection = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandledRejection);
  const report = vi.fn();

  try {
    const chunk = createLazyChunk({
      name: "test-highlighter",
      report,
      importModule: () => Promise.reject(new Error("chunk load failed")),
    });

    const code = 'print("<pending>")';
    const { container } = renderWith(chunk, <PythonCode code={code} streaming />);

    expect(container.querySelector("pre")?.textContent).toBe(code);
    expect(container.querySelector("pre .token")).toBeNull();

    await waitFor(() => {
      expect(container.querySelector("pre")?.textContent).toBe(code);
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(container.querySelector("pre .token")).toBeNull();
    expect(report).toHaveBeenCalledTimes(1);
  } finally {
    process.off("unhandledRejection", onUnhandledRejection);
  }

  expect(unhandled).toEqual([]);
});

it("retries and highlights on the next mount after a prior rejection", async () => {
  let attempts = 0;
  const report = vi.fn();
  const chunk = createLazyChunk({
    name: "test-highlighter",
    report,
    importModule: () => {
      attempts += 1;
      return attempts === 1 ? Promise.reject(new Error("chunk load failed")) : realImport();
    },
  });
  const code = 'print("<pending>")';
  const first = renderWith(chunk, <PythonCode code={code} streaming />);
  await waitFor(() => {
    expect(first.container.querySelector("pre")?.textContent).toBe(code);
  });
  expect(first.container.querySelector("pre .token")).toBeNull();
  first.unmount();

  await chunk.preload();
  expect(attempts).toBe(2);

  const retry = renderWith(chunk, <PythonCode code={code} streaming />);
  expect(retry.container.querySelector("pre .token.string")?.textContent).toBe('"<pending>"');
});
