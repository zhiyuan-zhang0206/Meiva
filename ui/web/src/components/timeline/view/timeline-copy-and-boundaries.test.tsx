import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards, setUserSettings } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { formatItemTime } from "../timestamp";
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
  it("copies human inbound_chat raw body stripped of its envelope header", async () => {
    const { browser } = render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "User [2026-05-15]:\n\nhello raw body",
          }),
        ]}
      />,
    );
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(browser.actions.copied).toEqual(["hello raw body"]));
  });
});

// meiva:note
// Copy is a PER-KIND whitelist (card.tsx CardConfig.copy): a block offers it only
// when its payload is a message the reader can quote — the human's message and
// the agent's own reply. Thinking, tool calls, tool output, the system prompt,
// attachments and system notes are the agent's machinery, not its conversation,
// and carry no copy control anywhere in the block: neither the hover band
// (timeline/buttons.tsx) nor a body-level one (PythonCode / EnvelopeContent).
// A whitelist, not "everything except …", so a kind added later must opt in
// instead of inheriting a button (user 2026-10-29).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
describe("Copy is scoped to message text (per-kind whitelist)", () => {
  const cases: {
    label: string;
    item: BackendTimelineItem;
    copy: boolean;
  }[] = [
    {
      label: "human inbound_chat (human text)",
      item: makeItem({ kind: "inbound_chat", source: "user", payload: "hi" }),
      copy: true,
    },
    {
      label: "agent_chat (agent text)",
      item: makeItem({ kind: "agent_chat", payload: "done" }),
      copy: true,
    },
    {
      label: "inbound_chat from a peer agent (agent text)",
      item: makeItem({ kind: "inbound_chat", source: "agent:42", payload: "peer says hi" }),
      copy: true,
    },
    {
      label: "inbound_chat from the system (no author to quote)",
      item: makeItem({ kind: "inbound_chat", source: "scheduler", payload: "wake-up" }),
      copy: false,
    },
    {
      label: "agent_reasoning (thinking)",
      item: makeItem({ kind: "agent_reasoning", payload: "thinking" }),
      copy: false,
    },
    {
      label: "agent_code (tool call)",
      item: makeItem({ kind: "agent_code", payload: "print(1)" }),
      copy: false,
    },
    {
      label: "code_output (tool output)",
      item: makeItem({ kind: "code_output", payload: "stdout" }),
      copy: false,
    },
    {
      label: "system_marker (system note)",
      item: makeItem({ kind: "system_marker", source: "memory", payload: "memory updated" }),
      copy: false,
    },
    {
      label: "system_prompt",
      item: makeItem({ kind: "system_prompt", payload: "You are a test agent." }),
      copy: false,
    },
    {
      label: "attach",
      item: makeItem({ kind: "attach", payload: "attached file" }),
      copy: false,
    },
  ];

  for (const { label, item, copy } of cases) {
    it(`${label}: ${copy ? "offers" : "offers no"} copy control`, () => {
      render(<TimelineView items={[item]} />);
      revealCollapsedCards();
      const copies = screen.queryAllByRole("button", { name: /copy/i });
      expect(copies).toHaveLength(copy ? 1 : 0);
      if (copy) expect(screen.getByLabelText("Copy message")).toBeTruthy();
    });
  }

  it("a kind with neither Copy nor a stamp mounts no band (no empty hover strip)", () => {
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "system_prompt", payload: "prompt" })]} />,
    );
    revealCollapsedCards();
    const row = container.querySelector(".timeline-item");
    expect(row?.className).not.toContain("group/msgcard");
    expect(screen.queryByTestId("card-chrome")).toBeNull();
    expect(screen.getByTestId("card-toggle")).toBeTruthy();
  });

  it("secondary items such as thinking mount neither Copy nor a below-card timestamp", () => {
    const item = makeItem({ kind: "agent_reasoning", payload: "thinking" });
    render(<TimelineView items={[item]} />);
    revealCollapsedCards();
    expect(screen.queryByTestId("card-chrome")).toBeNull();
    expect(screen.queryAllByRole("button", { name: /copy/i })).toHaveLength(0);
  });

  it("a streaming primary reply keeps its timestamp and drops only the button", () => {
    const item = makeItem({ kind: "agent_chat", payload: "streaming", partial: true });
    render(<TimelineView items={[item]} />);
    const stamp = screen.getByText(formatItemTime(item.created_at!, { showWeekday: true }));
    expect(stamp.parentElement?.className).toContain("justify-end");
    expect(stamp.parentElement?.querySelectorAll("button")).toHaveLength(0);
  });
});

