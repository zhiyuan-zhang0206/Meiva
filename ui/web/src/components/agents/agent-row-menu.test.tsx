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
    awaiting_response_count: 0, has_pending_question: false, highest_notice_priority: null,
    unread_notice_count: 0,
    unread_user_message_count: 0,
    heartbeat_paused_until: null,
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
  onForceKill: noop,
  onRestart: noop,
  onResurrect: noop,
  onFork: noop,
  onRename: noop,
  onCompact: noop,
};

it("keeps time off the row even when an older client saved a time display mode", () => {
  const { getByRole } = renderWithSettings(
    <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} label="Draft review" depth={0} ancestorsIsLast={[]} />,
    { "display.time_mode": "spawned" },
  );
  expect(getByRole("button", { name: "#1 · Draft review" }).textContent).toBe("#1 · Draft review");
});

describe("AgentRow response indicators", () => {
  it("puts the blue reply dot at the end of the row by default", () => {
    const { getByRole } = render(
      <AgentRow {...baseProps} agent={ag(1, { awaiting_response_count: 1 })} depth={0} ancestorsIsLast={[]} />,
    );
    const indicator = getByRole("img", { name: "Awaiting a reply" });
    expect(indicator.parentElement?.lastElementChild).toBe(indicator);
    expect(indicator.querySelector(".rounded-full")).toBeTruthy();
  });

  it("uses the question icon ahead of a regular reply and keeps the working title", () => {
    const { container, getByRole, queryByRole } = render(
      <AgentRow
        {...baseProps}
        agent={ag(1, { status: "running", awaiting_response_count: 2, has_pending_question: true })}
        label="A long agent title that has to truncate before the response icon"
        pending="compacting"
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    const indicator = getByRole("img", { name: "Question awaiting an answer" });
    expect(indicator.parentElement?.lastElementChild).toBe(indicator);
    expect(indicator.classList.contains("shrink-0")).toBe(true);
    expect(indicator.querySelector("svg")).toBeTruthy();
    expect(queryByRole("img", { name: "Awaiting a reply" })).toBeNull();
    expect(container.querySelector(".agent-title-working.truncate")).toBeTruthy();
    expect(container.querySelector(".right-9 svg")).toBeTruthy();
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

  it("Rename menu item opens the inline rename input without closing on menu teardown, so touch long-press can rename", () => {
    const onRename = vi.fn();
    const { container } = render(
      <AgentRow
        {...baseProps}
        label="old-name"
        onRename={onRename}
        agent={ag(9)}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    openMenu(container.querySelector("li")!);
    fireEvent.click(screen.getByText("Rename"));
    const input = screen.getByLabelText<HTMLInputElement>("Rename agent 9");
    expect(input.value).toBe("old-name");
    fireEvent.change(input, { target: { value: "touch-renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRename).toHaveBeenCalledExactlyOnceWith("touch-renamed");
  });
});
