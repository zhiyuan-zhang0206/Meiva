// meiva:note
// Pure turn-collapse classification, summary aggregation, and grouping for the
// conversation timeline. classifyItem splits primary user-readable messages from
// secondary work/context items (including pre-token llmWaiting thinking items
// and system markers); groupIntoTurns folds maximal runs of adjacent
// secondary items into collapsible TurnBlocks.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { EXTERNAL_AGENT_PREFIX } from "@/lib/format/inbound-source";
import { orderSdkCalls, type SdkCall } from "@/lib/format/item-summary";
import enMessages from "../../../../messages/en";

import { classifyMarker, HARNESS_SWITCH_SOURCE, isSilentMarker } from "./marker-classify";

export type TurnTranslator = (
  key: string,
  values?: Record<string, string | number>,
) => string;

const EN_TURN: TurnTranslator = (key, values) => {
  const dict: Record<string, string> = (
    enMessages as unknown as Record<string, Record<string, string>>
  ).timeline;
  let s = dict[key] ?? key;
  if (values) {
    for (const [k, v] of Object.entries(values)) {
      s = s.replaceAll(`{${k}}`, String(v));
    }
  }
  return s;
};

// meiva:note
// Mirrors base/agents/messages/envelope.py sender classes: agent:*, external_agent:*,
// human (user), and system wake-ups/notices.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
export function inboundKind(source: string | null): "human" | "agent" | "external_agent" | "system" {
  const s = source ?? "";
  if (s.startsWith("agent:")) return "agent";
  if (s.startsWith(EXTERNAL_AGENT_PREFIX)) return "external_agent";
  if (s === "user") return "human";
  return "system";
}

export type ItemClass = "primary" | "secondary" | "bare";

// meiva:note
// The item kinds this build renders. A kind outside it comes from a backend newer than the loaded
// page; such an item renders nothing and takes no place in a turn, so a stale page shows no empty
// row where the new kind sits.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
const RENDERED_KINDS: Record<BackendTimelineItem["kind"], true> = {
  inbound_chat: true,
  inbound_compact_summary: true,
  inbound_compact_request: true,
  attach: true,
  agent_chat: true,
  agent_code: true,
  agent_reasoning: true,
  code_output: true,
  system_prompt: true,
  system_marker: true,
};

export function isRenderedKind(kind: string): boolean {
  return Object.hasOwn(RENDERED_KINDS, kind);
}

// meiva:note
// Items that intentionally render no visible DOM: an item of a kind this build does not know, and the ephemeral bookkeeping
// markers (`compact_done`, `cancelled`, `status:Cancellation requested.`,
// `compact_request:*`) and the companion
// `[files]` placeholder after `system:upload`. Skipping them during turn
// grouping prevents invisible items from splitting adjacent TurnBlocks or
// mounting empty vertical padding gaps. NoteTag markers are NOT here - every
// note the agent read now renders (user ruling 2026-11-09).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
export function isSilentTimelineItem(item: BackendTimelineItem): boolean {
  if (!isRenderedKind(item.kind)) return true;
  if (item.kind === "system_marker" && isSilentMarker(item.source, item.payload)) {
    return true;
  }
  if (
    item.kind === "inbound_chat" &&
    item.source === "user" &&
    !item.files?.length &&
    !item.images?.length &&
    item.payload === "[files]"
  ) {
    return true;
  }
  return false;
}

export function classifyItem(item: BackendTimelineItem): ItemClass {
  switch (item.kind) {
    case "agent_chat":
      return item.user_facing === false ? "secondary" : "primary";
    case "inbound_chat":
      return inboundKind(item.source) === "human" ? "primary" : "secondary";
    case "attach":
    case "agent_reasoning":
    case "agent_code":
    case "code_output":
    case "inbound_compact_summary":
    case "inbound_compact_request":
    case "system_prompt":
    case "system_marker":
      return "secondary";
  }
  const _exhaustive: never = item.kind;
  console.warn("[timeline] classifyItem: unknown item kind", _exhaustive);
  return "bare";
}

