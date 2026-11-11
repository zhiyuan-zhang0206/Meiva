// AgentRow tree-guide render tests — verify the 1px vertical line shows
// / hides correctly based on depth and ancestorsIsLast.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { beforeEach, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import {
  AGENT_HOVER_OPEN_DELAY_MS,
  AGENT_HOVER_WARMUP_MS,
  AgentHoverGroup,
  AgentRow,
} from "./agent-row";
import { fakeBrowser } from "@/lib/browser/testing";
import type { AgentRow as AgentRowType } from "@/lib/contracts/types";
import { renderWithBrowser as rtlRender } from "@/test-support/render";
import { WallClockProvider } from "@/lib/transport/transport-context";
import { resetTransport, transport } from "@/test-support/fake-api";
import { useStore } from "@/lib/state/store";
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
// meiva:link ui/web/src/docs/frontend-components/frontend-components.md
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => undefined;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
  Element.prototype.getAnimations = () => [];
});

// meiva:note
// AgentRow uses useQuery(["status"]) (machine badge) and useUserSettings
// (opt-in signals) — wrap in QueryClientProvider so the hooks work. The fake
// transport REJECTS the settings reads: no real network in tests. On CI there is no
// gateway (fetches would spray ECONNREFUSED); locally a live gateway is
// worse — real prod settings would leak into the merged settings map and
// green-wash assertions that depend on the defaults. Tests that need
// non-default status / settings seed the query cache directly.
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
    llm_model: null, reasoning_effort: null,
    liveness_state: "online",
    has_pending_question: false,
    has_unread_user_messages: false,
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

function guides(container: HTMLElement) {
  return Array.from(container.querySelectorAll('[data-testid="tree-guide"]'));
}

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

