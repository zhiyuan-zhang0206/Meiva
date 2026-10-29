import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { interactionTimingResetForTest, noteTurnStart } from "@/lib/format/interaction-timing";
import { telemetryTestControl } from "@/lib/telemetry/telemetry";
import { BOTTOM_BAND_CLASS, BOTTOM_BAND_ROW_CLASS } from "@/lib/layout/layout";
import { pressEnter } from "@/test-support/enter-key";
import { Composer } from "./composer";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getCommands.mockImplementation((_agentId?: number | null) => Promise.resolve(commandList));
  transport.getContextBreakdown.mockImplementation(() =>
      Promise.resolve({
        total_input_tokens: 1000,
        estimated: false,
        exact_fraction: 1,
        max_input_tokens: 1_000_000,
        soft_compact_tokens: 600_000,
        hard_compact_tokens: 800_000,
        sections: [],
        categories: [{ kind: "system_prompt", tokens: 1000, estimated: false, exact_fraction: 1 }],
      }));
});

let commandList: { name: string; description: string; instruction_hint: string }[] = [];

const getCommandsMock = transport.getCommands;
beforeEach(() => {
  interactionTimingResetForTest();
  telemetryTestControl.reset();
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

function renderComposerTree(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithBrowser(ui, { wrapper: ({ children }) =>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
}

describe("Composer placeholder", () => {
  it("explains that messaging a terminated agent resurrects it", () => {
    const { rerender } = renderComposerTree(<Composer {...baseProps} mode="idle" />);
    expect(screen.getByTestId("composer-input").getAttribute("placeholder")).toBe(
      "Send a message",
    );
    rerender(<Composer {...baseProps} mode="idle" agentTerminated />);
    expect(screen.getByTestId("composer-input").getAttribute("placeholder")).toBe(
      "Send a message to resurrect this agent",
    );
  });
});

describe("Composer reply suggestion", () => {
  const SUGGESTION = "Yes, go ahead and deploy.";

  function input(): HTMLTextAreaElement {
    return screen.getByTestId<HTMLTextAreaElement>("composer-input");
  }
  it("browses numbered candidates in the placeholder and sends only the accepted text", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={["Use A.", "Use B.", "Use C.", "Use D."]} />);
    expect(input().placeholder).toBe("[1/4] Use A.");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[2/4] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/4] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[4/4] Use D.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[4/4] Use D.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[3/4] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[4/4] Use D.");
    expect(input().value).toBe("");
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    await waitFor(() => expect(input().value).toBe("Use D."));
    expect(input().selectionStart).toBe("Use D.".length);
    expect(input().selectionEnd).toBe("Use D.".length);
    fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("Use D.", [], expect.any(String)));
    await waitFor(() => expect(input().value).toBe(""));
    expect(input().placeholder).toBe("Send a message");
    expect(fireEvent.keyDown(input(), { key: "ArrowRight" })).toBe(true);
    expect(input().value).toBe("");
  });
  it("stops at the first and last candidate instead of wrapping around", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B.", "Use C."]} />);
    expect(input().placeholder).toBe("[1/3] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[2/3] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/3] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/3] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[3/3] Use C.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[2/3] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
  });
  it("restores the selected candidate when an accepted or edited draft is cleared", async () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B.", "Use C."]} />);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    await waitFor(() => expect(input().value).toBe("Use B."));
    fireEvent.change(input(), { target: { value: "Use B, with changes." } });
    fireEvent.change(input(), { target: { value: "" } });
    expect(input().placeholder).toBe("[2/3] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(input().placeholder).toBe("[1/3] Use A.");
  });
  it("keeps the selected candidate after a failed send and shows new candidates after success", async () => {
    const onSend = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const options = ["Use A.", "Use B."];
    const { rerender } = renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={options} />);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    expect(input().value).toBe("Use B.");
    fireEvent.change(input(), { target: { value: "" } });
    expect(input().placeholder).toBe("[2/2] Use B.");
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => expect(input().value).toBe(""));
    expect(input().placeholder).toBe("Send a message");
    rerender(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={["Continue.", "Stop here."]} />);
    expect(input().placeholder).toBe("[1/2] Continue.");
  });
  it("resets to the first candidate for a new response or another agent", () => {
    const options = ["Use A.", "Use B."];
    const { rerender } = renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} suggestions={options} />);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().placeholder).toBe("[2/2] Use B.");
    rerender(<Composer {...baseProps} mode="idle" agentId={8} suggestions={options} />);
    expect(input().placeholder).toBe("[1/2] Use A.");
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    rerender(<Composer {...baseProps} mode="idle" agentId={8} suggestions={["Explain X.", "Explain Y."]} />);
    expect(input().placeholder).toBe("[1/2] Explain X.");
  });
  it.each(["ArrowUp", "ArrowDown", "ArrowRight"])("leaves %s to the input method during composition", (key) => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B."]} />);
    expect(fireEvent.keyDown(input(), { key, isComposing: true })).toBe(true);
    expect(input().placeholder).toBe("[1/2] Use A.");
    expect(input().value).toBe("");
  });
  it.each(["ArrowUp", "ArrowDown"])("leaves modified %s and nonempty drafts alone", (key) => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={["Use A.", "Use B."]} />);
    for (const modifier of [{ shiftKey: true }, { ctrlKey: true }, { altKey: true }, { metaKey: true }]) {
      expect(fireEvent.keyDown(input(), { key, ...modifier })).toBe(true);
      expect(input().placeholder).toBe("[1/2] Use A.");
    }
    fireEvent.change(input(), { target: { value: "My reply" } });
    expect(fireEvent.keyDown(input(), { key })).toBe(true);
    expect(input().value).toBe("My reply");
    expect(input().placeholder).toBe("Send a message");
  });
  it("shows the suggestion as the placeholder of the empty input", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    expect(input().getAttribute("placeholder")).toBe(SUGGESTION);
    expect(input().value).toBe("");
  });
  it("ArrowRight accepts it: the input holds the text and the caret is at the end", async () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight" });
    expect(notPrevented).toBe(false);
    await waitFor(() => {
      expect(input().value).toBe(SUGGESTION);
    });
    await waitFor(() => {
      expect(input().selectionStart).toBe(SUGGESTION.length);
    });
    expect(input().selectionEnd).toBe(SUGGESTION.length);
  });
  it("an accepted suggestion is an ordinary draft: Enter sends it", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} suggestions={[SUGGESTION]} />);
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    await waitFor(() => {
      expect(input().value).toBe(SUGGESTION);
    });
    fireEvent.keyDown(input(), { key: "Enter" });
    await waitFor(() => {
      expect(onSend).toHaveBeenCalledWith(SUGGESTION, [], expect.any(String));
    });
  });
  it("typing hides it, and emptying the input brings it back", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    fireEvent.change(input(), { target: { value: "n" } });
    expect(input().getAttribute("placeholder")).toBe("Send a message");
    fireEvent.change(input(), { target: { value: "" } });
    expect(input().getAttribute("placeholder")).toBe(SUGGESTION);
  });
  it("ArrowRight in a non-empty input moves the caret as usual", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    fireEvent.change(input(), { target: { value: "draft" } });
    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight" });
    expect(notPrevented).toBe(true);
    expect(input().value).toBe("draft");
  });
  it.each([
    ["Shift", { shiftKey: true }],
    ["Ctrl", { ctrlKey: true }],
    ["Alt", { altKey: true }],
    ["Meta", { metaKey: true }],
  ])("%s+ArrowRight does not accept it", (_name, modifier) => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight", ...modifier });
    expect(notPrevented).toBe(true);
    expect(input().value).toBe("");
  });
  it("ArrowRight during IME composition is left to the input method", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight", isComposing: true });
    expect(notPrevented).toBe(true);
    expect(input().value).toBe("");
  });
  it("without a suggestion ArrowRight does nothing special", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" />);
    const notPrevented = fireEvent.keyDown(input(), { key: "ArrowRight" });
    expect(notPrevented).toBe(true);
    expect(input().value).toBe("");
  });
  it("a suggestion that goes away restores the normal placeholder", () => {
    const { rerender } = renderComposerTree(<Composer {...baseProps} mode="idle" suggestions={[SUGGESTION]} />);
    rerender(<Composer {...baseProps} mode="idle" suggestions={null} />);
    expect(input().getAttribute("placeholder")).toBe("Send a message");
  });
  it("works while the agent is busy: the user may queue a reply", async () => {
    renderComposerTree(<Composer {...baseProps} mode="busy" suggestions={[SUGGESTION]} />);
    fireEvent.keyDown(input(), { key: "ArrowRight" });
    await waitFor(() => {
      expect(input().value).toBe(SUGGESTION);
    });
  });
});

