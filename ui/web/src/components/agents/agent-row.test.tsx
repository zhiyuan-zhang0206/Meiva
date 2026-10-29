// AgentRow tree-guide render tests — verify the 1px vertical line shows
// / hides correctly based on depth and ancestorsIsLast.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { beforeEach, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AgentRow } from "./agent-row";
import type { AgentRow as AgentRowType } from "@/lib/contracts/types";
import { renderWithApi as rtlRender } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getSystemStatus.mockRejectedValue(new Error("no network in tests"));
  transport.getSettings.mockRejectedValue(new Error("no network in tests"));
  transport.putSetting.mockRejectedValue(new Error("no network in tests"));
  transport.getAgentInspectStatistics.mockResolvedValue({});
  transport.getAgentInspectLive.mockResolvedValue({});
});


afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  transport.getAgentInspectStatistics.mockClear();
  transport.getAgentInspectLive.mockClear();
});

// meiva:note
// Radix ContextMenu calls pointer-capture / scroll APIs that happy-dom
// doesn't implement; stub them so the menu can open in the test DOM.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => undefined;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
});

// meiva:note
// AgentRow uses useQuery(["status"]) (machine badge) and useUserSettings
// (opt-in signals) — wrap in QueryClientProvider so the hooks work. The fake
// transport REJECTS the settings reads: no real network in tests. On CI there is no
// gateway (fetches would spray ECONNREFUSED); locally a live gateway is
// worse — real prod settings would leak into the merged settings map and
// green-wash assertions that depend on the defaults. Tests that need
// non-default status / settings seed the query cache directly.
// meiva:note
// The row ages its timestamp against the page's clock; the app's own answer keeps this file's
// relative-time strings the ones it already asserts.
function render(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return rtlRender(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </WallClockProvider>,
  );
}

// Render with the user-settings cache pre-seeded — for testing opt-in
// signals (status colors / activity line / awaiting-reply badge) that are
// hidden at their quiet defaults.
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
  wide: false,
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

function guides(container: HTMLElement) {
  return Array.from(container.querySelectorAll('[data-testid="tree-guide"]'));
}

// meiva:note
// The row's relative timestamp comes from the page's clock: two instants, two strings. The row also
// ticks once a minute on its own; a fixed clock keeps both reads on the same instant here.
describe("AgentRow timestamp source", () => {
  it("ages the timestamp against the clock the page hands in", () => {
    const activeAt = "2026-05-07T00:00:00Z";
    const at = (ms: number) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      return rtlRender(
        <WallClockProvider wallNow={() => ms}>
          <QueryClientProvider client={qc}>
            <AgentRow
              {...baseProps}
              agent={ag(1, { last_active_at: activeAt })}
              depth={0}
              ancestorsIsLast={[]}
            />
          </QueryClientProvider>
        </WallClockProvider>,
      ).container.textContent;
    };
    expect(at(Date.parse("2026-05-07T03:00:00Z"))).not.toBe(at(Date.parse("2026-05-07T09:00:00Z")));
    expect(at(Date.parse("2026-05-07T03:00:00Z"))).toContain("3h");
  });
});

