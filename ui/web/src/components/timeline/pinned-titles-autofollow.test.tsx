import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { render as wallClockRender } from "@/lib/transport/tests/wall-clock-render";
import { ApiTestProvider } from "@/test-support/fake-api";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createLazyChunk } from "@/components/content/lazy-chunk";
import { PythonCodeHighlighterProvider } from "@/components/content/python-code";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem } from "@/lib/contracts/types";

import { TimelineView } from ".";

vi.mock("../content/markdown", () => ({
  ChatMarkdown: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock("../ui/scroll-area", () => ({
  ScrollArea: ({
    children,
    viewportClassName,
    viewportTestId,
  }: {
    children: ReactNode;
    viewportClassName?: string;
    viewportTestId?: string;
  }) => (
    <div>
      <div data-slot="scroll-area-viewport" data-testid={viewportTestId} className={viewportClassName}>
        {children}
      </div>
    </div>
  ),
}));

function makeItems(): BackendTimelineItem[] {
  return [
    { item_id: "1.0", kind: "agent_reasoning", payload: "thinking", source: null, created_at: "2026-05-15T12:00:00Z", inbound_id: null, show_timestamp: false },
    { item_id: "1.1", kind: "agent_code", payload: "code", source: null, created_at: "2026-05-15T12:00:00Z", inbound_id: null, show_timestamp: false },
    { item_id: "1.2", kind: "code_output", payload: "output", source: null, created_at: "2026-05-15T12:00:00Z", inbound_id: null, show_timestamp: false },
  ];
}

function setupExpandedBlock() {
  const highlighter = createLazyChunk({
    name: "test-highlighter",
    importModule: () => new Promise<never>(() => undefined),
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(["config", null], { fields: [] });
  qc.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS });
  const { container } = wallClockRender(
    <QueryClientProvider client={qc}>
      <PythonCodeHighlighterProvider value={highlighter}>
        <TimelineView items={makeItems()} />
      </PythonCodeHighlighterProvider>
    </QueryClientProvider>,
    { wrapper: ApiTestProvider },
  );
  const toggle = screen.getByTestId("turn-toggle");
  fireEvent.click(toggle);
  for (const btn of screen.queryAllByTestId("card-toggle")) {
    if (btn.getAttribute("data-expanded") === "false") fireEvent.click(btn);
  }
  const viewport = container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!;
  Object.defineProperty(viewport, "scrollHeight", { value: 2000, configurable: true });
  Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
  return { container, viewport, toggle };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("TimelineView bounded work blocks during scroll", () => {
  it("renders its code blocks from the injected pending chunk", async () => {
    // meiva:note
    // The setup injects a chunk whose import never resolves so the code item stays plain text while the test drives scroll geometry; a macrotask is long enough for the real prism chunk to load, so a tree that lost its provider would show .token spans here.
    // meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md
    const { container } = setupExpandedBlock();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(container.querySelector("[data-testid='python-code'] .token")).toBeNull();
  });

  it("keeps work headers non-sticky across rapid scroll gestures while bounding the inner scrollport", () => {
    const { container, viewport, toggle } = setupExpandedBlock();
    expect(toggle.className).not.toContain("sticky");
    expect(screen.getByTestId("turn-scroll-viewport").className).toContain(
      "max-h-[var(--timeline-turn-max-h,50dvh)]",
    );

    viewport.scrollTop = 1400;
    act(() => { fireEvent.scroll(viewport); });
    viewport.scrollTop = 1000;
    act(() => { fireEvent.scroll(viewport); });

    expect(toggle.className).not.toContain("sticky");
    expect(container.querySelector("[data-fast-scrolling]")).toBeNull();
  });
});
