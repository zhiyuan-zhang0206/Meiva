import type * as ApiModule from "@/lib/transport/api";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ContextBreakdownResponse,
  RunTimelineContext,
  RunTimelineLinks,
  RunTimelineMessages,
  RunTimelineResponse,
  UserSettingListResponse,
} from "@/lib/contracts/types";

const {
  getRunTimeline,
  getRunTimelineMessages,
  getRunTimelineContext,
  getRunTimelineLinks,
  getAgentRoster,
  getSettings,
  getContextBreakdown,
} =
  vi.hoisted(() => ({
    getRunTimeline:
      vi.fn<
        (agentId: number, options?: { from?: string; to?: string; signal?: AbortSignal }) => Promise<RunTimelineResponse>
      >(),
    getRunTimelineMessages:
      vi.fn<
        (
          agentId: number,
          range: { start: number; end: number; limit?: number; full?: boolean },
        ) => Promise<RunTimelineMessages>
      >(),
    getSettings: vi.fn<() => Promise<UserSettingListResponse>>(),
    getContextBreakdown: vi.fn<(agentId: number) => Promise<ContextBreakdownResponse>>(),
    getRunTimelineContext: vi.fn<(agentId: number, at: number) => Promise<RunTimelineContext>>(),
    getRunTimelineLinks: vi.fn<(agents: readonly number[], window: { from: string; to: string }) => Promise<RunTimelineLinks>>(),
    getAgentRoster: vi.fn(),
  }));

vi.mock("@/lib/transport/api", async (importOriginal) => ({
  ...(await importOriginal<typeof ApiModule>()),
  api: { getRunTimeline, getRunTimelineMessages, getRunTimelineContext, getRunTimelineLinks, getAgentRoster, getSettings, getContextBreakdown },
}));

import {
  clickAt,
  drawn,
  itemX,
  look,
  mockCanvas,
  paintFrame,
} from "@/components/run-timeline/canvas/run-timeline-test-canvas-mock";
import { useMediaQuery } from "@/lib/layout/use-media-query";
import RunTimelinePage from "./page";