describe("AgentRow tree-guide rendering", () => {
  it("shows a launch failure on the roster row even with status dots disabled", () => {
    render(<AgentRow {...baseProps} agent={ag(1, {
      status: "idling",
      availability: { reason: "launch_rejected", observed_at: new Date().toISOString() },
    })} depth={0} ancestorsIsLast={[]} />);
    expect(screen.getByText("Launch failed")).toBeTruthy();
  });

  it("does not show an ownership fallback when observation is missing", () => {
    render(<AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />);
    expect(screen.queryByText("owner unknown")).toBeNull();
  });

  it("does not show stale observation details", () => {
    render(<AgentRow {...baseProps} agent={ag(1, { observation: {
      machine_probe_at: new Date(Date.now() - 180_000).toISOString(),
      machine_probe_valid_until: new Date(Date.now() - 60_000).toISOString(),
      runtime_owner: "unknown",
    } })} depth={0} ancestorsIsLast={[]} />);
    expect(screen.queryByText("probe stale")).toBeNull();
  });

  it("depth=0 (top-level): renders no guide bars", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    expect(guides(container)).toHaveLength(0);
  });

  it("depth=1 self not last: draws 1 bar at d=0", () => {
    // ancestorsIsLast = [parent.isLast, self.isLast].  parent may be
    // last or not — the line at d=0 is governed by self's position.
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(2)} depth={1} ancestorsIsLast={[false, false]} />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(1);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
  });

  it("depth=1 parent is last but self is not: line still drawn (parent has children → line extends)", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(2)} depth={1} ancestorsIsLast={[true, false]} />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(1);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
  });

  it("depth=1 self is last: bar still drawn (every child gets a line)", () => {
    // Every child at every depth gets a vertical bar — the lineage column
    // is contiguous; isLast does not suppress the bar.
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(3)} depth={1} ancestorsIsLast={[false, true]} />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(1);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
  });

  it("depth=2 ancestorsIsLast=[false, false, true]: both d=0 and d=1 drawn (every child gets a line)", () => {
    // ancestorsIsLast = [grandparent.isLast, parent.isLast, self.isLast]
    // Every depth gets a bar — isLast does not suppress any line.
    const { container } = render(
      <AgentRow
        {...baseProps}
        agent={ag(4)}
        depth={2}
        ancestorsIsLast={[false, false, true]}
      />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(2);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
    expect(bars[1].getAttribute("data-depth")).toBe("1");
  });

  it("depth=2 ancestorsIsLast=[true, false, false]: both d=0 and d=1 drawn (line extends through non-last children)", () => {
    // grandparent is last → but parent is not last, so the line at d=0
    // extends from grandparent down through this branch.  parent not last
    // and self not last → line at d=1 extends as well.
    const { container } = render(
      <AgentRow
        {...baseProps}
        agent={ag(5)}
        depth={2}
        ancestorsIsLast={[true, false, false]}
      />,
    );
    const bars = guides(container);
    expect(bars).toHaveLength(2);
    expect(bars[0].getAttribute("data-depth")).toBe("0");
    expect(bars[1].getAttribute("data-depth")).toBe("1");
  });
});

describe("AgentRow inline rename accessibility", () => {
  it("names the inline rename input", () => {
    render(
      <AgentRow
        {...baseProps}
        agent={ag(1)}
        label="Focus"
        depth={0}
        ancestorsIsLast={[]}
      />,
    );

    fireEvent.doubleClick(screen.getByText("#1 · Focus"));

    expect(screen.getByLabelText("Rename agent 1")).toBeTruthy();
  });
});

