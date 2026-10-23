import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, PythonCodeMock, ScrollAreaMock, cleanupTimelineTest, displayLimits, makeItem, mockSettings, resetStore, revealCollapsedCards, setToggleState, setUserSettings, toggleState, userSettingsState } from "./timeline-test-harness";
vi.mock("@/lib/format/display-limits", async () => {
  const { useSyncExternalStore } = await import("react");
  return {
    useDisplayLimit: (envVar: string, fallback: number) => {
      useSyncExternalStore(mockSettings.subscribe, mockSettings.version);
      return displayLimits.get(envVar) ?? fallback;
    },
  };
});
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/content/python-code", () => ({
  PythonCode: PythonCodeMock,
  preloadPythonCodeHighlighter: () => Promise.resolve(),
}));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
vi.mock("@/lib/layout/content-toggle-store", async () => {
  const { useSyncExternalStore } = await import("react");
  return {
    useContentToggle: () => {
      useSyncExternalStore(mockSettings.subscribe, mockSettings.version);
      toggleState.reads += 1;
      return {
        detailsMode: toggleState.detailsMode,
        setDetailsMode: vi.fn(),
        isLoading: toggleState.isLoading,
      };
    },
    useContentToggleReset: (selector?: (s: typeof resetStore) => unknown) =>
      selector ? selector(resetStore) : resetStore,
  };
});
vi.mock("@/lib/state/use-user-settings", () => ({
  useUserSettings: () => ({
    settings: userSettingsState,
    setSetting: vi.fn(),
    isLoading: false,
  }),
}));
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);
describe("CopyButton feedback state machine", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      writable: true,
      configurable: true,
    });
  });
  it("click copy → 'copy' flips immediately to 'copied'", async () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
  });
  it("after 1s reverts to 'copy' automatically (timer fires)", async () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />,
    );
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
    await waitFor(() => expect(screen.getByLabelText("Copy message")).toBeTruthy(), {
      timeout: 1500,
    });
  });
});

// meiva:note
// The icon-only CopyButton from ./copy-button (aria-label "Copy <label>", no visible text) is separate from the block-level copy button (timeline/buttons.tsx, covered by the "block-level copy actions" describe block below). agent_code (via PythonCode) and code_output (via EnvelopeContent showCopy) render ONLY the icon-only per-block copy — the card-level block copy is skipped for these kinds to avoid duplicate overlapping buttons that copy the same payload. This block only guards the icon-only one; it must not appear under a still-partial agent_chat (no code/output content to speak of) or an ephemeral system_marker (no card).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
describe("CopyButton render guard (icon-only, code/output-scoped)", () => {
  it("agent_code item shows copy button (via PythonCode)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
      />,
    );
    expect(screen.getByRole("button", { name: /copy code/i })).toBeTruthy();
  });
  it("code_output item shows copy button (via EnvelopeContent showCopy)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "code_output", payload: "stdout" })]}
      />,
    );
    expect(screen.getByRole("button", { name: /copy command output/i })).toBeTruthy();
  });
  it("system_marker item shows no copy button", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", payload: "exec_start" })]}
      />,
    );
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
  it("partial agent_chat (in progress) shows no copy button", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "streaming", partial: true })]}
      />,
    );
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
});

