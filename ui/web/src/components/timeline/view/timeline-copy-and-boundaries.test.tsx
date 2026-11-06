import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards, setUserSettings } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
import { resetTransport, transport } from "@/test-support/fake-api";
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
      <TimelineView items={[makeItem({ kind: "agent_chat", user_facing: false, payload: "x" })]} />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
  });
  it("after 1s reverts to 'copy' automatically (timer fires)", async () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", user_facing: false, payload: "x" })]} />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    fireEvent.click(screen.getByLabelText("Copy message"));
    await waitFor(() => expect(screen.getByLabelText("Copied")).toBeTruthy());
    await waitFor(() => expect(screen.getByLabelText("Copy message")).toBeTruthy(), {
      timeout: 1500,
    });
  });
});

describe("Copy is scoped to message text (per-kind whitelist)", () => {
  const cases: {
    label: string;
    item: BackendTimelineItem;
    copy: boolean;
  }[] = [
    {
      label: "human inbound_chat (human text)",
      item: makeItem({ kind: "inbound_chat", source: "user", payload: "hi" }),
      copy: false,
    },
    {
      label: "agent_chat (primary agent text)",
      item: makeItem({ kind: "agent_chat", payload: "done" }),
      copy: false,
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

});

describe("timeline message actions", () => {
  it("marks a visible publication read without rendering floating actions", async () => {
    resetTransport();
    transport.resolveUserMessage.mockResolvedValue({ status: "idling", inbound_id: null });
    let reportIntersection: IntersectionObserverCallback | undefined;
    const observer = { observe: vi.fn<(node: Element) => void>(), disconnect: vi.fn() };
    vi.stubGlobal("IntersectionObserver", class {
      constructor(callback: IntersectionObserverCallback) {
        reportIntersection = callback;
      }
      observe = observer.observe;
      disconnect = observer.disconnect;
    });
    const item: BackendTimelineItem = {
      ...makeItem({ kind: "agent_chat", payload: "Published reply" }),
      userDelivery: {
        id: 12, agent_id: 7, mode: "prefix", source_key: "assistant:reply:0.0",
        content: "Published reply", suggestions: [], created_at: "2026-11-06T00:00:00Z", read_at: null,
      },
    };
    const { container } = render(<TimelineView items={[item]} />);
    const row = screen.getByTestId("agent-message-prose").closest(".timeline-item");
    const [anchor] = observer.observe.mock.calls.flat();
    expect(row?.contains(anchor)).toBe(true);
    expect(screen.queryByTestId("card-chrome")).toBeNull();
    expect(screen.queryByRole("button", { name: "Unread · mark read" })).toBeNull();
    expect(transport.resolveUserMessage).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Published reply");

    const report = (intersectionRatio: number) => reportIntersection?.(
      [{ isIntersecting: true, intersectionRatio } as IntersectionObserverEntry],
      observer as unknown as IntersectionObserver,
    );
    report(0);
    expect(transport.resolveUserMessage).not.toHaveBeenCalled();
    report(1);
    await waitFor(() => expect(transport.resolveUserMessage).toHaveBeenCalledWith(
      7, 12, { action: "read" }, expect.any(String),
    ));
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
        items={[makeItem({ kind: "agent_chat", user_facing: false, payload: "ok" })]}
      />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const copyBtn = screen.getByLabelText("Copy message").closest("button");
    expect(copyBtn?.hasAttribute("title")).toBe(false);
    expect(screen.getByLabelText("Copy message")).toBeTruthy();
  });

  // meiva:note
  // The reveal itself is CSS (`group-hover`/`group-focus-within`) and jsdom has no paint engine to evaluate it, so the tests pin the structure the browser turns into the behavior: hidden means no paint AND no hit-testing, focus inside the card reveals it for keyboard users, and a touch brings it up for a few seconds because a phone has no hover to gate on.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  it("Details Copy is hidden and click-through until the card is hovered or focused", () => {
    render(<TimelineView items={[makeItem({ kind: "agent_chat", user_facing: false, payload: "ok" })]} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
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
  it("reveals Details Copy when the block is touched", () => {
    // meiva:note
    // A phone has no hover, so touching the Details block reveals Copy briefly.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
    render(<TimelineView items={[makeItem({ kind: "agent_chat", user_facing: false, payload: "ok" })]} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
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
    render(<TimelineView items={[makeItem({ kind: "agent_chat", user_facing: false, payload: "ok" })]} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const band = screen.getByLabelText("Copy message").parentElement;
    const row = screen.getByLabelText("Copy message").closest(".timeline-item");
    expect(row).not.toBeNull();
    fireEvent.pointerDown(row as HTMLElement, { pointerType: "mouse" });
    expect(band?.className).toContain("opacity-0");
  });

});

describe("splitEnvelope boundary", () => {
  it("code_output never splits the first line into a separate header even when \\n\\n is < 200 chars", () => {
    const header = "h".repeat(199);
    const payload = `${header}\n\nbody-199`;
    render(
      <TimelineView items={[makeItem({ kind: "code_output", payload })]} />,
    );
    revealCollapsedCards();
    const bodyEl = screen.getByText(/body-199/);
    expect(bodyEl.textContent).toBe(payload);
  });
  it("human inbound_chat strips a single-line leading header when \\n\\n is < 200 chars and keeps it at >= 200 chars", () => {
    const shortHeader = `[${"2026-05-15 ".padEnd(197, "0")}]`;
    const longHeader = `[${"2026-05-15 ".padEnd(198, "0")}]`;
    const { rerender } = render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: `${shortHeader}\n\nbody-199`,
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("user-message-bubble").textContent).toBe("body-199");

    rerender(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: `${longHeader}\n\nbody-200`,
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("user-message-bubble").textContent).toBe(
      `${longHeader}\n\nbody-200`,
    );
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

describe("turnActive transition expansion", () => {
  it("rerender from turnActive=true to false collapses the trailing detail turn while keeping primary items visible", () => {
    setUserSettings({ "display.expand_working_block": true });
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
    setUserSettings({ "display.expand_working_block": true });
    const items = [makeItem({ kind: "agent_code", payload: "x" })];
    const { rerender } = render(<TimelineView turnActive items={items} streamingCode={false} />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("0");
    rerender(<TimelineView turnActive items={items} streamingCode />);
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
  });
  it("streamingCode true → false rerender, last agent_code data-streaming should update to 0", () => {
    setUserSettings({ "display.expand_working_block": true });
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