describe("AgentRow action button visibility", () => {
  // Alive rows carry no on-row action buttons — restart / terminate moved
  // into the right-click context menu (covered by its own describe below).
  it("alive agent: no on-row restart / terminate buttons", () => {
    const { queryByLabelText } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    expect(queryByLabelText(/Restart Agent #1/)).toBeNull();
    expect(queryByLabelText(/Terminate Agent #1/)).toBeNull();
  });

  // Task #723 (user ruling): the agent-switch row is a list item, not a
  // button — the global `button:not(:disabled) { cursor: pointer }` rule
  // (#709) must not give it a hand cursor. Class contract: cursor-default.
  it("agent switch row keeps the default cursor (no hand cursor)", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />,
    );
    const row = container.querySelector("li button");
    expect(row).not.toBeNull();
    expect(row!.className).toContain("cursor-default");
  });

  // Resurrect stays always visible (not hover-gated): mobile has no hover,
  // and it is the single primary action for a dead agent.
  it("terminated agent: resurrect button visible without hover", () => {
    const dead: AgentRowType = { ...ag(2), status: "terminated" };
    const { getByLabelText } = render(
      <AgentRow {...baseProps} agent={dead} depth={0} ancestorsIsLast={[]} />,
    );
    expect(getByLabelText(/Resurrect Agent #2/).tagName).toBe("BUTTON");
  });
});

describe("AgentRow inspector reads", () => {
  it("hover and selection never start speculative inspector requests", async () => {
    vi.useFakeTimers();
    const onSelect = vi.fn();
    const { container } = render(
      <AgentRow {...baseProps} onSelect={onSelect} agent={ag(7)} depth={0} ancestorsIsLast={[]} />,
    );
    const row = container.querySelector("li > button")!;
    fireEvent.mouseEnter(row);
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    fireEvent.click(row);
    fireEvent.mouseLeave(row);
    expect(onSelect).toHaveBeenCalledOnce();
    expect(transport.getAgentInspectLive).not.toHaveBeenCalled();
    expect(transport.getAgentInspectStatistics).not.toHaveBeenCalled();
  });
});

describe("AgentRow machine badge (removed, task #3904)", () => {
  // The machine name left the tree — it renders in the Inspector's Liveness
  // block now. Multi-machine data must not print it.
  it("never renders the machine name", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["status"], {
      cluster: { machines: [{ name: "m1" }, { name: "m2" }] },
    });
    const { queryByText } = rtlRender(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={qc}>
          <AgentRow {...baseProps} agent={ag(1)} depth={0} ancestorsIsLast={[]} />
        </QueryClientProvider>
      </WallClockProvider>,
    );
    expect(queryByText("test-machine")).toBeNull();
  });
});

describe("AgentRow status dot gating (display.show_agent_status)", () => {
  // The status dot is a rounded-full span directly in the row button; when the
  // setting is off it is not rendered at all (hidden entirely, not decolored).
  function statusDot(container: HTMLElement): HTMLElement | null {
    return container.querySelector("li button > span.rounded-full");
  }

  it("default (setting off): no status dot rendered at all", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "running" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(statusDot(container)).toBeNull();
  });

  it("default (setting off): no dot for any status (no leaked dynamic signal)", () => {
    const a = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "running" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(statusDot(a.container)).toBeNull();
    cleanup();
    const b = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
    );
    expect(statusDot(b.container)).toBeNull();
  });

  it("enabled: colored status dot", () => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status: "running" })} depth={0} ancestorsIsLast={[]} />,
      { "display.show_agent_status": true },
    );
    const d = statusDot(container);
    expect(d).toBeTruthy();
    expect(d!.className).toContain("bg-sky-500");
  });

  it("offline: grey dot shown even with the status opt-in off (Task #1174)", () => {
    // A machine drop / dead process must never masquerade as present under
    // the quiet default — the offline dot is presence, not a dynamic signal.
    const { container } = render(
      <AgentRow
        {...baseProps}
        agent={ag(1, { status: "idling", liveness_state: "offline" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    const d = statusDot(container);
    expect(d).toBeTruthy();
    expect(d!.className).toContain("bg-muted-foreground/50");
  });

  it("offline: grey dot overrides the status color and carries a tooltip", () => {
    const { container } = renderWithSettings(
      <AgentRow
        {...baseProps}
        agent={ag(1, { status: "idling", liveness_state: "offline" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
      { "display.show_agent_status": true },
    );
    const d = statusDot(container);
    expect(d).toBeTruthy();
    expect(d!.className).toContain("bg-muted-foreground/50");
    expect(d!.className).not.toContain("bg-emerald-500");
    expect(d!.getAttribute("title")).toBeTruthy();
  });

  it("online: unaffected by the liveness field (status color as before)", () => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status: "idling" })} depth={0} ancestorsIsLast={[]} />,
      { "display.show_agent_status": true },
    );
    const d = statusDot(container);
    expect(d!.className).toContain("bg-emerald-500");
  });

  it("enabled: impersonated status gets its own distinct color", () => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status: "impersonated" })} depth={0} ancestorsIsLast={[]} />,
      { "display.show_agent_status": true },
    );
    const d = statusDot(container);
    expect(d).toBeTruthy();
    expect(d!.className).toContain("bg-indigo-400");
  });
});
