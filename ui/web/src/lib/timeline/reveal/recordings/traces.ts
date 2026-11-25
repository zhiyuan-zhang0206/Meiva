// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md
import real from "./real-arrivals.json";

export interface Arrival {
  tMs: number;
  text: string;
}

export const REAL_TRACE_NAMES = [
  "antigravity_text_only",
  "antigravity_tool_call",
  "claude_text_only",
  "claude_thinking_text_tool",
  "codex_commentary_command_final",
  "codex_reasoning_summary",
  "codex_text_only",
] as const satisfies readonly (keyof typeof real)[];

export type RealTraceName = (typeof REAL_TRACE_NAMES)[number];

export interface RealDelta {
  tMs: number;
  kind: "chat" | "reasoning";
  text: string;
}

export function realDeltas(name: RealTraceName): RealDelta[] {
  return real[name].map(([t, kind, text]) => {
    if (typeof t !== "number" || typeof text !== "string" || (kind !== "chat" && kind !== "reasoning")) {
      throw new Error(`real-arrivals.json: malformed row in ${name}`);
    }
    return { tMs: t * 1000, kind, text };
  });
}

export function realArrivals(name: RealTraceName): Arrival[] {
  return realDeltas(name).map(({ tMs, text }) => ({ tMs, text }));
}

export function coalesceArrivals(arrivals: Arrival[], windowMs: number): Arrival[] {
  const out: Arrival[] = [];
  let windowStart = -Infinity;
  for (const a of arrivals) {
    if (a.tMs - windowStart < windowMs && out.length > 0) {
      out[out.length - 1].text += a.text;
    } else {
      out.push({ tMs: a.tMs, text: a.text });
      windowStart = a.tMs;
    }
  }
  return out;
}

export type SyntheticProfile = "claude-burst" | "codex-packets" | "antigravity-sentences" | "steady-then-stall";

function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Shape {
  size: [number, number];
  gap: [number, number];
  stall?: { p: number; ms: [number, number] };
}

const SHAPES: Record<SyntheticProfile, Shape> = {
  "claude-burst": { size: [100, 140], gap: [1600, 2200] },
  "codex-packets": { size: [3, 30], gap: [100, 300], stall: { p: 0.03, ms: [1500, 2500] } },
  "antigravity-sentences": { size: [30, 60], gap: [150, 200] },
  "steady-then-stall": { size: [4, 8], gap: [60, 140], stall: { p: 0.02, ms: [2000, 2000] } },
};

export function synthesizeArrivals(profile: SyntheticProfile, text: string, seed = 1): Arrival[] {
  const shape = SHAPES[profile];
  const rnd = prng(seed);
  const between = ([lo, hi]: [number, number]) => lo + rnd() * (hi - lo);
  const out: Arrival[] = [];
  const chars = Array.from(new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text), (s) => s.segment);
  let t = 0;
  for (let i = 0; i < chars.length; ) {
    const size = Math.max(1, Math.round(between(shape.size)));
    out.push({ tMs: Math.round(t), text: chars.slice(i, i + size).join("") });
    i += size;
    t += shape.stall && rnd() < shape.stall.p ? between(shape.stall.ms) : between(shape.gap);
  }
  return out;
}

export const SAMPLE_MARKDOWN = [
  "## Plan\n\nI read the **timeline store** first, then traced where each `chat_delta` lands. The reveal has to stay a *display* concern: the store keeps every byte exactly as it arrived.\n\n",
  "1. Segment the arrived text into graphemes, so an emoji such as 👩🏽‍💻 or a combining mark never splits.\n2. Advance a read pointer at `backlog / τ` graphemes per second.\n3. Flush the rest quickly when the stream commits.\n\n",
  "\u4e2d\u6587\u4e5f\u8981\u987a\uff1a\u6a21\u578b\u7684\u8f93\u51fa\u901f\u5ea6\u4f1a\u6296\u52a8\uff0c\u6240\u4ee5\u6211\u4eec\u5728\u524d\u7aef\u653e\u4e00\u4e2a\u7f13\u51b2\uff0c\u7528\u6e10\u53d8\u7684\u65b9\u5f0f\u628a\u6587\u5b57\u5e73\u7a33\u5730\u5448\u73b0\u51fa\u6765\uff0c\u800c\u4e0d\u662f\u670d\u52a1\u7aef\u53d1\u6765\u4ec0\u4e48\u5c31\u4e00\u6a21\u4e00\u6837\u5730\u5410\u51fa\u4ec0\u4e48\u3002\n\n",
  "```python\ndef reveal(backlog: int) -> float:\n    wanted = backlog / TAU\n    return min(CEILING, max(FLOOR, wanted))\n```\n\n",
  "Finally, [the replay page](https://example.com/replay) shows the old and the new behaviour side by side; a **bold phrase that opens early** and closes later proves the unclosed-emphasis repair.\n",
].join("");
