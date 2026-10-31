import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessageDeliveryUnknownError } from "@/lib/transport/api";
import { interactionTimingResetForTest, noteTurnStart } from "@/lib/format/interaction-timing";
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
// meiva:note
// The composer reports a failed command fetch to the sink the shell hands in, so every render in
// this file states one; a test that cares reads what it collected.
const diagnostics = { warn: vi.fn<(message: string) => void>(), error: vi.fn<(message: string, cause: unknown) => void>() };
const baseProps = {
  diagnostics,
  contextTokens: 0,
  onSend: vi.fn(() => Promise.resolve(true)),
  onStop: vi.fn(),
};

function renderComposerTree(ui: React.ReactElement, wallNow: () => number = () => Date.now()) {
  return renderWithProviders(ui, { wallNow });
}
const renderComposer = renderComposerTree;

function renderComposerAgent(agentId: number) {
  renderComposerTree(<Composer {...baseProps} mode="idle" agentId={agentId} />);
  return screen.getByTestId<HTMLTextAreaElement>("composer-input");
}

describe("Composer file upload (drag + paste)", () => {
  it("puts the upload control inside the input row, not in the meta line", () => {
    renderComposer(
      <Composer {...baseProps} mode="idle">
        <button type="button" aria-label="Upload file">attach</button>
      </Composer>,
    );
    const upload = screen.getByRole("button", { name: "Upload file" });
    const input = screen.getByTestId("composer-input");
    expect(upload.parentElement).toBe(input.parentElement);
    expect(screen.queryByTestId("composer-meta")).toBeNull();
  });
  it("does not show the file-upload hint when no file delivery is in flight", () => {
    renderComposerTree(<Composer {...baseProps} mode="idle" filesUploading={false} />);
    expect(screen.queryByTestId("composer-upload-hint")).toBeNull();
  });
  it("shows the file-upload hint without blocking Enter-to-send", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(
      <Composer
        {...baseProps}
        mode="idle"
        filesUploading
        onSend={onSend}
      />,
    );
    expect(screen.getByTestId("composer-upload-hint").textContent).toBe(
      "Uploading file(s) — Enter sends the text now; files are delivered when the upload finishes.",
    );
    const textarea = screen.getByTestId("composer-input");
    fireEvent.change(textarea, { target: { value: "send this now" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith("send this now", [], expect.any(String)),
    );
  });
  it("dropping files calls onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    const f1 = new File(["a"], "a.png", { type: "image/png" });
    const f2 = new File(["bb"], "b.png", { type: "image/png" });
    fireEvent.drop(screen.getByTestId("composer"), {
      dataTransfer: { files: [f1, f2], types: ["Files"] },
    });
    expect(onUploadFiles).toHaveBeenCalledWith([f1, f2]);
  });
  it("dropping with no files does not call onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    fireEvent.drop(screen.getByTestId("composer"), {
      dataTransfer: { files: [], types: [] },
    });
    expect(onUploadFiles).not.toHaveBeenCalled();
  });
  it("pasting files calls onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    const img = new File(["x"], "img.png", { type: "image/png" });
    fireEvent.paste(screen.getByTestId("composer-input"), {
      clipboardData: { files: [img] },
    });
    expect(onUploadFiles).toHaveBeenCalledWith([img]);
  });
  it("pasted images use file delivery and Enter sends text while that upload is in flight", async () => {
    let finishUpload: (() => void) | undefined;
    const onUploadFiles = vi.fn(
      () => new Promise<void>((resolve) => { finishUpload = resolve; }),
    );
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(
      <Composer
        {...baseProps}
        mode="idle"
        onSend={onSend}
        onUploadFiles={onUploadFiles}
      />,
    );
    const textarea = screen.getByTestId("composer-input");
    const image = new File(["img"], "paste.png", { type: "image/png" });
    fireEvent.paste(textarea, {
      clipboardData: { files: [image], types: ["Files"] },
    });
    expect(onUploadFiles).toHaveBeenCalledWith([image]);
    expect(screen.queryByTestId("composer-attachments")).toBeNull();
    fireEvent.change(textarea, { target: { value: "describe the uploaded file" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        "describe the uploaded file",
        [],
        expect.any(String),
      ),
    );
    finishUpload?.();
  });
  it("pasting plain text (no files) does not call onUploadFiles", () => {
    const onUploadFiles = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    fireEvent.paste(screen.getByTestId("composer-input"), {
      clipboardData: { files: [] },
    });
    expect(onUploadFiles).not.toHaveBeenCalled();
  });
});

