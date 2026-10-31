import type {
  ContextBreakdownResponse,
  RunTimelineMessages,
  RunTimelineResponse,
} from "@/lib/contracts/types";

export const LIFETIME = { from: "2026-10-04T12:00:00.000000Z", to: "2026-10-04T16:00:00.000000Z" };
export const LEAF_A = { from: "2026-10-04T12:00:00.123456Z", to: "2026-10-04T13:00:00.654321Z" };

export const usage = { calls: 3, input: 3000, cache_read: 2400, output: 120, cache_write: 0, cost_usd: 0.0123, cost_calls: 3 };

export const lifetimeResponse: RunTimelineResponse = {
  agent_id: 42,
  units_omitted: 0,
  window: LIFETIME,
  lifetime: LIFETIME,
  nodes: [
    {
      id: "1",
      level: 1,
      parent: "3",
      start: LEAF_A.from,
      end: LEAF_A.to,
      span_start: 1,
      span_end: 5,
      summary: "The agent read the repo\nand planned the change.",
      usage,
      generation: { calls: 2, input: 9000, cache_read: 8800, output: 400, seconds: 31.5 },
      context_tokens: 4200,
      estimated: true,
    },
    {
      id: "2",
      level: 1,
      parent: "3",
      start: "2026-10-04T13:00:01.000000Z",
      end: "2026-10-04T16:00:00.000000Z",
      span_start: 6,
      span_end: 9,
      summary: "It implemented and tested it.",
      usage: { calls: 1, input: 100, cache_read: 0, output: 10, cache_write: 0, cost_usd: 0, cost_calls: 0 },
      generation: null,
      context_tokens: null,
      estimated: null,
    },
    {
      id: "3",
      level: 2,
      parent: null,
      start: LEAF_A.from,
      end: "2026-10-04T16:00:00.000000Z",
      span_start: 1,
      span_end: 9,
      summary: "A whole task, start to finish.",
      usage: { calls: 4, input: 3100, cache_read: 2400, output: 130, cache_write: 0, cost_usd: 0, cost_calls: 0 },
      generation: null,
      context_tokens: null,
      estimated: null,
    },
  ],
  units: [
    {
      kind: "inbound",
      i0: 1,
      i1: 1,
      start: "2026-10-04T12:00:00.123456Z",
      end: "2026-10-04T12:00:00.123456Z",
      source: "user",
      inbound_id: null,
      preview: "please fix the bug",
      parent: "1",
      context_tokens: 100,
      generation_tokens: null,
      estimated: true,
      session: 0,
      context_total: 800,
      request: null,
    },
    {
      kind: "output",
      i0: 2,
      i1: 3,
      start: "2026-10-04T12:05:00.000000Z",
      end: "2026-10-04T12:06:00.000000Z",
      source: null,
      inbound_id: null,
      preview: "look at the failing test",
      parent: "1",
      context_tokens: 300,
      generation_tokens: null,
      estimated: false,
      session: 0,
      context_total: 1350,
      request: null,
    },

    {
      kind: "text",
      i0: 2,
      i1: 2,
      start: "2026-10-04T12:05:00.000000Z",
      end: "2026-10-04T12:05:00.000000Z",
      source: null,
      inbound_id: null,
      preview: "on it",
      parent: "1",
      context_tokens: 50,
      generation_tokens: null,
      estimated: false,
      session: 0,
      context_total: 1050,
      request: { calls: 1, input: 1000, cache_read: 400, output: 50, cache_write: 0, cost_usd: 0.0021, cost_calls: 1 },
    },
    {
      kind: "thinking",
      i0: 7,
      i1: 7,
      start: "2026-10-04T14:00:00.000000Z",
      end: "2026-10-04T14:00:00.000000Z",
      source: null,
      inbound_id: null,
      preview: "second session",
      parent: "2",
      context_tokens: 20,
      generation_tokens: null,
      estimated: true,
      session: 1,
      context_total: 420,
      request: { calls: 1, input: 400, cache_read: 0, output: 20, cache_write: 0, cost_usd: 0, cost_calls: 0 },
    },
  ],
};

export const messagesResponse: RunTimelineMessages = {
  messages: [
    {
      idx: 2,
      ts: "2026-10-04T12:05:00.000000Z",
      source: null,
      parts: [
        { kind: "think", chars: 11, text: "need a plan", text_truncated: false },
        { kind: "text", chars: 5, text: "on it", text_truncated: false },
        { kind: "call", chars: 2, text: "ls", text_truncated: false },
      ],
      context_tokens: 1234,
      estimated: true,
    },
  ],
  next_start: null,
};

export const cbdFixture: ContextBreakdownResponse = {
  total_input_tokens: 1000,
  estimated: false,
  exact_fraction: 1,
  max_input_tokens: 1_000_000,
  soft_compact_tokens: 374_000,
  hard_compact_tokens: 512_000,
  sections: [{ name: "(preamble)", tokens: 100, estimated: true }],
  categories: [{ kind: "system_prompt", tokens: 400, estimated: false, exact_fraction: 1 }],
};

// meiva:note
// jsdom has no layout: every element measures 0x0, and the timeline divides the cursor's offset
// by the width of the row that carries `data-track`, so a wheel or a drag has nothing to divide
// by. Give the geometry to the page this test rendered — 1000px wide for the rows, starting
// 100px from the left edge — rather than to `Element.prototype`, which would hand the same
// numbers to every element in the process for as long as the file is loaded.
export function measureTheChart(chart: Element): void {
  const rect = (width: number) => ({
    left: 100,
    top: 0,
    width,
    height: 0,
    right: 100 + width,
    bottom: 0,
    x: 100,
    y: 0,
    toJSON: () => ({}),
  });
  const give = (element: Element, width: number): void => {
    Object.defineProperty(element, "getBoundingClientRect", { configurable: true, value: () => rect(width) });
  };
  give(chart, 0);
  for (const element of chart.querySelectorAll("*")) {
    give(element, element.hasAttribute("data-track") ? 1000 : 0);
  }
}