export interface TurnSummary {
  readonly total: number;
  readonly thinking: number;
  readonly code: number;
  readonly output: number;
  readonly turns: number;
  readonly systemPrompts: number;
  readonly compactSummaries: number;
  readonly memories: number;
  readonly agentMessages: number;
  readonly systemNotes: number;
  // meiva:note
  // Active-segment timing summary (epoch ms / durations in ms) for this work block:
  // splits the block's event items into active segments at inter-turn idle boundaries
  // (hidden agent_chat close, post-output inbound wakes, and queued inbound restarts)
  // so idle gaps between turns inside one work block are excluded (user ruling 2026-11-22).
  // - `settledActiveMs`: sum of durations of all segments containing agent output, or null
  //   if none exist or any active segment lacks timestamps.
  // - `priorActiveMs`: lower bound of the live Working total: sum of the measurable durations
  //   of completed active segments before the trailing live segment (0 when none; a segment
  //   without timestamps contributes 0 so the live clock never stops counting).
  // - `liveSegmentStartMs`: start timestamp of the open trailing segment, or null when
  //   the trailing segment is already closed (hidden agent_chat, lifecycle_terminate /
  //   lifecycle_interrupt, or trailing non-wake marker) or lacks a start timestamp.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  readonly settledActiveMs: number | null;
  readonly priorActiveMs: number;
  readonly liveSegmentStartMs: number | null;
  readonly sdkCalls: readonly SdkCall[];
}

function parseTimestampMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) && ms > 0 ? ms : null;
}

function isStandingContextItem(it: BackendTimelineItem): boolean {
  return (
    it.kind === "system_prompt" ||
    (it.kind === "system_marker" && !it.show_timestamp)
  );
}

// meiva:note
// Derive an item's wall-clock start from the fields timeline items already carry:
// live SSE items carry explicit `*StartedAt` anchors; committed items with a duration
// (`reasoning_ms`, `code_elapsed_ms`, `exec_ms`) stamp `created_at` at completion so
// start is `created_at - duration`; frontend-frozen reasoning/code keep `created_at`
// at start; instantaneous items start at `created_at`.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
function itemStartMs(it: BackendTimelineItem): number | null {
  if (it.kind === "agent_reasoning") {
    if (it.reasoningStartedAt != null && Number.isFinite(it.reasoningStartedAt) && it.reasoningStartedAt > 0) {
      return it.reasoningStartedAt;
    }
    const created = parseTimestampMs(it.created_at);
    if (created === null) return null;
    if (it.reasoning_ms != null && Number.isFinite(it.reasoning_ms)) {
      return created - Math.max(0, it.reasoning_ms);
    }
    return created;
  }
  if (it.kind === "agent_code") {
    if (it.codeStartedAt != null && Number.isFinite(it.codeStartedAt) && it.codeStartedAt > 0) {
      return it.codeStartedAt;
    }
    const created = parseTimestampMs(it.created_at);
    if (created === null) return null;
    if (it.code_elapsed_ms != null && Number.isFinite(it.code_elapsed_ms)) {
      return created - Math.max(0, it.code_elapsed_ms);
    }
    return created;
  }
  if (it.kind === "code_output") {
    if (it.execStartedAt != null && Number.isFinite(it.execStartedAt) && it.execStartedAt > 0) {
      return it.execStartedAt;
    }
    const created = parseTimestampMs(it.created_at);
    if (created === null) return null;
    const duration = it.exec_ms ?? it.execElapsedMs;
    if (duration != null && Number.isFinite(duration)) {
      return created - Math.max(0, duration);
    }
    return created;
  }
  return parseTimestampMs(it.created_at);
}

function itemEndMs(it: BackendTimelineItem): number | null {
  const created = parseTimestampMs(it.created_at);
  if (created === null) return null;
  if (it.kind === "agent_reasoning") {
    if (it.reasoning_ms != null && Number.isFinite(it.reasoning_ms)) {
      return created;
    }
    if (it.reasoningElapsedMs != null && Number.isFinite(it.reasoningElapsedMs)) {
      return created + Math.max(0, it.reasoningElapsedMs);
    }
    return created;
  }
  if (it.kind === "agent_code") {
    if (it.code_elapsed_ms != null && Number.isFinite(it.code_elapsed_ms)) {
      return created;
    }
    if (it.codeElapsedMs != null && Number.isFinite(it.codeElapsedMs)) {
      return created + Math.max(0, it.codeElapsedMs);
    }
    return created;
  }
  return created;
}

interface ActiveSegment {
  readonly items: BackendTimelineItem[];
  hasAgentOutput: boolean;
  closed: boolean;
  closingChatStep: string | null;
}

