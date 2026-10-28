import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { formatAbsolute, formatRelative } from "@/lib/format/time";
import { foldNotices } from "@/lib/fold/notices";
import type { PageRow, SystemEvent } from "@/lib/contracts/types";
import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgent,
  getAgentInspectLive,
  getAgentInspectStatistics,
  getAgentInspectWidgets,
  getAgentRoster,
  getSettings,
  liveFixture,
  panelState,
  putSetting,
  QueryClientProvider,
  render,
  resolveNotice,
  setupInspectorTest,
  setInspectorPages,
  streamHandlers,
  widgetFixture,
  widgetNotice,
} from "./inspector-test-mock";

vi.mock("@/lib/transport/api", () => ({
  api: {
    getAgentInspectStatistics,
    getAgentInspectLive,
    getAgentInspectWidgets,
    getAgentRoster,
    getAgent,
    resolveNotice,
    putSetting,
    getSettings,
  },
}));

vi.mock("@/lib/transport/useEventStream", () => ({
  EventStreamProvider: ({ children }: { children: React.ReactNode }) => children,
  useEventStream: (
    onSystemEvent: (event: unknown) => void,
    onConnectionEvent: (event: { type: string }) => void,
  ) => {
    streamHandlers.system = onSystemEvent;
    streamHandlers.connection = onConnectionEvent;
  },
}));