import {
  cbdFixture,
  LIFETIME,
  lifetimeResponse,
  messagesResponse,
} from "./run-timeline-test-fixtures-mock";
function render({ contextSize = true }: { contextSize?: boolean } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = rtlRender(
    <QueryClientProvider client={queryClient}>
      <RunTimelinePage params={Promise.resolve({ agents: "42" })} />
    </QueryClientProvider>,
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

const lookOf = async (item: Item) => {
  await paintFrame();
  return look(item.row, xOf(item));
};
const ringOf = async (item: Item) => (await lookOf(item)).ring;

beforeEach(() => {
  expect(typeof useMediaQuery).toBe("function");
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


describe("the default window", () => {
  it("asks for the latest day with bounded message detail", async () => {
    render();
    await waitFor(() => expect(getRunTimeline).toHaveBeenCalledWith(42, { recentHours: 24, maxUnits: 5000, signal: expect.any(AbortSignal) as AbortSignal }));
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("draws every tree level, topmost first, with the message units below", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    const rows = screen
      .getAllByTestId(/^run-timeline-row-/)
      .map((row) => row.getAttribute("data-testid"));
    expect(rows).toEqual([
      "run-timeline-row-level-2",
      "run-timeline-row-level-1",
      "run-timeline-row-units",
      "run-timeline-row-context",
      "run-timeline-row-user",
      "run-timeline-row-other",
    ]);

    expect(screen.queryAllByTestId("run-timeline-node")).toHaveLength(0);
    await paintFrame();
    expect(screen.getByTestId("run-timeline-canvas-level-1")).toBeTruthy();
    expect(screen.getByTestId("run-timeline-window").textContent).toContain("3 summary nodes");
  });

  it("has no compare entry and no session switch", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    expect(screen.queryByRole("link", { name: "Compare agents" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Compact session" })).toBeNull();
  });

  it("shows the context of a selected request, not the agent's current context", async () => {
    render();
    clickItem(await unitOf("thinking"));
    await waitFor(() => expect(getRunTimelineContext).toHaveBeenCalledWith(42, 7));
    expect(getContextBreakdown).not.toHaveBeenCalled();
    expect((await screen.findByTestId("context-breakdown-heading")).textContent).toContain("request · session 2 of 2");
  });
});

describe("ancestors", () => {
  it("lights a node and its ancestors, and steps the others back", async () => {
    render();
    clickItem(await nodeOf("1"));
    expect(await ringOf(await nodeOf("1"))).toBe("primary");
    expect(await ringOf(await nodeOf("3"))).toBe("linked");
    expect(await ringOf(await nodeOf("2"))).toBe("none");
  });

  it("lights a message block's covering leaf and every ancestor above it", async () => {
    render();
    clickItem(await unitOf("text"));
    expect(await ringOf(await unitOf("text"))).toBe("primary");
    expect(await ringOf(await nodeOf("1"))).toBe("linked");
    expect(await ringOf(await nodeOf("3"))).toBe("linked");
    expect(await ringOf(await nodeOf("2"))).toBe("none");
  });

});

describe("selecting", () => {
  it("shows a node's summary, span and both costs in the side panel", async () => {
    render();
    clickItem(await nodeOf("1"));

    const detail = await screen.findByTestId("run-timeline-node-detail");
    expect(within(detail).getByTestId("run-timeline-summary").textContent).toBe(
      "The agent read the repo\nand planned the change.",
    );
    expect(detail.textContent).toContain("5 messages");

    const facts = within(detail).getByTestId("run-timeline-details");
    expect(facts.textContent).toContain("Time");
    expect(facts.textContent).toContain("Calls");
    expect(detail.textContent).not.toContain("Agent cost over this span");
    expect(detail.textContent).not.toContain("Cost of generating this summary");
    expect(detail.textContent).not.toContain("of input from cache");

    expect(within(detail).getAllByText("3.0k").length).toBeGreaterThan(0);
    expect(within(detail).getAllByText("2.4k").length).toBeGreaterThan(0);
  });

  it("names the agent above its rows with no present-state facts", async () => {
    render();
    expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Agent view");
    await waitFor(() => expect(screen.getByTestId("run-timeline-agent").textContent).toBe("Agent #42 · planner"));
    expect(screen.queryByTestId("run-timeline-status")).toBeNull();
    expect(screen.queryByTestId("run-timeline-model")).toBeNull();
  });

  it("renders a message's Markdown and shows the tokens it occupies, marked when estimated", async () => {
    getRunTimelineMessages.mockResolvedValue({
      messages: [
        {
          idx: 2,
          ts: "2026-10-04T12:05:00.000000Z",
          source: null,
          parts: [{ kind: "text", chars: 9, text: "**bold** x", text_truncated: false }],
          context_tokens: 1234,
          estimated: true,
        },
      ],
      next_start: null,
    });
    render();
    clickItem(await nodeOf("1"));
    const list = await screen.findByTestId("run-timeline-messages");
    expect((await within(list).findByText("bold")).tagName).toBe("STRONG");
    expect(within(list).getByTestId("run-timeline-message-tokens").textContent).toBe("~1.2k tokens");
    const detail = screen.getByTestId("run-timeline-node-detail");
    expect(within(detail).getByText("Details")).toBeTruthy();
  });

  it("reads a unit's own raw parts: a text unit shows the text, not the thinking or the call", async () => {
    render();
    clickItem(await unitOf("text"));

    const detail = await screen.findByTestId("run-timeline-unit-detail");
    expect(within(detail).queryByRole("checkbox")).toBeNull();
    await waitFor(() =>
      expect(getRunTimelineMessages).toHaveBeenCalledWith(42, {
        start: 2,
        end: 2,
        limit: 50,
        full: true,
      }),
    );
    expect(await within(detail).findByText("on it")).toBeTruthy();

    expect(within(detail).getAllByTestId("run-timeline-message-tokens")).toHaveLength(1);
    expect(within(detail).queryByText("need a plan")).toBeNull();
    expect(within(detail).queryByText("ls")).toBeNull();
  });
});

// meiva:link ui/web/src/docs/frontend-components/run-timeline/axis-and-keyboard.meiva.okf.md
function wheel(target: Element, init: WheelEventInit, count = 1) {
  act(() => {
   for (let i = 0; i < count; i++) {

    const event = new MouseEvent("wheel", { bubbles: true, cancelable: true, clientX: init.clientX });
    Object.defineProperties(event, {
      deltaX: { value: init.deltaX ?? 0 },
      deltaY: { value: init.deltaY ?? 0 },
    });
    target.dispatchEvent(event);
   }
  });
}

const summaryText = () => screen.getByTestId("run-timeline-window").textContent;

describe("no drill-in", () => {
  it("a double-click neither zooms nor drills, there is no Drill button, no breadcrumb and no sessions panel", async () => {
    render();
    const whole = (await screen.findByTestId("run-timeline-window")).textContent;
    const target = await nodeOf("1");
    clickItem(target);
    fireEvent.doubleClick(screen.getByTestId(`run-timeline-canvas-${target.row}`));
    expect(await screen.findByTestId("run-timeline-node-detail")).toBeTruthy();
    expect(summaryText()).toBe(whole);
    expect(screen.queryByRole("button", { name: "Drill in" })).toBeNull();
    expect(screen.queryByTestId("run-timeline-crumbs")).toBeNull();
    expect(screen.queryByTestId("run-timeline-sessions-toggle")).toBeNull();
  });
});

describe("zoom and pan", () => {
  beforeEach(() => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const wide = this.hasAttribute("data-track");
      return { left: 100, top: 0, width: wide ? 1000 : 0, height: 0, right: 1100, bottom: 0, x: 100, y: 0, toJSON: () => ({}) };
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("the wheel zooms around the cursor and the loaded data is not read again", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    const whole = summaryText();
    wheel(chart, { deltaY: -400, clientX: 100 });
    const zoomed = summaryText();
    expect(zoomed).not.toBe(whole);

    expect(zoomed.split(" – ")[0]).toBe(whole.split(" – ")[0]);
    expect(getRunTimeline).toHaveBeenCalledTimes(1);
  });

  it("wheel events that arrive before React renders compose instead of repeating the old view", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    wheel(chart, { deltaY: -300, clientX: 600 });
    wheel(chart, { deltaY: -300, clientX: 600 });
    const sequential = summaryText();
    fireEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    wheel(chart, { deltaY: -300, clientX: 600 }, 2);
    expect(summaryText()).toBe(sequential);
  });

  it("a horizontal scroll pans a zoomed view", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    wheel(chart, { deltaY: -600, clientX: 100 });
    const zoomed = summaryText();
    wheel(chart, { deltaX: 300, deltaY: 0, clientX: 600 });
    expect(summaryText()).not.toBe(zoomed);
  });

  it("keeps painted rows and a fixed status slot while nearby pans reuse prefetched data", async () => {
    const recent = {
      ...lifetimeResponse, snapshot: "stable", nodes: [], units: [],
      window: { from: "2026-10-04T14:00:00.000Z", to: LIFETIME.to },
    };
    getRunTimeline.mockResolvedValueOnce(recent)
      .mockImplementationOnce((_id, options) => Promise.resolve({
        ...lifetimeResponse, snapshot: "stable", window: { from: options!.from!, to: options!.to! },
      }));
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    const status = screen.getByTestId("run-timeline-window-status-42");
    expect(status.textContent).toBe("");
    wheel(chart, { deltaX: -400, clientX: 600 });
    await screen.findByTestId("run-timeline-row-level-2");
    await waitFor(() => expect(status.textContent).toBe(""));
    await paintFrame();
    const canvas = screen.getByTestId("run-timeline-canvas-level-2");
    expect(drawn("level-2").some((shape) => shape.op === "fill")).toBe(true);

    wheel(chart, { deltaX: -100, clientX: 600 });
    expect(screen.getByTestId("run-timeline-canvas-level-2")).toBe(canvas);
    expect(screen.getByTestId("run-timeline-window-status-42")).toBe(status);
    expect(status.textContent).toBe("");
    await act(() => new Promise((resolve) => setTimeout(resolve, 150)));
    expect(getRunTimeline).toHaveBeenCalledTimes(2);
    await paintFrame();
    expect(screen.getByTestId("run-timeline-canvas-level-2")).toBe(canvas);
    expect(drawn("level-2").some((shape) => shape.op === "fill")).toBe(true);
    await waitFor(() => expect(status.textContent).toBe(""));
    expect(screen.getByTestId("run-timeline-canvas-level-2")).toBe(canvas);
  });

  it("a drag pans and its release does not select the block under the pointer", async () => {
    render();
    const chart = await screen.findByTestId("run-timeline-chart");
    chart.setPointerCapture = vi.fn();
    wheel(chart, { deltaY: -600, clientX: 100 });
    const zoomed = summaryText();

    const unit = screen.getByTestId("run-timeline-canvas-units");
    fireEvent.pointerDown(unit, { clientX: 400, button: 0, pointerId: 1 });
    fireEvent.pointerMove(unit, { clientX: 200, pointerId: 1 });
    fireEvent.pointerUp(unit, { clientX: 200, pointerId: 1 });
    fireEvent.click(unit, { clientX: 100 });
    expect(summaryText()).not.toBe(zoomed);
    expect(screen.queryByTestId("run-timeline-unit-detail")).toBeNull();
  });

  it("a press that barely moves is still a click", async () => {
    render();
    const human = await unitOf("inbound");
    const unit = screen.getByTestId("run-timeline-canvas-units");
    fireEvent.pointerDown(unit, { clientX: 400, button: 0, pointerId: 1 });
    fireEvent.pointerUp(unit, { clientX: 401, pointerId: 1 });

    clickAt(human.row, xOf(human) + 100);
    expect(await screen.findByTestId("run-timeline-unit-detail")).toBeTruthy();
  });

  it("the buttons zoom and the fit button restores the whole lifetime", async () => {
    render();
    await screen.findByTestId("run-timeline-chart");
    const whole = summaryText();
    expect(screen.getByRole("button", { name: "Reset zoom" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(summaryText()).not.toBe(whole);
    fireEvent.click(screen.getByRole("button", { name: "Reset zoom" }));
    expect(summaryText()).toBe(whole);
  });
});
