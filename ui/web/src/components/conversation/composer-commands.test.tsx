import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/transport/api";

import { Composer } from "./composer";

let commandList: { name: string; description: string; instruction_hint: string }[] = [];
vi.mock("@/lib/transport/api", () => ({
  MessageDeliveryUnknownError: class MessageDeliveryUnknownError extends Error {
    constructor(readonly clientMessageId: string) {
      super("delivery unconfirmed");
    }
  },
  api: {
    getCommands: vi.fn((_agentId?: number | null) => Promise.resolve(commandList)),
    getContextBreakdown: () =>
      Promise.resolve({
        total_input_tokens: 1000,
        estimated: false,
        exact_fraction: 1,
        max_input_tokens: 1_000_000,
        soft_compact_tokens: 600_000,
        hard_compact_tokens: 800_000,
        sections: [],
        categories: [{ kind: "system_prompt", tokens: 1000, estimated: false, exact_fraction: 1 }],
      }),
  },
}));

const getCommandsMock = vi.mocked(api.getCommands);

beforeEach(() => {
  getCommandsMock.mockImplementation((_agentId?: number | null) =>
    Promise.resolve(commandList),
  );
});

afterEach(() => {
  cleanup();
  commandList = [];
  getCommandsMock.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const baseProps = {
  contextTokens: 0,
  onSend: vi.fn(() => Promise.resolve(true)),
  onStop: vi.fn(),
};


function render(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return rtlRender(ui, { wrapper: ({ children }) =>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
}


const renderComposer = render;


describe("Composer slash commands", () => {
  const recap = { name: "recap", description: "recap it", instruction_hint: "a focus" };

  const input = () =>
    screen.getByTestId<HTMLTextAreaElement>("composer-input");

  function moveCaret(ta: HTMLTextAreaElement, pos: number) {
    ta.setSelectionRange(pos, pos);
    fireEvent.keyUp(ta, { key: "ArrowLeft" });
  }

  function typeInto(ta: HTMLTextAreaElement, marked: string) {
    const caret = marked.indexOf("|");
    const value = marked.replace("|", "");
    fireEvent.change(ta, { target: { value } });
    if (caret >= 0 && caret !== value.length) moveCaret(ta, caret);
  }

  it("requests the global command list when no agent is selected", async () => {
    render(<Composer {...baseProps} mode="idle" agentId={null} />);

    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledTimes(1));
    expect(getCommandsMock).toHaveBeenCalledWith();
  });

  it("requests the selected agent's command list", async () => {
    render(<Composer {...baseProps} mode="idle" agentId={7} />);

    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledWith(7));
  });

  it("fetches each agent once and reuses the cached list when switching back", async () => {
    const compact = {
      name: "compact",
      description: "compact context",
      instruction_hint: "a focus",
    };
    let resolveCompact: (commands: typeof recap[]) => void = () => undefined;
    getCommandsMock.mockImplementation((selectedAgentId?: number | null) =>
      selectedAgentId === 7
        ? Promise.resolve([recap])
        : new Promise((resolve) => { resolveCompact = resolve; }),
    );
    const { rerender } = render(<Composer {...baseProps} mode="idle" agentId={7} />);

    fireEvent.change(input(), { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();

    rerender(<Composer {...baseProps} mode="idle" agentId={8} />);
    fireEvent.change(input(), { target: { value: "/" } });
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    resolveCompact([compact]);
    expect(await screen.findByTestId("slash-option-compact")).toBeTruthy();

    rerender(<Composer {...baseProps} mode="idle" agentId={7} />);
    fireEvent.change(input(), { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();
    expect(getCommandsMock.mock.calls).toEqual([[7], [8]]);
  });

  it("logs an agent command fetch failure and retries it after switching back", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    getCommandsMock.mockImplementation((selectedAgentId?: number | null) =>
      selectedAgentId === 7
        ? Promise.reject(new Error("catalog unavailable"))
        : Promise.resolve([]),
    );
    const { rerender } = render(<Composer {...baseProps} mode="idle" agentId={7} />);

    await waitFor(() =>
      expect(warn).toHaveBeenCalledWith(
        "[composer] getCommands failed: catalog unavailable",
      ),
    );
    expect(screen.getByTestId("composer-input")).toBeTruthy();

    rerender(<Composer {...baseProps} mode="idle" agentId={8} />);
    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledTimes(2));
    rerender(<Composer {...baseProps} mode="idle" agentId={7} />);

    await waitFor(() => expect(getCommandsMock).toHaveBeenCalledTimes(3));
    expect(getCommandsMock.mock.calls).toEqual([[7], [8], [7]]);
  });

  it("typing '/' opens the dropdown with available commands", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/" } });
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();
  });

  it("cannot apply an open slash option while the send snapshot is locked", async () => {
    commandList = [recap];
    let resolveSend: (value: boolean) => void = () => undefined;
    const onSend = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSend = resolve; }),
    );
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "/rec");
    const option = await screen.findByTestId("slash-option-recap");

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Sending" });
    fireEvent.mouseDown(option);
    expect(ta.value).toBe("/rec");

    resolveSend(true);
    await waitFor(() => expect(ta.value).toBe(""));
  });

  const abc = [
    { name: "alpha", description: "", instruction_hint: "" },
    { name: "beta", description: "", instruction_hint: "" },
    { name: "gamma", description: "", instruction_hint: "" },
  ];

  it("ArrowDown / ArrowUp move the active option (and wrap around the ends)", async () => {
    commandList = abc;
    render(<Composer {...baseProps} mode="idle" />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/" } });
    await screen.findByTestId("slash-option-alpha");
    const sel = () =>
      ["alpha", "beta", "gamma"].find(
        (n) => screen.getByTestId(`slash-option-${n}`).getAttribute("aria-selected") === "true",
      );
    expect(sel()).toBe("alpha");
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    expect(sel()).toBe("beta");
    fireEvent.keyDown(ta, { key: "ArrowUp" });
    expect(sel()).toBe("alpha");
    fireEvent.keyDown(ta, { key: "ArrowUp" });
    expect(sel()).toBe("gamma");
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    expect(sel()).toBe("alpha");
  });

  it("Tab picks the active option — fills `/<name> ` and waits, does not send", async () => {
    commandList = abc;
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "/" } });
    await screen.findByTestId("slash-option-alpha");
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(ta.value).toBe("/beta ");
    expect(onSend).not.toHaveBeenCalled();
    expect(ta.selectionStart).toBe(ta.value.length);
    expect(ta.selectionEnd).toBe(ta.value.length);
  });

  it("once the name is committed, the command's instruction_hint shows in the meta row", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = screen.getByTestId("composer-input");
    const meta = () => screen.getByTestId("composer-meta").textContent;
    fireEvent.change(ta, { target: { value: "/recap" } });
    await screen.findByTestId("slash-option-recap");
    expect(screen.queryByTestId("composer-meta")).toBeNull();
    fireEvent.change(ta, { target: { value: "/recap " } });
    await waitFor(() => expect(meta()).toBe("a focus"));
  });

  it("no hint row for an unknown command or a command with an empty hint — falls back to the readout", async () => {
    commandList = abc;
    const meta = () => screen.getByTestId("composer-meta");
    renderComposer(<Composer {...baseProps} mode="idle" contextTokens={500} showContextMeter />);
    const ta = screen.getByTestId("composer-input");
    const gauge = () => meta().querySelector('[data-testid="context-meter"]');
    fireEvent.change(ta, { target: { value: "/alpha " } });
    await waitFor(() => expect(meta().textContent).toBe(""));
    expect(gauge()).toBeTruthy();
    fireEvent.change(ta, { target: { value: "/zzz " } });
    expect(meta().textContent).toBe("");
    expect(gauge()).toBeTruthy();
  });

  it("Enter on a partial name fills `/<name> ` and waits — does not send", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/recap ");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
  });

  it("after filling, typing the instruction + Enter sends the raw `/<name> <instruction>`", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/recap ");
    fireEvent.change(ta, { target: { value: "/recap just the PRs" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/recap just the PRs", [], expect.any(String)),
    );
  });

  it("a fully-typed slash message (whitespace present) sends raw — dropdown is open but Enter sends", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/recap just the PRs" } });
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/recap just the PRs", [], expect.any(String)),
    );
  });

  it("clicking a command option fills `/<name> ` and waits — does not send", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "/" } });
    const option = await screen.findByTestId("slash-option-recap");
    fireEvent.mouseDown(option);
    await waitFor(() => expect(ta.value).toBe("/recap "));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("Escape dismisses the dropdown, then Enter sends the raw text", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/recap" } });
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("slash-option-recap")).toBeNull());
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/recap", [], expect.any(String)),
    );
  });

  it("a non-matching /token sends as plain text (no dropdown)", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/nope" } });
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/nope", [], expect.any(String)),
    );
  });

  const compact = { name: "compact", description: "compact context", instruction_hint: "an instruction hint" };

  it("sends raw multi-command text through to onSend", async () => {
    commandList = [recap, compact];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "/compact /update");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-compact")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/compact /update", [], expect.any(String)),
    );
  });

  it("a slash mid-message never opens the dropdown — Enter sends the raw text (user ruling #836)", async () => {
    commandList = [recap, compact];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "hello /compact");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-compact")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("hello /compact", [], expect.any(String)),
    );
  });

  it("the instruction hint follows the FIRST command only (#836)", async () => {
    commandList = [recap, compact];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = input();
    const meta = () => screen.getByTestId("composer-meta").textContent;
    typeInto(ta, "/compact ping /recap the PRs");
    await waitFor(() => expect(meta()).toBe("an instruction hint"));
    moveCaret(ta, "/compact ping /recap the".length);
    await waitFor(() => expect(meta()).toBe("an instruction hint"));
    moveCaret(ta, "/compact".length);
    await waitFor(() => expect(screen.queryByTestId("composer-meta")).toBeNull());
  });

  it("mid-message slash does not surface a hint (#836)", async () => {
    commandList = [recap, compact];
    render(<Composer {...baseProps} mode="idle" />);
    typeInto(input(), "heads up /compact ping");
    await waitFor(() => expect(screen.queryByTestId("composer-meta")).toBeNull());
  });

  it("a later command in the message gets no dropdown (#836)", async () => {
    commandList = [recap, compact];
    render(<Composer {...baseProps} mode="idle" />);
    typeInto(input(), "/compact ping /rec");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
  });

  it("a command started mid-message gets no dropdown (#836)", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    typeInto(input(), "look at this /rec");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
  });

  it("a command typed in front of existing text gets a dropdown", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    typeInto(input(), " /rec| the PRs are piling up");
    expect(await screen.findByTestId("slash-option-recap")).toBeTruthy();
  });

  it("a mid-message slash cannot be selected — Enter sends the raw text (#836)", async () => {
    commandList = [recap, compact];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "/compact ping /rec");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("/compact ping /rec", [], expect.any(String)),
    );
  });

  it("selecting mid-message keeps both sides and reuses the existing space", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = input();
    typeInto(ta, " /rec| the PRs");
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe(" /recap the PRs");
    expect(ta.selectionStart).toBe(" /recap ".length);
  });

  it("selecting a name that is already spelled out just moves the caret past it", async () => {
    commandList = [recap];
    render(<Composer {...baseProps} mode="idle" />);
    const ta = input();
    typeInto(ta, "/recap| ");
    await screen.findByTestId("slash-option-recap");
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/recap ");
    expect(ta.selectionStart).toBe("/recap ".length);
    await waitFor(() => expect(screen.queryByTestId("slash-option-recap")).toBeNull());
  });

  it("a path-shaped token doesn't open the dropdown", async () => {
    commandList = [recap];
    const onSend = vi.fn(() => Promise.resolve(true));
    render(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = input();
    typeInto(ta, "read /etc/hosts");
    await Promise.resolve();
    expect(screen.queryByTestId("slash-option-recap")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("read /etc/hosts", [], expect.any(String)),
    );
  });
});