describe("Composer image attachments", () => {
  // meiva:note
  // The previews are object URLs the browser port hands out (`fakeBrowser().objectUrls`), so a case
  // reads `browser.actions.createdUrls` / `revokedUrls` instead of replacing the URL API; a preview
  // that is never released shows up as a created URL with no matching revoke.
  const imageFile = () => new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  it("pasting an image attaches a thumbnail and uploads it", async () => {
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/shot.png"));
    renderComposerTree(<Composer {...baseProps} mode="idle" onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("composer-attachments")).toBeTruthy();
  });
  it("revokes the preview object URL when the composer unmounts", async () => {
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/shot.png"));
    const { browser, unmount } = renderComposerTree(<Composer {...baseProps} mode="idle" onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(screen.getByTestId("composer-attachments")).toBeTruthy());
    unmount();
    expect(browser.actions.createdUrls).toHaveLength(1);
    expect(browser.actions.revokedUrls).toEqual(browser.actions.createdUrls);
  });
  it("sends text + the resolved image url as onSend's second arg", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/shot.png"));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalled());
    fireEvent.change(ta, { target: { value: "what is this" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        "what is this",
        ["/api/agents/7/uploads/shot.png"],
        expect.any(String),
      ),
    );
  });
  it("drops native images when switching agents while keeping text drafts and ordinary file delivery", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onUploadFiles = vi.fn();
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/807/uploads/shot.png"));
    const { browser, rerender } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={807} onSend={onSend} onAttachImage={onAttachImage} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "native draft" } });
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(false));
    sessionStorage.setItem("composer-draft-809", "external draft");
    rerender(<Composer {...baseProps} mode="idle" agentId={809} onSend={onSend} onUploadFiles={onUploadFiles} />);
    expect(screen.queryByTestId("composer-attachments")).toBeNull();
    expect(browser.actions.revokedUrls).toEqual(browser.actions.createdUrls);
    expect(ta.value).toBe("external draft");
    expect(sessionStorage.getItem("composer-draft-807")).toBe("native draft");
    const ordinaryFile = new File(["notes"], "notes.txt", { type: "text/plain" });
    const pastedImage = imageFile();
    fireEvent.paste(ta, { clipboardData: { files: [pastedImage, ordinaryFile], types: ["Files"] } });
    expect(onUploadFiles).toHaveBeenCalledWith([pastedImage]);
    expect(onUploadFiles).toHaveBeenCalledWith([ordinaryFile]);
    expect(onAttachImage).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("external draft", [], expect.any(String)));
  });
  it("releases an image preview when selection changes before the attachment commits", () => {
    const onAttachImage = vi.fn(() => new Promise<string>(() => undefined));
    const { browser, rerender } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={837} onAttachImage={onAttachImage} />,
    );
    act(() => {
      fireEvent.paste(screen.getByTestId("composer-input"), {
        clipboardData: { files: [imageFile()], types: ["Files"] },
      });
      rerender(<Composer {...baseProps} mode="idle" agentId={839} />);
    });
    expect(screen.queryByTestId("composer-attachments")).toBeNull();
    expect(browser.actions.revokedUrls).toEqual(browser.actions.createdUrls);
  });
  it("ignores an old agent's pending upload after switching away and returning", async () => {
    let resolveUpload!: (url: string) => void;
    const onAttachImage = vi.fn(() => new Promise<string>((resolve) => { resolveUpload = resolve; }));
    const onSend = vi.fn(() => Promise.resolve(true));
    const { rerender } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={817} onSend={onSend} onAttachImage={onAttachImage} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "saved native text" } });
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(true);
    rerender(<Composer {...baseProps} mode="idle" agentId={819} onSend={onSend} />);
    fireEvent.change(ta, { target: { value: "send while old upload is pending" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("send while old upload is pending", [], expect.any(String)));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    rerender(<Composer {...baseProps} mode="idle" agentId={817} onSend={onSend} onAttachImage={onAttachImage} />);
    expect(ta.value).toBe("saved native text");
    await act(() => {
      resolveUpload("/api/agents/817/uploads/shot.png");
      return Promise.resolve();
    });
    expect(screen.queryByTestId("composer-attachments")).toBeNull();
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenLastCalledWith("saved native text", [], expect.any(String)));
  });
  it("invalidates pending images on capability loss without resetting them for callback replacement", async () => {
    let resolveOldUpload!: (url: string) => void;
    let resolveNewUpload!: (url: string) => void;
    const oldAttach = vi.fn(() => new Promise<string>((resolve) => { resolveOldUpload = resolve; }));
    const newAttach = vi.fn((_file: File) => new Promise<string>((resolve) => { resolveNewUpload = resolve; }));
    const onSend = vi.fn(() => Promise.resolve(true));
    const { rerender } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={827} onSend={onSend} onAttachImage={oldAttach} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "keep this draft" } });
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    rerender(<Composer {...baseProps} mode="idle" agentId={827} onSend={onSend} />);
    expect(screen.queryByTestId("composer-attachments")).toBeNull();
    expect(ta.value).toBe("keep this draft");
    rerender(<Composer {...baseProps} mode="idle" agentId={827} onSend={onSend} onAttachImage={newAttach} />);
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await act(() => {
      resolveOldUpload("/api/agents/827/uploads/old.png");
      return Promise.resolve();
    });
    expect(screen.getAllByRole("button", { name: "Remove shot.png" })).toHaveLength(1);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(true);
    rerender(<Composer {...baseProps} mode="idle" agentId={827} onSend={onSend} onAttachImage={(file) => newAttach(file)} />);
    await act(() => {
      resolveNewUpload("/api/agents/827/uploads/new.png");
      return Promise.resolve();
    });
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("keep this draft", ["/api/agents/827/uploads/new.png"], expect.any(String)));
  });
  it("normal Send preserves an unchanged image snapshot identity after ambiguity", async () => {
    const onSend = vi
      .fn<(content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>>()
      .mockImplementationOnce((_content, _imageUrls, clientMessageId) =>
        Promise.reject(new MessageDeliveryUnknownError(clientMessageId)),
      )
      .mockResolvedValueOnce(true);
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/shot.png"));
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
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sending" })).toBeNull());
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Remove shot.png" }).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
    expect(onSend.mock.calls[1][1]).toEqual(["/api/agents/7/uploads/shot.png"]);
    expect(onSend.mock.calls[1][2]).toBe(onSend.mock.calls[0][2]);
  });
  it("send stays disabled while an image is still uploading", async () => {
    let resolve!: (u: string) => void;
    const onAttachImage = vi.fn(() => new Promise<string>((r) => { resolve = r; }));
    renderComposerTree(<Composer {...baseProps} mode="idle" onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "hi" } });
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalled());
    const btn = screen.getByRole<HTMLButtonElement>("button", { name: "Send message" });
    expect(btn.disabled).toBe(true);
    resolve("/api/agents/7/uploads/shot.png");
    await waitFor(() => expect(btn.disabled).toBe(false));
  });
  it("image-only (no text) is sendable once uploaded", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    const onAttachImage = vi.fn(() => Promise.resolve("/api/agents/7/uploads/a.png"));
    renderComposerTree(<Composer {...baseProps} mode="idle" onSend={onSend} onAttachImage={onAttachImage} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(onAttachImage).toHaveBeenCalled());
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith(
        "",
        ["/api/agents/7/uploads/a.png"],
        expect.any(String),
      ),
    );
  });
});