import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel", () => {
  it("renders the notice section when agent has an open notice", async () => {
    const createdAt = "2026-06-15T13:30:00Z";
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 1,
          title: "Approve deploy?",
          content: "Can we push to prod?",
          priority: "P0",
          require_response: true,
          blocking: true,
          created_at: createdAt,
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Notice")).toBeTruthy());
    expect(screen.getByText("P0")).toBeTruthy();
    expect(screen.getByText("Blocking")).toBeTruthy();
    expect(screen.queryByText("Needs your answer")).toBeNull();
    expect(
      screen.getByText(`${formatRelative(createdAt, new Date())}, ${formatAbsolute(createdAt)}`),
    ).toBeTruthy();
    expect(screen.getByText("Approve deploy?")).toBeTruthy();
    expect(screen.getByText("Can we push to prod?")).toBeTruthy();
  });

  it("renders the same numbered question controls in the inspector", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 9,
          title: "Which layout?",
          content: "Choose the scope.",
          priority: "P2",
          require_response: true,
          blocking: false,
          created_at: "2026-10-14T00:00:00Z",
          question: {
            type: "single_select",
            options: [{ id: "z", label: "First layout" }, { id: "a", label: "Second layout" }],
            allow_other: false,
          },
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByRole("radio", { name: "1. First layout" })).toBeTruthy());
    expect(screen.getByRole("radio", { name: "2. Second layout" })).toBeTruthy();
    expect(screen.queryByRole("radio", { name: "3. Other" })).toBeNull();
    expect(screen.getByLabelText("Comment (optional)")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Submit answer" }).hasAttribute("disabled")).toBe(true);
  });

  it("hides the Notice section when no notice is open", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ notice: null }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.queryByText("Notice")).toBeNull();
    expect(screen.queryByText("No open notice")).toBeNull();
  });

  it("updates the notice without rereading statistics when notice SSE arrives", async () => {
    const queryClient = createInspectorQueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    renderWithBrowser(
      <QueryClientProvider client={queryClient}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());

    act(() => {
      const event = { agent_id: 1, role: "notice_posted", notice_id: 1, priority: "P2", title: "t", task_id: null } as SystemEvent;
      for (const { key } of foldNotices(event).invalidations) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
      streamHandlers.system?.(event);
    });

    expect(invalidate.mock.calls.filter(([options]) => options?.queryKey?.[0] === "agent-inspect-live")).toHaveLength(1);
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["agent-inspect", 1] });
    expect(getAgentInspectStatistics).toHaveBeenCalledTimes(1);
  });

  it("renders the Notice reply surface last without redundant open wording", async () => {
    const page = {
      id: 7,
      agent_id: 1,
      name: "task-dashboard",
      port: 4173,
      title: "Task dashboard",
      serve_dir: null,
      url: "http://gateway.test/pages/7-task-dashboard/",
      created_at: "2026-08-24T12:00:00Z",
      closed_at: null,
    } satisfies PageRow;
    setInspectorPages([page]);
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 5,
          title: "Approve?",
          content: "ok?",
          priority: "P1",
          require_response: true,
          blocking: false,
          created_at: "2026-06-14T12:00:00Z",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Notice")).toBeTruthy());
    expect(screen.getByRole("textbox")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close without answering" })).toBeTruthy();
    const notice = screen.getByText("Notice");
    const timelineLink = screen.getByText("Open run timeline");
    expect(
      timelineLink.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    const noticeSection = notice.closest("section");
    expect(noticeSection).not.toBeNull();
    expect(within(noticeSection!).queryByText(/open/i)).toBeNull();
  });

  it("resolving a notice re-enables the reply box when a new notice takes its place", async () => {
    const mk = (id: number, title: string) => ({
      id,
      title,
      content: null,
      priority: "P1" as const,
      require_response: true,
      blocking: false,
      created_at: "2026-06-14T12:00:00Z",
    });
    getAgentInspectLive
      .mockResolvedValueOnce(liveFixture({ notice: mk(5, "First?") }))
      .mockResolvedValue(liveFixture({ notice: mk(6, "Second?") }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("First?")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "done" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    await waitFor(() => expect(screen.getByText("Second?")).toBeTruthy());

    const send = screen.getByLabelText("Send answer");
    expect(send.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "next" } });
    expect(send.hasAttribute("disabled")).toBe(false);
  });

  it("shows a nonblocking question without redundant answer status", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        notice: {
          id: 2,
          title: "Milestone reached",
          content: null,
          priority: "P2",
          require_response: true,
          blocking: false,
          created_at: "2026-06-14T12:00:00Z",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Milestone reached")).toBeTruthy());
    expect(screen.queryByText("Needs your answer")).toBeNull();
    expect(screen.queryByText("Blocking")).toBeNull();
    expect(screen.getByText("P2")).toBeTruthy();
    expect(screen.getByText("Milestone reached")).toBeTruthy();
  });

  it("hides available-but-empty shells and an empty config overlay", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ shells: [], config_overlay: {} }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Liveness")).toBeTruthy());
    expect(screen.queryByText("Persistent shells")).toBeNull();
    expect(screen.queryByText("Configuration overlay")).toBeNull();
  });

  it("does not render observation details", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Birth")).toBeTruthy());
    expect(screen.queryByText("Observation")).toBeNull();
    expect(screen.queryByText(/Machine probe:/)).toBeNull();
  });

  it("does not report an unavailable shell observation as an empty set", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ shells: [], shells_available: false }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Shell observation unavailable")).toBeTruthy());
    expect(screen.getByText("Persistent shells")).toBeTruthy();
  });

  it("renders skill-list config keys in canonical dash spelling (display_name)", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        config_overlay: {
          skills_to_inject_into_system_prompt: ["example_skill", "*"],
          llm_model: "claude-opus-4-8",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Configuration overlay")).toBeTruthy());
    expect(screen.getByText('["example-skill","*"]')).toBeTruthy();
    expect(screen.queryByText(/example_skill/)).toBeNull();
    expect(screen.getByText("claude-opus-4-8")).toBeTruthy();
  });

  it("window selector re-queries with the chosen hours", async () => {
    getAgentInspectStatistics.mockResolvedValueOnce(fixture({ window_hours: 24 }));
    getAgentInspectStatistics.mockResolvedValue(fixture({ window_hours: 1 }));
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    expect(getAgentInspectStatistics).toHaveBeenCalledWith(1, 24, expect.any(AbortSignal));
    expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("24");

    fireEvent.change(screen.getByLabelText("Cost + activity window"), { target: { value: "1" } });

    await waitFor(() =>
      expect(getAgentInspectStatistics).toHaveBeenCalledWith(1, 1, expect.any(AbortSignal)),
    );
    await waitFor(() =>
      expect(screen.getByLabelText<HTMLSelectElement>("Cost + activity window").value).toBe("1"),
    );
  });

  it("labels a retention-clamped selected window with the applied hours", async () => {
    panelState.hours = 168;
    getAgentInspectStatistics.mockResolvedValue(
      fixture({ window_hours: 168, applied_window_hours: 84 }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());
    const select = screen.getByLabelText<HTMLSelectElement>("Cost + activity window");
    expect(select.selectedOptions[0].text).toBe("7d · 84h");
    expect(Array.from(select.options).find((option) => option.value === "24")?.text).toBe(
      "24h",
    );
  });

  it("offers only cumulative and time-based windows", async () => {
    render(<InspectorPanel agentId={1} />);
    await screen.findByText("Persistent shells");
    const select = screen.getByLabelText<HTMLSelectElement>("Cost + activity window");
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "all", "0", "1", "24", "168",
    ]);
  });

  it("shows an error message when the fetch fails", async () => {
    getAgentInspectLive.mockRejectedValue(new Error("boom"));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy());
  });

  it("stale refresh: a failed background poll keeps the sections and shows the stale dot (no cold-error swap)", async () => {
    const qc = createInspectorQueryClient();
    getAgentInspectStatistics.mockResolvedValueOnce(fixture());
    renderWithBrowser(
      <QueryClientProvider client={qc}>
        <InspectorPanel agentId={1} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Persistent shells")).toBeTruthy());

    getAgentInspectStatistics.mockRejectedValue(new Error("refresh failed"));
    await act(async () => {
      await qc.refetchQueries({ queryKey: ["agent-inspect", 1, 24] });
    });

    await waitFor(() => expect(screen.getByLabelText("Live refresh failing")).toBeTruthy());
    expect(screen.getByText("Persistent shells")).toBeTruthy();
    expect(screen.queryByText("refresh failed")).toBeNull();
  });

  it("refuses an inspect payload whose agent_id does not match the selection", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({ agent_id: Number.NaN }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(getAgentInspectLive).toHaveBeenCalled());
    expect(screen.queryByText("dev-server")).toBeNull();
    await waitFor(() => expect(screen.getByText("No data")).toBeTruthy());
  });

  it("renders shell rows as plain text when a shell id is invalid", async () => {
    getAgentInspectLive.mockResolvedValue(
      liveFixture({
        shells: [
          { id: Number.NaN, name: "bad-shell", created_at: null, uptime_seconds: 10 },
        ],
      }),
    );
    render(<InspectorPanel agentId={1} />);

    await waitFor(() => expect(screen.getByText("bad-shell")).toBeTruthy());
    expect(screen.getByText("bad-shell").closest("a")).toBeNull();
  });
});


