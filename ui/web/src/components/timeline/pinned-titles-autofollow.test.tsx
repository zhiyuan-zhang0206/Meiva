import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { render as wallClockRender } from "@/lib/transport/tests/wall-clock-render";
import { ApiTestProvider } from "@/test-support/fake-api";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { __resetHighlighterCacheForTests, __setHighlighterImportForTests } from "@/components/content/python-code";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem } from "@/lib/contracts/types";

import { TimelineView } from ".";

vi.mock("../content/markdown", () => ({
  ChatMarkdown: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock("../ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children: ReactNode }) => (
    <div>
      <div data-slot="scroll-area-viewport">{children}</div>
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

function mockRect(top: number, bottom: number, height: number): DOMRect {
  return {
    top,
    bottom,
    height,
    left: 0,
    right: 400,
    width: 400,
    x: 0,
    y: top,
    toJSON: () => undefined,
  };
}

function setupStuckBlock() {
  __resetHighlighterCacheForTests();
  __setHighlighterImportForTests(() => new Promise(() => undefined));
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(["config", null], { fields: [] });
  qc.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS, "display.expand_runs_mode": "all" });
  const { container } = wallClockRender(
    <QueryClientProvider client={qc}>
      <TimelineView items={makeItems()} />
    </QueryClientProvider>,
    { wrapper: ApiTestProvider },
  );
  const viewport = container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]')!;
  Object.defineProperty(viewport, "scrollHeight", { value: 2000, configurable: true });
  Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
  vi.spyOn(viewport, "getBoundingClientRect").mockReturnValue(mockRect(0, 800, 800));
  const turnEl = container.querySelector<HTMLElement>('[data-turn-expanded="true"]')!;
  vi.spyOn(turnEl, "getBoundingClientRect").mockReturnValue(mockRect(10, 700, 690));
  return { container, viewport, toggle: screen.getByTestId("turn-toggle") };
}

afterEach(() => {
  cleanup();
  __resetHighlighterCacheForTests();
  __setHighlighterImportForTests(null);
  vi.restoreAllMocks();
});

describe("TimelineView pinned titles during auto-follow", () => {
  it("keeps the pinned header through an auto-follow growth pin", async () => {
    const { viewport, toggle } = setupStuckBlock();
    expect(toggle.getAttribute("data-stuck")).toBe("false");

    viewport.scrollTop = 1400;
    act(() => { fireEvent.scroll(viewport); });

    await waitFor(() => expect(toggle.getAttribute("data-stuck")).toBe("true"));
    expect(toggle.className).toContain("backdrop-blur-md");
  });

  it("keeps the pin when the reader leaves the bottom at speed", async () => {
    const { container, viewport, toggle } = setupStuckBlock();

    viewport.scrollTop = 1400;
    act(() => { fireEvent.scroll(viewport); });
    viewport.scrollTop = 1000;
    act(() => { fireEvent.scroll(viewport); });

    await waitFor(() => expect(toggle.getAttribute("data-stuck")).toBe("true"));
    expect(toggle.className).toContain("sticky");
    expect(container.querySelector("[data-fast-scrolling]")).toBeNull();
  });
});
