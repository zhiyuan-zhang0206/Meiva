import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";
import { describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";

import { CardHeader, messageCardConfig } from "../card";

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

const noop = vi.fn();

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

describe("CardHeader rich summaries", () => {
  it("renders rich summary for agent_reasoning with committed duration", () => {
    const rItem = item("agent_reasoning", {
      payload: "thinking...",
      reasoning_ms: 8000,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thought for 8s");
  });

  it("reasoning with reasoning_tokens shows token count", () => {
    const rItem = item("agent_reasoning", {
      payload: "x",
      reasoning_ms: 5000,
      reasoning_tokens: 1500,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("1.5k tokens");
  });

  it("reasoning with live clock (reasoningStartedAt)", () => {
    const rItem = item("agent_reasoning", {
      payload: "t",
      reasoningStartedAt: Date.now() - 3000,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thinking for");
  });

  it("reasoning with reasoningElapsedMs (frozen clock)", () => {
    const rItem = item("agent_reasoning", {
      payload: "t",
      reasoningElapsedMs: 12000,
    });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thought for 12s");
  });

  it("reasoning without timing → bare 'Thinking'", () => {
    const rItem = item("agent_reasoning", { payload: "old" });
    const cfg = messageCardConfig(rItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={rItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Thinking");
  });

  it("agent_code with SDK calls shows method names", () => {
    const cItem = item("agent_code", {
      payload: "meiva.files.read('x')",
      sdk_calls: [
        { method: "files.read", count: 2 },
        { method: "shell.run", count: 1 },
      ],
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("files.read");
    expect(container.textContent).toContain("× 2");
    expect(container.textContent).toContain("shell.run");
  });

  it("agent_code header keeps SDK chips in namespace and count order", () => {
    const cItem = item("agent_code", {
      sdk_calls: [
        { method: "shell.run", count: 10 },
        { method: "files.write", count: 1 },
        { method: "agents.spawn", count: 1 },
        { method: "files.read", count: 2 },
      ],
    });
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={messageCardConfig(cItem)!} expanded={false} onToggle={noop} />,
    );
    const methods = Array.from(container.querySelectorAll('span[class="text-foreground/80"]'))
      .map((chip) => chip.textContent);
    expect(methods).toEqual(["agents.spawn", "files.read", "files.write", "shell.run"]);
  });

  it("agent_code streaming (codeStartedAt, no committed duration) → live 'Writing code for'", () => {
    const cItem = item("agent_code", {
      payload: "x = 1",
      codeStartedAt: Date.now() - 2000,
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Writing code for");
  });

  it("agent_code with committed code_elapsed_ms → past-tense 'Wrote code for'", () => {
    const cItem = item("agent_code", {
      payload: "x = 1",
      code_elapsed_ms: 5000,
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Wrote code for 5s");
  });

  it("agent_code with committed code_elapsed_ms === 0 (single-chunk args) → floored 'Wrote code for 0.1s'", () => {
    const cItem = item("agent_code", { payload: "x = 1", code_elapsed_ms: 0 });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Wrote code for 0.1s");
  });

  it("agent_code falls back to frozen codeElapsedMs when code_elapsed_ms absent", () => {
    const cItem = item("agent_code", { payload: "x = 1", codeElapsedMs: 3000 });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Wrote code for 3s");
  });

  it("committed 0ms duration → all three inner blocks show a floored timer", () => {
    const cases: [BackendTimelineItem["kind"], Partial<BackendTimelineItem>, string][] = [
      ["agent_reasoning", { reasoning_ms: 0 }, "Thought for 0.1s"],
      ["agent_code", { code_elapsed_ms: 0 }, "Wrote code for 0.1s"],
      ["code_output", { exec_ms: 0 }, "Ran in 0.1s"],
    ];
    for (const [kind, overrides, expected] of cases) {
      const it = item(kind, { payload: "x", ...overrides });
      const cfg = messageCardConfig(it)!;
      const { container } = renderWithQuery(
        <CardHeader item={it} config={cfg} expanded={false} onToggle={noop} />,
      );
      expect(container.textContent).toContain(expected);
    }
  });

  it("agent_code without SDK calls → line count", () => {
    const cItem = item("agent_code", {
      payload: "x = 1\ny = 2\nz = 3",
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("3 lines of code");
  });

  it("agent_code without sdk_calls never scans the payload for call names", () => {
    const cItem = item("agent_code", {
      payload: "meiva.files.read('x')\nmeiva.shell.run('ls')",
    });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("2 lines of code");
    expect(container.textContent).not.toContain("files.read");
    expect(container.textContent).not.toContain("shell.run");
  });

  it("agent_code with external tool source renders tool name and line count without 'of code'", () => {
    const cItem = item("agent_code", {
      source: "Bash",
      payload: "git status\ngit diff",
      sdk_calls: [{ method: "Bash", count: 1 }],
    });
    const cfg = messageCardConfig(cItem)!;
    expect(cfg.icon?.displayName).toBe("Terminal");
    const { container } = renderWithQuery(
      <CardHeader item={cItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Bash · 2 lines");
    expect(container.textContent).not.toContain("of code");
    expect(container.textContent).not.toContain("× 1");
  });

  it("code_output with exec_ms shows duration", () => {
    const oItem = item("code_output", {
      payload: "line1\nline2",
      exec_ms: 8000,
    });
    const cfg = messageCardConfig(oItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={oItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Ran in 8s");
    expect(container.textContent).toContain("2 lines");
  });

  it("code_output with error text → same chip as any other output", () => {
    const oItem = item("code_output", {
      payload: "ValueError: something went wrong",
    });
    const cfg = messageCardConfig(oItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={oItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).not.toContain("Error");
    expect(container.querySelector(".text-destructive")).toBeNull();
    expect(container.textContent).toContain("1 line");
  });

  it("code_output live execution (execStartedAt, no exec_ms)", () => {
    const oItem = item("code_output", {
      payload: "running...",
      execStartedAt: Date.now() - 2000,
    });
    const cfg = messageCardConfig(oItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={oItem} config={cfg} expanded={false} onToggle={noop} />,
    );
    expect(container.textContent).toContain("Running for");
  });
});

describe("CardHeader sticky header (task #3136)", () => {
  const chatItem = item("agent_chat", { payload: "hello" });

  it("plain cards (stickyHeader unset) keep the resting header — no sticky classes", () => {
    const cfg = messageCardConfig(chatItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={chatItem} config={cfg} expanded={true} onToggle={noop} />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.className).not.toContain("sticky");
    expect(btn.getAttribute("data-stuck")).toBe("false");
  });

  it("expanded sticky card pins the header at top-11 while resting", () => {
    const cfg = messageCardConfig(chatItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={chatItem} config={cfg} expanded={true} onToggle={noop} stickyHeader="top" />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.className).toContain("sticky");
    expect(btn.className).toContain("top-11");
    expect(btn.className).toContain("z-10");
    expect(btn.getAttribute("data-stuck")).toBe("false");
    expect(btn.className).not.toContain("backdrop-blur-md");
    expect(btn.className).toContain("before:content-['']");
    expect(btn.className).toContain("before:bg-transparent");
    expect(btn.className).toContain("before:backdrop-blur-none");
    expect(btn.className).toContain("before:transition-[background-color,backdrop-filter]");
  });

  it("collapsed sticky card does not pin (the rest position stays in flow)", () => {
    const cfg = messageCardConfig(chatItem)!;
    const { container } = renderWithQuery(
      <CardHeader item={chatItem} config={cfg} expanded={false} onToggle={noop} stickyHeader="top" />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.className).not.toContain("sticky");
  });

  it("stuck state switches to the elevated mask styling", () => {
    const cfg = messageCardConfig(chatItem)!;
    const { container } = renderWithQuery(
      <CardHeader
        item={chatItem}
        config={cfg}
        expanded={true}
        onToggle={noop}
        stickyHeader="top"
        isStuck={true}
      />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.getAttribute("data-stuck")).toBe("true");
    expect(btn.className).toContain("hover:bg-transparent");
    expect(btn.className).not.toContain("hover:bg-accent/30");
    expect(btn.className).toContain("before:backdrop-blur-md");
    expect(btn.className).toContain("before:bg-background/95");
    expect(btn.className).toContain("hover:before:bg-accent/30");
    expect(btn.className).toContain("before:bottom-0");
    expect(btn.className).toContain("before:-z-10");
    expect(btn.className).toContain("16px_0_0_0_var(--background)");
    expect(btn.className).toContain("before:-top-[2px]");
    expect(btn.className).toContain("after:bottom-0");
    expect(btn.className).toContain("after:h-px");
    expect(btn.className).not.toContain("after:top-full");
    expect(btn.className).not.toContain("-mb-px");
    expect(btn.className).toContain("motion-reduce:transition-none");
  });

  it("stays clickable while stuck — the toggle still fires", () => {
    const onToggle = vi.fn();
    const cfg = messageCardConfig(chatItem)!;
    const { container } = renderWithQuery(
      <CardHeader
        item={chatItem}
        config={cfg}
        expanded={true}
        onToggle={onToggle}
        stickyHeader="top"
        isStuck={true}
      />,
    );
    fireEvent.click(container.querySelector("button")!);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("nested sticky (work-block child) pins under the block header, not at the bar line", () => {
    const cItem = item("agent_reasoning", { payload: "thinking...", reasoning_ms: 8000 });
    const cfg = messageCardConfig(cItem)!;
    const { container } = renderWithQuery(
      <CardHeader
        item={cItem}
        config={cfg}
        expanded={true}
        onToggle={noop}
        stickyHeader="nested"
      />,
    );
    const btn = container.querySelector("button")!;
    expect(btn.className).toContain("sticky");
    expect(btn.className).toContain("top-[calc(2.75rem_+_var(--turn-header-h,0px))]");
    expect(btn.className).toContain("z-[5]");
    expect(btn.className).not.toContain("top-11");
  });
});

describe("impersonation provenance", () => {
  it.each(["agent_chat", "inbound_chat"] as const)("never surfaces the external executor on %s cards", (kind) => {
    const message = item(kind, {
      source: kind === "inbound_chat" ? "user" : "agent:42",
      impersonation: {
        agent_id: 42, session_id: 0, name: "Fix login", executor_name: "Codex: my helper",
        provider: "codex", seq: 2,
      },
    });
    const { getByTestId, queryByTestId } = renderWithQuery(
      <CardHeader item={message} config={messageCardConfig(message)!} expanded onToggle={noop} />,
    );
    expect(getByTestId("card-toggle")).toBeTruthy();
    expect(queryByTestId("impersonation-badge")).toBeNull();
  });
});
