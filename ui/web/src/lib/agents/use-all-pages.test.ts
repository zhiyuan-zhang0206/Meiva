// useAllPages hook tests — the fleet-wide open-pages cache (every agent's live
// page in one fetch), invalidated by page events. Mirrors use-agent-pages
// without the per-agent filter.

// meiva:note
// @vitest-environment happy-dom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { PageRow, SystemEvent } from "../contracts/types";
import { startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig } from "../transport/tests/api/api-provider";
import { eventStreamHarness, type EventStreamHarness } from "../transport/tests/stream/event-stream-harness";
import { useAllPages } from "./use-all-pages";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

let rows: PageRow[];
let held: Deferred<PageRow[]>[];
let gateway: TestGateway;

function hold(): Deferred<PageRow[]> {
  const response = deferred<PageRow[]>();
  held.push(response);
  return response;
}

let _pid = 0;
function pageRow(overrides: Partial<PageRow> & { name: string; agent_id: number }): PageRow {
  return {
    id: ++_pid,
    port: 9000 + _pid,
    title: overrides.name,
    serve_dir: null,
    url: `http://host/${overrides.name}`,
    created_at: "2026-01-01T00:00:00Z",
    closed_at: null,
    ...overrides,
  };
}

function pageOpened(over: {
  name: string;
  agent_id: number;
  port?: number;
  url?: string;
}): SystemEvent {
  return {
    role: "page_opened",
    agent_id: over.agent_id,
    page_id: ++_pid,
    name: over.name,
    port: over.port ?? 9100,
    title: over.name,
    url: over.url ?? `http://host/${over.name}`,
  };
}

let queryClient: QueryClient;
let stream: EventStreamHarness;

beforeEach(async () => {
  _pid = 0;
  rows = [];
  held = [];
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  gateway = await startGateway((request) => {
    if (request.path !== "/api/pages") return { kind: "text", status: 404, body: "unexpected route" };
    const response = held.shift();
    return response === undefined
      ? { kind: "json", body: rows }
      : response.promise.then((body): GatewayReply => ({ kind: "json", body }));
  });
  stream = eventStreamHarness({ api: gatewayRig(gateway).api, queryClient, wallNow: () => Date.now() });
});
afterEach(async () => {
  cleanup();
  await gateway.close();
});

async function waitForFetch(): Promise<void> {
  await waitFor(() => expect(queryClient.getQueryData(["all-pages"])).toBeDefined());
}

describe("useAllPages", () => {
  it("fetches every agent's open pages on mount", async () => {
    rows = [pageRow({ name: "a", agent_id: 1 }), pageRow({ name: "b", agent_id: 2 })];
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitFor(() =>
      expect(result.current.map((p) => `${p.agent_id}:${p.name}`)).toEqual(["1:a", "2:b"]),
    );
    expect(gateway.requests.length).toBe(1);
  });

  it("page_opened for any agent refetches the authoritative list", async () => {
    const first = pageRow({ name: "a", agent_id: 1 });
    rows = [first];
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await stream.whenSourceExists();

    rows = [first, pageRow({ name: "b", agent_id: 2 })];
    stream.deliver(pageOpened({ name: "b", agent_id: 2 }));
    await waitFor(() =>
      expect(result.current.map((p) => `${p.agent_id}:${p.name}`)).toEqual(["1:a", "2:b"]),
    );
    expect(gateway.requests.length).toBe(2);
    expect(queryClient.getQueryData(["agent-pages", 2])).toBeUndefined();
  });

  it("coalesces a burst of page events into one list GET", async () => {
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitForFetch();
    await stream.whenSourceExists();

    rows = [
      pageRow({ name: "first", agent_id: 1 }),
      pageRow({ name: "second", agent_id: 2 }),
    ];
    stream.deliver(pageOpened({ name: "first", agent_id: 1 }));
    stream.deliver(pageOpened({ name: "second", agent_id: 2 }));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["first", "second"]));
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(gateway.requests.length).toBe(2);
  });

  it("same agent and name reads the server's replacement row", async () => {
    rows = [pageRow({ name: "dash", agent_id: 1, port: 9000 })];
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await stream.whenSourceExists();

    rows = [pageRow({ name: "dash", agent_id: 1, port: 9999, url: "http://host/dash-new" })];
    stream.deliver(pageOpened({ name: "dash", agent_id: 1, port: 9999, url: "http://host/dash-new" }));
    await waitFor(() => expect(result.current[0].port).toBe(9999));
    expect(result.current).toHaveLength(1);
    expect(result.current[0].url).toBe("http://host/dash-new");
    expect(gateway.requests.length).toBe(2);
  });

  it("same name under a different agent remains a distinct server row", async () => {
    const first = pageRow({ name: "dash", agent_id: 1 });
    rows = [first];
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await stream.whenSourceExists();

    rows = [first, pageRow({ name: "dash", agent_id: 2 })];
    stream.deliver(pageOpened({ name: "dash", agent_id: 2 }));
    await waitFor(() => expect(result.current).toHaveLength(2));
    expect(result.current.map((p) => p.agent_id).sort()).toEqual([1, 2]);
  });

  it("page_closed refetches without the matching server row", async () => {
    const second = pageRow({ name: "dash", agent_id: 2 });
    rows = [pageRow({ name: "dash", agent_id: 1 }), second];
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current).toHaveLength(2));
    await stream.whenSourceExists();

    rows = [second];
    stream.deliver({ role: "page_closed", agent_id: 1, name: "dash" });
    await waitFor(() => expect(result.current.map((p) => p.agent_id)).toEqual([2]));
    expect(gateway.requests.length).toBe(2);
  });

  it("page_opened before the fetch lands does not seed partial event data", async () => {
    const initial = hold();
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await stream.whenSourceExists();
    await waitFor(() => expect(gateway.requests).toHaveLength(1));

    stream.deliver(pageOpened({ name: "early", agent_id: 1 }));
    expect(queryClient.getQueryData(["all-pages"])).toBeUndefined();
    expect(result.current).toEqual([]);

    rows = [pageRow({ name: "from-fetch", agent_id: 1 }), pageRow({ name: "early", agent_id: 1 })];
    act(() => initial.resolve([pageRow({ name: "from-fetch", agent_id: 1 })]));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["from-fetch", "early"]));
    expect(gateway.requests.length).toBe(2);
  });

  it("repairs an old initial GET snapshot after a page opens mid-flight", async () => {
    const initial = hold();
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitFor(() => expect(gateway.requests.length).toBe(1));
    await stream.whenSourceExists();

    stream.deliver(pageOpened({ name: "new", agent_id: 2 }));
    expect(queryClient.getQueryData(["all-pages"])).toBeUndefined();
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(gateway.requests.length).toBe(1);
    expect(queryClient.isFetching({ queryKey: ["all-pages"] })).toBe(1);

    rows = [pageRow({ name: "new", agent_id: 2 })];
    act(() => initial.resolve([]));

    await waitFor(() => expect(gateway.requests.length).toBe(2), { timeout: 2_000 });
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["new"]));
  });

  it("reconnect (open) refetches to reconcile events missed during the gap", async () => {
    const { result } = renderHook(() => useAllPages(), { wrapper: stream.wrapper });
    await waitForFetch();
    await stream.whenSourceExists();
    expect(gateway.requests.length).toBe(1);

    rows = [pageRow({ name: "missed", agent_id: 3 })];
    stream.open();
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["missed"]));
    expect(gateway.requests.length).toBe(2);
  });
});