// meiva:note
// The hover band itself (position, reveal, focus, touch) is unchanged by the
// whitelist; these assertions pin that it still exists wherever a kind does
// offer Copy.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
describe("block-level copy actions (hover band)", () => {
  it("the footer holds Copy and no other control", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "ok" })]}
      />,
    );
    const band = screen.getByLabelText("Copy message").parentElement;
    expect(band?.querySelectorAll("button")).toHaveLength(1);
  });
  it("floats Copy inside expanded non-user-facing agent_chat without inflating bottom spacing", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "agent_reasoning", payload: "thought" }),
          makeItem({ item_id: "1.1", kind: "agent_chat", payload: "internal reply", user_facing: false }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "final reply", user_facing: true }),
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const copyButtons = screen.getAllByLabelText("Copy message");
    const band = copyButtons[0].parentElement;
    expect(band?.getAttribute("data-testid")).toBe("card-chrome");
    expect(band?.parentElement?.className).toContain("relative");
    expect(band?.parentElement?.className).toContain("pb-1.5");
    expect(band?.parentElement?.className).not.toContain("pb-5");
    expect(band?.className).toContain("bottom-1");
    expect(band?.className).toContain("right-1");
    expect(band?.className).toContain("justify-end");
    expect(band?.className).not.toContain("top-1");
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
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "user", payload: "hi" }),
          makeItem({ item_id: "2.0", kind: "agent_chat", payload: "ok" }),
        ]}
      />,
    );
    const [humanCopy, agentCopy] = screen.getAllByLabelText("Copy message");
    expect(humanCopy.parentElement?.className).toContain("justify-end");
    const band = agentCopy.parentElement;
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
    revealCollapsedCards();
    const bodyEl = screen.getByText(/body-200-boundary/);
    expect(bodyEl.textContent).toContain(header);
  });
  it("idx === 199 (\\n\\n exactly at char 199) → splits into header + body normally", () => {
    const header = "h".repeat(199);
    const payload = `${header}\n\nbody-199`;
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    revealCollapsedCards();
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

describe("formatItemTime: clock format without zone name", () => {
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
  it("renders a 12-hour clock with no zone name when the viewer selects 12h", () => {
    setUserSettings({ "display.clock_format": "12h" });
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
    expect(screen.getByText("2026-05-15 Fri 12:00:00 PM")).toBeTruthy();
  });
});

describe("turnActive transition expansion", () => {
  it("rerender from turnActive=true to false collapses the trailing detail turn while keeping primary items visible", () => {
    const items = [
      makeItem({ item_id: "1", kind: "agent_chat", payload: "first" }),
      makeItem({ item_id: "2", kind: "agent_code", payload: "last_code()" }),
    ];
    const { rerender } = render(<TimelineView turnActive items={items} />);
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.getByTestId("python-code")).toBeTruthy();
    rerender(<TimelineView items={items} />);
    expect(screen.getByText("first")).toBeTruthy();
    expect(screen.queryByTestId("python-code")).toBeNull();
  });
});

describe("ItemView memo comparator: streaming prop flip is visible", () => {
  it("streamingCode false → true rerender, last agent_code data-streaming should update to 1", () => {
    const items = [makeItem({ kind: "agent_code", payload: "x" })];
    const { rerender } = render(<TimelineView turnActive items={items} streamingCode={false} />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
    rerender(<TimelineView turnActive items={items} streamingCode />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
  });
  it("streamingCode true → false rerender, last agent_code data-streaming should update to 0", () => {
    const items = [makeItem({ kind: "agent_code", payload: "x" })];
    const { rerender } = render(<TimelineView turnActive items={items} streamingCode />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
    rerender(<TimelineView turnActive items={items} streamingCode={false} />);
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
    revealCollapsedCards();
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
