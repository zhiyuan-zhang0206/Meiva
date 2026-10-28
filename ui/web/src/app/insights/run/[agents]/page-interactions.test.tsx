import { WallClockProvider } from "@/lib/transport/transport-context";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ContextBreakdownResponse, RunTimelineContext, RunTimelineLinks, RunTimelineMessages, RunTimelineResponse, UserSettingListResponse } from "@/lib/contracts/types";
const { getRunTimeline, getRunTimelineMessages, getRunTimelineContext, getRunTimelineLinks, getAgentRoster, getSettings, getContextBreakdown } =
  vi.hoisted(() => ({
    getRunTimeline: vi.fn<(agentId: number, options?: { from?: string; to?: string; signal?: AbortSignal }) => Promise<RunTimelineResponse>>(),
    getRunTimelineMessages: vi.fn<(agentId: number, range: { start: number; end: number; limit?: number; full?: boolean }) => Promise<RunTimelineMessages>>(),
    getSettings: vi.fn<() => Promise<UserSettingListResponse>>(),
    getContextBreakdown: vi.fn<(agentId: number) => Promise<ContextBreakdownResponse>>(),
    getRunTimelineContext: vi.fn<(agentId: number, at: number) => Promise<RunTimelineContext>>(),
    getRunTimelineLinks: vi.fn<(agents: readonly number[], window: { from: string; to: string }) => Promise<RunTimelineLinks>>(),
    getAgentRoster: vi.fn(),
  }));

import { clickAt, drawn, itemX, leave, look, mockCanvas, paintFrame, pointAt } from "@/components/run-timeline/canvas/run-timeline-canvas-test-mock";
import RunTimelinePage from "./page";
import Loading from "./loading";
import { cbdFixture, lifetimeResponse, messagesResponse } from "./run-timeline-test-mock";
import { renderWithApi as rtlRender } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.getRunTimeline.mockImplementation(getRunTimeline);
  transport.getRunTimelineMessages.mockImplementation(getRunTimelineMessages);
  transport.getRunTimelineContext.mockImplementation(getRunTimelineContext);
  transport.getRunTimelineLinks.mockImplementation(getRunTimelineLinks);
  transport.getAgentRoster.mockImplementation(getAgentRoster);
  transport.getSettings.mockImplementation(getSettings);
  transport.getContextBreakdown.mockImplementation(getContextBreakdown);
});

