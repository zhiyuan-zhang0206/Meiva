import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  cleanupInspectorTest,
  createInspectorQueryClient,
  fixture,
  getAgentInspectLive,
  getAgentInspectStatistics,
  liveFixture,
  panelState,
  QueryClientProvider,
  render,
  setupInspectorTest,
} from "./inspector-test-mock";
import { InspectorPanel } from "./inspector-panel";

beforeEach(setupInspectorTest);
afterEach(cleanupInspectorTest);

describe("InspectorPanel", () => {
  it("hides available-but-empty shells and an empty config overlay", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture({ shells: [], config_overlay: {} }));
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.queryByLabelText("Liveness loading")).toBeNull());
    expect(screen.queryByText("Persistent shells")).toBeNull();
    expect(screen.queryByText("Configuration")).toBeNull();
  });

  it("does not render observation details", async () => {
    getAgentInspectLive.mockResolvedValue(liveFixture());
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.queryByLabelText("Liveness loading")).toBeNull());
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
          fast_acknowledgement: true,
          llm_model: "claude-opus-4-8",
        },
      }),
    );
    render(<InspectorPanel agentId={1} />);
    await waitFor(() => expect(screen.getByText("Configuration")).toBeTruthy());
    expect(screen.getByText('["example-skill","*"]')).toBeTruthy();
    expect(screen.getByText("fast_acknowledgement")).toBeTruthy();
    expect(screen.getByText("true")).toBeTruthy();
    expect(screen.queryByText(/example_skill/)).toBeNull();
    expect(screen.getByText("Claude Opus 4.8")).toBeTruthy();
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

  it("stale refresh: a failed background poll keeps the sections quietly (no stale dot, no cold-error swap)", async () => {
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

    expect(screen.queryByLabelText("Live refresh failing")).toBeNull();
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