describe("Composer button mode dispatch", () => {
  it("root is width-capped, centered, aligned with the timeline width", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" maxWidthCss="min(40vw, 1280px)" />);
    const root = screen.getByTestId("composer");
    expect(root.className).toContain("mx-auto");
    expect(root.className).toContain("w-full");
    expect(root.className).not.toContain("max-w-[45rem]");
    expect(root.style.maxWidth).toBe("min(40vw, 1280px)");
  });
  it("the tightened gap above the rule, unchanged textarea height", () => {
    // meiva:note
    // The 2026-10-29 ruling split a 20px gap into 6px for the list (its pb-1.5)
    // and 4px here (pt-1); the user then read that line as too airy, so the two
    // shares are 4px (the timeline's pb-1) and 2px (this root's pt-0.5).
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    renderComposerTree(<Composer {...baseProps} mode="idle" />);
    const root = screen.getByTestId("composer");
    expect(root.className.split(" ")).toContain("pt-0.5");
    expect(root.className.split(" ")).not.toContain("pt-1");
    expect(screen.getByTestId("composer-input").className).toContain("min-h-9");
  });
  // meiva:note
  // The band grows upward with the draft, and that is the whole story: the
  // composer publishes nothing another column could follow (user report
  // 2026-10-29 — the sidebar's divider tracked this one for a day).
  // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
  it("publishes no band geometry for another column to track", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" />);
    expect(document.documentElement.style.getPropertyValue("--composer-band-height")).toBe("");
  });
  it("draws the shared bottom band: the rule the sidebar footer also draws", () => {
    // meiva:note
    // Both columns are bottom-anchored, so the two rules land on the same y only
    // if both bands are composed the same way — hence one primitive, not two
    // hand-tuned paddings (user ruling 2026-10-29, before: 16px apart). The band
    // padding box is the 36px control row, not the composer's root.
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    renderComposerTree(<Composer {...baseProps} mode="idle" />);
    const band = screen.getByTestId("composer-band");
    expect(band.className).toContain(BOTTOM_BAND_CLASS);
    expect(band.className.split(" ")).toContain("border-t");
    expect(band.className).toContain("pb-3");
    const row = screen.getByTestId("composer-send").parentElement!;
    expect(row.className).toContain(BOTTOM_BAND_ROW_CLASS);
  });
  it("root maxWidth follows the maxWidthCss prop (setting-derived)", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" maxWidthCss="min(60vw, 1280px)" />);
    const root = screen.getByTestId("composer");
    expect(root.style.maxWidth).toBe("min(60vw, 1280px)");
  });
  it("undefined maxWidthCss (mobile) → no inline maxWidth, full-width", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" maxWidthCss={undefined} />);
    const root = screen.getByTestId("composer");
    expect(root.style.maxWidth).toBe("");
    expect(root.className).toContain("w-full");
    expect(root.className).toContain("mx-auto");
  });
  it("mode='idle' + non-empty input → click triggers onSend, not onStop", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} onStop={onStop} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("hello", [], expect.any(String));
    expect(onStop).not.toHaveBeenCalled();
  });
  it("marks send latency at submit time before message delivery starts", () => {
    let now = 100;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    let latencyRecordedBeforeSendResolves = 0;
    const onSend = vi.fn(() => {
      telemetryTestControl.reset();
      now = 150;
      noteTurnStart(42);
      latencyRecordedBeforeSendResolves = telemetryTestControl.bufferSize();
      return new Promise<boolean>(() => undefined);
    });
    renderComposerTree(<Composer {...baseProps} agentId={42} mode="idle" onSend={onSend} />);
    fireEvent.change(screen.getByTestId("composer-input"), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSend).toHaveBeenCalledOnce();
    expect(latencyRecordedBeforeSendResolves).toBe(1);
  });
  it("mode='busy' + non-empty input → click triggers onSend (goes through inbound queue, doesn't stop turn)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="busy" onSend={onSend} onStop={onStop} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("draft", [], expect.any(String));
    expect(onStop).not.toHaveBeenCalled();
  });
  it("mode='busy' + empty input → click triggers onStop (cancel path)", () => {
    const onStop = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="busy" onStop={onStop} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop current turn" }));
    expect(onStop).toHaveBeenCalled();
  });
  it("mode='busy' typing a char → aria-label switches from 'Stop current turn' to 'Send message'", () => {
    renderComposerTree(<Composer {...baseProps} mode="busy" />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "x" } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Send message");
    fireEvent.change(ta, { target: { value: "" } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });
  it("mode='busy' + whitespace-only → button still stop (empty after trim; guards against .trim() being silently dropped, flipping to send)", () => {
    renderComposerTree(<Composer {...baseProps} mode="busy" />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "   \n  " } });
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });
  it("mode='disabled' → button disabled, neither onSend nor onStop fires", () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="disabled" onSend={onSend} onStop={onStop} />);
    const btn = screen.getByRole<HTMLButtonElement>("button");
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(onSend).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });
  it("mode='idle' + empty input → button disabled (empty send is meaningless)", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" />);
    const btn = screen.getByRole<HTMLButtonElement>("button", { name: "Send message" });
    expect(btn.disabled).toBe(true);
  });
  it("aria-label follows mode ('Send message' ↔ 'Stop current turn')", () => {
    const { rerender } = renderComposerTree(<Composer {...baseProps} mode="idle" />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Send message");
    rerender(<Composer {...baseProps} mode="busy" />);
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe("Stop current turn");
  });
});

