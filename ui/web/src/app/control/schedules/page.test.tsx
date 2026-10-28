// /control/schedules page tests — loading/error/empty/data states, NL draft
// (spawns writer + navigates), start/stop toggle, and delete with confirm.
//
// happy-dom + RTL + real QueryClient (mock at the api layer); next/navigation
// router is mocked.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Highlight as HighlightComponentType } from "prism-react-renderer";

import { createLazyChunk, type LazyChunk } from "@/components/content/lazy-chunk";
import { PythonCodeHighlighterProvider } from "@/components/content/python-code";
import { useStore } from "@/lib/state/store";
import type { ScheduleSummary, ScheduleView } from "@/lib/contracts/types";

import SchedulesPage from "./page";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});

// meiva:note
// Every render keeps the highlighter pending: a script block's plain text is what these assertions read, and the real chunk would tokenize it mid-test.
// meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md
const pendingHighlighter = createLazyChunk({
  name: "schedules-test-highlighter",
  importModule: () => new Promise<never>(() => undefined),
});

const FULL: ScheduleView = {
  id: 1,
  name: "memory-arbiter",
  description: "consolidate nightly",
  command: "python schedule.py",
  enabled: true,
  status: "running",
  last_error: "boom traceback",
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-29T09:00:00Z",
  script: "print('hi')",
};

const pushSpy = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: pushSpy }) }));

afterEach(() => {
  cleanup();
});
beforeEach(() => {
  vi.restoreAllMocks();
  pushSpy.mockReset();
});

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrap(
  ui: React.ReactElement,
  highlighter: LazyChunk<typeof HighlightComponentType> = pendingHighlighter,
) {
  return renderWithBrowser(
    <QueryClientProvider client={makeQc()}>
      <PythonCodeHighlighterProvider value={highlighter}>{ui}</PythonCodeHighlighterProvider>
    </QueryClientProvider>,
  );
}

const SCHEDULE: ScheduleSummary = {
  id: 1,
  name: "memory-arbiter",
  description: "consolidate nightly",
  command: "python schedule.py",
  enabled: true,
  status: "running",
  last_error: null,
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-29T09:00:00Z",
};

