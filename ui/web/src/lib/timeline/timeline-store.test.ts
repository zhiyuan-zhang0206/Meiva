// timeline-store.ts direct unit tests — the SSE-driven streaming timeline
// store, split out of store.ts (which now holds only UI + cluster state).
//
// Complements the use-timeline.test.ts integration tests:
//   - use-timeline: renderHook actually runs React state, tests hook wiring
//   - here:          direct act on `useTimelineStore.getState()` actions,
//                    locking down pure-state-machine behavior — SSE role × flag
//                    transitions, connection-event writes, switchThread
//                    defaults, selected-event routing, etc.

import { act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useTimelineStore } from "./timeline-store";
import type { BackendTimelineItem, SystemEvent } from "../contracts/types";

// -- helpers ───────────────────────────────────────────────────────────────

let _id_counter = 0;
function item(overrides: Partial<BackendTimelineItem>): BackendTimelineItem {
  return {
    kind: "agent_chat",
    source: null,
    payload: "",
    created_at: "2026-01-01T00:00:00Z",
    item_id: `s.${++_id_counter}`,
    inbound_id: null,
    show_timestamp: true,
    ...overrides,
  };
}

/** Reset timeline state to prevent cross-test contamination. */
function resetStore(): void {
  useTimelineStore.setState({
    activeThreadId: 42, // 42 so isEventForThread does not block agent_id=42 SSE events
    items: [],
    compactBuffer: null,
    compactEpoch: 0,
    streamingIds: new Set(),
    streamingCode: false,
    turnActive: false,
    connectionState: "open",
    tokenUsage: 0,
    reasoningTokens: 0,
    maxContextTokens: 0,
    hasMoreOlder: false,
    loadingOlder: false,
    olderFetchCount: 0,
    scrollToBottomRequest: 0,
    liveCompact: null,
    compactReplaceSeq: 0,
    compactReplaceAgent: null,
  });
}

beforeEach(() => {
  resetStore();
});

