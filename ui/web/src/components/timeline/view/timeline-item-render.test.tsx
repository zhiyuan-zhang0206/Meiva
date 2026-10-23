import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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
describe("ItemView: agent_chat", () => {
  it("partial=false → does not show 'streaming…' marker", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "hello" })]} />,
    );
    expect(screen.queryByText(/streaming…/)).toBeNull();
    expect(screen.queryByText(/streaming interrupted/i)).toBeNull();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("hello");
  });
  it("partial=true → shows 'streaming…' label + ellipsis prefix", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "in progress", partial: true })]}
      />,
    );
    expect(screen.getByText(/streaming/)).toBeTruthy();
  });
  it("interrupted=true → shows 'streaming interrupted' label + InterruptedNotice", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_chat",
            payload: "broke",
            partial: true,
            interrupted: true,
          }),
        ]}
      />,
    );
    expect(screen.getAllByText(/streaming interrupted/i).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/reconnects resume receiving/)).toBeTruthy();
  });
});

describe("ItemView: agent_code + agent_reasoning", () => {
  it("agent_code renders PythonCode below an always-visible toggle chip", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
      />,
    );
    expect(screen.getByTestId("python-code").textContent).toBe("print(1)");
    expect(screen.getByText(/1 line of code/)).toBeTruthy();
  });
  it("agent_code streamingCode=true on last item → PythonCode streaming=1", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "x" })]}
        streamingCode
      />,
    );
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
  });
  it("agent_reasoning renders markdown by default below a 'Thinking' toggle chip", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_reasoning", payload: "thinking..." })]}
      />,
    );
    expect(screen.getByTestId("chat-markdown").textContent).toBe("thinking...");
    expect(screen.getByText("Thinking")).toBeTruthy();
  });
  it("agent_reasoning renders raw text when markdown rendering is disabled", () => {
    setUserSettings({ "display.render_reasoning_markdown": false });
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_reasoning", payload: "raw thinking" })]}
      />,
    );
    expect(screen.queryByTestId("chat-markdown")).toBeNull();
    expect(screen.getByText("raw thinking").tagName).toBe("PRE");
  });
});

describe("ItemView: system_prompt", () => {
  const PROMPT = "You are Meiva.\nAct via execute_code.\nThird line.";
  it("collapsed by default in 'none' mode: turn wraps the prompt unlabeled, body is hidden", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: PROMPT, created_at: null })]}
      />,
    );
    expect(screen.queryByText("system prompt")).toBeNull();
    expect(screen.getByTestId("turn-toggle")).toBeTruthy();
    expect(screen.queryByText(/Act via execute_code/)).toBeNull();
    expect(screen.queryByText("· 3 lines")).toBeNull();
  });
  it("clicking the turn toggle opens the prompt card collapsed; a second click expands it", () => {
    setToggleState({ detailsMode: "none" });
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: PROMPT, created_at: null })]}
      />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByText("· 3 lines")).toBeTruthy();
    expect(screen.queryByText(/Act via execute_code/)).toBeNull();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.getByText(/Act via execute_code/)).toBeTruthy();
  });
  it("singular 'line' for a one-line prompt", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: "just one line", created_at: null })]}
      />,
    );
    expect(screen.getByText("· 1 line")).toBeTruthy();
  });
});

describe("toggle chip: summary on expanded items", () => {
  it("committed thinking chip shows 'Thought for' + tokens exactly once", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            reasoning_ms: 8200,
            reasoning_tokens: 1234,
          }),
        ]}
      />,
    );
    expect(screen.getAllByText(/Thought for 8s/)).toHaveLength(1);
    expect(screen.getByText(/1\.2k tokens/)).toBeTruthy();
    expect(screen.getByText("ponder")).toBeTruthy();
  });
  it("streaming thinking chip shows live 'Thinking for'", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            reasoningStartedAt: Date.now() - 3000,
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Thinking for/)).toBeTruthy();
  });
  it("frozen thinking chip (reasoningElapsedMs, no reasoning_ms yet) shows 'Thought for'", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            reasoningElapsedMs: 4000,
          }),
        ]}
      />,
    );
    expect(screen.getAllByText(/Thought for 4s/)).toHaveLength(1);
    expect(screen.queryByText(/Thinking for/)).toBeNull();
  });
  it("interrupted streaming thinking does not tick", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_reasoning",
            payload: "ponder",
            partial: true,
            interrupted: true,
            reasoningStartedAt: Date.now() - 3000,
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Thinking for/)).toBeNull();
  });
  it("thinking with no timing data falls back to a plain 'Thinking' label", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_reasoning", payload: "ponder" })]} />,
    );
    expect(screen.queryByText(/Thought for|Thinking for|tokens/)).toBeNull();
    expect(screen.getByText("Thinking")).toBeTruthy();
  });
  it("agent_code chip shows the recorded per-method SDK histogram", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "agent_code",
            payload: "meiva.files.read('a')\nmeiva.files.read('b')\nmeiva.shell.run()",
            sdk_calls: [
              { method: "files.read", count: 2 },
              { method: "shell.run", count: 1 },
            ],
          }),
        ]}
      />,
    );
    expect(screen.getAllByText("files.read")).toHaveLength(2);
    expect(screen.getAllByText("shell.run")).toHaveLength(2);
    expect(screen.getAllByText(/× 2/)).toHaveLength(2);
  });
  it("plain-python code chip shows no histogram", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_code", payload: "x = 1\nprint(x)" })]} />,
    );
    expect(screen.queryByText(/×/)).toBeNull();
  });
  it("code_output chip shows 'ran in Xs' exactly once when exec_ms present", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "Code execution output [t]:\n\nstdout: hi",
            exec_ms: 1300,
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Ran in 1s/)).toBeTruthy();
  });
  it("code_output without exec_ms shows no 'ran in' meta", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "code_output", payload: "stdout: hi" })]}
      />,
    );
    expect(screen.queryByText(/Ran in/)).toBeNull();
  });
  it("streaming code_output chip shows live 'Running for' when execStartedAt is set", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "stdout: hi",
            execStartedAt: Date.now() - 3000,
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Running for/)).toBeTruthy();
  });
  it("code_output with execStartedAt but interrupted shows no live clock", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "stdout: hi",
            execStartedAt: Date.now() - 3000,
            interrupted: true,
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Running for/)).toBeNull();
  });
});