describe("Composer sessionStorage draft persistence", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });
  const draftKey = (id: number) => `composer-draft-${id}`;
  it("restores the draft from sessionStorage on mount", () => {
    sessionStorage.setItem(draftKey(7), "hello agent 7");
    const ta = renderComposerAgent(7);
    expect(ta.value).toBe("hello agent 7");
  });
  it("starts empty when no draft exists in sessionStorage", () => {
    const ta = renderComposerAgent(7);
    expect(ta.value).toBe("");
  });
  it("saves the draft to sessionStorage as the user types", () => {
    renderComposerAgent(7);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "draft text" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("draft text");
  });
  it("removes the draft from sessionStorage when the input is cleared", () => {
    renderComposerAgent(7);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "something" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("something");
    fireEvent.change(ta, { target: { value: "" } });
    expect(sessionStorage.getItem(draftKey(7))).toBeNull();
  });
  it("clears the draft on a successful send", async () => {
    const onSend = vi.fn(() => Promise.resolve(true));
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "send this" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("send this");
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(sessionStorage.getItem(draftKey(7))).toBeNull();
  });
  it("does NOT clear the draft on a failed send", async () => {
    let now = 100;
    const onSend = vi.fn(() => Promise.resolve(false));
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} onSend={onSend} />, () => now);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "failed send" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("failed send");
    fireEvent.keyDown(ta, { key: "Enter" });
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    await waitFor(() => {
      telemetryTestControl.reset();
      now = 150;
      noteTurnStart(7, now);
      expect(telemetryTestControl.bufferSize()).toBe(0);
    });
    expect(sessionStorage.getItem(draftKey(7))).toBe("failed send");
  });
  it("switches draft when agentId prop changes", () => {
    const { rerender } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={7} />,
    );
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "for agent 7" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("for agent 7");
    sessionStorage.setItem(draftKey(9), "for agent 9");
    rerender(<Composer {...baseProps} mode="idle" agentId={9} />);
    expect(sessionStorage.getItem(draftKey(7))).toBe("for agent 7");
    expect((screen.getByTestId<HTMLTextAreaElement>("composer-input")).value).toBe(
      "for agent 9",
    );
  });
  it("does not write to sessionStorage when agentId is null", () => {
    renderComposerTree(<Composer {...baseProps} mode="disabled" agentId={null} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "no agent" } });
    for (let i = 0; i < 100; i++) {
      expect(sessionStorage.getItem(draftKey(i))).toBeNull();
    }
  });
  it("clears old draft on empty input after switching agents", () => {
    const { rerender } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={7} />,
    );
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "text for 7" } });
    expect(sessionStorage.getItem(draftKey(7))).toBe("text for 7");
    sessionStorage.removeItem(draftKey(9));
    rerender(<Composer {...baseProps} mode="idle" agentId={9} />);
    expect((screen.getByTestId<HTMLTextAreaElement>("composer-input")).value).toBe("");
    expect(sessionStorage.getItem(draftKey(7))).toBe("text for 7");
  });
});
