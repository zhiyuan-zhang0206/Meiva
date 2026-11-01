import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupHomePageTest, gatewayWrapper, HomePageBound as HomePage, hooksState, makeAgent, resetHomePageTestState, wrap } from "./page-test-mock";

import { transport } from "@/test-support/fake-api";
afterEach(cleanupHomePageTest);
beforeEach(resetHomePageTestState);

describe("sidebar app-entry reset", () => {
  it("resets a loaded collapsed preference when the home surface is entered", () => {
    hooksState.sidebarCollapsed = true;
    wrap(<HomePage />);
    expect(hooksState.setSetting).toHaveBeenCalledWith(
      "display.sidebar_collapsed",
      false,
    );
  });
  it("preserves a collapse that happens after the home surface has mounted", () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const view = renderWithBrowser(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={queryClient}>
          <HomePage />
        </QueryClientProvider>
      </WallClockProvider>,
      { wrapper: gatewayWrapper },
    );
    expect(hooksState.setSetting).not.toHaveBeenCalled();
    hooksState.sidebarCollapsed = true;
    view.rerender(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={queryClient}>
          <HomePage />
        </QueryClientProvider>
      </WallClockProvider>,
    );
    expect(hooksState.setSetting).not.toHaveBeenCalled();
  });
});

describe("composerMode derivation", () => {
  it("activeId not null + status='terminated' → idle (send auto-resurrects)", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "terminated" }),
    ];
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("idle");
  });
  it("activeId + turnActive=true → busy", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "running" }),
    ];
    hooksState.turnActive = true;
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
  });
  it("activeId + not terminated + turnActive=false → idle", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "idling" }),
    ];
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("idle");
  });
  it("status='running' + turnActive=false → busy (Stop available between actions)", () => {
    hooksState.activeId = 5;
    hooksState.agents = [
      makeAgent({ agent_id: 5, status: "running" }),
    ];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
  });
  it("TimelineView receives busy=true while status='running' even when turnActive=false", () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "running" })];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").getAttribute("data-turn-active")).toBe("1");
  });
  it("TimelineView receives busy=false when idle and no turn in flight", () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    hooksState.turnActive = false;
    wrap(<HomePage />);
    expect(screen.getByTestId("timeline").getAttribute("data-turn-active")).toBe("0");
  });
});

describe("Spawn flow", () => {
  it("spawn succeeds → focusComposer is called", async () => {
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("sidebar-spawn"));
    await waitFor(() => expect(hooksState.focusComposer).toHaveBeenCalled());
  });
  it("spawn returns null → focusComposer not called", async () => {
    hooksState.spawn = vi.fn().mockResolvedValue(null);
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("sidebar-spawn"));
    await waitFor(() => expect(hooksState.spawn).toHaveBeenCalled());
    expect(hooksState.focusComposer).not.toHaveBeenCalled();
  });
});

// meiva:note
// A send asks the timeline to stay with a FOLLOWER; the view decides, so a
// reader parked mid-history is never moved.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
describe("send asks the timeline to stay with the tail", () => {
  it("send succeeds → requestScrollToBottom called", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() =>
      expect(transport.sendMessage).toHaveBeenCalledWith(5, "hi", expect.any(String)),
    );
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).toHaveBeenCalledTimes(1);
  });
  it("send fails → requestScrollToBottom NOT called", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    transport.sendMessage.mockRejectedValueOnce(new Error("boom"));
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalled());
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).not.toHaveBeenCalled();
  });
});

describe("UploadButton receives activeId", () => {
  it("activeId=null → upload-button data-agent-id 'null'", () => {
    wrap(<HomePage />);
    expect(screen.getByTestId("upload-button").getAttribute("data-agent-id")).toBe("null");
  });
  it("activeId=7 → upload-button data-agent-id '7'", () => {
    hooksState.activeId = 7;
    wrap(<HomePage />);
    expect(screen.getByTestId("upload-button").getAttribute("data-agent-id")).toBe("7");
  });
});

describe("model capability notice & unified file staging", () => {
  it("explains unknown model capabilities while text, stop and file staging remain available", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, supports_vision: null, status: "running" })];
    wrap(<HomePage />);
    expect(screen.getByText("Model capabilities are unavailable. You can still send text or request a stop.")).toBeTruthy();
    expect(screen.getByTestId("composer").getAttribute("data-mode")).toBe("busy");
    fireEvent.click(screen.getByTestId("composer-send-plain"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalled());
    fireEvent.click(screen.getByTestId("composer-stop"));
    await waitFor(() => expect(transport.cancel).toHaveBeenCalledWith(5));
    expect(hooksState.agents[0].status).toBe("running");
    fireEvent.click(screen.getByTestId("upload-button"));
    expect(screen.getByTestId("composer").getAttribute("data-stage-seq")).toBe("1");
    expect(screen.getByTestId("composer").getAttribute("data-stage-files")).toBe("picked.txt");
  });

  it.each(["claude_code", "codex"] as const)("keeps file staging for %s harnesses", (harness) => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, harness, supports_vision: true })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("upload-button"));
    expect(screen.getByTestId("composer").getAttribute("data-stage-files")).toBe("picked.txt");
  });
});

describe("multi-command dispatch", () => {
  it("sends one message for two space-separated commands", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalledTimes(1));
    expect(transport.sendMessage).toHaveBeenCalledWith(
      5,
      "/compact /update",
      expect.any(String),
    );
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).toHaveBeenCalledTimes(1);
  });
  it("sends command arguments verbatim in that one message", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi-args"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalledTimes(1));
    expect(transport.sendMessage).toHaveBeenCalledWith(
      5,
      "/search hello world /compact",
      expect.any(String),
    );
  });
  it("sends a single message for plain text (no slash prefix)", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-plain"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalledTimes(1));
    expect(transport.sendMessage).toHaveBeenCalledWith(
      5,
      "plain text",
      expect.any(String),
    );
  });
  it("does not ask for the tail when the send fails", async () => {
    hooksState.activeId = 5;
    hooksState.agents = [makeAgent({ agent_id: 5, status: "idling" })];
    transport.sendMessage.mockRejectedValueOnce(new Error("boom"));
    wrap(<HomePage />);
    fireEvent.click(screen.getByTestId("composer-send-multi"));
    await waitFor(() => expect(transport.sendMessage).toHaveBeenCalled());
    await act(() => Promise.resolve());
    expect(hooksState.requestScrollToBottom).not.toHaveBeenCalled();
  });
});
