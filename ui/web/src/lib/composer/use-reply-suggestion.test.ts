// meiva:note
// @vitest-environment happy-dom

import { act, cleanup, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem, ReplySuggestionResponse } from "../contracts/types";
import { ApiProvider } from "../transport/transport-context";
import { startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig, httpRig, noUpload, type HttpRig } from "../transport/tests/api/api-provider";
import { UncancellableFetch } from "../transport/tests/api/api-provider";
import { systemKeySource } from "../transport/system-transport";
import { REPLY_SUGGESTION_DELAY_MS, useReplySuggestion } from "./use-reply-suggestion";

function item(kind: BackendTimelineItem["kind"], id: string, extra: Partial<BackendTimelineItem> = {}) {
  return {
    item_id: id,
    kind,
    payload: "text",
    source: null,
    created_at: null,
    inbound_id: null,
    ...extra,
  } as BackendTimelineItem;
}

const AGENT_SAYS = [item("inbound_chat", "1.0", { source: "user" }), item("agent_chat", "2.0")];

type Props = Parameters<typeof useReplySuggestion>[0];

const idle: Props = { agentId: 7, status: "idling", busy: false, items: AGENT_SAYS, enabled: true };

interface Pending {
  agentId: number;
  signal: AbortSignal;
  finish(reply: GatewayReply): void;
}

let pending: Pending[];
let visibility: "visible" | "hidden";
let gateway: TestGateway;
let rig: HttpRig;

function Wrapper({ children }: { children: ReactNode }) {
  return createElement(ApiProvider, { api: rig.api, children });
}

const wrapper = Wrapper;