describe("Composer Enter / IME / Shift+Enter", () => {
  it("plain Enter → triggers action (onSend when idle)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "msg" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("msg", [], expect.any(String));
  });
  it("Enter in mode='busy' goes to onSend, not onStop (stop is click-only)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="busy" onSend={onSend} onStop={onStop} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "draft" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledWith("draft", [], expect.any(String));
    expect(onStop).not.toHaveBeenCalled();
  });
  it("mode='busy' + empty input → Enter no-op (nothing to send, also not stop)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onStop = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="busy" onSend={onSend} onStop={onStop} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });
  it("IME composing (isComposing=true) → Enter does not trigger (CJK pinyin must be protected)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "\u4f60\u597d" } });
    fireEvent.keyDown(ta, { key: "Enter", isComposing: true });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
  it("IME Process key fallback → Enter does not trigger (even with isComposing=false)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "\u4f60\u597d" } });
    fireEvent.keyDown(ta, { key: "Process", isComposing: false });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
  it("Shift+Enter → does not trigger send (default newline behavior, let textarea handle it)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "line1" } });
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
  it("Shift+Enter breaks the line at the caret and keeps the draft", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "firstsecond" } });
    ta.setSelectionRange(5, 5);
    pressEnter(ta, { shiftKey: true });
    await Promise.resolve();
    expect(ta.value).toBe("first\nsecond");
    expect(ta.selectionStart).toBe(6);
    expect(onSend).not.toHaveBeenCalled();
  });
  it("the legacy IME key code 229 → Enter does not trigger", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "\u4f60\u597d" } });
    fireEvent.keyDown(ta, { key: "Enter", keyCode: 229 });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
});