describe("compact transition buffer", () => {
  const oldReply = () => item({ item_id: "90.0", payload: "old reply" });
  const current = () => item({ item_id: "1.0", payload: "new reply" });
  const historical = () => item({ item_id: "s1.boundary.89.0", payload: "old reply" });

  function startCompact(oldItems: BackendTimelineItem[] = [oldReply()]): void {
    useTimelineStore.getState().switchThread(42, oldItems, true);
    useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 42 });
    useTimelineStore.getState().processSseEvent({
      role: "timeline_snapshot", agent_id: 42, msg_count: 2, items: [current()],
    });
  }

  it("keeps old rows through the SSE swap and releases with the covering page in one update", () => {
    startCompact();
    expect(useTimelineStore.getState().items.map((row) => row.item_id)).toEqual(["1.0"]);
    expect(useTimelineStore.getState().compactBuffer?.rows.map((row) => row.item.item_id)).toEqual(["90.0"]);
    useTimelineStore.getState().reloadSnapshot([current()], 2, true);
    const transitions: { ids: string[]; buffered: boolean }[] = [];
    const unsubscribe = useTimelineStore.subscribe((s) => transitions.push({
      ids: s.items.map((row) => row.item_id), buffered: s.compactBuffer !== null,
    }));
    useTimelineStore.getState().prependOlder([historical()], false);
    unsubscribe();
    expect(transitions).toEqual([{ ids: ["s1.boundary.89.0", "1.0"], buffered: false }]);
  });

  it("excludes partial and re-attached head context from coverage", () => {
    startCompact([
      item({ item_id: "0.0", kind: "system_prompt" }),
      item({ item_id: "1.0", kind: "system_marker", source: "memory" }),
      oldReply(),
      item({ item_id: "91.0", partial: true }),
    ]);
    expect(useTimelineStore.getState().compactBuffer?.oldestRealByRank.get(1)).toBe("90.0");
    useTimelineStore.getState().reloadSnapshot([current()], 2, true);
    useTimelineStore.getState().prependOlder([item({ item_id: "s1.boundary.90.0" })], true);
    expect(useTimelineStore.getState().compactBuffer).not.toBeNull();
    useTimelineStore.getState().prependOlder([historical()], true);
    expect(useTimelineStore.getState().compactBuffer).toBeNull();
  });

  it("buffers the crossed-compact REST swap", () => {
    useTimelineStore.getState().switchThread(42, [oldReply()], true);
    useTimelineStore.getState().reloadSnapshot([
      item({ item_id: "0.0", kind: "system_prompt" }),
      item({ item_id: "1.0", kind: "inbound_compact_request" }),
      current(),
    ], 3, true);
    expect(useTimelineStore.getState().compactBuffer?.rows[0].item.payload).toBe("old reply");
  });

  it("releases uncovered rows when the existing finite page budget settles", () => {
    startCompact([item({ item_id: "80.0" }), oldReply()]);
    const epoch = useTimelineStore.getState().compactEpoch;
    useTimelineStore.getState().reloadSnapshot([current()], 2, true);
    useTimelineStore.getState().prependOlder([historical()], true);
    expect(useTimelineStore.getState().compactBuffer).not.toBeNull();
    useTimelineStore.getState().finishCompactTransition(42, epoch);
    expect(useTimelineStore.getState().compactBuffer).toBeNull();
    expect(useTimelineStore.getState().items.map((row) => row.item_id)).toEqual([
      "s1.boundary.89.0", "1.0",
    ]);
  });

  it("invalidates stale owners on hide, reconnect, newer compact, and switch", () => {
    startCompact();
    const first = useTimelineStore.getState().compactEpoch;
    useTimelineStore.getState().invalidateCompactEpoch(); // visibility hide
    expect(useTimelineStore.getState().compactEpoch).toBe(first + 1);
    expect(useTimelineStore.getState().compactBuffer?.epoch).toBe(first + 1);
    useTimelineStore.getState().processConnectionEvent({ type: "open" });
    expect(useTimelineStore.getState().compactEpoch).toBe(first + 2);
    useTimelineStore.getState().finishCompactTransition(42, first);
    expect(useTimelineStore.getState().compactBuffer).not.toBeNull();
    useTimelineStore.getState().processSseEvent({ role: "compact_done", agent_id: 42 });
    useTimelineStore.getState().processSseEvent({
      role: "timeline_snapshot", agent_id: 42, msg_count: 2, items: [current()],
    });
    expect(useTimelineStore.getState().compactEpoch).toBe(first + 3);
    expect(useTimelineStore.getState().compactBuffer?.rows.map((row) => row.rank)).toEqual([2, 1]);
    useTimelineStore.getState().switchThread(7, [], false);
    expect(useTimelineStore.getState().compactBuffer).toBeNull();
    expect(useTimelineStore.getState().compactEpoch).toBe(first + 4);
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// -- 1. SSE role dispatch (table-driven) ───────────────────────────────────
//
// Exhaustive table for the processSseEvent 5-way OR chain — each role × each flag.
// This kills the ~30-mutant cluster (turnStart / turnEnd / codeStart / codeEnd /
// token_usage ConditionalExpression + StringLiteral + EqualityOperator).

// Each role expresses its effect on each flag as "set true" / "set false" / "keep" (None).
// This semantics lets us precisely test "flag is actually flipped" regardless of prior state.
type FlagOp = "set-true" | "set-false" | "keep";

interface RoleCase {
  readonly name: string;
  readonly event: SystemEvent;
  readonly turnActive: FlagOp;
  readonly streamingCode: FlagOp;
  /** 'set' = write event.input_tokens; 'keep' = leave unchanged */
  readonly tokenUsage: "set" | "keep";
}

const ROLE_CASES: readonly RoleCase[] = [
  // turnStart chain: inbound_arrived / chat_start / code_start / reasoning_start / exec_start
  {
    name: "inbound_arrived → turnActive set true; streaming keep",
    event: {
      role: "inbound_arrived",
      agent_id: 42,
      inbound_id: 1,
      kind: "chat",
      source: "user",
      content: "hi",
    },
    turnActive: "set-true",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "chat_start → turnActive set true; streaming keep",
    event: { role: "chat_start", agent_id: 42, item_id: "1.0" },
    turnActive: "set-true",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "code_start → turnActive set true + streamingCode set true",
    event: { role: "code_start", agent_id: 42, item_id: "1.0" },
    turnActive: "set-true",
    streamingCode: "set-true",
    tokenUsage: "keep",
  },
  {
    name: "reasoning_start → turnActive set true; streaming keep",
    event: { role: "reasoning_start", agent_id: 42, item_id: "1.0" },
    turnActive: "set-true",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "exec_start → turnActive set true + streaming set false (codeEnd)",
    event: { role: "exec_start", agent_id: 42, item_id: "5.0" },
    turnActive: "set-true",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  // turnEnd chain: llm_done / cancelled / error
  {
    name: "llm_done → turnActive set false; streaming keep",
    event: { role: "llm_done", agent_id: 42 },
    turnActive: "set-false",
    streamingCode: "keep",
    tokenUsage: "keep",
  },
  {
    name: "cancelled → turnActive set false + streaming set false (in codeEnd chain)",
    event: { role: "cancelled", agent_id: 42 },
    turnActive: "set-false",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  {
    name: "error → turnActive set false + streaming set false (in codeEnd chain)",
    event: { role: "error", agent_id: 42, content: "boom" },
    turnActive: "set-false",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  {
    name: "exec_output → turnActive set false + streaming set false",
    event: { role: "exec_output", agent_id: 42, item_id: "2.0", content: "out" },
    turnActive: "set-false",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  {
    name: "compact_done → streaming set false; turnActive keep",
    event: { role: "compact_done", agent_id: 42 },
    turnActive: "keep",
    streamingCode: "set-false",
    tokenUsage: "keep",
  },
  // token_usage: writes tokenUsage; others keep
  {
    name: "token_usage → writes tokenUsage; turnActive/streaming keep",
    event: { role: "token_usage", agent_id: 42, input_tokens: 1234, output_tokens: 56 },
    turnActive: "keep",
    streamingCode: "keep",
    tokenUsage: "set",
  },
];

/**
 * Run one dispatch + assertion for a single flag.
 * Initial state set to the inverse of the "expected result" so mutations breaking the OR chain
 * (cond → false / role string changed to "" etc.) → the ternary takes the keep branch and preserves the original value
 * → contradicts the expectation → the test fails. This is how table-driven kills every role × flag mutation.
 *
 */
function runFlagCase(
  event: SystemEvent,
  flag: "turnActive" | "streamingCode",
  op: FlagOp,
): void {
  // Initial: inverse of the set-true expected value
  const initial = op === "set-true" ? false : true;
  const expected = op === "set-true" ? true : op === "set-false" ? false : initial;

  act(() => {
    useTimelineStore.setState({ [flag]: initial });
    useTimelineStore.getState().processSseEvent(event);
  });
  expect(useTimelineStore.getState()[flag]).toBe(expected);
}

describe("processSseEvent role dispatch (table-driven)", () => {
  // Each case runs two independent sub-tests (one for turnActive, one for streamingCode);
  // each starts in the inverse state of expected so keep mutations and cond mutations are both distinguishable.
  it.each(ROLE_CASES)("$name [turnActive]", ({ event, turnActive }) => {
    runFlagCase(event, "turnActive", turnActive);
  });

  it.each(ROLE_CASES)("$name [streamingCode]", ({ event, streamingCode }) => {
    runFlagCase(event, "streamingCode", streamingCode);
  });

  it.each(ROLE_CASES)("$name [tokenUsage]", ({ event, tokenUsage }) => {
    // tokenUsage: starts at 999, set op → event.input_tokens (1234), keep → 999
    act(() => {
      useTimelineStore.setState({ tokenUsage: 999 });
      useTimelineStore.getState().processSseEvent(event);
    });
    if (tokenUsage === "set") {
      // Only token_usage events take this path; expect input_tokens to be written
      expect(useTimelineStore.getState().tokenUsage).toBe(1234);
    } else {
      expect(useTimelineStore.getState().tokenUsage).toBe(999);
    }
  });

  // Separate test of the turnStart-then-turnEnd chain — ensures that the turnActive set
  // earlier is flipped back by the subsequent turnEnd (verifies "ternary order: turnStart? : turnEnd? : keep")
  it("turnStart sets true, subsequent turnEnd flips to false", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().turnActive).toBe(true);

    act(() => {
      useTimelineStore.getState().processSseEvent({ role: "llm_done", agent_id: 42 });
    });
    expect(useTimelineStore.getState().turnActive).toBe(false);
  });

  // Roles not in the turnStart/turnEnd map must not modify turnActive (keep s.turnActive)
  it("unrelated role (chat_delta) does not modify turnActive", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().turnActive).toBe(true);

    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_delta",
        agent_id: 42,
        item_id: "1.0",
        content: "hi",
      });
    });
    // chat_delta is not in the turnEnd map → turnActive stays true
    expect(useTimelineStore.getState().turnActive).toBe(true);
  });

  // After codeStart sets true, roles not in the codeEnd map must not clear streamingCode
  it("unrelated role (code_delta) does not modify streamingCode", () => {
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "code_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().streamingCode).toBe(true);

    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "code_delta",
        agent_id: 42,
        item_id: "1.0",
        content: "x = 1",
      });
    });
    expect(useTimelineStore.getState().streamingCode).toBe(true);
  });

  // token_usage with reasoning_tokens > 0 sets reasoningTokens
  it("token_usage with reasoning_tokens writes reasoningTokens", () => {
    act(() => {
      useTimelineStore.setState({ reasoningTokens: 0 });
      useTimelineStore.getState().processSseEvent({
        role: "token_usage",
        agent_id: 42,
        input_tokens: 1234,
        output_tokens: 56,
        reasoning_tokens: 300,
      });
    });
    expect(useTimelineStore.getState().reasoningTokens).toBe(300);
  });

  // token_usage without reasoning_tokens defaults to 0
  it("token_usage without reasoning_tokens keeps reasoningTokens at 0", () => {
    act(() => {
      useTimelineStore.setState({ reasoningTokens: 99 });
      useTimelineStore.getState().processSseEvent({
        role: "token_usage",
        agent_id: 42,
        input_tokens: 1234,
        output_tokens: 56,
      });
    });
    expect(useTimelineStore.getState().reasoningTokens).toBe(0);
  });

  // non-token_usage role must not modify tokenUsage (keep s.tokenUsage)
  it("non-token_usage role does not touch tokenUsage", () => {
    act(() => {
      useTimelineStore.setState({ tokenUsage: 999 });
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 42,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().tokenUsage).toBe(999);
  });

  // agent_id guard: cross-thread SSE must not contaminate state
  it("cross-thread SSE blocked by the isEventForThread guard", () => {
    // activeThreadId=42, an agent_id=99 event arrives
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "chat_start",
        agent_id: 99,
        item_id: "1.0",
      });
    });
    expect(useTimelineStore.getState().turnActive).toBe(false); // not set
  });

  // timeline_snapshot uses a dedicated branch (mergeSnapshotWithStreaming); does not touch turnActive/streamingCode/tokenUsage
  it("timeline_snapshot replaces items, does not touch lifecycle flags", () => {
    act(() => {
      useTimelineStore.setState({ tokenUsage: 500, turnActive: true, streamingCode: true });
    });
    const snapshot = [item({ item_id: "1.0", kind: "agent_chat", payload: "hello" })];
    act(() => {
      useTimelineStore.getState().processSseEvent({
        role: "timeline_snapshot",
        agent_id: 42,
      msg_count: 0,
        items: snapshot,
      });
    });
    const s = useTimelineStore.getState();
    expect(s.items).toHaveLength(1);
    expect(s.items[0].payload).toBe("hello");
    // lifecycle flags untouched (snapshot branch returns early)
    expect(s.turnActive).toBe(true);
    expect(s.streamingCode).toBe(true);
    expect(s.tokenUsage).toBe(500);
  });
});

// -- 2. processConnectionEvent ──────────────────────────────────────────────


describe("create() initial defaults (fresh module)", () => {
  // Verify the initial state set in create() — these mutations
  // are masked by setState/switchThread after beforeEach; the module must be **freshly imported**
  // to observe (vi.resetModules + dynamic import = fresh singleton).
  it("freshly imported timeline store: all initial defaults match contract", async () => {
    vi.resetModules();
    const mod = await import("./timeline-store");
    const s = mod.useTimelineStore.getState();
    expect(s.items).toEqual([]);
    expect(s.turnActive).toBe(false);
    expect(s.streamingCode).toBe(false);
    expect(s.connectionState).toBe("open");
    expect(s.tokenUsage).toBe(0);
    expect(s.reasoningTokens).toBe(0);
    expect(s.maxContextTokens).toBe(0);
    expect(s.activeThreadId).toBeNull();
    expect(s.scrollToBottomRequest).toBe(0);
    expect(s.hasMoreOlder).toBe(false);
    expect(s.loadingOlder).toBe(false);
    expect(s.olderFetchCount).toBe(0);
  });
});
