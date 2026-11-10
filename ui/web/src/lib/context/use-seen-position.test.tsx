import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem, UserMessage } from "../contracts/types";
import { useStore } from "../state/store";
import type { Api } from "../transport/api";
import { cacheOnlyApi } from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import {
  REVEAL_MAX_OLDER_PAGES,
  SEEN_RETRY_DELAYS_MS,
  newestDeliveryId,
  unseenTarget,
  useSeenPosition,
} from "./use-seen-position";

const NOW = 1_000_000;

function record(id: number, content: string | null = `reply ${id}`): UserMessage {
  return {
    id, agent_id: 7, mode: "prefix", source_key: `assistant:m${id}:0.0`, content,
    suggestions: [], created_at: "2026-11-10T00:00:00Z", seen: false,
  };
}
function row(id: number, withRecord = true): BackendTimelineItem {
  return {
    item_id: `${id}.0`, kind: "agent_chat", source: null, payload: `reply ${id}`,
    created_at: "2026-11-10T00:00:00Z", inbound_id: null, user_facing: true, show_timestamp: true,
    ...(withRecord ? { userDelivery: record(id) } : {}),
  };
}

interface Props {
  agentId: number | null;
  seenId: number | undefined;
  messages: readonly UserMessage[];
  items: readonly BackendTimelineItem[];
  updatedAt: number;
  timelineLoading?: boolean;
  hasMoreOlder?: boolean;
  loadingOlder?: boolean;
}
const entered: Props = {
  agentId: 7, seenId: 4, messages: [record(3), record(5), record(8)], items: [row(3), row(5), row(8)],
  updatedAt: NOW + 1000,
};

const markUserMessagesSeen = vi.fn<Api["markUserMessagesSeen"]>();
const loadOlder = vi.fn<() => void>();
let mintedKeys = 0;
const api: Api = { ...cacheOnlyApi(), markUserMessagesSeen, newOperationKey: () => `key-${++mintedKeys}` };

let client: QueryClient;
function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}
function mount(initial: Props = entered, { fetched = true }: { fetched?: boolean } = {}) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ApiProvider api={api}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ApiProvider>
  );
  const view = renderHook(
    (props: Props) => useSeenPosition({
      agentId: props.agentId,
      feed: { seenId: props.seenId, messages: props.messages, updatedAt: props.updatedAt },
      items: props.items,
      timelineLoading: props.timelineLoading ?? false,
      hasMoreOlder: props.hasMoreOlder ?? false,
      loadingOlder: props.loadingOlder ?? false,
      loadOlder,
    }),
    { wrapper, initialProps: fetched ? { ...initial, updatedAt: 0 } : initial },
  );
  if (fetched) view.rerender(initial);
  return view;
}
async function settle(ms = 0) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
}

beforeEach(() => {
  vi.useFakeTimers();
  mintedKeys = 0;
  markUserMessagesSeen.mockReset();
  markUserMessagesSeen.mockResolvedValue({ seen_id: 8, advanced: true });
  loadOlder.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  setVisibility("visible");
  useStore.setState({ readingAgentId: null });
});

describe("unseenTarget", () => {
  it("names the loaded item of the lowest delivery above the cursor", () => {
    expect(unseenTarget([row(3), row(5), row(8)], [record(3), record(5), record(8)], 4)).toEqual({
      itemId: "5.0", complete: true, any: true,
    });
  });
  it("is incomplete when the oldest unseen delivery has no loaded item", () => {
    expect(unseenTarget([row(8)], [record(5), record(8)], 4)).toEqual({ itemId: "8.0", complete: false, any: true });
  });
  it("finds nothing past the cursor, and ignores deliveries without content", () => {
    expect(unseenTarget([row(3)], [record(3)], 3)).toEqual({ itemId: null, complete: false, any: false });
    expect(unseenTarget([], [record(9, null)], 0).any).toBe(false);
  });
  it("reports the newest delivery id the page holds", () => {
    expect(newestDeliveryId([record(3), record(9), record(5)])).toBe(9);
    expect(newestDeliveryId([])).toBeNull();
  });
});

