import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DeliveredUploadStatus, StagedFile } from "@/lib/contracts/types";
import { interactionTimingResetForTest, noteTurnStart } from "@/lib/format/interaction-timing";
import { telemetryTestControl } from "@/lib/telemetry/telemetry";
import { Composer } from "./composer";

import { resetTransport, transport } from "@/test-support/fake-api";

const stagedId1 = "a".repeat(32);
const stagedId2 = "b".repeat(32);
const batchId1 = "c".repeat(32);
const sha = "d".repeat(64);

beforeEach(() => {
  resetTransport();
  transport.getCommands.mockImplementation((_agentId?: number | null) => Promise.resolve(commandList));
  transport.deleteStagedUploadFile.mockResolvedValue(undefined);
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
    }),
  );
});

let commandList: { name: string; description: string; instruction_hint: string }[] = [];

const getCommandsMock = transport.getCommands;
beforeEach(() => {
  interactionTimingResetForTest();
  telemetryTestControl.reset();
  getCommandsMock.mockImplementation((_agentId?: number | null) => Promise.resolve(commandList));
});
afterEach(() => {
  cleanup();
  commandList = [];
  getCommandsMock.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const diagnostics = {
  warn: vi.fn<(message: string) => void>(),
  error: vi.fn<(message: string, cause: unknown) => void>(),
};
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
  it("dropping files calls onUploadFiles when provided", () => {
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
  it("pasting files calls onUploadFiles when provided", () => {
    const onUploadFiles = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    const img = new File(["x"], "img.png", { type: "image/png" });
    fireEvent.paste(screen.getByTestId("composer-input"), {
      clipboardData: { files: [img] },
    });
    expect(onUploadFiles).toHaveBeenCalledWith([img]);
  });
  it("pasting plain text (no files) does not stage or upload files", () => {
    const onUploadFiles = vi.fn();
    renderComposerTree(<Composer {...baseProps} mode="idle" onUploadFiles={onUploadFiles} />);
    fireEvent.paste(screen.getByTestId("composer-input"), {
      clipboardData: { files: [] },
    });
    expect(onUploadFiles).not.toHaveBeenCalled();
  });
});

describe("Composer unified staged attachments & batch delivery", () => {
  const imageFile = () => new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  const textFile = () => new File(["hello"], "notes.txt", { type: "text/plain" });

  it("stages pasted images and non-image files into the attachment strip", async () => {
    transport.stageUploadFile
      .mockResolvedValueOnce({
        file_id: stagedId1,
        name: "shot.png",
        size: 3,
        content_type: "image/png",
        sha256: sha,
      })
      .mockResolvedValueOnce({
        file_id: stagedId2,
        name: "notes.txt",
        size: 5,
        content_type: "text/plain",
        sha256: sha,
      });
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile(), textFile()], types: ["Files"] } });
    await waitFor(() => expect(transport.stageUploadFile).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("composer-attachments")).toBeTruthy();
    expect(screen.getByText("notes.txt")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(
        false,
      ),
    );
  });

  it("revokes image preview object URLs when the composer unmounts", async () => {
    transport.stageUploadFile.mockResolvedValue({
      file_id: stagedId1,
      name: "shot.png",
      size: 3,
      content_type: "image/png",
      sha256: sha,
    });
    const { browser, unmount } = renderComposerTree(
      <Composer {...baseProps} mode="idle" agentId={7} />,
    );
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile()], types: ["Files"] } });
    await waitFor(() => expect(screen.getByTestId("composer-attachments")).toBeTruthy());
    unmount();
    expect(browser.actions.createdUrls).toHaveLength(1);
    expect(browser.actions.revokedUrls).toEqual(browser.actions.createdUrls);
  });

  it("aborts an in-flight stage on remove and deletes a ready staged file on remove", async () => {
    let capturedSignal: AbortSignal | undefined;
    let callCount = 0;
    transport.stageUploadFile.mockImplementation((_agentId, _file, _key, _onProgress, signal) => {
      callCount += 1;
      if (callCount === 1) {
        capturedSignal = signal;
        return new Promise<StagedFile>(() => undefined);
      }
      return Promise.resolve({
        file_id: stagedId2,
        name: "notes.txt",
        size: 5,
        content_type: "text/plain",
        sha256: sha,
      });
    });
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.paste(ta, { clipboardData: { files: [imageFile(), textFile()], types: ["Files"] } });
    await waitFor(() => expect(transport.stageUploadFile).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "Remove shot.png" }));
    expect(capturedSignal?.aborted).toBe(true);
    await waitFor(() =>
      expect(screen.getByTestId("composer-attachment-card").getAttribute("data-status")).toBe(
        "ready",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.txt" }));
    expect(transport.deleteStagedUploadFile).toHaveBeenCalledWith(7, stagedId2);
  });

  it("disables Send while a stage is uploading or failed, and retries a failed stage with the same key", async () => {
    transport.stageUploadFile
      .mockRejectedValueOnce(new Error("network glitch"))
      .mockResolvedValueOnce({
        file_id: stagedId1,
        name: "notes.txt",
        size: 5,
        content_type: "text/plain",
        sha256: sha,
      });
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} />);
    const ta = screen.getByTestId("composer-input");
    fireEvent.change(ta, { target: { value: "please check" } });
    fireEvent.paste(ta, { clipboardData: { files: [textFile()], types: ["Files"] } });
    const retryBtn = await screen.findByRole("button", { name: "Retry notes.txt" });
    const sendBtn = screen.getByRole<HTMLButtonElement>("button", { name: "Send message" });
    expect(sendBtn.disabled).toBe(true);
    const firstKey = transport.stageUploadFile.mock.calls[0][2];
    fireEvent.click(retryBtn);
    await waitFor(() => expect(transport.stageUploadFile).toHaveBeenCalledTimes(2));
    expect(transport.stageUploadFile.mock.calls[1][2]).toBe(firstKey);
    await waitFor(() => expect(sendBtn.disabled).toBe(false));
  });

  it("submits staged file IDs and text on Send, polls until accepted, and keeps Stop usable when busy", async () => {
    const onStop = vi.fn();
    transport.stageUploadFile.mockResolvedValueOnce({
      file_id: stagedId1,
      name: "notes.txt",
      size: 5,
      content_type: "text/plain",
      sha256: sha,
    });
    transport.submitUploadedFiles.mockResolvedValueOnce({
      batch_id: batchId1,
      agent_id: 7,
      directory: `/tmp/uploads/${batchId1}`,
      status_url: `/api/keyed/v1/agents/7/uploads/${batchId1}`,
      files: [
        {
          ordinal: 0,
          name: "notes.txt",
          staged_id: stagedId1,
          size: 5,
          sha256: sha,
          content_type: "text/plain",
        },
      ],
    });
    let resolveStatus!: (status: DeliveredUploadStatus) => void;
    transport.getUploadDeliveryStatus.mockImplementation(
      () => new Promise<DeliveredUploadStatus>((r) => { resolveStatus = r; }),
    );
    renderComposerTree(
      <Composer {...baseProps} mode="busy" agentId={7} onStop={onStop} />,
    );
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    fireEvent.change(ta, { target: { value: "review notes" } });
    fireEvent.paste(ta, { clipboardData: { files: [textFile()], types: ["Files"] } });
    await waitFor(() =>
      expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(
        false,
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() =>
      expect(transport.submitUploadedFiles).toHaveBeenCalledWith(
        7,
        [stagedId1],
        "review notes",
        expect.any(String),
      ),
    );
    await waitFor(() => expect(ta.value).toBe(""));
    expect(screen.getByTestId("composer-upload-hint").textContent).toContain("Delivering");
    const stopBtn = screen.getByRole<HTMLButtonElement>("button", { name: "Stop current turn" });
    expect(stopBtn.disabled).toBe(false);
    fireEvent.click(stopBtn);
    expect(onStop).toHaveBeenCalledTimes(1);

    act(() => {
      resolveStatus({
        batch_id: batchId1,
        state: "accepted",
        reason: null,
        directory: `/tmp/uploads/${batchId1}`,
        message_text: "review notes",
        files: [],
        inbound_id: 42,
        source: { machine: "m1", home: "/tmp" },
        target: { machine: "m1", home: "/tmp" },
        attempts: 1,
        next_attempt_at: null,
      });
    });
    await waitFor(() => expect(screen.queryByTestId("composer-upload-hint")).toBeNull());
  });

  it("surfaces a held batch with Restore to draft and Retry delivery actions", async () => {
    sessionStorage.setItem("composer-upload-batch-7", batchId1);
    transport.getUploadDeliveryStatus.mockResolvedValueOnce({
      batch_id: batchId1,
      state: "hold",
      reason: "disk full",
      directory: `/tmp/uploads/${batchId1}`,
      message_text: "held text",
      files: [
        {
          ordinal: 0,
          name: "notes.txt",
          staged_id: stagedId1,
          size: 5,
          sha256: sha,
          content_type: "text/plain",
        },
      ],
      inbound_id: null,
      source: { machine: "m1", home: "/tmp" },
      target: { machine: "m1", home: "/tmp" },
      attempts: 3,
      next_attempt_at: null,
    });
    renderComposerTree(<Composer {...baseProps} mode="idle" agentId={7} />);
    const banner = await screen.findByTestId("composer-held-batch");
    expect(banner.textContent).toContain("disk full");
    fireEvent.click(screen.getByTestId("composer-held-restore"));
    const ta = screen.getByTestId<HTMLTextAreaElement>("composer-input");
    expect(ta.value).toBe("held text");
    expect(screen.getByText("notes.txt")).toBeTruthy();
    expect(screen.queryByTestId("composer-held-batch")).toBeNull();
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