// meiva:note
// The general block-level copy (text "copy", timeline/buttons.tsx) attaches inside EVERY card's bottom-right corner via MessageCard's `actions` overlay — any kind that renders a card (messageCardConfig !== null), once expanded and no longer partial. It mounts only while eligible — a collapsed card has no copy button in the DOM at all (not just hidden).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
describe("block-level copy actions (MessageCard overlay)", () => {
  it("human inbound_chat (a user message) shows the copy button", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "inbound_chat", source: "user", payload: "hi" })]}
      />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });
  it("agent_reasoning shows the block-level copy button once expanded", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_reasoning", payload: "thinking" })]}
      />,
    );
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });
  it("agent_code has only the icon-only body copy (card-level duplicate suppressed)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
      />,
    );
    expect(screen.getByRole("button", { name: /copy code/i })).toBeTruthy();
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
  it("code_output has only the icon-only body copy (card-level duplicate suppressed)", () => {
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload: "stdout" })]} />,
    );
    expect(screen.getByRole("button", { name: /copy command output/i })).toBeTruthy();
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
  it("collapsed-by-default card (system_prompt) shows no copy button until the turn is expanded", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView items={[makeItem({ kind: "system_prompt", payload: "line1\nline2" })]} />,
    );
    expect(screen.queryByLabelText("Copy message")).toBeNull();
    // meiva:note
    // None mode (#4780): opening the turn keeps the inner card collapsed, and copy attaches to expanded cards only — it appears on the CARD's own click, not the turn's.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.queryByLabelText("Copy message")).toBeNull();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.queryByLabelText("Copy message")).toBeNull();
  });
  it("the footer holds Copy and no other control", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
      />,
    );
    const band = screen.getByLabelText("Copy message").parentElement;
    expect(band?.querySelectorAll("button")).toHaveLength(1);
  });
  it("copy button carries no title tooltip (aria-label only)", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
      />,
    );
    const copyBtn = screen.getByLabelText("Copy message").closest("button");
    expect(copyBtn?.hasAttribute("title")).toBe(false);
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });

  // meiva:note
  // The reveal itself is CSS (`group-hover`/`group-focus-within`) and jsdom has no paint engine to evaluate it, so the tests pin the structure the browser turns into the behavior: hidden means no paint AND no hit-testing, focus inside the card reveals it for keyboard users, and a touch brings it up for a few seconds because a phone has no hover to gate on.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  it("footer is hidden and click-through until the card is hovered or focused", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "ok" })]} />,
    );
    const band = screen.getByLabelText("Copy message").parentElement;
    expect(band?.className).toContain("justify-end");
    expect(band?.className).toContain("opacity-0");
    expect(band?.className).toContain("pointer-events-none");
    expect(band?.className).toContain("group-hover/msgcard:opacity-100");
    expect(band?.className).toContain("group-hover/msgcard:pointer-events-auto");
    expect(band?.className).toContain("group-focus-within/msgcard:opacity-100");
    expect(band?.className).toContain("group-focus-within/msgcard:pointer-events-auto");
    // meiva:note
    // Nothing keeps it painted on a coarse pointer: a phone reveals it by touch.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
    expect(band?.className).not.toContain("pointer-coarse:opacity-100");
  });
  it("reveals the footer when the block is touched, and only then", () => {
    // meiva:note
    // A phone has no hover, so touching the block brings the time and Copy up for
    // a few seconds; leaving the band painted put chrome under every message for
    // good (user 2026-10-22).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "ok" })]} />);
    const band = screen.getByLabelText("Copy message").parentElement;
    const row = screen.getByLabelText("Copy message").closest(".timeline-item");
    expect(row).not.toBeNull();
    expect(band?.className).toContain("opacity-0");
    fireEvent.pointerDown(row as HTMLElement, { pointerType: "touch" });
    expect(band?.className).toContain("opacity-100");
    expect(band?.className).toContain("pointer-events-auto");
    fireEvent.pointerDown(row as HTMLElement, { pointerType: "mouse" });
    expect(band?.className).toContain("opacity-100");
  });
  it("keeps the band hidden when a mouse presses the block", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "ok" })]} />);
    const band = screen.getByLabelText("Copy message").parentElement;
    const row = screen.getByLabelText("Copy message").closest(".timeline-item");
    expect(row).not.toBeNull();
    fireEvent.pointerDown(row as HTMLElement, { pointerType: "mouse" });
    expect(band?.className).toContain("opacity-0");
  });
  it("sizes the Copy glyph to the band it shares with the stamp", () => {
    // meiva:note
    // The stamp is 9px and the band 12px; at the body's size-3 the glyph read as
    // visibly bigger than the time beside it (user 2026-10-22).
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "ok" })]} />);
    const icon = screen.getByLabelText("Copy message").querySelector("svg");
    expect(icon?.getAttribute("class")).toContain("size-2.5");
  });
});

