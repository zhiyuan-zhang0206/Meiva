// useAgentPages hook tests — the inspector's open-pages list, SSE-driven over
// a TanStack Query cache. Covers the initial fetch, page event invalidation,
// authoritative rows, no partial seed, in-flight repair, per-agent scope, and
// reconnect reconciliation.

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { PageRow, SystemEvent } from "../contracts/types";
import { startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig } from "../transport/tests/api/api-provider";
import { eventStreamHarness, type EventStreamHarness } from "../transport/tests/stream/event-stream-harness";
import { useAgentPages } from "./use-agent-pages";

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
function pageRow(overrides: Partial<PageRow> & { name: string }): PageRow {
  return {
    id: ++_pid,
    agent_id: 1,
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
  agent_id?: number;
  port?: number;
  title?: string | null;
  url?: string;
}): SystemEvent {
  return {
    role: "page_opened",
    agent_id: over.agent_id ?? 1,
    page_id: ++_pid,
    name: over.name,
    port: over.port ?? 9100,
    title: over.title ?? over.name,
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
    const match = /^\/api\/agents\/(\d+)\/pages$/.exec(request.path);
    if (match === null) return { kind: "text", status: 404, body: "unexpected route" };
    const response = held.shift();
    if (response !== undefined) return response.promise.then((body): GatewayReply => ({ kind: "json", body }));
    return { kind: "json", body: rows.filter((row) => row.agent_id === Number(match[1])) };
  });
  stream = eventStreamHarness({ api: gatewayRig(gateway).api, queryClient, wallNow: () => Date.now() });
});
afterEach(async () => {
  cleanup();
  await gateway.close();
});

/** Wait until the initial fetch has landed in the cache (distinguishes a
 *  fetched empty list [] from the pre-fetch undefined — result.current is [] in
 *  both, so it can't gate the SSE push on its own). */
async function waitForFetch(): Promise<void> {
  await waitFor(() =>
    expect(queryClient.getQueryData(["agent-pages", 1])).toBeDefined(),
  );
}

describe("useAgentPages", () => {
  it("fetches the initial page list on mount", async () => {
    rows = [pageRow({ name: "panel-a" })];
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["panel-a"]));
    expect(gateway.requests.map((request) => Number(request.path.split("/")[3]))).toEqual([1]);
  });

  it("page_opened refetches the authoritative list", async () => {
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitForFetch();
    await stream.whenSourceExists();

    rows = [pageRow({ name: "panel-a" })];
    stream.deliver(pageOpened({ name: "panel-a" }));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["panel-a"]));
    expect(gateway.requests.length).toBe(2);
  });

  it("page_opened reads the server's replacement row, including fields absent from SSE", async () => {
    rows = [pageRow({ name: "panel-a", port: 9000 })];
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current).toHaveLength(1));
    await stream.whenSourceExists();

    rows = [pageRow({ name: "panel-a", port: 9999, url: "http://host/panel-a-new", serve_dir: "/srv/new" })];
    stream.deliver(pageOpened({ name: "panel-a", port: 9999, url: "http://host/panel-a-new" }));
    await waitFor(() => expect(result.current[0].port).toBe(9999));
    expect(result.current).toHaveLength(1);
    expect(result.current[0].url).toBe("http://host/panel-a-new");
    expect(result.current[0].serve_dir).toBe("/srv/new");
    expect(gateway.requests.length).toBe(2);
  });

  it("page_closed refetches the list without the closed page", async () => {
    const panelB = pageRow({ name: "panel-b" });
    rows = [pageRow({ name: "panel-a" }), panelB];
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitFor(() => expect(result.current).toHaveLength(2));
    await stream.whenSourceExists();

    rows = [panelB];
    stream.deliver({ role: "page_closed", agent_id: 1, name: "panel-a" });
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["panel-b"]));
    expect(gateway.requests.length).toBe(2);
  });

  it("page_opened before the initial fetch does not seed partial event data", async () => {
    const initial = hold();
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await stream.whenSourceExists();
    await waitFor(() => expect(gateway.requests).toHaveLength(1));

    stream.deliver(pageOpened({ name: "early" }));
    expect(queryClient.getQueryData(["agent-pages", 1])).toBeUndefined();
    expect(result.current).toEqual([]);

    rows = [pageRow({ name: "from-fetch" }), pageRow({ name: "early" })];
    act(() => initial.resolve([pageRow({ name: "from-fetch" })]));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["from-fetch", "early"]));
    expect(gateway.requests.length).toBe(2);
  });

  it("repairs an old initial GET snapshot after a page opens mid-flight", async () => {
    const initial = hold();
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitFor(() => expect(gateway.requests.length).toBe(1));
    await stream.whenSourceExists();

    stream.deliver(pageOpened({ name: "new" }));
    expect(queryClient.getQueryData(["agent-pages", 1])).toBeUndefined();
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(gateway.requests.length).toBe(1);
    expect(queryClient.isFetching({ queryKey: ["agent-pages", 1] })).toBe(1);

    rows = [pageRow({ name: "new" })];
    act(() => initial.resolve([]));

    await waitFor(() => expect(gateway.requests.length).toBe(2), { timeout: 2_000 });
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["new"]));
  });

  it("first page opening into a fetched empty list triggers a read", async () => {
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitForFetch();
    await stream.whenSourceExists();

    rows = [pageRow({ name: "first" })];
    stream.deliver(pageOpened({ name: "first" }));
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["first"]));
    expect(gateway.requests.length).toBe(2);
  });

  it("ignores events for other agents", async () => {
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitForFetch();
    await stream.whenSourceExists();

    rows = [pageRow({ name: "other", agent_id: 2 })];
    stream.deliver(pageOpened({ name: "other", agent_id: 2 }));
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(result.current).toEqual([]);
    expect(queryClient.getQueryData(["agent-pages", 1])).toEqual([]);
    expect(gateway.requests.length).toBe(1);
  });

  it("reconnect (open) refetches to reconcile events missed during the gap", async () => {
    const { result } = renderHook(() => useAgentPages(1), { wrapper: stream.wrapper });
    await waitForFetch();
    await stream.whenSourceExists();
    expect(gateway.requests.length).toBe(1);

    rows = [pageRow({ name: "missed" })];
    stream.open();
    await waitFor(() => expect(result.current.map((p) => p.name)).toEqual(["missed"]));
    expect(gateway.requests.length).toBe(2);
  });
});