interface ComputedSegment {
  readonly hasAgentOutput: boolean;
  readonly closed: boolean;
  readonly startMs: number | null;
  readonly durationMs: number | null;
}

const WAKE_MARKER_SOURCES: ReadonlySet<string> = new Set([
  "heartbeat",
  HARNESS_SWITCH_SOURCE,
  "lifecycle_restart",
  "lifecycle_resurrect",
  "lifecycle_fork",
]);

const CLOSING_MARKER_SOURCES: ReadonlySet<string> = new Set([
  "lifecycle_terminate",
  "lifecycle_interrupt",
]);

function stepPrefix(itemId: string): string | null {
  const dot = itemId.lastIndexOf(".");
  return dot > 0 ? itemId.slice(0, dot) : null;
}

function isAgentOutputItem(it: BackendTimelineItem): boolean {
  return (
    it.kind === "agent_reasoning" ||
    it.kind === "agent_code" ||
    it.kind === "code_output" ||
    it.kind === "attach" ||
    it.kind === "agent_chat"
  );
}

function isWakeMarker(it: BackendTimelineItem): boolean {
  return it.kind === "system_marker" && it.source !== null && WAKE_MARKER_SOURCES.has(it.source);
}

function isTurnClosingMarker(it: BackendTimelineItem): boolean {
  return it.kind === "system_marker" && it.source !== null && CLOSING_MARKER_SOURCES.has(it.source);
}

function isNonWakeMarker(it: BackendTimelineItem): boolean {
  return it.kind === "system_marker" && !isWakeMarker(it);
}

function isInboundWakeItem(it: BackendTimelineItem): boolean {
  return (
    it.kind === "inbound_chat" ||
    it.kind === "inbound_compact_request" ||
    it.kind === "inbound_compact_summary" ||
    isWakeMarker(it)
  );
}

function isQueuedWakeRestart(it: BackendTimelineItem): boolean {
  return it.kind === "inbound_chat" || isWakeMarker(it);
}

function isSameClosingChatStep(seg: ActiveSegment, it: BackendTimelineItem): boolean {
  if (seg.closingChatStep === null || !isAgentOutputItem(it)) return false;
  const prefix = stepPrefix(it.item_id);
  return prefix !== null && prefix === seg.closingChatStep;
}

function shouldStartNewSegment(seg: ActiveSegment, it: BackendTimelineItem): boolean {
  if (seg.closed) {
    return !isSameClosingChatStep(seg, it);
  }
  if (isNonWakeMarker(it) && !seg.hasAgentOutput) return true;
  if (seg.hasAgentOutput && isInboundWakeItem(it)) return true;
  if (!seg.hasAgentOutput && isQueuedWakeRestart(it)) return true;
  return false;
}

function splitActiveSegments(eventItems: readonly BackendTimelineItem[]): ActiveSegment[] {
  const segments: ActiveSegment[] = [];
  for (const it of eventItems) {
    let seg = segments.length > 0 ? segments[segments.length - 1] : null;
    if (seg !== null && shouldStartNewSegment(seg, it)) {
      seg = null;
    }
    if (seg === null) {
      seg = { items: [], hasAgentOutput: false, closed: false, closingChatStep: null };
      segments.push(seg);
    }
    seg.items.push(it);
    if (it.kind === "agent_chat") {
      seg.hasAgentOutput = true;
      seg.closed = true;
      seg.closingChatStep = stepPrefix(it.item_id) ?? "";
    } else if (isAgentOutputItem(it)) {
      seg.hasAgentOutput = true;
      seg.closed = false;
      seg.closingChatStep = null;
    } else if (isTurnClosingMarker(it) || (isNonWakeMarker(it) && !seg.hasAgentOutput)) {
      seg.closed = true;
      seg.closingChatStep = null;
    }
  }
  return segments;
}

function hasMeasuredDuration(items: readonly BackendTimelineItem[]): boolean {
  return items.some(
    (it) =>
      (it.reasoning_ms != null && Number.isFinite(it.reasoning_ms)) ||
      (it.reasoningElapsedMs != null && Number.isFinite(it.reasoningElapsedMs)) ||
      (it.code_elapsed_ms != null && Number.isFinite(it.code_elapsed_ms)) ||
      (it.codeElapsedMs != null && Number.isFinite(it.codeElapsedMs)) ||
      (it.exec_ms != null && Number.isFinite(it.exec_ms)) ||
      (it.execElapsedMs != null && Number.isFinite(it.execElapsedMs)),
  );
}

