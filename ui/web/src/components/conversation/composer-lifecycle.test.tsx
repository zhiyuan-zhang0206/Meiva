import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessageDeliveryUnknownError } from "@/lib/transport/api";
import { interactionTimingResetForTest, noteTurnStart } from "@/lib/format/interaction-timing";
import { fakeBrowser } from "@/lib/browser/testing";
import { telemetryTestControl } from "@/lib/telemetry/telemetry";
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

function renderComposerTree(
  ui: React.ReactElement,
  failStorage?: "write" | "remove",
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const browser = fakeBrowser();
  if (failStorage !== undefined) browser.failStorage("session", failStorage);
  return renderWithBrowser(ui, {
    browser,
    wrapper: ({ children }) =>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
  });
}

const renderComposer = renderComposerTree;

describe("Composer input lifecycle", () => {
  it("send succeeds (returns true) → input cleared", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(ta.value).toBe(""));
  });
  it("send fails (returns false) → input preserved, no lost user text (H2 fix)", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(false));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "important draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(ta.value).toBe("important draft");
  });
  it("mode='busy' send fails (returns false) → input preserved (busy goes through the same submit path; failure branch must be covered)", async () => {
    const onSend = vi.fn<(content: string) => Promise<boolean>>(() => Promise.resolve(false));
    renderComposerTree(<Composer {...baseProps} mode="busy" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "queued draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(ta.value).toBe("queued draft");
  });
  it("during sending the button is disabled, repeated clicks fire onSend only once (issue 6 dedup guard)", async () => {
    const deferred: { resolve: (v: boolean) => void } = {
      resolve: () => undefined,
    };
    const onSend = vi.fn<(content: string) => Promise<boolean>>(
      () => new Promise<boolean>((resolve) => { deferred.resolve = resolve; }),
    );
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "first" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => {
      const btn = screen.getByRole<HTMLButtonElement>("button", { name: "Sending" });
      expect(btn.disabled).toBe(true);
    });
    const sendingBtn = screen.getByRole("button", { name: "Sending" });
    fireEvent.click(sendingBtn);
    fireEvent.click(sendingBtn);
    expect(onSend).toHaveBeenCalledTimes(1);
    deferred.resolve(true);
    await waitFor(() => expect(ta.value).toBe(""));
  });
  it("retrying an uncertain send reuses its client message id", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "uncertain delivery" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    const firstId = onSend.mock.calls[0][2];
    const retryId = onSend.mock.calls[1][2];
    expect(firstId).toEqual(expect.any(String));
    expect(firstId.length).toBeGreaterThan(0);
    expect(retryId).toBe(firstId);
  });
  it("keeps an unknown draft editable and normal Send reuses its identity", async () => {
    let now = 100;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "uncertain delivery" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    telemetryTestControl.reset();
    now = 150;
    noteTurnStart(7);
    expect(telemetryTestControl.bufferSize()).toBe(1);
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled,
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(onSend.mock.calls[0][2]);
    await waitFor(() => expect(ta.value).toBe(""));
    expect(screen.queryByText(/Delivery unconfirmed/)).toBeNull();
  });
  it("keeps the same identity across an ambiguous send and a failed normal retry", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "still ambiguous" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    const clientMessageId = onSend.mock.calls[0][2];
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(clientMessageId);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(3));
    expect(onSend.mock.calls[2][2]).toBe(clientMessageId);
    await waitFor(() => expect(screen.queryByText(/Delivery unconfirmed/)).toBeNull());
  });
  it("gives edited text a new identity after an ambiguous send", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "old uncertain" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    fireEvent.change(ta, { target: { value: "new explicit message" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).not.toBe(onSend.mock.calls[0][2]);
  });
  it("storage failure falls back to memory without changing retry identity", async () => {
    const agentId = 7071;
    const attemptKey = `composer-send-attempt-${agentId}`;
    const persistentStorage = sessionStorage;
    persistentStorage.setItem(
      attemptKey,
      JSON.stringify({
        signature: JSON.stringify([agentId, "stale body", []]),
        clientMessageId: "stale-client-id",
        agentId,
        content: "stale body",
        imageUrls: [],
      }),
    );
    expect(persistentStorage.getItem(attemptKey)).toContain("stale-client-id");
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    const first = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />,
      "write",
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "send despite storage" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    const firstId = onSend.mock.calls[0][2];
    expect(firstId).not.toBe("stale-client-id");
    // meiva:note
    // The page refused the write, so the attempt lives in memory for this page view; the stale copy
    // the server-rendered tab started with is still there.
    // meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
    expect(sessionStorage.getItem(attemptKey)).toContain("stale-client-id");
    first.unmount();
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).toBe(firstId);
    const restored = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    await waitFor(() => expect(restored.value).toBe(""));
    expect(screen.queryByRole("button", { name: "Sending" })).toBeNull();
  });
  it("successful send keeps an authoritative tombstone when storage removal fails", async () => {
    const agentId = 7072;
    const persistentStorage = sessionStorage;
    const attemptKey = `composer-send-attempt-${agentId}`;
    persistentStorage.setItem(attemptKey, "stale tombstone");
    const onSend = vi.fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>(() => Promise.resolve(true));
    const first = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />,
      "remove",
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "sent message" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(ta.value).toBe(""));
    expect(persistentStorage.getItem(attemptKey)).toBeTruthy();
    const firstId = onSend.mock.calls[0][2];
    first.unmount();
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={agentId} onSend={onSend} />);
    const restored = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    expect(restored.value).toBe("");
    fireEvent.change(restored, { target: { value: "sent message" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).not.toBe(firstId);
  });
  it("an A send completing after switch to B does not clear B's draft", async () => {
    let resolveSend: (value: boolean) => void = () => undefined;
    const onSend = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSend = resolve; }),
    );
    const { rerender } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "message for A" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    sessionStorage.setItem("composer-draft-9", "draft for B");
    rerender(<Composer {...baseProps} mode="idle" agentId={9} onSend={onSend} />);
    await waitFor(() => expect(ta.value).toBe("draft for B"));
    resolveSend(true);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    expect(ta.value).toBe("draft for B");
  });
  it("keeps edits typed while a send is in flight and still refuses attachments", async () => {
    let resolveSend: (value: boolean) => void = () => undefined;
    const onSend = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSend = resolve; }),
    );
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/new.png"));
    renderComposerTree(
      <Composer
        {...baseProps}
        mode="idle"
        agentId={7}
        onSend={onSend}
        onAttachImage={onAttachImage}
      />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "original snapshot" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Sending" });

    // meiva:note
    // The textarea stays editable mid-flight: `readOnly` here would tear down an
    // Android IME connection and silently drop the next voice-input commit.
    // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
    fireEvent.change(ta, { target: { value: "later edit" } });
    expect(ta.value).toBe("later edit");
    const image = new File(["png"], "later.png", { type: "image/png" });
    fireEvent.drop(screen.getByTestId("composer"), {
      dataTransfer: { files: [image], types: ["Files"] },
    });
    expect(onAttachImage).not.toHaveBeenCalled();
    resolveSend(true);
    await waitFor(() => expect(ta.value).toBe("later edit"));
    expect(screen.queryByRole("button", { name: "Sending" })).toBeNull();
  });
  it("editing a failed draft creates a new client message id", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockResolvedValue(false);
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "first draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    fireEvent.change(ta, { target: { value: "changed draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][2]).not.toBe(onSend.mock.calls[0][2]);
  });
  it("whitespace-only input → does not trigger onSend (button also disabled)", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} />);
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "   \n  " } });
    expect((screen.getByRole<HTMLButtonElement>("button")).disabled).toBe(true);
    fireEvent.keyDown(ta, { key: "Enter" });
    await Promise.resolve();
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("Composer context meter (opt-in)", () => {
  it("renders no context readout by default — those numbers live in the inspector", () => {
    renderComposerTree(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
      />,
    );
    expect(screen.queryByTestId("context-meter")).toBeNull();
    expect(screen.queryByTestId("context-meter-button")).toBeNull();
    expect(screen.queryByTestId("context-meter-ghost")).toBeNull();
    expect(screen.queryByTestId("composer-meta")).toBeNull();
  });
  it("renders no meta line when the agent has no LLM call yet and no hint", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" contextTokens={0} showContextMeter />);
    expect(screen.queryByTestId("composer-meta")).toBeNull();
  });
  it("shows the loading ghost while the token snapshot is pending (cold switch)", () => {
    renderComposer(
      <Composer {...baseProps} mode="idle" contextTokens={0} contextPending showContextMeter />,
    );
    expect(screen.getByTestId("context-meter-ghost")).toBeTruthy();
    expect(screen.queryByTestId("context-meter-button")).toBeNull();
    expect(screen.getByTestId("composer-meta").textContent).toBe("");
  });
  it("shows the bare gauge — never the numeric readout — when opted in", () => {
    renderComposer(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
        showContextMeter
      />,
    );
    const span = screen.getByTestId("composer-meta");
    expect(span.querySelector('[data-testid="context-meter"]')).toBeTruthy();
    expect(span.textContent).toBe("");
  });
});

