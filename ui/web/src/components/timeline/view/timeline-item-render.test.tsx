import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, makeItem, render, revealCollapsedCards } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);
describe("ItemView: agent_chat", () => {
  it("partial=false → renders unboxed canvas prose with no streaming marker", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_chat", payload: "hello" })]} />,
    );
    expect(screen.getByTestId("agent-message-prose")).toBeTruthy();
    expect(screen.queryByTestId("streaming-cursor")).toBeNull();
    expect(screen.queryByText(/streaming…/)).toBeNull();
    expect(screen.queryByText(/streaming interrupted/i)).toBeNull();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("hello");
  });
  it("partial=true → renders normal prose + pulsing streaming cursor without ellipsis prefix", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_chat", payload: "in progress", partial: true })]}
      />,
    );
    expect(screen.getByTestId("chat-markdown").textContent).toBe("in progress");
    expect(screen.getByTestId("streaming-cursor")).toBeTruthy();
    expect(screen.queryByText(/streaming…/)).toBeNull();
  });
  it("interrupted=true → shows InterruptedNotice and drops streaming cursor", () => {
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
    expect(screen.queryByTestId("streaming-cursor")).toBeNull();
    expect(screen.getAllByText(/streaming interrupted/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/reconnects resume receiving/)).toBeTruthy();
  });
});

describe("ItemView: agent_code + agent_reasoning", () => {
  it("agent_code renders PythonCode below a monochrome 'Code' toggle chip without a line count", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_code", payload: "print(1)" })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByTestId("python-code").textContent).toBe("print(1)");
    expect(screen.getByText("Code")).toBeTruthy();
    expect(screen.queryByText(/line of code/)).toBeNull();
  });
  it("keeps the highlighter pending, so a code block stays plain source", async () => {
    // meiva:note
    // The shared wrapper injects a chunk whose import never resolves. A macrotask is long enough for the real prism chunk to load and tokenize, so a wrapper that lost its provider would show .token spans here — this is what keeps every timeline test reading plain source.
    // meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md
    const { container } = render(
      <TimelineView items={[makeItem({ kind: "agent_code", payload: "print(1)\nprint(2)" })]} />,
    );
    revealCollapsedCards();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(container.querySelector("[data-testid='python-code'] .token")).toBeNull();
    expect(screen.getByTestId("python-code").textContent).toBe("print(1)\nprint(2)");
  });

  it("agent_code streamingCode=true on last item → PythonCode streaming=1", () => {
    render(
      <TimelineView
        turnActive
        items={[makeItem({ kind: "agent_code", payload: "x" })]}
        streamingCode
      />,
    );
    expect(screen.getByTestId("python-code").getAttribute("data-streaming")).toBe("1");
  });
  it("agent_reasoning renders markdown below a 'Thinking' toggle chip", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "agent_reasoning", payload: "thinking..." })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByTestId("chat-markdown").textContent).toBe("thinking...");
    expect(screen.getByText("Thinking")).toBeTruthy();
  });
});

describe("ItemView: system_prompt", () => {
  const PROMPT = "You are Meiva.\nAct via execute_code.\nThird line.";
  it("collapsed by default: turn wraps the prompt unlabeled, body is hidden", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: PROMPT, created_at: null })]}
      />,
    );
    expect(screen.queryByText("system prompt")).toBeNull();
    expect(screen.getByTestId("turn-toggle")).toBeTruthy();
    expect(screen.queryByText(/Act via execute_code/)).toBeNull();
    expect(screen.queryByText(/lines/)).toBeNull();
  });
  it("clicking the turn toggle opens the prompt card collapsed without a line count; a second click expands it", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "system_prompt", payload: PROMPT, created_at: null })]}
      />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    expect(screen.getByText("System prompt")).toBeTruthy();
    expect(screen.queryByText(/lines/)).toBeNull();
    expect(screen.queryByText(/Act via execute_code/)).toBeNull();
    fireEvent.click(screen.getByTestId("card-toggle"));
    expect(screen.getByText(/Act via execute_code/)).toBeTruthy();
  });
});