function computeSegment(seg: ActiveSegment): ComputedSegment {
  let startMs: number | null = null;
  if (seg.items.length > 0) {
    const firstStart = itemStartMs(seg.items[0]);
    if (firstStart !== null) {
      startMs = firstStart;
      for (let i = 1; i < seg.items.length; i++) {
        const s = itemStartMs(seg.items[i]);
        if (s !== null && s < startMs) startMs = s;
      }
    }
  }

  const settledItems = seg.items.filter((it) => !it.llmWaiting);
  let endMs: number | null = null;
  if (settledItems.length > 0) {
    const lastEnd = itemEndMs(settledItems[settledItems.length - 1]);
    if (lastEnd !== null) {
      endMs = lastEnd;
      for (let i = 0; i < settledItems.length - 1; i++) {
        const e = itemEndMs(settledItems[i]);
        if (e !== null && e > endMs) endMs = e;
      }
    }
  }

  let durationMs: number | null = null;
  if (startMs !== null && endMs !== null) {
    if (endMs > startMs) {
      durationMs = endMs - startMs;
    } else if (hasMeasuredDuration(settledItems)) {
      durationMs = 0;
    }
  }
  return {
    hasAgentOutput: seg.hasAgentOutput,
    closed: seg.closed,
    startMs,
    durationMs,
  };
}

function sumActiveSegments(
  segments: readonly ComputedSegment[],
): { readonly totalMs: number | null; readonly activeCount: number } {
  let total = 0;
  let activeCount = 0;
  for (const seg of segments) {
    if (!seg.hasAgentOutput) continue;
    activeCount += 1;
    if (seg.durationMs === null) return { totalMs: null, activeCount };
    total += seg.durationMs;
  }
  return { totalMs: total, activeCount };
}

function sumMeasuredActiveMs(segments: readonly ComputedSegment[]): number {
  let total = 0;
  for (const seg of segments) {
    if (seg.hasAgentOutput && seg.durationMs !== null) total += seg.durationMs;
  }
  return total;
}

function summarizeActiveTiming(items: readonly BackendTimelineItem[]): {
  readonly settledActiveMs: number | null;
  readonly priorActiveMs: number;
  readonly liveSegmentStartMs: number | null;
} {
  const eventItems = items.filter((it) => !isStandingContextItem(it));
  const segments = splitActiveSegments(eventItems).map(computeSegment);
  const all = sumActiveSegments(segments);
  const settledActiveMs = all.activeCount > 0 ? all.totalMs : null;

  const lastSeg = segments.length > 0 ? segments[segments.length - 1] : null;
  if (lastSeg !== null && !lastSeg.closed) {
    return {
      settledActiveMs,
      priorActiveMs: sumMeasuredActiveMs(segments.slice(0, -1)),
      liveSegmentStartMs: lastSeg.startMs,
    };
  }
  return {
    settledActiveMs,
    priorActiveMs: sumMeasuredActiveMs(segments),
    liveSegmentStartMs: null,
  };
}

export function summarizeTurn(items: readonly BackendTimelineItem[]): TurnSummary {
  let thinking = 0;
  let code = 0;
  let output = 0;
  let systemPrompts = 0;
  let compactSummaries = 0;
  let memories = 0;
  let agentMessages = 0;
  let systemNotes = 0;
  const sdkCounts = new Map<string, number>();

  for (const it of items) {
    if (it.llmWaiting) continue;
    if (it.kind === "agent_reasoning") {
      thinking += 1;
    } else if (it.kind === "agent_code") {
      code += 1;
      if (it.sdk_calls) {
        for (const c of it.sdk_calls) {
          sdkCounts.set(c.method, (sdkCounts.get(c.method) ?? 0) + c.count);
        }
      }
    } else if (it.kind === "code_output") {
      output += 1;
    } else if (it.kind === "system_prompt") {
      systemPrompts += 1;
    } else if (it.kind === "inbound_compact_summary" || it.kind === "inbound_compact_request") {
      compactSummaries += 1;
    } else if (it.kind === "inbound_chat") {
      const sender = inboundKind(it.source);
      if (sender === "agent" || sender === "external_agent") agentMessages += 1;
      else systemNotes += 1;
    } else if (it.kind === "system_marker") {
      if (classifyMarker(it.source, it.payload).kind === "memory") memories += 1;
      else systemNotes += 1;
    } else {
      // meiva:note
      // `attach` and non-user-facing `agent_chat` (internal background-wake notes)
      // land here so every secondary member contributes to a non-empty summary count.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md
      systemNotes += 1;
    }
  }
  const sdkCalls = orderSdkCalls(
    [...sdkCounts.entries()].map(([method, count]) => ({ method, count })),
  );
  const { settledActiveMs, priorActiveMs, liveSegmentStartMs } = summarizeActiveTiming(items);

  return {
    total: items.length,
    thinking,
    code,
    output,
    turns: Math.max(thinking, code, output),
    systemPrompts,
    compactSummaries,
    memories,
    agentMessages,
    systemNotes,
    settledActiveMs,
    priorActiveMs,
    liveSegmentStartMs,
    sdkCalls,
  };
}

