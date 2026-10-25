// Each selected context reads its latest token snapshot on activation.

// meiva:note
// @vitest-environment happy-dom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ReactNode } from "react";

import { apiWrapper } from "../transport/tests/api/api-provider";
import { startGateway, type GatewayReply, type TestGateway } from "../transport/tests/api/api-provider";
import { gatewayRig, type HttpRig } from "../transport/tests/api/api-provider";
import {
  AgentEventStreamContext,
  type EventStreamContextValue,
} from "../transport/useEventStream";
import { useTokenUsage } from "./use-token-usage";
import type { TokenUsageResponse } from "../contracts/types";

const noopAgentStream: EventStreamContextValue = {
  subscribe: () => () => undefined,
};

function tokenFixture(agentId: number): TokenUsageResponse {
  return {
    input_tokens: 1000 * agentId,
    output_tokens: 100,
    reasoning_tokens: 0,
    max_input_tokens: 200_000,
    soft_compact_tokens: 100_000,
    hard_compact_tokens: 150_000,
  };
}

let gateway: TestGateway;
let rig: HttpRig;
let held: Promise<GatewayReply> | null;

function wrapperFor(client: QueryClient) {
  const Gateway = apiWrapper(rig.api);
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <Gateway>
        <AgentEventStreamContext.Provider value={noopAgentStream}>
          {children}
        </AgentEventStreamContext.Provider>
      </Gateway>
    </QueryClientProvider>
  );
}

beforeEach(async () => {
  held = null;
  gateway = await startGateway((request) => {
    if (held !== null) return held;
    const match = /^\/api\/agents\/(\d+)\/token-usage$/.exec(request.path);
    if (match === null) return { kind: "text", status: 404, body: "unexpected route" };
    return { kind: "json", body: tokenFixture(Number(match[1])) };
  });
  rig = gatewayRig(gateway);
});

afterEach(async () => {
  cleanup();
  await gateway.close();
});

describe("useTokenUsage agent switch", () => {
  it("serves a returned-to agent from cache and fires no read of its own", async () => {
    // Cold keys fetch regardless of a global staleTime default.
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 5 * 60_000 } },
    });
    const wrapper = wrapperFor(qc);
    const { result, rerender } = renderHook(
      ({ id }: { id: number | null }) => useTokenUsage(id, () => undefined),
      { initialProps: { id: 1 }, wrapper },
    );

    // Cold first visit: the observer fetches on its own.
    await waitFor(() => expect(result.current.contextTokens).toBe(1000));
    expect(gateway.requests.map((request) => request.path)).toEqual(["/api/agents/1/token-usage"]);
    expect(rig.sent[0].init.signal).toBeInstanceOf(AbortSignal);

    // Switch to another agent (cold): fetches on its own.
    rerender({ id: 2 });
    await waitFor(() => expect(result.current.contextTokens).toBe(2000));
    expect(gateway.requests[1].path).toBe("/api/agents/2/token-usage");
    expect(rig.sent[1].init.signal).toBeInstanceOf(AbortSignal);

    // Returning inside the retention window seeds from cache and fires no
    // read of its own (task #3900 batch 2) — the re-attach reconcile
    // (agent-reconcile.ts) is the one refresh.
    rerender({ id: 1 });
    await waitFor(() => expect(result.current.contextTokens).toBe(1000));
    expect(gateway.requests).toHaveLength(2);
  });

  it("reports contextPending until the cold key's first snapshot lands", async () => {
    let finish!: (reply: GatewayReply) => void;
    held = new Promise((resolve) => { finish = resolve; });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = wrapperFor(qc);
    const initialProps: { id: number | null } = { id: 7 };
    const { result, rerender } = renderHook(
      ({ id }: { id: number | null }) => useTokenUsage(id, () => undefined),
      { initialProps, wrapper },
    );

    // Cold key: pending until the first snapshot lands.
    expect(result.current.contextPending).toBe(true);

    await waitFor(() => expect(gateway.requests).toHaveLength(1));
    finish({ kind: "json", body: tokenFixture(7) });
    await waitFor(() => expect(result.current.contextPending).toBe(false));
    expect(result.current.contextTokens).toBe(7000);

    // No active agent: nothing is loading, so never "pending".
    rerender({ id: null });
    expect(result.current.contextPending).toBe(false);
  });
});