describe("ItemView: inbound_chat source-driven color", () => {
  function getBorderEl(payloadText: string) {
    revealCollapsedCards();
    const txt = screen.getByText(payloadText);
    const accent = txt.closest(".timeline-item")?.querySelector('[data-testid="card-accent"]');
    if (!accent) throw new Error("no card accent");
    return accent as HTMLElement;
  }
  it("source='agent:5' → violet border (from another agent)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "agent:5",
            payload: "from agent 5",
          }),
        ]}
      />,
    );
    expect(getBorderEl("from agent 5").className).toContain("border-violet");
  });
  it("source='user' → gray border (frontend user)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "from web",
          }),
        ]}
      />,
    );
    expect(getBorderEl("from web").className).toContain("border-gray");
  });
  it("source='kernel' → sky border (system-injected)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "kernel",
            payload: "from kernel",
          }),
        ]}
      />,
    );
    expect(getBorderEl("from kernel").className).toContain("border-sky");
  });
  it("source=null → sky border (default branch)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "inbound_chat", source: null, payload: "no source" }),
        ]}
      />,
    );
    expect(getBorderEl("no source").className).toContain("border-sky");
  });
});

describe("ItemView: Markdown-authored inbound bodies", () => {
  it("compact summary body goes through ChatMarkdown", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "inbound_compact_summary", payload: "Compact summary [ts]:\n\n## Goal" })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("## Goal");
  });
  it("inter-agent and human inbound_chat bodies go through ChatMarkdown", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "1.0", kind: "inbound_chat", source: "agent:7", payload: "Agent 7:\n\nsee **this**" }),
          makeItem({ item_id: "2.0", kind: "inbound_chat", source: "user", payload: "User:\n\nrender **markdown**" }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getAllByTestId("chat-markdown").map((n) => n.textContent)).toEqual([
      "see **this**",
      "render **markdown**",
    ]);
  });
  it("system_prompt and system_marker bodies go through ChatMarkdown", () => {
    render(
      <TimelineView
        items={[
          makeItem({ item_id: "0.0", kind: "system_prompt", payload: "# Role\n\nFollow **rules**" }),
          makeItem({ item_id: "1.0", kind: "system_marker", source: "memory", payload: "- [Title](a.md)" }),
          makeItem({ item_id: "2.0", kind: "system_marker", source: "sdk_hint", payload: "use **markdown** text" }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getAllByTestId("chat-markdown").map((n) => n.textContent)).toEqual([
      "# Role\n\nFollow **rules**",
      "- [Title](a.md)",
      "use **markdown** text",
    ]);
  });
});

describe("ItemView: inbound_chat envelope split", () => {
  it("payload contains '<header>\\n\\n<body>' → header and body render separately", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "User [2026-05-15]:\n\nhello world",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/User/)).toBeTruthy();
    expect(screen.getByText("hello world")).toBeTruthy();
  });
  it("payload without '\\n\\n' → entire payload is body, no header", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "inbound_chat", source: "system", payload: "no envelope" }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("no envelope")).toBeTruthy();
  });
});

describe("ItemView: code_output", () => {
  it("envelope split — header + body", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "code_output",
            payload: "Code execution output [2026-05-15]:\n\nstdout: hi",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Code execution output/)).toBeTruthy();
    expect(screen.getByText(/stdout: hi/)).toBeTruthy();
  });
});

describe("ItemView: configurable timeline colors (tasks #3304/#3312)", () => {
  it("a seeded color override drives the rendered marker classes", () => {
    setUserSettings({ "display.color.note": "fuchsia" });
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "system_marker",
            source: "sdk_hint",
            payload: "[note] palette override",
          }),
        ]}
      />,
    );
    const accent = screen.getByText("Note").closest(".timeline-item")?.querySelector('[data-testid="card-accent"]');
    expect(accent?.className).toContain("border-fuchsia-500/40");
    expect(accent?.parentElement?.className).toContain("bg-fuchsia-50/40");
  });
});