describe("InspectorPanel plugin widgets", () => {
  it("renders a taskList widget as a titled section of fleet-task links", async () => {
    getAgentInspectWidgets.mockResolvedValue([widgetFixture()]);
    render(<InspectorPanel agentId={1} />);

    expect(await screen.findByText("Tasks")).toBeTruthy();
    const first = await screen.findByRole("link", { name: /Ship the inspector fix/ });
    expect(first.getAttribute("href")).toBe("/fleet?task=42");
    expect(screen.getByRole("link", { name: /Reply to QA/ }).getAttribute("href")).toBe(
      "/fleet?task=43",
    );
  });

  it("puts the fleet task list below the page section and above the runtime sections", async () => {
    const page = {
      id: 7,
      agent_id: 1,
      name: "task-dashboard",
      port: 4173,
      title: "Task dashboard",
      serve_dir: null,
      url: "http://gateway.test/pages/7-task-dashboard/",
      created_at: "2026-08-24T12:00:00Z",
      closed_at: null,
    } satisfies PageRow;
    setInspectorPages([page]);
    getAgentInspectWidgets.mockResolvedValue([widgetFixture()]);
    render(<InspectorPanel agentId={1} />);

    const pageSection = await screen.findByText("Page");
    const tasks = await screen.findByText("Tasks");
    const shells = await screen.findByText("Persistent shells");
    const before = (first: Node, second: Node) =>
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING;
    expect(before(pageSection, tasks)).toBeTruthy();
    expect(before(tasks, shells)).toBeTruthy();
    setInspectorPages([]);
  });

  it("slots a widget into the section order (750: between the configuration overlay and the notice)", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ notice: widgetNotice }));
    getAgentInspectWidgets.mockResolvedValue([widgetFixture({ order: 750 })]);
    render(<InspectorPanel agentId={1} />);

    const runLink = await screen.findByRole("link", { name: "Open run timeline" });
    const widgetLink = await screen.findByRole("link", { name: /Ship the inspector fix/ });
    const reply = screen.getByRole("textbox");
    const configOverlay = screen.getByText("Configuration overlay");
    expect(
      runLink.compareDocumentPosition(widgetLink) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      configOverlay.compareDocumentPosition(widgetLink) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      widgetLink.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("the notice section carries the jump into the fleet inbox", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ notice: widgetNotice }));
    render(<InspectorPanel agentId={1} />);

    const jump = await screen.findByRole("link", { name: "Notification" });
    expect(jump.getAttribute("href")).toBe("/fleet?notice=92");
  });

  it("renders no widget area when no plugin registers one", async () => {
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(getAgentInspectWidgets).toHaveBeenCalled());
    expect(screen.queryByText("Tasks")).toBeNull();
  });

  it("a widgets fetch failure leaves the rest of the panel intact", async () => {
    getAgentInspectWidgets.mockRejectedValue(new Error("HTTP 500: widgets unavailable"));
    render(<InspectorPanel agentId={1} />);

    await screen.findByRole("link", { name: "Open run timeline" });
    await screen.findAllByText("Activity");
    expect(screen.queryByText("Tasks")).toBeNull();
  });
});