export function turnSummaryEqual(a: TurnSummary, b: TurnSummary): boolean {
  if (a === b) return true;
  for (const key of Object.keys(a) as (keyof TurnSummary)[]) {
    if (key !== "sdkCalls" && a[key] !== b[key]) return false;
  }
  return (
    a.sdkCalls.length === b.sdkCalls.length &&
    a.sdkCalls.every((call, index) =>
      call.method === b.sdkCalls[index].method && call.count === b.sdkCalls[index].count)
  );
}

export function formatTurnSummary(summary: TurnSummary, t: TurnTranslator = EN_TURN): string {
  const parts: string[] = [];
  if (summary.turns > 0) {
    parts.push(
      t("turns", {
        count: summary.turns,
        unit: t(summary.turns === 1 ? "turnSingular" : "turnPlural"),
      }),
    );
  }
  // meiva:note
  // Omit the ever-present system prompt from the collapsed turn label while
  // keeping compact summaries, memories, system notes, and agent messages in sentence case.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.md
  if (summary.compactSummaries > 0) {
    parts.push(
      summary.compactSummaries === 1
        ? t("compactSummarySingular")
        : t("compactSummaries", {
            count: summary.compactSummaries,
            unit: t("compactSummariesPlural"),
          }),
    );
  }
  if (summary.memories > 0) {
    parts.push(
      t("memories", {
        count: summary.memories,
        unit: t(summary.memories === 1 ? "memory" : "memories_plural"),
      }),
    );
  }
  if (summary.systemNotes > 0) {
    parts.push(
      t("systemNotes", {
        count: summary.systemNotes,
        unit: t(summary.systemNotes === 1 ? "systemNoteSingular" : "systemNotesPlural"),
      }),
    );
  }
  if (summary.agentMessages > 0) {
    parts.push(
      t("agentMessages", {
        count: summary.agentMessages,
        unit: t(summary.agentMessages === 1 ? "agentMessageSingular" : "agentMessagesPlural"),
      }),
    );
  }
  return parts.join(" · ");
}

export type TimelineGroup =
  | { readonly kind: "single"; readonly item: BackendTimelineItem; readonly index: number }
  | {
      readonly kind: "turn";
      readonly items: readonly BackendTimelineItem[];
      readonly startIndex: number;
      readonly summary: TurnSummary;
    };

export function groupIntoTurns(
  items: readonly BackendTimelineItem[],
  opts: { readonly collapseTurns: boolean; readonly liveIndex: number | null },
): TimelineGroup[] {
  if (!opts.collapseTurns) {
    return items.map((item, index) => ({ kind: "single", item, index }));
  }

  const groups: TimelineGroup[] = [];
  let run: BackendTimelineItem[] = [];
  let runStart = 0;

  const flush = () => {
    if (run.length === 0) return;
    groups.push({ kind: "turn", items: run, startIndex: runStart, summary: summarizeTurn(run) });
    run = [];
  };

  items.forEach((item, index) => {
    if (isSilentTimelineItem(item)) return;
    const cls = classifyItem(item);
    const groupable = cls === "secondary";

    if (groupable) {
      if (run.length === 0) runStart = index;
      run.push(item);
    } else {
      flush();
      groups.push({ kind: "single", item, index });
    }
  });
  flush();
  return groups;
}
