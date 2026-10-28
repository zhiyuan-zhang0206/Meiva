import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { memo, type ReactElement } from "react";

import { createLazyChunk, type LazyChunk } from "./lazy-chunk";
import { ChatMarkdown, ChatMarkdownRendererProvider } from "./markdown";
import type { ChatMarkdownProps, ChatMarkdownRenderer } from "./markdown-renderer";

type Renderer = typeof ChatMarkdownRenderer;

const FakeRenderer = memo(({ content }: ChatMarkdownProps) => (
  <strong data-testid="fake-renderer">{content}</strong>
));

function chunkOf(
  importModule: () => Promise<Renderer>,
  report: (message: string) => void = vi.fn(),
): LazyChunk<Renderer> {
  return createLazyChunk({ name: "test-markdown", importModule, report });
}

function renderWith(chunk: LazyChunk<Renderer>, ui: ReactElement) {
  return render(<ChatMarkdownRendererProvider value={chunk}>{ui}</ChatMarkdownRendererProvider>);
}

afterEach(() => {
  cleanup();
});

describe("ChatMarkdown lazy boundary", () => {
  it.each([false, true])("upgrades the lazy fallback to the renderer with inline=%s", async (inline) => {
    let resolveImport: (renderer: Renderer) => void = () => undefined;
    const pending = new Promise<Renderer>((resolve) => {
      resolveImport = resolve;
    });
    const chunk = chunkOf(() => pending);

    const { container } = renderWith(chunk, <ChatMarkdown content="**bold** text" inline={inline} />);

    const fallback = screen.getByTestId("chat-markdown-plain");
    expect(fallback.textContent).toBe("**bold** text");
    expect(fallback.classList.contains("chat-md")).toBe(true);
    expect(fallback.tagName).toBe(inline ? "SPAN" : "DIV");
    expect(screen.queryByTestId("fake-renderer")).toBeNull();

    resolveImport(FakeRenderer);

    await waitFor(() => {
      expect(screen.getByTestId("fake-renderer").textContent).toBe("**bold** text");
    });
    expect(screen.queryByTestId("chat-markdown-plain")).toBeNull();
    if (inline) expect(container.querySelector("div, p, pre")).toBeNull();
  });

  it("renders through the resolved renderer on the first paint when the chunk was already prefetched", async () => {
    const chunk = chunkOf(() => Promise.resolve(FakeRenderer));
    await chunk.preload();

    renderWith(chunk, <ChatMarkdown content="**bold** text" />);

    expect(screen.getByTestId("fake-renderer").textContent).toBe("**bold** text");
    expect(screen.queryByTestId("chat-markdown-plain")).toBeNull();
  });

  it("keeps the fallback after a rejected chunk load and recovers on the next attempt", async () => {
    const unhandled: unknown[] = [];
    const onUnhandledRejection = (reason: unknown) => {
      unhandled.push(reason);
    };
    process.on("unhandledRejection", onUnhandledRejection);

    try {
      let attempts = 0;
      const chunk = chunkOf(() => {
        attempts += 1;
        return attempts === 1
          ? Promise.reject(new Error("chunk load failed"))
          : Promise.resolve(FakeRenderer);
      });

      const first = renderWith(chunk, <ChatMarkdown content="**bold** text" />);
      expect(screen.getByTestId("chat-markdown-plain")).toBeDefined();

      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(screen.queryByTestId("fake-renderer")).toBeNull();
      expect(screen.getByTestId("chat-markdown-plain")).toBeDefined();
      first.unmount();

      await chunk.preload();
      expect(attempts).toBe(2);

      renderWith(chunk, <ChatMarkdown content="**bold** text" />);
      expect(screen.getByTestId("fake-renderer").textContent).toBe("**bold** text");
    } finally {
      process.off("unhandledRejection", onUnhandledRejection);
    }

    expect(unhandled).toEqual([]);
  });
});