function setVisibility(value: "visible" | "hidden") {
  visibility = value;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

async function elapse(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function settle(response: ReplySuggestionResponse, index = pending.length - 1) {
  await act(async () => {
    pending[index].finish({ kind: "json", body: response });
    await vi.waitFor(() => expect(gateway.closedRequests.length).toBeGreaterThan(index));
  });
}

beforeEach(async () => {
  pending = [];
  gateway = await startGateway((request) => {
    const match = /^\/api\/agents\/(\d+)\/reply-suggestion$/.exec(request.path);
    if (match === null) return { kind: "text", status: 404, body: "unexpected route" };
    const signal = rig.sent.findLast((sent) => sent.url === `${gateway.origin}${request.path}`)?.init.signal;
    if (signal == null) throw new Error("the native request has no observed abort signal");
    return new Promise<GatewayReply>((finish) => pending.push({ agentId: Number(match[1]), signal, finish }));
  });
  rig = gatewayRig(gateway);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
});

afterEach(async () => {
  cleanup();
  for (const request of pending) request.finish({ kind: "json", body: { suggestions: [], reason: null } });
  vi.useRealTimers();
  Reflect.deleteProperty(document, "visibilityState");
  await gateway.close();
});

describe("useReplySuggestion", () => {
  it("requests after the quiet period, not before, and returns the answer", async () => {
    const { result } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS - 1);
    expect(rig.sent).toHaveLength(0);

    await elapse(1);
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    expect(pending[0].agentId).toBe(7);
    expect(result.current).toBeNull();

    await settle({ suggestions: ["Use A.", "Use B.", "Use C.", "Use D."], reason: null });
    await vi.waitFor(() => expect(result.current).toEqual(["Use A.", "Use B.", "Use C.", "Use D."]));
  });

  it("starts nothing when a new turn begins within the quiet period", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS - 100);
    rerender({ ...idle, busy: true });
    await elapse(REPLY_SUGGESTION_DELAY_MS * 2);

    expect(rig.sent).toHaveLength(0);
  });

  it("waits for the running -> idling transition: no request while busy or running", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: { ...idle, busy: true, status: "running" },
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS * 2);
    expect(rig.sent).toHaveLength(0);

    rerender(idle);
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await vi.waitFor(() => expect(pending).toHaveLength(1));
  });

  it("aborts the request and clears the suggestion when a new turn starts", async () => {
    const fault = new UncancellableFetch();
    rig = httpRig({ base: gateway.origin, fetch: fault.fetch, now: () => performance.now(),
      keys: systemKeySource(), openUpload: noUpload });
    try {
      const { result, rerender } = renderHook((props: Props) => useReplySuggestion(props), {
        wrapper, initialProps: idle,
      });
      await elapse(REPLY_SUGGESTION_DELAY_MS);
      expect(rig.sent).toHaveLength(1);
      rerender({ ...idle, busy: true });
      expect(rig.sent[0].init.signal?.aborted).toBe(true);
      await act(async () => { fault.respond({ suggestions: ["stale"], reason: null }); await Promise.resolve(); });
      rerender(idle);
      expect(result.current).toBeNull();
    } finally {
      fault.respond({ suggestions: [], reason: null });
    }
  });

  it("clears a shown suggestion as soon as a new turn starts", async () => {
    const { result, rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await settle({ suggestions: ["Yes"], reason: null });
    await vi.waitFor(() => expect(result.current).toEqual(["Yes"]));

    rerender({ ...idle, busy: true });
    expect(result.current).toBeNull();
  });

  it("clears it when the user's own message becomes the last item", async () => {
    const { result, rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await settle({ suggestions: ["Yes"], reason: null });

    rerender({ ...idle, items: [...AGENT_SAYS, item("inbound_chat", "3.0", { source: "user" })] });
    expect(result.current).toBeNull();
  });

  it("aborts when the selected agent changes", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));

    rerender({ ...idle, agentId: 8, items: [item("agent_chat", "9.0")] });

    expect(pending[0].signal.aborted).toBe(true);
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await vi.waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].agentId).toBe(8);
  });

  it("aborts on unmount", async () => {
    const { unmount } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));

    unmount();

    expect(pending[0].signal.aborted).toBe(true);
  });

  it("aborts when the page is hidden and asks again when it is visible", async () => {
    renderHook((props: Props) => useReplySuggestion(props), { wrapper, initialProps: idle });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));

    setVisibility("hidden");
    expect(pending[0].signal.aborted).toBe(true);

    setVisibility("visible");
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await vi.waitFor(() => expect(pending).toHaveLength(2));
  });

  it("sends nothing while the page is hidden", async () => {
    visibility = "hidden";
    renderHook((props: Props) => useReplySuggestion(props), { wrapper, initialProps: idle });

    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    expect(rig.sent).toHaveLength(0);
  });

  it("sends nothing when the setting is off", async () => {
    renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: { ...idle, enabled: false },
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    expect(rig.sent).toHaveLength(0);
  });

  it("aborts when the setting is turned off mid-request", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));

    rerender({ ...idle, enabled: false });

    expect(pending[0].signal.aborted).toBe(true);
  });

  it.each([
    ["the agent is terminated", { status: "terminated" as const }],
    ["there is no agent", { agentId: null }],
    ["the last item is not the agent's message", { items: [item("inbound_chat", "1.0", { source: "user" })] }],
    ["the agent's message is still streaming", { items: [item("agent_chat", "2.0", { partial: true })] }],
    ["the agent ended on code output", { items: [...AGENT_SAYS, item("code_output", "3.0")] }],
    ["the timeline is empty", { items: [] }],
  ])("sends nothing when %s", async (_name, override) => {
    renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: { ...idle, ...override },
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    expect(rig.sent).toHaveLength(0);
  });

  it("looks through trailing framework markers to the agent's message", async () => {
    renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: { ...idle, items: [...AGENT_SAYS, item("system_marker", "3.0")] },
    });

    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));

    await vi.waitFor(() => expect(pending).toHaveLength(1));
  });

  it("requests each turn once, however many times the conditions flicker", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await settle({ suggestions: [], reason: "not_useful" });

    rerender({ ...idle, busy: true });
    rerender(idle);
    await elapse(REPLY_SUGGESTION_DELAY_MS * 3);

    await vi.waitFor(() => expect(pending).toHaveLength(1));
  });

  it("does not request a turn again after a failure", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await act(async () => {
      pending[0].finish({ kind: "text", status: 503, body: "unavailable" });
      await vi.waitFor(() => expect(rig.responses).toHaveLength(1));
    });

    rerender({ ...idle, busy: true });
    rerender(idle);
    await elapse(REPLY_SUGGESTION_DELAY_MS * 2);

    await vi.waitFor(() => expect(pending).toHaveLength(1));
  });

  it("requests the next turn when the agent speaks again", async () => {
    const { rerender } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    await settle({ suggestions: ["first"], reason: null });

    rerender({ ...idle, busy: true });
    rerender({
      ...idle,
      items: [...AGENT_SAYS, item("inbound_chat", "3.0", { source: "user" }), item("agent_chat", "4.0")],
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));

    await vi.waitFor(() => expect(pending).toHaveLength(2));
  });

  it("returns nothing for an empty suggestion", async () => {
    const { result } = renderHook((props: Props) => useReplySuggestion(props), {
      wrapper,
      initialProps: idle,
    });
    await elapse(REPLY_SUGGESTION_DELAY_MS);
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));

    await settle({ suggestions: [], reason: "not_useful" });

    expect(result.current).toBeNull();
  });
});