describe("splitEnvelope boundary", () => {
  it("idx === 200 (\\n\\n exactly at char 200) → entire payload is body, no header", () => {
    const header = "h".repeat(200);
    const payload = `${header}\n\nbody-200-boundary`;
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    const bodyEl = screen.getByText(/body-200-boundary/);
    expect(bodyEl.textContent).toContain(header);
  });
  it("idx === 199 (\\n\\n exactly at char 199) → splits into header + body normally", () => {
    const header = "h".repeat(199);
    const payload = `${header}\n\nbody-199`;
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    const bodyEl = screen.getByText("body-199");
    expect(bodyEl.textContent).toBe("body-199");
  });
});

describe("UnknownMarkerChip source render", () => {
  it("source=null → renders 'source = null' (no quotes)", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[
            makeItem({ kind: "system_marker", source: null, payload: "wat" }),
          ]}
        />,
      );
      expect(screen.getByText(/source = null$/)).toBeTruthy();
    } finally {
      warnSpy.mockRestore();
    }
  });
  it("source is a string → renders JSON.stringify (with double quotes)", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[
            makeItem({
              kind: "system_marker",
              source: "future:kind",
              payload: "wat",
            }),
          ]}
        />,
      );
      expect(screen.getByText(/source = "future:kind"/)).toBeTruthy();
    } finally {
      warnSpy.mockRestore();
    }
  });
});

describe("formatItemTime: clock format and zone name", () => {
  it("renders 24-hour text with no zone name by default", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "x",
            created_at: "2026-05-15T12:00:00Z",
          }),
        ]}
      />,
    );
    expect(screen.getByText("2026-05-15 Fri 12:00:00")).toBeTruthy();
  });
  it("renders a 12-hour clock and the zone name when the viewer turns them on", () => {
    setUserSettings({ "display.clock_format": "12h", "display.show_timezone": true });
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "x",
            created_at: "2026-05-15T12:00:00Z",
          }),
        ]}
      />,
    );
    expect(screen.getByText("2026-05-15 Fri 12:00:00 PM UTC")).toBeTruthy();
  });
});

describe("ContentToggle deps completeness (mid-mount toggle)", () => {
  it("rerender with detailsMode flip → visibleItems re-filtered", () => {
    setToggleState({ detailsMode: "all" });
    const items = [
      makeItem({ item_id: "1", kind: "agent_chat", payload: "chat" }),
      makeItem({ item_id: "2", kind: "agent_reasoning", payload: "deep thought" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    expect(screen.getByText("chat")).toBeTruthy();
    expect(screen.getByText("deep thought")).toBeTruthy();
    setToggleState({ detailsMode: "none" });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("chat")).toBeTruthy();
    expect(screen.queryByText("deep thought")).toBeNull();
  });
  it('rerender with detailsMode "last" → primary items always visible, detail turn collapsed without turnActive', () => {
    setToggleState({ detailsMode: "all" });
    const items = [
      makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
      makeItem({ item_id: "2", kind: "agent_code", payload: "last_code()" }),
    ];
    const { rerender } = render(<TimelineView items={items} />);
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.getByTestId("python-code")).toBeTruthy();
    setToggleState({ detailsMode: "last" });
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.queryByTestId("python-code")).toBeNull();
  });
  it('detailsMode="last" with turnActive → last detail turn auto-expanded', () => {
    setToggleState({ detailsMode: "last" });
    render(
      <TimelineView
        turnActive
        items={[
          makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
          makeItem({ item_id: "2", kind: "agent_code", payload: "streaming_code()" }),
        ]}
      />,
    );
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.getByTestId("python-code")).toBeTruthy();
  });
});

