import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ChatMarkdown,
  __resetMarkdownCacheForTests,
  __setMarkdownImportForTests,
  preloadChatMarkdown,
} from "./markdown";
import { ChatMarkdownRenderer } from "./markdown-renderer";

type Renderer = typeof ChatMarkdownRenderer;

beforeEach(() => {
  __resetMarkdownCacheForTests();
});

afterEach(() => {
  cleanup();
  __setMarkdownImportForTests(null);
});

describe("ChatMarkdown lazy boundary", () => {
  it.each([false, true])("upgrades the lazy fallback to markdown with inline=%s", async (inline) => {
    let resolveImport: (renderer: Renderer) => void = () => undefined;
    const pending = new Promise<Renderer>((resolve) => {
      resolveImport = resolve;
    });
    __setMarkdownImportForTests(() => pending);

    const { container } = render(<ChatMarkdown content="**bold** text" inline={inline} />);

    const fallback = screen.getByTestId("chat-markdown-plain");
    expect(fallback.textContent).toBe("**bold** text");
    expect(fallback.classList.contains("chat-md")).toBe(true);
    expect(fallback.tagName).toBe(inline ? "SPAN" : "DIV");
    expect(container.querySelector("strong")).toBeNull();

    resolveImport(ChatMarkdownRenderer);

    await waitFor(() => {
      expect(container.querySelector("strong")?.textContent).toBe("bold");
    });
    expect(screen.queryByTestId("chat-markdown-plain")).toBeNull();
    if (inline) expect(container.querySelector("div, p, pre")).toBeNull();
  });

  it("renders markdown on the first paint when the renderer was already prefetched", async () => {
    await preloadChatMarkdown();

    const { container } = render(<ChatMarkdown content="**bold** text" />);

    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(screen.queryByTestId("chat-markdown-plain")).toBeNull();
  });

  it("keeps the fallback after a rejected chunk load and recovers on the next attempt", async () => {
    const unhandled: unknown[] = [];
    const onUnhandledRejection = (reason: unknown) => {
      unhandled.push(reason);
    };
    process.on("unhandledRejection", onUnhandledRejection);

    try {
      __setMarkdownImportForTests(() => Promise.reject(new Error("chunk load failed")));

      const first = render(<ChatMarkdown content="**bold** text" />);
      expect(screen.getByTestId("chat-markdown-plain")).toBeDefined();

      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(first.container.querySelector("strong")).toBeNull();
      expect(screen.getByTestId("chat-markdown-plain")).toBeDefined();
      first.unmount();

      __setMarkdownImportForTests(null);
      await preloadChatMarkdown();

      const retry = render(<ChatMarkdown content="**bold** text" />);
      expect(retry.container.querySelector("strong")?.textContent).toBe("bold");
    } finally {
      process.off("unhandledRejection", onUnhandledRejection);
    }

    expect(unhandled).toEqual([]);
  });
});
