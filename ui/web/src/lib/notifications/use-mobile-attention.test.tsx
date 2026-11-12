import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it } from "vitest";
import { realNetwork, startGateway, gatewayRig, type TestGateway } from "../transport/tests/api/api-provider";
import { eventStreamHarness, type EventStreamHarness } from "../transport/tests/stream/event-stream-harness";
import { useMobileAttention } from "./use-mobile-attention";

let gateway: TestGateway;
let stream: EventStreamHarness;
let counts = { unread_agents: 0, open_questions: 0 };
let fail = false;
let calls = 0;
beforeEach(async () => {
  counts = { unread_agents: 0, open_questions: 0 }; fail = false; calls = 0;
  gateway = await startGateway(realNetwork.createServer, (request) => {
    if (request.path !== "/api/attention/counts") return { kind: "text", status: 404, body: "unexpected" };
    calls++;
    return fail ? { kind: "text", status: 503, body: "unavailable" } : { kind: "json", body: counts };
  });
  stream = eventStreamHarness({ api: gatewayRig(gateway).api, queryClient: new QueryClient({ defaultOptions: { queries: { retry: false } } }), wallNow: () => Date.now() });
});
afterEach(async () => { cleanup(); await gateway.close(); });

it("loads exact zero and sums unread agents plus question rows", async () => {
  const { result } = renderHook(() => useMobileAttention(true), { wrapper: stream.wrapper });
  expect(result.current.count).toBeNull();
  await waitFor(() => expect(result.current).toEqual({ count: 0, state: "ready" }));
  await stream.whenSourceExists();
  counts = { unread_agents: 2, open_questions: 1 };
  stream.deliver({ role: "question_asked", agent_id: 1, question_id: 1, title: "decision" });
  await waitFor(() => expect(result.current.count).toBe(3));
  counts = { unread_agents: 2, open_questions: 0 };
  stream.deliver({ role: "question_resolved", agent_id: 1, question_id: 1 });
  await waitFor(() => expect(result.current.count).toBe(2));
  counts = { unread_agents: 1, open_questions: 0 };
  stream.deliver({ role: "agent_updated", agent_id: 1 });
  await waitFor(() => expect(result.current.count).toBe(1));
  counts = { unread_agents: 0, open_questions: 4 };
  stream.open();
  await waitFor(() => expect(result.current.count).toBe(4));
});
it("does not fetch a total for the desktop", async () => {
  const { result } = renderHook(() => useMobileAttention(false), { wrapper: stream.wrapper });
  await stream.whenSourceExists();
  expect(calls).toBe(0);
  expect(result.current.count).toBeNull();
});
it("reports a failed snapshot instead of a known empty state", async () => {
  fail = true;
  const { result } = renderHook(() => useMobileAttention(true), { wrapper: stream.wrapper });
  await waitFor(() => expect(result.current.state).toBe("error"));
  expect(result.current.count).toBeNull();
});
