import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { beforeEach, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { AgentRow as AgentRowType } from "@/lib/contracts/types";

import { AgentRow } from "./agent-row";
import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";
import { renderWithBrowser as rtlRender } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getSystemStatus.mockRejectedValue(new Error("no network in tests"));
  transport.getSettings.mockRejectedValue(new Error("no network in tests"));
  transport.putSetting.mockRejectedValue(new Error("no network in tests"));
});


afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => undefined;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
});

// meiva:note
// The row asks the page before it ends a session or kills an agent; the answer is a value here
// (`answerConfirm`), and `page.actions.confirmed` is what it asked, in order.
function render(ui: React.ReactElement, page: FakeBrowser = fakeBrowser()) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = rtlRender(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </WallClockProvider>,
    { browser: page },
  );
  return { ...result, page };
}

function renderWithSettings(ui: React.ReactElement, settings: Record<string, unknown>) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  qc.setQueryData(["user-settings"], settings);
  return rtlRender(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </WallClockProvider>,
  );
}

function ag(agent_id: number, overrides: Partial<AgentRowType> = {}): AgentRowType {
  return {
    agent_id,
    spawner: "user",
    harness: "meiva",
    fork_source_agent_id: null,
    status: "running",
    spawned_at: "2026-05-07T00:00:00.000000Z",
    last_active_at: "2026-05-07T00:00:00.000000Z", last_inbound_at: "2026-05-07T00:00:00.000000Z",
    label: null,
    machine: "test-machine",
    supports_vision: true,
    liveness_state: "online",
    awaiting_response_count: 0, highest_notice_priority: null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    open_impersonation_session_id: null,
    ...overrides,
  };
}

const noop = () => undefined;
const baseProps = {
  label: undefined,
  active: false,
  pending: undefined,
  onSelect: noop,
  onTerminate: noop,
  onForceExpire: noop,
  onForceKill: noop,
  onRestart: noop,
  onResurrect: noop,
  onFork: noop,
  onRename: noop,
  onCompact: noop,
};

describe("AgentRow time display (display.time_mode)", () => {
  function timeCell(container: HTMLElement): HTMLElement | null {
    return container.querySelector("li button span.tabular-nums");
  }

  it("default (last_active): timestamp rendered", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(timeCell(container)).toBeTruthy();
  });

  it("hidden: no timestamp rendered", () => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
      { "display.time_mode": "hidden" },
    );
    expect(timeCell(container)).toBeNull();
  });
});