describe("toggle chip: summary on expanded items", () => {
  it("committed thinking chip shows 'Thought for' without a token count", () => {
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
    revealCollapsedCards();
    expect(screen.getAllByText(/Thought for 8s/)).toHaveLength(1);
    expect(screen.queryByText(/tokens/)).toBeNull();
    expect(screen.getByText("ponder")).toBeTruthy();
  });
  it("streaming thinking chip shows live 'Thinking for'", () => {
    render(
      <TimelineView
        turnActive
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
    revealCollapsedCards();
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
    revealCollapsedCards();
    expect(screen.queryByText(/Thinking for/)).toBeNull();
  });
  it("thinking with no timing data falls back to a plain 'Thinking' label", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_reasoning", payload: "ponder" })]} />,
    );
    revealCollapsedCards();
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
    revealCollapsedCards();
    expect(screen.getAllByText("files.read")).toHaveLength(2);
    expect(screen.getAllByText("shell.run")).toHaveLength(2);
    expect(screen.getAllByText(/× 2/)).toHaveLength(2);
  });
  it("plain-python code chip shows no histogram", () => {
    render(
      <TimelineView items={[makeItem({ kind: "agent_code", payload: "x = 1\nprint(x)" })]} />,
    );
    revealCollapsedCards();
    expect(screen.queryByText(/×/)).toBeNull();
  });
  it("code_output chip shows 'ran in Xs' without a line count when exec_ms present", () => {
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
    revealCollapsedCards();
    expect(screen.getByText(/Ran in 1s/)).toBeTruthy();
    expect(screen.queryByText(/lines?/)).toBeNull();
  });
  it("code_output without exec_ms shows neutral 'Output' label and no 'ran in' meta", () => {
    render(
      <TimelineView
        items={[makeItem({ kind: "code_output", payload: "stdout: hi" })]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("Output")).toBeTruthy();
    expect(screen.queryByText(/Ran in/)).toBeNull();
  });
  it("streaming code_output chip shows live 'Running for' when execStartedAt is set", () => {
    render(
      <TimelineView
        turnActive
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
    revealCollapsedCards();
    expect(screen.queryByText(/Running for/)).toBeNull();
  });
});

describe("ItemView: inbound_chat source-driven rendering", () => {
  it("source='agent:5' → renders secondary CardHeader 'Agent 5'", () => {
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
    revealCollapsedCards();
    expect(screen.getByText("Agent 5")).toBeTruthy();
    expect(screen.getByText("from agent 5")).toBeTruthy();
    expect(screen.queryByTestId("user-message-bubble")).toBeNull();
  });
  it("source='user' → renders right-aligned user-message-bubble with no CardHeader", () => {
    const { container } = render(
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
    expect(screen.getByTestId("user-message-bubble").textContent).toBe("from web");
    expect(container.querySelector('[data-message-role="user"]')).not.toBeNull();
    expect(screen.queryByTestId("card-toggle")).toBeNull();
  });
  it("source='kernel' → renders secondary CardHeader 'System'", () => {
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
    revealCollapsedCards();
    expect(screen.getByText("System")).toBeTruthy();
    expect(screen.getByText("from kernel")).toBeTruthy();
  });
  it("source=null → renders secondary CardHeader 'System' (default branch)", () => {
    render(
      <TimelineView
        items={[
          makeItem({ kind: "inbound_chat", source: null, payload: "no source" }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText("System")).toBeTruthy();
    expect(screen.getByText("no source")).toBeTruthy();
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
  it("source='user' strips '<header>\\n\\n' envelope so only the human body renders", () => {
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
    expect(screen.queryByText(/User \[2026-05-15\]/)).toBeNull();
    expect(screen.getByText("hello world")).toBeTruthy();
  });
  it("non-user inbound_chat with '<header>\\n\\n<body>' renders header and body separately", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "agent:5",
            payload: "Agent 5 [2026-05-15]:\n\nhello world",
          }),
        ]}
      />,
    );
    revealCollapsedCards();
    expect(screen.getByText(/Agent 5 \[2026-05-15\]/)).toBeTruthy();
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
    revealCollapsedCards();
    expect(screen.getByText(/Code execution output/)).toBeTruthy();
    expect(screen.getByText(/stdout: hi/)).toBeTruthy();
  });
});

describe("ItemView: monochrome secondary headers and turn child defaults", () => {
  it("renders Memory and Note marker headers in the shared monochrome style without tinted classes", () => {
    const { container } = render(
      <TimelineView
        items={[
          makeItem({
            item_id: "1.0",
            kind: "system_marker",
            source: "memory",
            payload: "[memory] recalled fact",
          }),
          makeItem({
            item_id: "1.1",
            kind: "system_marker",
            source: "sdk_hint",
            payload: "[note] monochrome note",
          }),
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const memoryWrapper = screen.getByText("Memory").parentElement;
    const noteWrapper = screen.getByText("Note").parentElement;
    expect(memoryWrapper?.className).not.toContain("violet");
    expect(memoryWrapper?.className).not.toContain("uppercase");
    expect(noteWrapper?.className).not.toContain("fuchsia");
    expect(container.querySelector('[data-testid="card-accent"]')).toBeNull();
  });

  it("renders non-user-facing agent_chat inside a turn expanded by default while keeping other secondary steps collapsed", () => {
    const { container } = render(
      <TimelineView
        items={[
          makeItem({
            item_id: "1.0",
            kind: "agent_reasoning",
            payload: "hidden reasoning",
          }),
          makeItem({
            item_id: "1.1",
            kind: "agent_chat",
            user_facing: false,
            payload: "internal background note",
          }),
          makeItem({
            item_id: "2.0",
            kind: "agent_chat",
            user_facing: true,
            payload: "final user reply",
          }),
        ]}
      />,
    );
    expect(container.querySelectorAll('[data-testid="agent-message-prose"]')).toHaveLength(1);
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const toggles = screen.getAllByTestId("card-toggle");
    expect(toggles[0].getAttribute("aria-expanded")).toBe("false");
    expect(toggles[1].getAttribute("aria-expanded")).toBe("true");
    expect(screen.queryByText("hidden reasoning")).toBeNull();
    expect(screen.getByText("internal background note")).toBeTruthy();
    fireEvent.click(toggles[1]);
    expect(screen.queryByText("internal background note")).toBeNull();
  });

  it("surfaces the timeline's last non-empty agent_chat on the main canvas as fallback and refolds it when a newer agent_chat arrives", () => {
    const { container, rerender } = render(
      <TimelineView
        items={[
          makeItem({
            item_id: "1.0",
            kind: "agent_reasoning",
            payload: "background reasoning",
          }),
          makeItem({
            item_id: "1.1",
            kind: "agent_chat",
            user_facing: false,
            payload: "Fallback visible closing text",
          }),
        ]}
      />,
    );
    expect(container.querySelectorAll('[data-testid="agent-message-prose"]')).toHaveLength(1);
    expect(screen.getByText("Fallback visible closing text")).toBeTruthy();

    rerender(
      <TimelineView
        items={[
          makeItem({
            item_id: "1.0",
            kind: "agent_reasoning",
            payload: "background reasoning",
          }),
          makeItem({
            item_id: "1.1",
            kind: "agent_chat",
            user_facing: false,
            payload: "Fallback visible closing text",
          }),
          makeItem({
            item_id: "2.0",
            kind: "agent_chat",
            user_facing: false,
            payload: "Newer closing text",
          }),
        ]}
      />,
    );
    expect(container.querySelectorAll('[data-testid="agent-message-prose"]')).toHaveLength(1);
    expect(screen.queryByText("Fallback visible closing text")).toBeNull();
    expect(screen.getByText("Newer closing text")).toBeTruthy();
  });
});