describe("Composer context breakdown panel", () => {
  it("expands anchored above the meta row; the composer input stays interactive", async () => {
    renderComposer(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
        showContextMeter
        agentId={7}
      />,
    );
    fireEvent.click(screen.getByTestId("context-meter-button"));
    const panel = await screen.findByTestId("context-breakdown-panel");
    expect(panel.className).toContain("absolute");
    expect(panel.className).toContain("bottom-full");
    expect(screen.queryByRole("dialog")).toBeNull();
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    expect(ta.disabled).toBe(false);
    fireEvent.change(ta, { target: { value: "still typing" } });
    expect(ta.value).toBe("still typing");
    fireEvent.click(screen.getByTestId("context-breakdown-close"));
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
    expect(ta.value).toBe("still typing");
  });
  it("is mutually exclusive with the slash dropdown (single popover state)", async () => {
    commandList = [{ name: "recap", description: "recap the thread", instruction_hint: "" }];
    renderComposer(
      <Composer
        {...baseProps}
        mode="idle"
        contextTokens={26000}
        maxContextTokens={1000000}
        softCompactTokens={600000}
        hardCompactTokens={800000}
        showContextMeter
        agentId={7}
      />,
    );
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "/rec" } });
    await screen.findByTestId("slash-autocomplete");
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-panel");
    expect(screen.queryByTestId("slash-autocomplete")).toBeNull();
  });
});