describe("ItemView memo comparator: streaming prop flip is visible", () => {
  it("streamingCode false → true rerender, last agent_code data-streaming should update to 1", () => {
    const items = [makeItem({ kind: "agent_code", payload: "x" })];
    const { rerender } = render(<TimelineView items={items} streamingCode={false} />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
    rerender(<TimelineView items={items} streamingCode />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
  });
  it("streamingCode true → false rerender, last agent_code data-streaming should update to 0", () => {
    const items = [makeItem({ kind: "agent_code", payload: "x" })];
    const { rerender } = render(<TimelineView items={items} streamingCode />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
    rerender(<TimelineView items={items} streamingCode={false} />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
  });
});

describe("system_marker error prefix boundary", () => {
  it("payload='error:' (empty content) → still goes to error branch ([error] empty)", () => {
    render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "error:" })]} />,
    );
    expect(screen.getByText(/error/)).toBeTruthy();
  });
  it("payload contains 'error' but no colon → skips error branch, goes to UnknownMarkerChip", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(
        <TimelineView
          items={[makeItem({ kind: "system_marker", payload: "error_no_colon" })]}
        />,
      );
      expect(screen.getByText(/Unrecognized system_marker/)).toBeTruthy();
    } finally {
      warnSpy.mockRestore();
    }
  });
});

describe("splitEnvelope idx === 0 boundary", () => {
  it("payload starts with \\n\\n → body keeps leading \\n\\n (entire payload as body), no split", () => {
    const payload = "\n\nthe-body-text";
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    const bodyEl = screen.getByText(/the-body-text/);
    expect(bodyEl.textContent).toBe("\n\nthe-body-text");
  });
});

// meiva:note
// The pin is driven by the thread key alone, consumed by one useLayoutEffect;
// a send reaches the timeline only as ordinary content growth. jsdom does not
// render scroll, so the pin itself stays covered by sticky.test.ts.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.meiva.okf.md
describe("force-scroll: renders without a scroll-token prop", () => {
  it("mounts and renders content (the pin needs no prop)", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", payload: "x" })]} />);
    expect(screen.getByText("x")).toBeTruthy();
  });
});

describe("EphemeralMarker isError path", () => {
  it("payload='error:msg' → renders text-destructive class (red error)", () => {
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "error:failed" })]} />,
    );
    const errEl = container.querySelector(".text-destructive");
    if (!errEl) throw new Error("no destructive el");
    expect(errEl.textContent).toMatch(/error.*failed/);
  });
  it("payload='exec_start' → unrecognized, renders text-destructive (red alarm)", () => {
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "exec_start" })]} />,
    );
    const errEl = container.querySelector(".text-destructive");
    expect(errEl).toBeTruthy();
  });
  it("payload='compact_done' → no text-destructive rendered (renders nothing)", () => {
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "system_marker", payload: "compact_done" })]} />,
    );
    expect(container.querySelector(".text-destructive")).toBeNull();
    expect(screen.queryByText(/compact/)).toBeNull();
  });
});

describe("LifecycleChip payload render", () => {
  it("terminate chip fully renders payload string", () => {
    const payload = "[2026-05-15 12:00:00 PDT] You are terminated by user";
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", source: "lifecycle_terminate", payload })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(payload)).toBeTruthy();
  });
  it("restart chip fully renders payload string", () => {
    const payload = "[ts] Restart was accepted from yourself";
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", source: "lifecycle_restart", payload })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(payload)).toBeTruthy();
  });
  it("resurrect chip fully renders payload string", () => {
    const payload = "[ts] You were resurrected by user";
    render(
      <TimelineView
        items={[makeItem({ kind: "system_marker", source: "lifecycle_resurrect", payload })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(payload)).toBeTruthy();
  });
});
