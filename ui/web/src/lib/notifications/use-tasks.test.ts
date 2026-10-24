// useTasks tests — the Task Graph data source, focused on its resilience
// contract: the board self-heals (keeps polling after an error) and never
// blanks a loaded board on a transient poll failure (stale-while-error).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import type { TaskFields, TaskListResponse, TaskRow, TaskSummaryRow } from "../contracts/types";
import { apiWrapper, fakeApi } from "../transport/tests/api/fake-api";
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

interface TaskRequest {
  window?: string;
  fields?: string;
}

// meiva:note
// The renderer for these tests: the hook runs against a scripted gateway whose
// task read is `read`, inside a real QueryClient with retries off so the success
// and error edges run end to end. `requests` keeps the projections the hook asked
// for. The gateway's `getTasks` is generic over the projection and its row type
// follows that parameter, so the scripted read is asserted to the row type once,
// here.
function renderTasks(read: () => Promise<TaskListResponse>, client: QueryClient = freshClient()) {
  const requests: TaskRequest[] = [];
  const ApiWrapper = apiWrapper(fakeApi({
    getTasks: <T extends TaskFields>(request?: { window?: string; fields?: T }) => {
      requests.push({ ...request });
      return read() as Promise<TaskListResponse<T extends "full" ? TaskRow : TaskSummaryRow>>;
    },
  }));
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(ApiWrapper, {
      children: createElement(QueryClientProvider, { client, children }),
    });
  return { ...renderHook(() => useTasks(), { wrapper }), requests };
}

function freshClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

afterEach(cleanup);

describe("useTasks", () => {
  it("while pending: empty list, loading=true, error=false", () => {
    const { result } = renderTasks(() => new Promise<TaskListResponse>(() => undefined));

    expect(result.current.tasks).toEqual([]);
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBe(false);
  });

  it("on success: returns the tasks, loading=false, error=false", async () => {
    const { result } = renderTasks(() => Promise.resolve(RESPONSE));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
    expect(result.current.error).toBe(false);
  });

  it("defaults to the full task projection", async () => {
    const { result, requests } = renderTasks(() => Promise.resolve(RESPONSE));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requests).toEqual([{ window: "all", fields: "full" }]);
  });

  it("cold error: empty list, loading=false, error=true", async () => {
    const { result } = renderTasks(() => Promise.reject(new Error("HTTP 500")));

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.loading).toBe(false);
    expect(result.current.tasks).toEqual([]);
  });

  it("stale-while-error: a failed poll after data keeps the last good tasks (error flagged, not blanked)", async () => {
    let gatewayDown = false;
    const client = freshClient();

    const { result } = renderTasks(
      () => (gatewayDown ? Promise.reject(new Error("gateway down")) : Promise.resolve(RESPONSE)),
      client,
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
    expect(result.current.error).toBe(false);

    gatewayDown = true;
    await act(async () => {
      await client.refetchQueries({ queryKey: ["tasks"] });
    });

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.tasks).toEqual(RESPONSE.tasks);
  });
});
