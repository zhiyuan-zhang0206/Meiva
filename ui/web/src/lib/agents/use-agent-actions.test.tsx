// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { AGENT_DETAIL_QUERY_KEY, AGENTS_QUERY_KEY } from "../fold/agents";
import { useStore } from "../state/store";
import { ApiError, type Api } from "../transport/api";
import {
  cacheOnlyApi,
  httpRig,
  keysWithoutRandomUUID,
  noUpload,
} from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import { useAgentActions } from "./use-agent-actions";

const MESSAGES = {
  agentRow: {
    launchFailedCreated: "Agent #{id} was created, but launch failed. Select it to retry.",
  },
};

function actionWrapper(testApi: Api, client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <NextIntlClientProvider locale="en" messages={MESSAGES}>
        <ApiProvider api={testApi}>
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        </ApiProvider>
      </NextIntlClientProvider>
    );
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  useStore.getState().setActiveId(null);
});

it("selects the committed agent after launch failure instead of retrying create", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(AGENTS_QUERY_KEY, { agents: [], ancestors: [] });
  client.setQueryData([...AGENT_DETAIL_QUERY_KEY, 123], {});
  const spawn = vi.fn<Api["spawnAgent"]>().mockRejectedValue(
    new ApiError(502, "launch failed", {
      reason: "agent_launch_failed",
      agent_id: 123,
      state: { status: "idling", availability: { reason: "launch_unreachable" } },
      retry_launch_path: "/api/keyed/v1/agents/123/retry-launch",
    }),
  );
  const testApi: Api = { ...cacheOnlyApi(), spawnAgent: spawn };
  const showError = vi.fn();
  const wrapper = actionWrapper(testApi, client);
  const { result } = renderHook(() => useAgentActions(showError, []), { wrapper });
  await act(async () => {
    expect(await result.current.spawn()).toBeNull();
  });
  expect(spawn).toHaveBeenCalledTimes(1);
  expect(useStore.getState().activeId).toBe(123);
  expect(showError).toHaveBeenCalledWith(expect.stringContaining("Agent #123 was created"));
  expect(client.getQueryState(AGENTS_QUERY_KEY)?.isInvalidated).toBe(true);
  expect(client.getQueryState([...AGENT_DETAIL_QUERY_KEY, 123])?.isInvalidated).toBe(true);
});

it("sends the chosen harness and leaves the built-in default implicit", async () => {
  const spawn = vi.fn<Api["spawnAgent"]>().mockResolvedValue({ id: 7 });
  const testApi: Api = { ...cacheOnlyApi(), spawnAgent: spawn };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = actionWrapper(testApi, client);
  const { result } = renderHook(() => useAgentActions(vi.fn(), []), { wrapper });

  await act(async () => {
    await result.current.spawn(undefined, undefined, undefined, "codex");
  });
  expect(spawn.mock.calls[0][0]).toMatchObject({ harness: "codex" });

  await act(async () => {
    await result.current.spawn(undefined, undefined, undefined, undefined);
  });
  expect(spawn.mock.calls[1][0]).not.toHaveProperty("harness");
  client.clear();
});

it("holds one creation key across mutation retries and gives concurrent actions distinct keys", async () => {
  const fallbackKeys = httpRig({
    base: "",
    fetch: () => Promise.reject(new Error("unused")),
    now: () => 0,
    keys: keysWithoutRandomUUID(),
    openUpload: noUpload,
  }).api.newOperationKey;
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: 1, retryDelay: 0 }, queries: { retry: false } },
  });
  const spawn = vi
    .fn<Api["spawnAgent"]>()
    .mockRejectedValueOnce(new Error("before send"))
    .mockResolvedValue({ id: 42 });
  const testApi: Api = {
    ...cacheOnlyApi(),
    newOperationKey: fallbackKeys,
    spawnAgent: spawn,
  };
  const wrapper = actionWrapper(testApi, client);
  const { result } = renderHook(() => useAgentActions(vi.fn(), []), { wrapper });
  await act(async () => {
    await result.current.spawn();
  });
  expect(spawn.mock.calls[0][1]).toBe(spawn.mock.calls[1][1]);
  await act(async () => {
    await Promise.all([result.current.spawn(), result.current.spawn()]);
  });
  expect(spawn.mock.calls[2][1]).not.toBe(spawn.mock.calls[3][1]);
  client.clear();
});

it("requests compaction of the agent it was asked for", async () => {
  const client = new QueryClient();
  const compact = vi.fn<Api["compact"]>().mockResolvedValue({ inbound_id: 3 });
  const testApi: Api = { ...cacheOnlyApi(), compact };
  const wrapper = actionWrapper(testApi, client);
  const { result } = renderHook(() => useAgentActions(vi.fn(), []), { wrapper });
  await act(async () => {
    await result.current.compact(8);
  });
  expect(compact).toHaveBeenCalledWith(8);
  client.clear();
});