describe("SchedulesPage", () => {
  it("ignores Enter while the draft is pending", async () => {
    transport.listSchedules.mockResolvedValue([]);
    const draft = transport.draftSchedule.mockReturnValue(new Promise(() => undefined));
    wrap(<SchedulesPage />);
    const input = await screen.findByPlaceholderText(/Describe a scheduled task/);
    fireEvent.change(input, { target: { value: "run nightly" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Describe" }).hasAttribute("disabled")).toBe(true));
    fireEvent.keyDown(input, { key: "Enter" });
    expect(draft).toHaveBeenCalledOnce();
  });

  it("does not inherit automatic mutation retries", async () => {
    useStore.setState({ toast: null });
    transport.listSchedules.mockResolvedValue([]);
    const draft = transport.draftSchedule.mockRejectedValue(new Error("response lost"));
    const client = new QueryClient({ defaultOptions: { mutations: { retry: 3, retryDelay: 0 } } });
    renderWithBrowser(<QueryClientProvider client={client}><SchedulesPage /></QueryClientProvider>);
    const input = await screen.findByPlaceholderText(/Describe a scheduled task/);
    fireEvent.change(input, { target: { value: "run nightly" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(useStore.getState().toast).toBe("Draft failed: response lost"));
    expect(draft).toHaveBeenCalledOnce();
    expect(pushSpy).not.toHaveBeenCalled();
  });

  it("shows loading spinner", () => {
    transport.listSchedules.mockReturnValue(new Promise<ScheduleSummary[]>(() => undefined));
    wrap(<SchedulesPage />);
    expect(document.querySelector(".animate-spin")).toBeTruthy();
  });

  it("shows error state", async () => {
    transport.listSchedules.mockRejectedValue(new Error("fail"));
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/Couldn't load schedules/)).toBeTruthy());
  });

  it("shows empty state", async () => {
    transport.listSchedules.mockResolvedValue([]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/No schedules configured/)).toBeTruthy());
  });

  it("renders a schedule with name + status", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    expect(screen.getByText("running")).toBeTruthy();
  });

  it("description cell carries a title attribute for the truncated full text", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    wrap(<SchedulesPage />);
    const cell = await screen.findByText("consolidate nightly");
    expect(cell.getAttribute("title")).toBe("consolidate nightly");
  });

  it("draft: spawns a writer and navigates to the conversation", async () => {
    transport.listSchedules.mockResolvedValue([]);
    const draft = transport.draftSchedule.mockResolvedValue({ agent_id: 42 });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/No schedules configured/)).toBeTruthy());

    const input = screen.getByPlaceholderText(/Describe a scheduled task/);
    fireEvent.change(input, { target: { value: "consolidate nightly" } });
    fireEvent.click(screen.getByRole("button", { name: "Describe" }));

    await waitFor(() => expect(draft).toHaveBeenCalledWith("consolidate nightly"));
    await waitFor(() => expect(pushSpy).toHaveBeenCalledWith("/"));
  });

  it("toggle: a running schedule stops", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    const stop = transport.stopSchedule.mockResolvedValue({ ...SCHEDULE, script: "x", enabled: false });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(stop).toHaveBeenCalledWith(1));
  });

  it("delete: confirms then deletes", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    const del = transport.deleteSchedule.mockResolvedValue({ status: "deleted" });
    window.confirm = vi.fn(() => true);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledWith(1));
  });

  it("toggle: a stopped schedule starts", async () => {
    const stopped: ScheduleSummary = { ...SCHEDULE, enabled: false, status: "stopped" };
    transport.listSchedules.mockResolvedValue([stopped]);
    const start = transport.startSchedule.mockResolvedValue({ ...FULL, enabled: false });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(start).toHaveBeenCalledWith(1));
  });

  it("restart: calls restartSchedule", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    const restart = transport.restartSchedule.mockResolvedValue(FULL);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(restart).toHaveBeenCalledWith(1));
  });

  it("raw form: fills + creates a schedule", async () => {
    transport.listSchedules.mockResolvedValue([]);
    const create = transport.createSchedule.mockResolvedValue(FULL);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText(/No schedules configured/)).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /New \(raw\)/ }));
    fireEvent.change(screen.getByPlaceholderText("memory-arbiter"), {
      target: { value: "my-sched" },
    });
    const textareas = document.querySelectorAll("textarea");
    fireEvent.change(textareas[textareas.length - 1], { target: { value: "print(1)" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(expect.objectContaining({ name: "my-sched" })),
    );
  });

  it("hovering or focusing the expand button warms the code-highlighter chunk (intent prefetch)", async () => {
    // meiva:note
    // Regression coverage for the P2 adversarial-review finding on Meiva #3463: this wiring had no assertion that pointer-enter/focus actually invokes the prefetch, only that a mocked no-op did not blow up. Each event gets a fresh chunk and that chunk's import is the probe, so a row wired to anything else never calls it.
    // meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    const hoverImport = vi.fn(() => new Promise<never>(() => undefined));
    wrap(<SchedulesPage />, createLazyChunk({ name: "test-highlighter", importModule: hoverImport }));
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.pointerEnter(screen.getByRole("button", { name: "Expand" }));
    expect(hoverImport).toHaveBeenCalledTimes(1);

    cleanup();
    const focusImport = vi.fn(() => new Promise<never>(() => undefined));
    wrap(<SchedulesPage />, createLazyChunk({ name: "test-highlighter", importModule: focusImport }));
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.focus(screen.getByRole("button", { name: "Expand" }));
    expect(focusImport).toHaveBeenCalledTimes(1);
  });

  it("expand: shows script, last_error, logs, and run history", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    transport.getSchedule.mockResolvedValue(FULL);
    transport.scheduleLogs.mockResolvedValue({ source: "session", lines: ["log-line-1"] });
    transport.scheduleRuns.mockResolvedValue([
      { id: 7, ran_at: "2026-06-29T09:00:00Z", ok: true, agent_id: 5, note: "did the thing" },
    ]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    await waitFor(() => expect(screen.getByText("print('hi')")).toBeTruthy());
    expect(screen.getByText(/boom traceback/)).toBeTruthy();
    await waitFor(() => expect(screen.getByText("log-line-1")).toBeTruthy());
    expect(screen.getByText(/did the thing/)).toBeTruthy();
  });

  it("expand: an in-progress run renders as … (ok=null), an interrupted one as ✗", async () => {
    // QA P3-6: the run-history "…" path (ok IS NULL = genuinely in-progress)
    // must render — a regression here would leave the live-run state blank.
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    transport.getSchedule.mockResolvedValue(FULL);
    transport.scheduleLogs.mockResolvedValue({ source: "none", lines: [] });
    transport.scheduleRuns.mockResolvedValue([
      { id: 9, ran_at: "2026-06-30T09:00:00Z", ok: null, agent_id: null, note: null },
      { id: 8, ran_at: "2026-06-29T09:00:00Z", ok: false, agent_id: null, note: "interrupted" },
      { id: 7, ran_at: "2026-06-29T08:00:00Z", ok: true, agent_id: null, note: null },
    ]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    await waitFor(() => expect(screen.getByText(/…/)).toBeTruthy());
    expect(screen.getByText(/interrupted/)).toBeTruthy();
  });

  it("does not refetch expanded schedule logs before 15 seconds", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    transport.getSchedule.mockResolvedValue(FULL);
    const logs = transport.scheduleLogs.mockResolvedValue({ source: "none", lines: [] });
    transport.scheduleRuns.mockResolvedValue([]);
    const view = wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());

    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "Expand" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(logs).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(14_999);
      });
      expect(logs).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(logs).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("edit: opens the editor and saves a new script", async () => {
    transport.listSchedules.mockResolvedValue([SCHEDULE]);
    transport.getSchedule.mockResolvedValue(FULL);
    transport.scheduleLogs.mockResolvedValue({ source: "none", lines: [] });
    transport.scheduleRuns.mockResolvedValue([]);
    const update = transport.updateSchedule.mockResolvedValue({ ...FULL, script: "print(2)" });
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("memory-arbiter")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save" })).toBeTruthy());
    const textareas = document.querySelectorAll("textarea");
    fireEvent.change(textareas[textareas.length - 1], { target: { value: "print(2)" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
  });

  it("raw form cancel closes it; a disabled schedule shows (off)", async () => {
    const off: ScheduleSummary = { ...SCHEDULE, enabled: false, status: "stopped" };
    transport.listSchedules.mockResolvedValue([off]);
    wrap(<SchedulesPage />);
    await waitFor(() => expect(screen.getByText("(off)")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /New \(raw\)/ }));
    expect(screen.getByText("Create schedule")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Create schedule")).toBeNull());
  });
});