const wallNow = () => Date.now();
function renderPage({ contextSize = true }: { contextSize?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = rtlRender(
    <WallClockProvider wallNow={wallNow}>
      <BrowserProvider browser={fakeBrowser()}>
      <QueryClientProvider client={queryClient}>
        <RunTimelinePage params={Promise.resolve({ agents: "42" })} />
      </QueryClientProvider>
      </BrowserProvider>
    </WallClockProvider>,
  );
  if (contextSize) fireEvent.click(screen.getByTestId("agent-view-context-size"));
  return view;
}
interface Item {
  row: string;
  key: string;
}
const nodeOf = async (id: string): Promise<Item> => {
  await screen.findByTestId("run-timeline-chart");
  const node = lifetimeResponse.nodes.find((candidate) => candidate.id === id);
  if (node === undefined) throw new Error(`no node ${id}`);
  return { row: `level-${node.level}`, key: `n${id}` };
};
const unitOf = async (kind: string): Promise<Item> => {
  await screen.findByTestId("run-timeline-chart");
  const unit = lifetimeResponse.units.find((candidate) => candidate.kind === kind);
  if (unit === undefined) throw new Error(`no ${kind} unit`);
  return { row: "units", key: `u${kind}-${unit.i0}-${unit.i1}` };
};
const xOf = (item: Item) => itemX(lifetimeResponse, item.row, item.key);
const clickItem = (item: Item) => clickAt(item.row, xOf(item));
const hoverItem = (item: Item) => pointAt(item.row, xOf(item));
const leaveItem = (item: Item) => leave(item.row);
const lookOf = async (item: Item) => {
  await paintFrame();
  return look(item.row, xOf(item));
};
const faded = async (item: Item) => (await lookOf(item)).faded;
const ringOf = async (item: Item) => (await lookOf(item)).ring;
beforeEach(() => {
  mockCanvas();
  getRunTimeline.mockReset();
  getRunTimelineLinks.mockReset();
  getRunTimelineLinks.mockResolvedValue({ links: [] });
  getRunTimeline.mockResolvedValue(lifetimeResponse);
  getRunTimelineMessages.mockReset();
  getRunTimelineMessages.mockResolvedValue(messagesResponse);
  getSettings.mockReset();
  getSettings.mockResolvedValue({ settings: [] });
  getAgentRoster.mockReset();
  getAgentRoster.mockResolvedValue({
    agents: [{ agent_id: 42, label: "planner", status: "running" }],
    ancestors: [],
  });
  getContextBreakdown.mockReset();
  getContextBreakdown.mockResolvedValue(cbdFixture);
  getRunTimelineContext.mockReset();
  getRunTimelineContext.mockImplementation((_agent, at) => {
    const requests = lifetimeResponse.units.filter((candidate) => candidate.request !== null);
    const request = requests.find((candidate) => candidate.i0 >= at) ?? requests[1];
    return Promise.resolve({
      ...cbdFixture,
      categories: [
        { kind: "system_prompt", tokens: 300, estimated: false, exact_fraction: 1 },
        { kind: "user_input", tokens: 200, estimated: false, exact_fraction: 1 },
        { kind: "reasoning", tokens: 100, estimated: true, exact_fraction: 0 },
      ],
      request: request.i0,
      session: request.session,
      sessions: 2,
      ts: request.start,
    });
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// meiva:link ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.meiva.okf.md
function wheel(target: Element, init: WheelEventInit, count = 1) {
  act(() => {
    for (let i = 0; i < count; i++) {
      const event = new MouseEvent("wheel", {
        bubbles: true,
        cancelable: true,
        clientX: init.clientX,
      });
      Object.defineProperties(event, {
        deltaX: { value: init.deltaX ?? 0 },
        deltaY: { value: init.deltaY ?? 0 },
      });
      target.dispatchEvent(event);
    }
  });
}

describe("failure and loading", () => {
  it("aborts the pending read on unmount without turning cancellation into a failed agent", async () => {
    let signal: AbortSignal | undefined;
    getRunTimeline.mockImplementation((_agent, options) => {
      signal = options?.signal;
      return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const view = renderPage();
    await waitFor(() => expect(signal).toBeDefined());
    expect(signal?.aborted).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
    view.unmount();
    expect(signal?.aborted).toBe(true);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });
  it("offers a retry after a pending read times out, then loads the fresh result", async () => {
    let fail: ((error: DOMException) => void) | undefined;
    getRunTimeline.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    renderPage();
    await waitFor(() => expect(fail).toBeDefined());
    const initialSignal = getRunTimeline.mock.calls[0]?.[1]?.signal;
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    await act(async () => {
      fail?.(new DOMException("run timeline exceeded 35000ms", "TimeoutError"));
      await Promise.resolve();
    });
    expect(await screen.findByText("Could not load the timeline of agent 42.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByTestId("run-timeline-chart");
    expect(getRunTimeline.mock.calls.map(([agent]) => agent)).toEqual([42, 42]);
    const retrySignal = getRunTimeline.mock.calls[1]?.[1]?.signal;
    expect(retrySignal).toBeInstanceOf(AbortSignal);
    expect(retrySignal).not.toBe(initialSignal);
    expect(retrySignal?.aborted).toBe(false);
  });
  it("offers a retry when the read fails", async () => {
    getRunTimeline.mockRejectedValueOnce(new Error("boom"));
    renderPage();
    expect(await screen.findByText("Could not load the timeline of agent 42.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByTestId("run-timeline-chart");
  });
  it("the route loading shell renders", () => {
    const { container } = rtlRender(<Loading />);
    expect(container.querySelector("main")).not.toBeNull();
  });
  it("clicking a thinking block reads only the thinking, a tool call block only the call", async () => {
    const response: RunTimelineResponse = {
      ...lifetimeResponse,
      units: [
        {
          kind: "thinking",
          i0: 2,
          i1: 2,
          start: "2026-10-04T12:04:00.000000Z",
          end: "2026-10-04T12:05:00.000000Z",
          source: null,
          inbound_id: null,
          preview: "need a plan",
          parent: "1",
          context_tokens: null,
          generation_tokens: null,
          estimated: null,
          session: 0,
          context_total: null,
          request: null,
        },
        {
          kind: "call",
          i0: 2,
          i1: 2,
          start: "2026-10-04T12:05:00.000000Z",
          end: "2026-10-04T12:05:00.000000Z",
          source: null,
          inbound_id: null,
          preview: "ls",
          parent: "1",
          context_tokens: null,
          generation_tokens: null,
          estimated: null,
          session: 0,
          context_total: null,
          request: null,
        },
      ],
    };
    getRunTimeline.mockResolvedValue(response);
    renderPage();
    await screen.findByTestId("run-timeline-canvas-units");
    clickAt("units", itemX(response, "units", "uthinking-2-2"));
    let detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText("need a plan")).toBeTruthy();
    expect(within(detail).queryByText("ls")).toBeNull();
    expect(within(detail).queryByText("on it")).toBeNull();
    clickAt("units", itemX(response, "units", "ucall-2-2"));
    detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(await within(detail).findByText((_, el) => el?.tagName === "PRE" && el.textContent === "ls")).toBeTruthy();
    expect(within(detail).queryByText("need a plan")).toBeNull();
  });
  it("draws every block on one lane and lists the block colors in a legend", async () => {
    renderPage();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    expect(screen.getAllByTestId(/^run-timeline-canvas-/).map((el) => el.getAttribute("data-testid"))).toEqual([
      "run-timeline-canvas-level-2",
      "run-timeline-canvas-level-1",
      "run-timeline-canvas-units",
      "run-timeline-canvas-input",
    ]);
    const legend = screen.getByTestId("run-timeline-legend");
    expect(within(legend).getAllByRole("listitem")).toHaveLength(7);
  });
});

describe("legend highlight", () => {
  it("lights one class, fades every other block and every summary block, and clears on a second click", async () => {
    renderPage();
    const human = await unitOf("inbound");
    const text = await unitOf("text");
    const legendHuman = screen.getByTestId("run-timeline-legend-human");
    expect(legendHuman.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(legendHuman);
    expect(legendHuman.getAttribute("aria-pressed")).toBe("true");
    expect(await faded(human)).toBe(false);
    expect(await faded(text)).toBe(true);
    expect(await faded(await nodeOf("1"))).toBe(true);
    expect(await faded(await nodeOf("3"))).toBe(true);
    fireEvent.click(legendHuman);
    expect(legendHuman.getAttribute("aria-pressed")).toBe("false");
    expect(await faded(text)).toBe(false);
    expect(await faded(await nodeOf("1"))).toBe(false);
  });
  it("keeps the highlight through a zoom", async () => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const wide = this.hasAttribute("data-track");
      return { left: 100, top: 0, width: wide ? 1000 : 0, height: 0, right: 1100, bottom: 0, x: 100, y: 0, toJSON: () => ({}) };
    });
    renderPage();
    const chart = await screen.findByTestId("run-timeline-chart");
    fireEvent.click(screen.getByTestId("run-timeline-legend-human"));
    wheel(chart, { deltaY: -400, clientX: 100 });
    await paintFrame();
    const [inbound, text] = [await unitOf("inbound"), await unitOf("text")];
    const probe = screen.getByTestId("run-timeline-canvas-units");
    expect(probe).toBeTruthy();
    expect(inbound.row).toBe(text.row);
    expect(screen.getByTestId("run-timeline-legend-human").getAttribute("aria-pressed")).toBe("true");
  });
  it("links to the context breakdown rows: a row lights its class, and the legend lights its row", async () => {
    renderPage();
    clickItem(await unitOf("thinking"));
    const row = await screen.findByTestId("context-breakdown-category-user_input");
    expect(row.getAttribute("aria-pressed")).toBe("false");
    expect(screen.queryByTestId("context-breakdown-category-system_prompt")).toBeNull();
    fireEvent.click(row);
    expect(screen.getByTestId("run-timeline-legend-human").getAttribute("aria-pressed")).toBe("true");
    expect(await faded(await unitOf("text"))).toBe(true);
    expect(row.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByTestId("run-timeline-legend-human"));
    expect(screen.getByTestId("context-breakdown-category-user_input").getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(screen.getByTestId("run-timeline-legend-thinking"));
    expect(screen.getByTestId("context-breakdown-category-reasoning").getAttribute("aria-pressed")).toBe("true");
  });
  it("splits inbound blocks by source: highlighting one agent's inbound fades the other's", async () => {
    const sourceResponse: RunTimelineResponse = {
      ...lifetimeResponse,
      units: [
        { ...lifetimeResponse.units[0], kind: "inbound", i0: 1, i1: 1, source: "agent:12", preview: "from twelve" },
        { ...lifetimeResponse.units[0], kind: "inbound", i0: 3, i1: 3, source: "agent:9", start: "2026-10-04T12:10:00.000000Z", end: "2026-10-04T12:10:00.000000Z", preview: "from nine" },
      ],
    };
    getRunTimeline.mockResolvedValue(sourceResponse);
    renderPage();
    fireEvent.click(await screen.findByTestId("run-timeline-legend-agent"));
    const select = await screen.findByTestId("run-timeline-source-select");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "All sources",
      "Inbound from agent 12",
      "Inbound from agent 9",
    ]);
    fireEvent.change(select, { target: { value: "agent:12" } });
    const twelve: Item = { row: "units", key: "uinbound-1-1" };
    const nine: Item = { row: "units", key: "uinbound-3-3" };
    const at = (item: Item) => itemX(sourceResponse, item.row, item.key);
    const fadedAt = async (item: Item) => {
      await paintFrame();
      return look(item.row, at(item)).faded;
    };
    expect(await fadedAt(twelve)).toBe(false);
    expect(await fadedAt(nine)).toBe(true);
    fireEvent.change(select, { target: { value: "" } });
    expect(await fadedAt(nine)).toBe(false);
  });
});

describe("hover", () => {
  const ringAfter = async (item: Item) => ringOf(item);
  it("highlights hovered items without visible text or selection announcements", async () => {
    renderPage();
    const human = await unitOf("inbound");
    const live = screen.getByTestId("run-timeline-selection-live");
    hoverItem(human);
    expect(await ringOf(human)).toBe("hover");
    expect(live.textContent).toBe("");
    expect(screen.queryByTestId("run-timeline-readout")).toBeNull();
    hoverItem(await nodeOf("1"));
    expect(await ringOf(await nodeOf("1"))).toBe("hover");
    expect(live.textContent).toBe("");
    leaveItem(await nodeOf("1"));
    expect(await ringOf(await nodeOf("1"))).toBe("none");
  });
  it("lights a hovered block's ancestor chain softly", async () => {
    renderPage();
    hoverItem(await unitOf("text"));
    expect(await ringAfter(await nodeOf("1"))).toBe("hover");
    expect(await ringAfter(await nodeOf("3"))).toBe("hover");
    expect(await ringAfter(await nodeOf("2"))).toBe("none");
    leaveItem(await unitOf("text"));
    expect(await ringAfter(await nodeOf("1"))).toBe("none");
  });
  it("lights a hovered node's covered blocks and its ancestors", async () => {
    renderPage();
    hoverItem(await nodeOf("1"));
    expect(await ringAfter(await nodeOf("1"))).toBe("hover");
    expect(await ringAfter(await nodeOf("3"))).toBe("hover");
    expect(await ringAfter(await unitOf("inbound"))).toBe("hover");
    expect(await ringAfter(await unitOf("text"))).toBe("hover");
    expect(await ringAfter(await nodeOf("2"))).toBe("none");
  });
  it("gives the selection priority over the hover", async () => {
    renderPage();
    clickItem(await nodeOf("2"));
    hoverItem(await unitOf("text"));
    expect(await ringOf(await nodeOf("2"))).toBe("primary");
    expect(await ringOf(await nodeOf("3"))).toBe("linked");
    expect(await ringOf(await nodeOf("1"))).toBe("hover");
  });
});

describe("side panel links", () => {
  it("lists a node's ancestors and children as chips that jump to them", async () => {
    renderPage();
    clickItem(await nodeOf("1"));
    let detail = await screen.findByTestId("run-timeline-node-detail");
    const up = within(detail).getAllByTestId("run-timeline-chip");
    expect(up.map((chip) => chip.textContent)).toEqual(["Level 2 · A whole task, start to finish."]);
    fireEvent.click(up[0]);
    detail = await screen.findByTestId("run-timeline-node-detail");
    expect(detail.textContent).toContain("Level 2 summary");
    expect(await ringOf(await nodeOf("3"))).toBe("primary");
    const down = within(detail).getAllByTestId("run-timeline-chip");
    expect(down.map((chip) => chip.getAttribute("data-node-id"))).toEqual(["1", "2"]);
    fireEvent.click(down[1]);
    expect(await ringOf(await nodeOf("2"))).toBe("primary");
  });
  it("shows the summary block a message block belongs to, and jumps to it", async () => {
    renderPage();
    clickItem(await unitOf("text"));
    const detail = await screen.findByTestId("run-timeline-unit-detail");
    fireEvent.click(within(detail).getByTestId("run-timeline-chip"));
    expect(await ringOf(await nodeOf("1"))).toBe("primary");
    expect(await screen.findByTestId("run-timeline-node-detail")).toBeTruthy();
  });
  it("says so when no summary block covers a message block", async () => {
    getRunTimeline.mockResolvedValue({
      ...lifetimeResponse,
      units: [{ ...lifetimeResponse.units[0], parent: null }],
    });
    renderPage();
    clickItem(await unitOf("inbound"));
    expect(await screen.findByTestId("run-timeline-uncovered")).toBeTruthy();
  });
});

describe("context breakdown follows selection", () => {
  it("follows a selected block, then a selected node, and is titled with its session and time", async () => {
    renderPage();
    expect(getRunTimelineContext).not.toHaveBeenCalled();
    clickItem(await unitOf("text"));
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 2));
    await waitFor(() =>
      expect(screen.getByTestId("context-breakdown-heading").textContent).toContain("request · session 1 of 2"),
    );
    clickItem(await nodeOf("2"));
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 6));
    await waitFor(() =>
      expect(screen.getByTestId("context-breakdown-heading").textContent).toContain("request · session 2 of 2"),
    );
  });
  it("does not read context on pan and keeps a selected context through pan and zoom", async () => {
    renderPage();
    const chart = await screen.findByTestId("run-timeline-chart");
    wheel(chart, { deltaY: -600, clientX: 100 });
    expect(getRunTimelineContext).not.toHaveBeenCalled();
    expect(screen.queryByTestId("context-breakdown-heading")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    clickItem(await unitOf("text"));
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 2));
    const calls = getRunTimelineContext.mock.calls.length;
    wheel(chart, { deltaY: -600, clientX: 100 });
    wheel(chart, { deltaX: 800, clientX: 100 });
    await act(() => new Promise((resolve) => setTimeout(resolve, 200)));
    expect(getRunTimelineContext).toHaveBeenCalledTimes(calls);
    expect(getRunTimelineContext).toHaveBeenLastCalledWith(42, 2);
  });
});

describe("context size row", () => {
  it("draws one bar per block, as tall as the context through it, with visual hover only", async () => {
    renderPage();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    const key = (k: string) => ({ row: "input", key: k });
    const barAt = (k: string) => {
      const x = xOf(key(k));
      return drawn("input").filter((d) => d.op === "fill" && d.x <= x && x <= d.x + d.w).at(-1);
    };
    const first = barAt("utext-2-2");
    const second = barAt("uthinking-7-7");
    expect(second?.h ?? 0).toBeGreaterThan(0);
    expect((second?.h ?? 0) / (first?.h ?? 1)).toBeCloseTo(420 / 1050);
    expect(first?.color).toContain("#3b82f6");
    expect(second?.color).toContain("#f59e0b");
    pointAt("input", xOf(key("uthinking-7-7")));
    await paintFrame();
    expect(await ringOf(await unitOf("thinking"))).toBe("hover");
    expect(screen.queryByTestId("run-timeline-readout")).toBeNull();
  });
  it("has exactly the blocks of the Messages row, to the pixel", async () => {
    renderPage();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    const fillsOf = (row: string) => drawn(row).filter((d) => d.op === "fill").map((d) => [d.x, d.w]);
    expect(fillsOf("input")).toEqual(fillsOf("units"));
    expect(fillsOf("input")).toHaveLength(lifetimeResponse.units.length);
  });
  it("shows the LLM request of an AIMessage in the details of each of its blocks, not on the timeline", async () => {
    renderPage();
    await screen.findByTestId("run-timeline-chart");
    await paintFrame();
    clickAt("input", xOf({ row: "input", key: "utext-2-2" }));
    const request = await screen.findByTestId("run-timeline-request");
    expect(request.textContent).toContain("1.0k");
    expect(request.textContent).toContain("$0.0021");
    clickAt("input", xOf({ row: "input", key: "uinbound-1-1" }));
    await waitFor(() => expect(screen.queryByTestId("run-timeline-request")).toBeNull());
  });
  it("is off by default, switches on and off, and has no Added context row", async () => {
    renderPage({ contextSize: false });
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByTestId("run-timeline-row-context")).toBeNull();
    expect(screen.queryByTestId("run-timeline-row-added")).toBeNull();
    fireEvent.click(screen.getByTestId("agent-view-context-size"));
    expect(screen.queryByTestId("run-timeline-row-context")).not.toBeNull();
    fireEvent.click(screen.getByTestId("agent-view-context-size"));
    expect(screen.queryByTestId("run-timeline-row-context")).toBeNull();
  });
  it("has no context row for an agent no request has read", async () => {
    getRunTimeline.mockResolvedValue({ ...lifetimeResponse, units: lifetimeResponse.units.map((u) => ({ ...u, context_tokens: null, context_total: null, request: null })) });
    renderPage();
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByTestId("run-timeline-row-context")).toBeNull();
  });
});