describe("AgentRow lifecycle markers", () => {
  function statusDot(container: HTMLElement): HTMLElement | null {
    return container.querySelector("li button > span.rounded-full");
  }

  it("an unread delivery does not stop the title shimmer or add a lifecycle dot", () => {
    const { container } = render(
      <AgentRow {...baseProps} agent={ag(1, { status: "running", has_unread_user_messages: true })} depth={0} ancestorsIsLast={[]} />,
    );
    const title = container.querySelector<HTMLElement>(".agent-title-working");
    expect(title?.textContent).toBe("#1");
    expect(title?.style.getPropertyValue("--agent-indent-px")).toBe("0px");
    expect(statusDot(container)).toBeNull();
  });

  it("synchronizes working title shimmer horizontal offset and animation startTime across rows", () => {
    const anim = { startTime: 1234 } as unknown as Animation;
    Element.prototype.getAnimations = () => [anim];
    try {
      const { container } = render(
        <AgentRow {...baseProps} agent={ag(2, { status: "running" })} depth={2} ancestorsIsLast={[false, true]} />,
      );
      const title = container.querySelector<HTMLElement>(".agent-title-working");
      expect(title?.style.getPropertyValue("--agent-indent-px")).toBe("28px");
      expect(anim.startTime).toBe(0);
    } finally {
      Element.prototype.getAnimations = () => [];
    }
  });

  it.each(["running", "idling"] as const)("omits the legacy %s dot even with additional markers enabled", (status) => {
    const { container } = renderWithSettings(
      <AgentRow {...baseProps} agent={ag(1, { status })} depth={0} ancestorsIsLast={[]} />,
      { "display.show_agent_status": true },
    );
    expect(statusDot(container)).toBeNull();
  });

  it("offline: renders no grey dot and suppresses working title shimmer", () => {
    const { container } = render(
      <AgentRow
        {...baseProps}
        agent={ag(1, { status: "running", liveness_state: "offline" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    expect(statusDot(container)).toBeNull();
    expect(container.querySelector(".agent-title-working")).toBeNull();
  });

  it("offline: omits the grey dot even with additional status markers enabled", () => {
    const { container } = renderWithSettings(
      <AgentRow
        {...baseProps}
        agent={ag(1, { status: "running", liveness_state: "offline" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
      { "display.show_agent_status": true },
    );
    expect(statusDot(container)).toBeNull();
    expect(container.querySelector(".agent-title-working")).toBeNull();
  });
});

// meiva:note
// One slot, one indicator: question mark > orange dot (unread) > nothing. Open reply requests
// and notice counts do not take part.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
describe("AgentRow attention indicator", () => {
  afterEach(() => useStore.setState({ readingAgentId: null }));
  function show(overrides: Partial<AgentRowType>) {
    return render(<AgentRow {...baseProps} agent={ag(1, overrides)} depth={0} ancestorsIsLast={[]} />);
  }

  it("shows nothing for an agent with no question and nothing unread", () => {
    show({});
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows the orange dot, named Unread, when deliveries lie past the seen position", () => {
    const { container } = show({ has_unread_user_messages: true });
    const mark = screen.getByRole("img", { name: "Unread update from the agent" });
    expect(mark.title).toBe("Unread update from the agent");
    expect(mark.querySelector("span.rounded-full")?.className).toContain("bg-meiva-sunset-orange");
    expect(mark.querySelector("svg")).toBeNull();
    expect(container.querySelectorAll('[role="img"]')).toHaveLength(1);
  });

  it("has no third state: an agent with no question and no unread delivery shows nothing", () => {
    const { container } = show({});
    expect(container.querySelectorAll('[role="img"]')).toHaveLength(0);
  });

  it("shows only the question mark when a question is pending, even with unread deliveries", () => {
    const { container } = show({ has_pending_question: true, has_unread_user_messages: true });
    const mark = screen.getByRole("img", { name: "Question awaiting an answer" });
    expect(mark.querySelector("svg")?.getAttribute("class")).toContain("text-meiva-sunset-orange");
    expect(mark.querySelector("span.rounded-full")).toBeNull();
    expect(container.querySelectorAll('[role="img"]')).toHaveLength(1);
  });

  it("does not light for the conversation being read, but keeps a question lit", () => {
    useStore.setState({ readingAgentId: 1 });
    show({ has_unread_user_messages: true });
    expect(screen.queryByRole("img")).toBeNull();
    cleanup();
    show({ has_pending_question: true, has_unread_user_messages: true });
    expect(screen.getByRole("img", { name: "Question awaiting an answer" })).toBeTruthy();
  });

  it("lights an unread agent other than the one being read", () => {
    useStore.setState({ readingAgentId: 2 });
    show({ has_unread_user_messages: true });
    expect(screen.getByRole("img", { name: "Unread update from the agent" })).toBeTruthy();
  });
});

describe("AgentRow hover card attention marks", () => {
  afterEach(() => useStore.setState({ readingAgentId: null }));
  async function openCard(overrides: Partial<AgentRowType>) {
    vi.useFakeTimers();
    const { container } = render(<AgentRow {...baseProps} agent={ag(1, overrides)} depth={0} ancestorsIsLast={[]} />);
    fireEvent.mouseEnter(container.querySelector("li > button")!);
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS));
    return screen.getByTestId("agent-hover-card");
  }

  it("marks a pending question with the sidebar's orange question mark, with no count", async () => {
    const card = await openCard({ has_pending_question: true });
    expect(card.textContent).toContain("Question awaiting an answer");
    expect(card.textContent).not.toMatch(/awaiting reply|\d unread/);
    expect(card.querySelector("svg")?.getAttribute("class")).toContain("text-meiva-sunset-orange");
  });

  it("marks unread deliveries with the same orange dot as the row, with no count", async () => {
    const card = await openCard({ has_unread_user_messages: true });
    expect(card.textContent).toContain("Unread update from the agent");
    expect(card.textContent).not.toMatch(/\d+ unread/);
    expect(card.querySelector("p span.rounded-full.bg-meiva-sunset-orange")).not.toBeNull();
  });

  it("shows no attention line for a quiet agent, or for the conversation being read", async () => {
    const bare = await openCard({});
    expect(bare.textContent).not.toContain("awaiting");
    expect(bare.querySelector("p span.bg-meiva-sunset-orange")).toBeNull();
    cleanup();
    useStore.setState({ readingAgentId: 1 });
    const reading = await openCard({ has_unread_user_messages: true });
    expect(reading.textContent).not.toContain("Unread");
  });
});

describe("AgentRow single-line truncation and hover peek card", () => {
  it("truncates long labels on a single line instead of wrapping", () => {
    const longLabel = "investigate-distributed-postgres-connection-pool-saturation-on-worker-node";
    render(
      <AgentRow
        {...baseProps}
        agent={ag(180)}
        label={longLabel}
        depth={2}
        ancestorsIsLast={[false, true]}
      />,
    );
    const labelSpan = screen.getByText(`#180 · ${longLabel}`);
    expect(labelSpan.className).toContain("truncate");
    expect(labelSpan.className).toContain("min-w-0");
    expect(labelSpan.className).not.toContain("break-words");
  });

  it("opens the peek card after 400ms with full label, status, harness, machine, timing, and anomalies", async () => {
    vi.useFakeTimers();
    const nowMs = Date.parse("2026-05-07T03:00:00Z");
    const longLabel = "very-long-agent-label-that-gets-truncated-in-the-sidebar-tree";
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = rtlRender(
      <WallClockProvider wallNow={() => nowMs}>
        <QueryClientProvider client={qc}>
          <AgentRow
            {...baseProps}
            label={longLabel}
            agent={ag(180, {
              status: "running",
              harness: "claude_code",
              machine: "mac-studio",
              spawner: "agent:42",
              spawned_at: "2026-05-07T00:00:00Z",
              last_active_at: "2026-05-07T02:55:00Z",
              heartbeat_paused_until: "2026-05-07T05:30:00Z",
            })}
            depth={0}
            ancestorsIsLast={[]}
          />
        </QueryClientProvider>
      </WallClockProvider>,
    );

    const row = container.querySelector("li > button")!;
    fireEvent.mouseEnter(row);

    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS - 50));
    expect(screen.queryByTestId("agent-hover-card")).toBeNull();

    await act(() => vi.advanceTimersByTimeAsync(50));
    const card = screen.getByTestId("agent-hover-card");
    expect(card.className).toContain("pointer-events-none");
    expect(card.textContent).toContain(`#180 · ${longLabel}`);
    expect(card.textContent).toContain("Running");
    expect(card.textContent).toContain("Claude Code");
    expect(card.textContent).toContain("mac-studio");
    expect(card.textContent).toContain("Active 5m ago");
    expect(card.textContent).toContain("Spawned 3h ago");
    expect(card.textContent).toContain("Spawned by #42");
    expect(card.textContent).not.toContain("awaiting reply");
    expect(card.textContent).toContain("Heartbeat paused until");

    fireEvent.mouseLeave(row);
    expect(screen.queryByTestId("agent-hover-card")).toBeNull();
  });

  it("shows Offline instead of Running in the hover peek card without a grey dot or duplicate line", async () => {
    vi.useFakeTimers();
    const { container } = render(
      <AgentRow
        {...baseProps}
        label="offline-agent"
        agent={ag(405, { status: "running", liveness_state: "offline" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );

    const row = container.querySelector("li > button")!;
    fireEvent.mouseEnter(row);
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS));

    const card = screen.getByTestId("agent-hover-card");
    expect(card.textContent).toContain("Offline — machine unreachable or process lost");
    expect(card.textContent).not.toContain("Running");
    expect(card.querySelector(".bg-muted-foreground\\/50")).toBeNull();
    expect(
      card.textContent.split("Offline — machine unreachable or process lost").length,
    ).toBe(2);
  });

  // meiva:note
  // The peek card names the model and effort the agent's calls run with, each on its own row
  // (user 2026-11-08): a reader judging a row should not have to open the Inspector for the two
  // facts that decide how it behaves. A half the roster could not resolve renders nothing rather
  // than a placeholder — the card does not guess.
  it("names the resolved model and reasoning effort on their own rows", async () => {
    vi.useFakeTimers();
    const { container } = render(
      <AgentRow
        {...baseProps}
        label="model-row"
        agent={ag(181, { llm_model: "claude-opus-5", reasoning_effort: "xhigh" })}
        depth={0}
        ancestorsIsLast={[]}
      />,
    );
    fireEvent.mouseEnter(container.querySelector("li > button")!);
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS));

    const model = screen.getByTestId("agent-hover-model");
    expect(model.textContent).toBe("Modelclaude-opus-5");
    const effort = screen.getByTestId("agent-hover-effort");
    expect(effort.textContent).toBe("Effortxhigh");
  });

  it("renders no model or effort row when the roster could not resolve them", async () => {
    vi.useFakeTimers();
    const { container } = render(
      <AgentRow {...baseProps} label="unknown-model" agent={ag(182)} depth={0} ancestorsIsLast={[]} />,
    );
    fireEvent.mouseEnter(container.querySelector("li > button")!);
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS));

    expect(screen.getByTestId("agent-hover-card")).toBeTruthy();
    expect(screen.queryByTestId("agent-hover-model")).toBeNull();
    expect(screen.queryByTestId("agent-hover-effort")).toBeNull();
  });

  it("ignores brief pointer sweeps shorter than 400ms and switches in 0ms within the 300ms warm-up window", async () => {
    vi.useFakeTimers();
    const { container } = render(
      <AgentHoverGroup>
        <ul>
          <AgentRow {...baseProps} label="first-agent" agent={ag(10)} depth={0} ancestorsIsLast={[]} />
          <AgentRow {...baseProps} label="second-agent" agent={ag(20)} depth={0} ancestorsIsLast={[]} />
        </ul>
      </AgentHoverGroup>,
    );

    const buttons = container.querySelectorAll("li > button");
    const first = buttons[0];
    const second = buttons[1];

    fireEvent.mouseEnter(first);
    await act(() => vi.advanceTimersByTimeAsync(150));
    fireEvent.mouseLeave(first);
    expect(screen.queryByTestId("agent-hover-card")).toBeNull();

    fireEvent.mouseEnter(first);
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS));
    expect(screen.getByTestId("agent-hover-card").textContent).toContain("#10 · first-agent");

    fireEvent.mouseLeave(first);
    expect(screen.queryByTestId("agent-hover-card")).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_WARMUP_MS - 100));
    fireEvent.mouseEnter(second);
    expect(screen.getByTestId("agent-hover-card").textContent).toContain("#20 · second-agent");

    fireEvent.mouseLeave(second);
    expect(screen.queryByTestId("agent-hover-card")).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_WARMUP_MS + 10));

    fireEvent.mouseEnter(first);
    expect(screen.queryByTestId("agent-hover-card")).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS));
    expect(screen.getByTestId("agent-hover-card").textContent).toContain("#10 · first-agent");
  });

  it("suppresses hover peek cards on coarse pointers so touch taps never stick a tooltip open", async () => {
    vi.useFakeTimers();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const browser = fakeBrowser({ media: { "(pointer: coarse)": true } });
    const { container } = rtlRender(
      <WallClockProvider wallNow={() => Date.now()}>
        <QueryClientProvider client={qc}>
          <AgentRow {...baseProps} label="touch-agent" agent={ag(30)} depth={0} ancestorsIsLast={[]} />
        </QueryClientProvider>
      </WallClockProvider>,
      { browser },
    );

    const row = container.querySelector("li > button")!;
    fireEvent.mouseEnter(row);
    await act(() => vi.advanceTimersByTimeAsync(AGENT_HOVER_OPEN_DELAY_MS + 100));
    expect(screen.queryByTestId("agent-hover-card")).toBeNull();
  });
});
