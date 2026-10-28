/**
 * card.tsx — unit tests for MessageCard, CardHeader, messageCardConfig,
 * and the rich summary components.
 *
 * Historical quarantine root cause: live-clock renders here (useNow's 100ms
 * setInterval) leaked past the file's end because `globals: false` prevented
 * automatic cleanup. A tick after happy-dom teardown surfaced as an unhandled
 * "ReferenceError: window is not defined" from react-dom's scheduler under
 * CI's parallel forks. The fix landed in vitest.setup.ts as
 * afterEach(cleanup), which unmounts and clears those intervals per test.
 * De-quarantined 2026-08-24.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { createLazyChunk } from "@/components/content/lazy-chunk";
import { PythonCodeHighlighterProvider } from "@/components/content/python-code";
import { CardHeader, MessageCard, messageCardConfig, type CardConfig } from "./card";
import { resolveTimelineColors } from "@/lib/timeline/timeline-colors";
import { formatItemTime } from "./timestamp";
import type { BackendTimelineItem } from "@/lib/contracts/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function item(
  kind: BackendTimelineItem["kind"],
  overrides: Partial<BackendTimelineItem> = {},
): BackendTimelineItem {
  return {
    item_id: "0.0",
    kind,
    source: null,
    payload: "",
    created_at: new Date().toISOString(),
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

// eslint-disable-next-line @typescript-eslint/no-empty-function
const noop = () => {};

// CardHeader now reads user settings via useUserSettings, which requires a
// React Query context. Seed with default settings including the new
// display.show_timestamp_weekday flag (default true).
function renderWithQuery(ui: React.ReactElement, overrides: Record<string, unknown> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  qc.setQueryData(["user-settings"], {
    "display.show_timestamp_weekday": true,
    ...overrides,
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

// ---------------------------------------------------------------------------
// messageCardConfig
// ---------------------------------------------------------------------------

describe("messageCardConfig", () => {
  it("returns null for ephemeral system_marker (no card)", () => {
    const cfg = messageCardConfig(
      item("system_marker", { source: null, payload: "error:boom" }),
    );
    expect(cfg).toBeNull();
  });

  it("system_prompt → rich=system_prompt, fixed default collapsed", () => {
    const cfg = messageCardConfig(item("system_prompt", { payload: "hello" }));
    expect(cfg).not.toBeNull();
    expect(cfg!.rich).toBe("system_prompt");
    expect(cfg!.fixedDefault).toBe(false);
  });

  it("agent_reasoning → rich=reasoning, collapsed when a None-mode turn is opened", () => {
    const cfg = messageCardConfig(item("agent_reasoning"));
    expect(cfg!.rich).toBe("reasoning");
    // Task #4780: a turn opened by hand keeps its children collapsed.
    expect(cfg!.fixedDefault).toBe(false);
  });

  it("agent_code → rich=code", () => {
    const cfg = messageCardConfig(item("agent_code"));
    expect(cfg!.rich).toBe("code");
  });

  it("attach → paperclip card, open by default", () => {
    const cfg = messageCardConfig(item("attach"));
    expect(cfg).not.toBeNull();
    expect(cfg!.icon?.displayName).toBe("Paperclip");
    expect(cfg!.titleKey).toBe("timeline.attachedFiles");
    expect(cfg!.fixedDefault).toBe(true);
    expect(cfg!.showTimestamp).toBe(true);
  });

  it("code_output → rich=output", () => {
    const cfg = messageCardConfig(item("code_output"));
    expect(cfg!.rich).toBe("output");
  });

  it("agent_chat → no rich, title='Meiva ▸'", () => {
    const cfg = messageCardConfig(item("agent_chat"));
    expect(cfg!.rich).toBeNull();
    expect(cfg!.title).toBe("Meiva ▸");
  });

  it("agent_chat partial → title includes '(streaming…)'", () => {
    const cfg = messageCardConfig(item("agent_chat", { partial: true }));
    expect(cfg!.title).toContain("(streaming…)");
  });

  it("agent_chat interrupted → title includes '(streaming interrupted)'", () => {
    const cfg = messageCardConfig(item("agent_chat", { interrupted: true }));
    expect(cfg!.title).toContain("(streaming interrupted)");
  });

  it("inbound_chat source=user → Human, gray border", () => {
    const cfg = messageCardConfig(item("inbound_chat", { source: "user" }));
    expect(cfg!.title).toBe("Human");
    expect(cfg!.border).toContain("gray");
  });

  it("inbound_chat source=agent:42 → Agent 42, violet border", () => {
    const cfg = messageCardConfig(item("inbound_chat", { source: "agent:42" }));
    expect(cfg!.title).toBe("Agent 42");
    expect(cfg!.border).toContain("violet");
  });

  it("inbound_chat ui:page → Human", () => {
    const cfg = messageCardConfig(
      item("inbound_chat", { source: "ui:page:foo" }),
    );
    expect(cfg!.title).toBe("Human");
  });

  it("inbound_chat unknown source → System, sky border", () => {
    const cfg = messageCardConfig(item("inbound_chat", { source: "kernel" }));
    expect(cfg!.title).toBe("System");
    expect(cfg!.border).toContain("sky");
  });

  it("inbound_compact → differentiated labels, sky border", () => {
    const cfgSummary = messageCardConfig(item("inbound_compact_summary"));
    expect(cfgSummary!.title).toBe("Compact summary");
    expect(cfgSummary!.border).toContain("sky");

    const cfgRequest = messageCardConfig(item("inbound_compact_request"));
    expect(cfgRequest!.title).toBe("Compact request");
    expect(cfgRequest!.border).toContain("sky");
  });

  it("system_marker lifecycle_terminate → Terminated, rose border", () => {
    const cfg = messageCardConfig(
      item("system_marker", { source: "lifecycle_terminate" }),
    );
    expect(cfg!.title).toBe("Terminated");
    expect(cfg!.border).toContain("rose");
  });

  it("system_marker memory → Memory, violet border", () => {
    for (const src of ["memory", "agent_memory"]) {
      const cfg = messageCardConfig(item("system_marker", { source: src }));
      expect(cfg!.title).toBe("Memory");
      expect(cfg!.border).toContain("violet");
    }
  });

  it("system_marker note → Note", () => {
    for (const src of [
      "sdk_hint",
      "memory_write_check",
      "agent_id",
      "context",
      "heartbeat",
      "security",
    ]) {
      const cfg = messageCardConfig(item("system_marker", { source: src }));
      expect(cfg!.title).toBe("Note");
    }
  });

  // ── Configurable palette (tasks #3304 / #3312) ──────────────────────────
  // Inbound defaults are pinned above (gray / violet / sky / rose). These pin
  // the changed defaults + the override path — overrides arrive as a resolved
  // color map from the settings hook, exactly like the timeline passes them.

  it("agent_code default → cyan-500/70 border", () => {
    const cfg = messageCardConfig(item("agent_code"));
    expect(cfg!.border).toBe("border-cyan-500/70");
  });

  it("agent_code with a recorded override → the family drives the border", () => {
    const colors = resolveTimelineColors({ "display.color.agent_code": "pink" });
    const cfg = messageCardConfig(item("agent_code"), colors);
    expect(cfg!.border).toBe("border-pink-500/70");
  });

  it("agent_reasoning default → blue-400/40 border + blue bg", () => {
    const cfg = messageCardConfig(item("agent_reasoning"));
    expect(cfg!.border).toBe("border-blue-400/40");
    expect(cfg!.bg).toBe("bg-blue-50/60 dark:bg-blue-900/20");
  });

  it("note default → teal border/bg/text tint", () => {
    const cfg = messageCardConfig(item("system_marker", { source: "sdk_hint" }));
    expect(cfg!.border).toBe("border-teal-500/40");
    expect(cfg!.bg).toBe("bg-teal-50/40 dark:bg-teal-950/15");
    expect(cfg!.cardText).toBe("text-teal-700 dark:text-teal-300");
  });

  it("note with a recorded override → fuchsia family", () => {
    const colors = resolveTimelineColors({ "display.color.note": "fuchsia" });
    const cfg = messageCardConfig(item("system_marker", { source: "sdk_hint" }), colors);
    expect(cfg!.border).toBe("border-fuchsia-500/40");
    expect(cfg!.cardText).toBe("text-fuchsia-700 dark:text-fuchsia-300");
  });
});

// ---------------------------------------------------------------------------
// MessageCard
// ---------------------------------------------------------------------------

describe("MessageCard", () => {
  const cfg: CardConfig = {
    rich: null,
    icon: null,
    title: "Test",
    border: "border-red-500",
    bg: "bg-red-50",
    fixedDefault: true,
    showTimestamp: false,
    copy: false,
  };

  it("marks the kind with a short accent at the header, not a full-height frame", () => {
    const { container } = render(
      <MessageCard config={cfg}>
        <div data-testid="child">hello</div>
      </MessageCard>,
    );
    const frame = container.firstElementChild;
    expect(frame?.className).not.toContain("border-l-2");
    const accent = frame?.querySelector('[data-testid="card-accent"]');
    expect(accent?.className).toContain("border-l-2");
    expect(accent?.className).toContain("border-red-500");
    expect(accent?.className).toContain("h-7");
  });

  // meiva:note
  // The card box is only the box: the timestamp/Copy band belongs to the row, in
  // the gap below the block (user 2026-10-21), so a card can never mount chrome of
  // its own and a kind that stamps nothing cannot carry an empty band. The band's
  // placement and its hover reveal are asserted at the TimelineView level.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  it("renders the box alone — the hover band belongs to the row, not the card", () => {
    const { container } = render(
      <MessageCard config={cfg}>
        <div data-testid="child">hello</div>
      </MessageCard>,
    );
    const frame = container.firstElementChild;
    expect(frame?.className).toContain("bg-red-50");
    expect(frame?.className).not.toContain("group/msgcard");
    expect(frame?.children).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// CardHeader
// ---------------------------------------------------------------------------

describe("CardHeader", () => {
  // meiva:note
  // The item timestamp left the header when it moved to the card's hover footer, so the header carries only the toggle chrome; the stamp's own placement is asserted at the TimelineView level.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  it("carries no item timestamp — the stamp lives in the card's hover footer", () => {
    const event = item("system_marker", {
      source: "impersonation",
      created_at: "2026-10-06T13:39:25+00:00",
    });
    const { container, getByTestId } = renderWithQuery(
      <CardHeader item={event} config={messageCardConfig(event)!} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).not.toContain(formatItemTime(event.created_at!, { showWeekday: true }));
    expect(getByTestId("card-toggle").classList.contains("flex-wrap")).toBe(true);
  });

  const baseItem = item("agent_chat", { payload: "test" });

  it("renders title and chevron for a non-rich config", () => {
    const cfg = messageCardConfig(baseItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={baseItem} config={cfg} expanded={true} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Meiva ▸");
  });

  it("collapsed state → aria-expanded=false, data-expanded=false, no title (no hover tooltip)", () => {
    const cfg = messageCardConfig(baseItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={baseItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(btn.getAttribute("data-expanded")).toBe("false");
    expect(btn.getAttribute("title")).toBeNull();
  });

  it("expanded state → aria-expanded=true, data-expanded=true, no title (no hover tooltip)", () => {
    const cfg = messageCardConfig(baseItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={baseItem} config={cfg} expanded={true} onToggle={noop} />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(btn.getAttribute("data-expanded")).toBe("true");
    expect(btn.getAttribute("title")).toBeNull();
  });

  it("hovering or focusing the toggle warms the code-highlighter chunk (intent prefetch)", () => {
    // meiva:note
    // Regression coverage for the P2 adversarial-review finding on Meiva #3463: this wiring had no assertion that pointer-enter/focus actually invokes the prefetch, only that a mocked no-op did not blow up. Each event gets a fresh chunk and that chunk's import is the probe, so a header wired to anything else never calls it.
    // meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md
    const cfg = messageCardConfig(baseItem)!;

    const hoverImport = vi.fn(() => new Promise<never>(() => undefined));
    const hoverChunk = createLazyChunk({ name: "test-highlighter", importModule: hoverImport });
    const hover = renderWithQuery(
      <PythonCodeHighlighterProvider value={hoverChunk}>
        <CardHeader item={baseItem} config={cfg} expanded={false} onToggle={noop} />
      </PythonCodeHighlighterProvider>,
    );
    fireEvent.pointerEnter(hover.getByTestId("card-toggle"));
    expect(hoverImport).toHaveBeenCalledTimes(1);
    hover.unmount();

    const focusImport = vi.fn(() => new Promise<never>(() => undefined));
    const focusChunk = createLazyChunk({ name: "test-highlighter", importModule: focusImport });
    const focus = renderWithQuery(
      <PythonCodeHighlighterProvider value={focusChunk}>
        <CardHeader item={baseItem} config={cfg} expanded={false} onToggle={noop} />
      </PythonCodeHighlighterProvider>,
    );
    fireEvent.focus(focus.getByTestId("card-toggle"));
    expect(focusImport).toHaveBeenCalledTimes(1);
  });

  it("renders rich summary for system_prompt kind", () => {
    const sysItem = item("system_prompt", {
      payload: "line1\nline2\nline3",
    });
    const cfg = messageCardConfig(sysItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={sysItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("System prompt");
    expect(container.textContent).toContain("3 lines");
  });
});