describe("AgentRow awaiting-reply badge gating (notification.awaiting_reply)", () => {
  const notice = {
    id: 7,
    title: "What next?",
    content: null,
    priority: "P1" as const,
    require_response: true,
    blocking: false,
    created_at: "2026-05-07T00:00:00Z",
  };

  it("default (setting off): no badge even with open notices", () => {
    const { queryByTitle } = render(
      <AgentRow
        {...baseProps}
        agent={ag(1, { awaiting_response_count: ([notice]).length, highest_notice_priority: ([notice])[0]?.priority ?? null })}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    expect(queryByTitle(/waiting on you/)).toBeNull();
  });

  it("enabled: badge shows the count", () => {
    const { getByText } = renderWithSettings(
      <AgentRow
        {...baseProps}
        agent={ag(1, { awaiting_response_count: ([notice]).length, highest_notice_priority: ([notice])[0]?.priority ?? null })}
        depth={0}
        ancestorsIsLast={[]}
      />,
      { "notification.awaiting_reply": true },
    );
    expect(getByText("1").textContent).toBe("1");
  });
});

describe("AgentRow right-click context menu", () => {
  function openMenu(row: HTMLElement) {
    fireEvent.contextMenu(row);
  }

  it("alive agent: menu shows Restart / Terminate / Fork / Fork with prompt / Kill", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    openMenu(container.querySelector("li")!);
    expect(screen.getByText("Restart")).toBeTruthy();
    expect(screen.getByText("Terminate")).toBeTruthy();
    expect(screen.getByText("Fork")).toBeTruthy();
    expect(screen.getByText("Fork with prompt…")).toBeTruthy();
    expect(screen.getByText("Kill")).toBeTruthy();
    expect(screen.queryByText("Resurrect")).toBeNull();
    expect(screen.queryByText("End external takeover session")).toBeNull();
  });

  it.each(["meiva", "claude_code", "codex"] as const)("offers native compaction, restart, and fork only on Meiva (%s)", (harness) => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1, { harness })} depth={0} ancestorsIsLast={[]} />,
    );
    openMenu(container.querySelector("li")!);
    const isNative = harness === "meiva";
    expect(screen.queryByText("Compact") !== null).toBe(isNative);
    expect(screen.queryByText("Restart") !== null).toBe(isNative);
    expect(screen.queryByText("Fork") !== null).toBe(isNative);
    expect(screen.queryByText("Fork with prompt…") !== null).toBe(isNative);
    expect(screen.getByText("Terminate")).toBeTruthy();
    expect(screen.getByText("Kill")).toBeTruthy();
  });

  it("shows the end-session item only for an open session and sends its rendered ID after confirmation", () => {
    const onForceExpire = vi.fn();
    const page = fakeBrowser();
    page.answerConfirm(true);
    const { container } = render(
      <AgentRow {...baseProps} onForceExpire={onForceExpire}
        agent={ag(1, { open_impersonation_session_id: 7 })} depth={0} ancestorsIsLast={[]} />,
      page,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("End external takeover session"));
    expect(page.actions.confirmed).toEqual([
      expect.stringContaining("agent resumes its own work"),
    ]);
    expect(onForceExpire).toHaveBeenCalledExactlyOnceWith(7);
  });

  it("does not end the session when confirmation is dismissed", () => {
    const onForceExpire = vi.fn();
    const page = fakeBrowser();
    page.answerConfirm(false);
    const { container } = render(
      <AgentRow {...baseProps} onForceExpire={onForceExpire}
        agent={ag(1, { open_impersonation_session_id: 7 })} depth={0} ancestorsIsLast={[]} />,
      page,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("End external takeover session"));
    expect(page.actions.confirmed).toHaveLength(1);
    expect(onForceExpire).not.toHaveBeenCalled();
  });

  it("terminated agent: menu shows Resurrect + Resurrect with prompt, no Fork / Kill", () => {
    const dead: AgentRowType = { ...ag(2), status: "terminated" };
    const { container } = render(
      <AgentRow {...baseProps} agent={dead} depth={0} ancestorsIsLast={[]} />,
    );
    openMenu(container.querySelector("li")!);
    expect(screen.getByText("Resurrect")).toBeTruthy();
    expect(screen.getByText("Resurrect with prompt…")).toBeTruthy();
    expect(screen.queryByText("Kill")).toBeNull();
    expect(screen.queryByText("Fork")).toBeNull();
  });

  it("Kill calls onForceKill only after the confirm is accepted", () => {
    const onForceKill = vi.fn();
    const page = fakeBrowser();
    page.answerConfirm(true);
    const { container } = render(
      <AgentRow
        {...baseProps}
        onForceKill={onForceKill}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
      page,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Kill"));
    expect(page.actions.confirmed).toHaveLength(1);
    expect(onForceKill).toHaveBeenCalledOnce();
  });

  it("Kill does nothing when the confirm is dismissed", () => {
    const onForceKill = vi.fn();
    const page = fakeBrowser();
    page.answerConfirm(false);
    const { container } = render(
      <AgentRow
        {...baseProps}
        onForceKill={onForceKill}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
      page,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Kill"));
    expect(page.actions.confirmed).toHaveLength(1);
    expect(onForceKill).not.toHaveBeenCalled();
  });

  it("Terminate menu item calls onTerminate (graceful)", () => {
    const onTerminate = vi.fn();
    const { container } = render(
      <AgentRow
        {...baseProps}
        onTerminate={onTerminate}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Terminate"));
    expect(onTerminate).toHaveBeenCalledOnce();
  });

  it("quick Fork calls onFork with no prompt (undefined)", () => {
    const onFork = vi.fn();
    const { container } = render(
      <AgentRow
        {...baseProps}
        onFork={onFork}
        agent={ag(1)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Fork"));
    expect(onFork).toHaveBeenCalledOnce();
    expect(onFork).toHaveBeenCalledWith();
  });
});

describe("AgentRow takeover control", () => {
  it("renders no on-row takeover label or button when a session is open", () => {
    render(
      <AgentRow {...baseProps}
        agent={ag(1, { open_impersonation_session_id: 7 })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(screen.queryByText("Takeover open")).toBeNull();
    expect(screen.queryByRole("button", { name: "End external takeover session" })).toBeNull();
  });

  it("renders no 'Ending takeover…' label while a force-expire is pending — the row shows only its generic spinner", () => {
    render(
      <AgentRow {...baseProps} pending="expiring"
        agent={ag(1, { open_impersonation_session_id: 7 })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(screen.queryByText("Ending takeover…")).toBeNull();
    expect(screen.queryByText("Takeover open")).toBeNull();
    expect(screen.queryByRole("button", { name: "End external takeover session" })).toBeNull();
  });
});
