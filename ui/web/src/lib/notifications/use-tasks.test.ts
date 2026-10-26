// useTasks tests — the Task Graph data source, focused on its resilience
// contract: the board self-heals (keeps polling after an error) and never
// blanks a loaded board on a transient poll failure (stale-while-error).

// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { TaskListResponse, TaskRow } from "../contracts/types";
import { apiWrapper } from "../transport/tests/api/api-provider";
import { realNetwork, startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig } from "../transport/tests/api/api-provider";
import { useTasks } from "./use-tasks";

function task(id: number): TaskRow {
  return {
    id,
    parent_id: null,
    title: `Task ${id}`,
    description: "",
    results: null,
    status: "in_progress",
    priority: "P2",
    owner: null,
    created_by: "user",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    reminder_count: 0,
  };
}

const RESPONSE: TaskListResponse = { tasks: [task(1), task(2)] };

let gateway: TestGateway;
let answer: GatewayReply;

function renderTasks(client: QueryClient = freshClient()) {
  const ApiWrapper = apiWrapper(gatewayRig(gateway).api);
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(ApiWrapper, {
      children: createElement(QueryClientProvider, { client, children }),
    });
  return renderHook(() => useTasks(), { wrapper });
}

function freshClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(async () => {
  answer = { kind: "json", body: RESPONSE };
  gateway = await startGateway(realNetwork.createServer, (request) => request.path === "/api/tasks?window=all&fields=full"
    ? answer : { kind: "text", status: 404, body: "unexpected route" });
});

afterEach(async () => {
  cleanup();
  await gateway.close();
});

describe("useTasks", () => {
  it("while pending: empty list, loading=true, error=false", async () => {
    answer = { kind: "hang" };
    const { result } = renderTasks();
    await waitFor(() => expect(gateway.requests).toHaveLength(1));

    expect(result.current.tasks).toEqual([]);
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBe(false);
  });

  it("on success: returns the tasks, loading=false, error=false", async () => {
    const { result } = renderTasks();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
    expect(result.current.error).toBe(false);
  });

  it("defaults to the full task projection", async () => {
    const { result } = renderTasks();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(gateway.requests.map((request) => request.path)).toEqual(["/api/tasks?window=all&fields=full"]);
  });

  it("cold error: empty list, loading=false, error=true", async () => {
    answer = { kind: "text", status: 500, body: "unavailable" };
    const { result } = renderTasks();

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.loading).toBe(false);
    expect(result.current.tasks).toEqual([]);
  });

  it("stale-while-error: a failed poll after data keeps the last good tasks (error flagged, not blanked)", async () => {
    const client = freshClient();
    const { result } = renderTasks(client);

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
    expect(result.current.error).toBe(false);

    answer = { kind: "drop" };
    await act(async () => {
      await client.refetchQueries({ queryKey: ["tasks"] });
    });

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
  });
});