describe("useSeenPosition", () => {
  it("advances the cursor to the newest delivery on entry, with no dwell and wherever the timeline is", async () => {
    mount();
    await settle();
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
    expect(markUserMessagesSeen).toHaveBeenCalledWith(7, { up_to_id: 8 }, expect.any(String));
  });

  it("reveals the first delivery past the cursor it held on entry, and keeps that target after the advance", async () => {
    const view = mount();
    await settle();
    expect(view.result.current).toEqual({ itemId: "5.0", token: 1 });
    view.rerender({ ...entered, seenId: 8, updatedAt: NOW + 2000 });
    await settle();
    expect(view.result.current).toEqual({ itemId: "5.0", token: 1 });
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
  });

  it("reveals nothing and sends nothing when no delivery lies past the cursor", async () => {
    const view = mount({ ...entered, seenId: 8 });
    await settle();
    expect(view.result.current).toBeNull();
    expect(markUserMessagesSeen).not.toHaveBeenCalled();
  });

  it("waits for a feed fetched since the look began", async () => {
    const view = mount(entered, { fetched: false });
    await settle(5000);
    expect(markUserMessagesSeen).not.toHaveBeenCalled();
    expect(view.result.current).toBeNull();
    view.rerender({ ...entered, updatedAt: entered.updatedAt + 10 });
    await settle();
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
  });

  it("publishes the agent as being read exactly while it is open in a visible page", () => {
    const view = mount();
    expect(useStore.getState().readingAgentId).toBe(7);
    act(() => setVisibility("hidden"));
    expect(useStore.getState().readingAgentId).toBeNull();
    act(() => setVisibility("visible"));
    expect(useStore.getState().readingAgentId).toBe(7);
    view.rerender({ ...entered, agentId: null });
    expect(useStore.getState().readingAgentId).toBeNull();
    view.unmount();
    expect(useStore.getState().readingAgentId).toBeNull();
  });

  it("sees nothing while the page is hidden, and looks again when it returns", async () => {
    act(() => setVisibility("hidden"));
    const view = mount();
    await settle(5000);
    expect(markUserMessagesSeen).not.toHaveBeenCalled();
    expect(view.result.current).toBeNull();
    act(() => setVisibility("visible"));
    view.rerender({ ...entered, updatedAt: NOW + 61_000 });
    await settle();
    expect(markUserMessagesSeen).toHaveBeenCalledWith(7, { up_to_id: 8 }, expect.any(String));
    expect(view.result.current?.itemId).toBe("5.0");
  });

  it("on return to a conversation that was left open, jumps only if something new arrived", async () => {
    const view = mount({ ...entered, seenId: 8 });
    await settle();
    act(() => setVisibility("hidden"));
    act(() => setVisibility("visible"));
    view.rerender({ ...entered, seenId: 8, updatedAt: NOW + 61_000 });
    await settle();
    expect(view.result.current).toBeNull();
    act(() => setVisibility("hidden"));
    act(() => setVisibility("visible"));
    view.rerender({
      ...entered, seenId: 8, messages: [...entered.messages, record(11)], items: [...entered.items, row(11)],
      updatedAt: NOW + 121_000,
    });
    await settle();
    expect(view.result.current?.itemId).toBe("11.0");
    expect(markUserMessagesSeen).toHaveBeenLastCalledWith(7, { up_to_id: 11 }, expect.any(String));
  });

  it("sees a delivery that arrives while it stays open and visible, without a new reveal", async () => {
    const view = mount({ ...entered, seenId: 8 });
    await settle();
    view.rerender({
      ...entered, seenId: 8, messages: [...entered.messages, record(11)], items: [...entered.items, row(11)],
      updatedAt: NOW + 2000,
    });
    await settle();
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
    expect(markUserMessagesSeen).toHaveBeenCalledWith(7, { up_to_id: 11 }, expect.any(String));
    expect(view.result.current).toBeNull();
    expect(useStore.getState().readingAgentId).toBe(7);
  });

  it("still advances when the oldest unseen delivery matches no row", async () => {
    const view = mount({ ...entered, items: [row(8)] });
    await settle();
    expect(view.result.current?.itemId).toBe("8.0");
    expect(markUserMessagesSeen).toHaveBeenCalledWith(7, { up_to_id: 8 }, expect.any(String));
  });

  it("reveals nothing when no unseen delivery has a loaded item", async () => {
    const view = mount({ ...entered, items: [] });
    await settle();
    expect(view.result.current).toBeNull();
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
  });

  it("loads older pages until the oldest unseen delivery is in, then reveals it", async () => {
    const props = { ...entered, items: [row(8)], hasMoreOlder: true };
    const view = mount(props);
    await settle();
    expect(loadOlder).toHaveBeenCalledTimes(1);
    expect(view.result.current).toBeNull();
    view.rerender({ ...props, items: [row(5), row(8)] });
    await settle();
    expect(loadOlder).toHaveBeenCalledTimes(1);
    expect(view.result.current?.itemId).toBe("5.0");
  });

  it("stops loading after the page limit and lands on the oldest unseen item it has", async () => {
    let props: Props = { ...entered, items: [row(8)], hasMoreOlder: true };
    const view = mount(props);
    for (let page = 1; page <= REVEAL_MAX_OLDER_PAGES; page += 1) {
      await settle();
      expect(loadOlder).toHaveBeenCalledTimes(page);
      props = { ...props, loadingOlder: true };
      view.rerender(props);
      props = { ...props, loadingOlder: false, items: [...props.items] };
      view.rerender(props);
      await settle();
    }
    await settle();
    expect(loadOlder).toHaveBeenCalledTimes(REVEAL_MAX_OLDER_PAGES);
    expect(view.result.current?.itemId).toBe("8.0");
  });

  it("does not decide while the timeline is still loading", async () => {
    const view = mount({ ...entered, items: [], timelineLoading: true, hasMoreOlder: true });
    await settle();
    expect(loadOlder).not.toHaveBeenCalled();
    expect(view.result.current).toBeNull();
    view.rerender({ ...entered, timelineLoading: false });
    await settle();
    expect(view.result.current?.itemId).toBe("5.0");
  });

  it("refetches the feed and the roster after the advance", async () => {
    mount();
    client.setQueryData(["user-messages", 7], { mode: "prefix", seen_id: 4, items: [] });
    client.setQueryData(["agents"], { agents: [] });
    client.setQueryData(["user-messages", 8], { mode: "prefix", seen_id: 0, items: [] });
    await settle();
    const stale = (key: unknown[]) => client.getQueryState(key)?.isInvalidated;
    expect([stale(["user-messages", 7]), stale(["agents"]), stale(["user-messages", 8])]).toEqual([true, true, false]);
  });

  it("retries a failed advance with growing waits and one operation key", async () => {
    markUserMessagesSeen.mockRejectedValueOnce(new Error("offline")).mockRejectedValueOnce(new Error("502"));
    mount();
    await settle();
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
    await settle(SEEN_RETRY_DELAYS_MS[0] - 1);
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
    await settle(1);
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(2);
    await settle(SEEN_RETRY_DELAYS_MS[1]);
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(3);
    expect(new Set(markUserMessagesSeen.mock.calls.map((call) => call[2])).size).toBe(1);
  });

  it("stops after the last retry and tries again when a fresh feed arrives", async () => {
    markUserMessagesSeen.mockRejectedValue(new Error("down"));
    const view = mount();
    await settle();
    for (const delay of SEEN_RETRY_DELAYS_MS) await settle(delay);
    const spent = 1 + SEEN_RETRY_DELAYS_MS.length;
    await settle(120_000);
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(spent);
    markUserMessagesSeen.mockResolvedValue({ seen_id: 8, advanced: true });
    view.rerender({ ...entered, updatedAt: NOW + 5000 });
    await settle();
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(spent + 1);
  });

  it("stops retrying once the page is hidden", async () => {
    markUserMessagesSeen.mockRejectedValue(new Error("down"));
    mount();
    await settle();
    act(() => setVisibility("hidden"));
    await settle(60_000);
    expect(markUserMessagesSeen).toHaveBeenCalledTimes(1);
  });

  it("starts over for another agent", async () => {
    const view = mount();
    await settle();
    const other = { ...entered, agentId: 9, seenId: 0, messages: [record(20)], items: [row(20)] };
    view.rerender({ ...other, updatedAt: 0 });
    view.rerender({ ...other, updatedAt: NOW + 3000 });
    await settle();
    expect(markUserMessagesSeen).toHaveBeenLastCalledWith(9, { up_to_id: 20 }, expect.any(String));
    expect(view.result.current?.itemId).toBe("20.0");
  });
});
