import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UserMessage } from "../contracts/types";
import { useStore } from "../state/store";
import type { Api } from "../transport/api";
import { cacheOnlyApi } from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import {
  READ_DWELL_MS,
  READ_RETRY_DELAYS_MS,
  unreadHighWater,
  useReadingAgent,
} from "./use-reading-agent";

function record(id: number, readAt: string | null = null): UserMessage {
  return {
    id, agent_id: 7, mode: "prefix", source_key: `assistant:m${id}:0.0`, content: `reply ${id}`,
    suggestions: [], created_at: "2026-11-10T00:00:00Z", read_at: readAt,
  };
}

interface Props {
  agentId: number | null;
  atLatest: boolean;
  messages: readonly UserMessage[];
  fetchedAt: number;
}
const open: Props = { agentId: 7, atLatest: true, messages: [record(3), record(5)], fetchedAt: 1 };

const readUserMessages = vi.fn<Api["readUserMessages"]>();
let mintedKeys = 0;
const api: Api = { ...cacheOnlyApi(), readUserMessages, newOperationKey: () => `key-${++mintedKeys}` };

let client: QueryClient;
function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}
function mount(initial: Props = open) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ApiProvider api={api}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ApiProvider>
  );
  return renderHook((props: Props) => useReadingAgent(props), { wrapper, initialProps: initial });
}
async function elapse(ms: number) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
}

beforeEach(() => {
  mintedKeys = 0;
  readUserMessages.mockReset();
  readUserMessages.mockResolvedValue({ read_count: 2 });
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  setVisibility("visible");
  useStore.setState({ readingAgentId: null });
});

describe("unreadHighWater", () => {
  it("is the highest fetched id when anything is unread, even if that record is already read", () => {
    expect(unreadHighWater([record(3), record(9, "2026-11-10T00:00:01Z")])).toBe(9);
  });
  it("is null when everything fetched is read or nothing was fetched", () => {
    expect(unreadHighWater([record(3, "2026-11-10T00:00:01Z")])).toBeNull();
    expect(unreadHighWater([])).toBeNull();
  });
});

describe("useReadingAgent", () => {
  it("marks everything up to the fetched high-water mark read once the dwell has passed", async () => {
    mount();
    await elapse(READ_DWELL_MS - 1);
    expect(readUserMessages).not.toHaveBeenCalled();
    await elapse(1);
    expect(readUserMessages).toHaveBeenCalledTimes(1);
    expect(readUserMessages).toHaveBeenCalledWith(7, { up_to_id: 5 }, expect.any(String));
  });

  it("refetches the feed and the roster after a successful mark", async () => {
    mount();
    client.setQueryData(["user-messages", 7], { mode: "prefix", items: [] });
    client.setQueryData(["agents"], { agents: [] });
    client.setQueryData(["user-messages", 8], { mode: "prefix", items: [] });
    await elapse(READ_DWELL_MS);
    const invalidated = (key: unknown[]) => client.getQueryState(key)?.isInvalidated;
    expect([invalidated(["user-messages", 7]), invalidated(["agents"]), invalidated(["user-messages", 8])]).toEqual([true, true, false]);
  });

  it("publishes the agent as being read exactly while the conversation is open, visible and at the latest position", () => {
    const view = mount();
    expect(useStore.getState().readingAgentId).toBe(7);
    view.rerender({ ...open, atLatest: false });
    expect(useStore.getState().readingAgentId).toBeNull();
    view.rerender(open);
    expect(useStore.getState().readingAgentId).toBe(7);
    act(() => setVisibility("hidden"));
    expect(useStore.getState().readingAgentId).toBeNull();
    act(() => setVisibility("visible"));
    expect(useStore.getState().readingAgentId).toBe(7);
    view.unmount();
    expect(useStore.getState().readingAgentId).toBeNull();
  });

  it("sends nothing while the reader is scrolled away from the latest position, and sends once they return", async () => {
    const view = mount({ ...open, atLatest: false });
    await elapse(READ_DWELL_MS * 5);
    expect(readUserMessages).not.toHaveBeenCalled();
    view.rerender(open);
    await elapse(READ_DWELL_MS);
    expect(readUserMessages).toHaveBeenCalledTimes(1);
  });

  it("sends nothing while the document is hidden", async () => {
    act(() => setVisibility("hidden"));
    mount();
    await elapse(READ_DWELL_MS * 5);
    expect(readUserMessages).not.toHaveBeenCalled();
  });

  it("does not send when the reader leaves before the dwell ends", async () => {
    const view = mount();
    await elapse(READ_DWELL_MS - 10);
    view.rerender({ ...open, atLatest: false });
    await elapse(READ_DWELL_MS * 2);
    expect(readUserMessages).not.toHaveBeenCalled();
  });

  it("sends nothing when nothing is unread or no agent is open", async () => {
    mount({ ...open, messages: [record(3, "2026-11-10T00:00:01Z")] });
    mount({ ...open, agentId: null });
    await elapse(READ_DWELL_MS * 5);
    expect(readUserMessages).not.toHaveBeenCalled();
  });

  it("retries a failed request with growing waits and the same operation key", async () => {
    readUserMessages
      .mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(new Error("502"))
      .mockResolvedValue({ read_count: 2 });
    mount();
    await elapse(READ_DWELL_MS);
    expect(readUserMessages).toHaveBeenCalledTimes(1);
    await elapse(READ_RETRY_DELAYS_MS[0] - 1);
    expect(readUserMessages).toHaveBeenCalledTimes(1);
    await elapse(1);
    expect(readUserMessages).toHaveBeenCalledTimes(2);
    await elapse(READ_RETRY_DELAYS_MS[1]);
    expect(readUserMessages).toHaveBeenCalledTimes(3);
    const keys = readUserMessages.mock.calls.map((call) => call[2]);
    expect(new Set(keys).size).toBe(1);
    await elapse(60_000);
    expect(readUserMessages).toHaveBeenCalledTimes(3);
  });

  it("stops after the last retry and tries again on the next trigger", async () => {
    readUserMessages.mockRejectedValue(new Error("down"));
    const view = mount();
    await elapse(READ_DWELL_MS);
    for (const delay of READ_RETRY_DELAYS_MS) await elapse(delay);
    const spent = 1 + READ_RETRY_DELAYS_MS.length;
    expect(readUserMessages).toHaveBeenCalledTimes(spent);
    await elapse(120_000);
    expect(readUserMessages).toHaveBeenCalledTimes(spent);

    readUserMessages.mockResolvedValue({ read_count: 2 });
    view.rerender({ ...open, fetchedAt: 2 });
    await elapse(READ_DWELL_MS);
    expect(readUserMessages).toHaveBeenCalledTimes(spent + 1);
  });

  it("stops retrying once the reader has left", async () => {
    readUserMessages.mockRejectedValue(new Error("down"));
    const view = mount();
    await elapse(READ_DWELL_MS);
    view.rerender({ ...open, atLatest: false });
    await elapse(60_000);
    expect(readUserMessages).toHaveBeenCalledTimes(1);
  });

  it("marks a newer delivery that arrives while the reader stays at the latest position, with a fresh key", async () => {
    const view = mount();
    await elapse(READ_DWELL_MS);
    view.rerender({ ...open, messages: [record(3), record(5), record(8)], fetchedAt: 2 });
    await elapse(READ_DWELL_MS);
    expect(readUserMessages.mock.calls.map((call) => call[1])).toEqual([{ up_to_id: 5 }, { up_to_id: 8 }]);
    const [first, second] = readUserMessages.mock.calls.map((call) => call[2]);
    expect(first).not.toBe(second);
  });
});
