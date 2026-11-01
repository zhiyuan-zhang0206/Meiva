// meiva:note
// Pure turn-collapse classification, summary aggregation, and grouping for the
// conversation timeline. classifyItem splits primary user-readable messages from
// secondary work/context items (including pre-token llmWaiting thinking items)
// and bare ephemeral markers; groupIntoTurns folds maximal runs of adjacent
// secondary items into collapsible TurnBlocks.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md

import { USER_REPLY_HEADER } from "@/lib/contracts/constants-generated";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { EXTERNAL_AGENT_PREFIX } from "@/lib/format/inbound-source";
import { orderSdkCalls, type SdkCall } from "@/lib/format/item-summary";
import enMessages from "../../../../messages/en";

import { classifyMarker, isSilentEphemeralPayload } from "./marker-classify";
import { isLiveCode, isLiveExecution, isLiveReasoning } from "./reasoning-clock";

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
// human (user / ui:page:*), and system wake-ups/notices.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md
export function inboundKind(source: string | null): "human" | "agent" | "external_agent" | "system" {
  const s = source ?? "";
  if (s.startsWith("agent:")) return "agent";
  if (s.startsWith(EXTERNAL_AGENT_PREFIX)) return "external_agent";
  if (s === "user" || s.startsWith("ui:page:")) return "human";
  return "system";
}

export type ItemClass = "primary" | "secondary" | "bare";

// meiva:note
// Backend envelopes wrap as "<header>:\n\n<body>" (bounded below 200 chars so a
// multi-paragraph stdout block is never mistaken for an envelope header).
// meiva:link agent/messages/docs/message-format.meiva.okf.md
export function splitEnvelope(payload: string): { header: string; body: string } {
  const idx = payload.indexOf("\n\n");
  if (idx > 0 && idx < 200) {
    return { header: payload.slice(0, idx), body: payload.slice(idx + 2) };
  }
  return { header: "", body: payload };
}

// meiva:note
// Items that intentionally render no visible DOM (silent ephemeral markers and
// the companion `[files]` placeholder after `system:upload`). Skipping them during
// turn grouping prevents invisible items from splitting adjacent TurnBlocks or
// mounting empty vertical padding gaps.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md
export function isSilentTimelineItem(item: BackendTimelineItem): boolean {
  if (item.kind === "system_marker" && item.source === "non_human_wake") {
    return true;
  }
  if (
    item.kind === "system_marker" &&
    classifyMarker(item.source).kind === "ephemeral" &&
    isSilentEphemeralPayload(item.payload)
  ) {
    return true;
  }
  if (
    item.kind === "inbound_chat" &&
    item.source === "user" &&
    !item.files?.length &&
    !item.images?.length &&
    splitEnvelope(item.payload).body === "[files]"
  ) {
    return true;
  }
  return false;
}

// meiva:note
// Fallback visibility predicate for the timeline's latest `agent_chat`: an empty block or an
// uncommitted SSE stream still typing `Meiva agent to user:` without body text is excluded so
// neither empty placeholders nor half-streamed headers flash on the main canvas.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md
export function isFallbackVisibleAgentChat(item: BackendTimelineItem): boolean {
  if (item.kind !== "agent_chat" || item.payload.trim().length === 0) {
    return false;
  }
  if (item.rawChatPayload !== undefined) {
    const trimmed = item.rawChatPayload.trimStart();
    if (USER_REPLY_HEADER.startsWith(trimmed)) {
      return false;
    }
    if (
      trimmed.startsWith(USER_REPLY_HEADER) &&
      trimmed.slice(USER_REPLY_HEADER.length).trim().length === 0
    ) {
      return false;
    }
  }
  return true;
}

export function findLastVisibleAgentChat(
  items: readonly BackendTimelineItem[],
): BackendTimelineItem | null {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    if (isFallbackVisibleAgentChat(items[i])) {
      return items[i];
    }
  }
  return null;
}

export function classifyItem(item: BackendTimelineItem): ItemClass {
  switch (item.kind) {
    case "agent_chat":
      return item.user_facing === false ? "secondary" : "primary";
    case "inbound_chat":
      return inboundKind(item.source) === "human" ||
        item.source === "system:upload" ||
        Boolean(item.files?.length)
        ? "primary"
        : "secondary";
    case "attach":
    case "agent_reasoning":
    case "agent_code":
    case "code_output":
    case "inbound_compact_summary":
    case "inbound_compact_request":
    case "system_prompt":
      return "secondary";
    case "system_marker":
      return classifyMarker(item.source).kind === "ephemeral" ? "bare" : "secondary";
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
  readonly thinkingMs: number;
  readonly codeMs: number;
  readonly execMs: number;
  // meiva:note
  // Sum of committed/frozen block durations (thinkingMs + codeMs + execMs),
  // excluding idle wall-clock gaps across restarts or wake-ups; shared by both
  // "Working for X" (plus live delta) and settled "Worked for X".
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  readonly workedMs: number;
  readonly lastLiveKind: "reasoning" | "code" | "output" | null;
  readonly lastLiveStartedAt: number;
  readonly sdkCalls: readonly SdkCall[];
}

function itemOwnMs(it: BackendTimelineItem): number {
  switch (it.kind) {
    case "agent_reasoning":
      return it.reasoning_ms ?? it.reasoningElapsedMs ?? 0;
    case "agent_code":
      return it.code_elapsed_ms ?? it.codeElapsedMs ?? 0;
    case "code_output":
      return it.exec_ms ?? it.execElapsedMs ?? 0;
    default:
      return 0;
  }
}

export function summarizeTurn(items: readonly BackendTimelineItem[]): TurnSummary {
  let thinking = 0;
  let code = 0;
  let output = 0;
  let thinkingMs = 0;
  let codeMs = 0;
  let execMs = 0;
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
      thinkingMs += itemOwnMs(it);
    } else if (it.kind === "agent_code") {
      code += 1;
      codeMs += itemOwnMs(it);
      if (it.sdk_calls) {
        for (const c of it.sdk_calls) {
          sdkCounts.set(c.method, (sdkCounts.get(c.method) ?? 0) + c.count);
        }
      }
    } else if (it.kind === "code_output") {
      output += 1;
      execMs += itemOwnMs(it);
    } else if (it.kind === "system_prompt") {
      systemPrompts += 1;
    } else if (it.kind === "inbound_compact_summary" || it.kind === "inbound_compact_request") {
      compactSummaries += 1;
    } else if (it.kind === "inbound_chat") {
      const sender = inboundKind(it.source);
      if (sender === "agent" || sender === "external_agent") agentMessages += 1;
      else systemNotes += 1;
    } else if (it.kind === "system_marker") {
      if (classifyMarker(it.source).kind === "memory") memories += 1;
      else systemNotes += 1;
    } else {
      // meiva:note
      // `attach` and non-user-facing `agent_chat` (internal background-wake notes)
      // land here so every secondary member contributes to a non-empty summary count.
      // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
      systemNotes += 1;
    }
  }
  const sdkCalls = orderSdkCalls(
    [...sdkCounts.entries()].map(([method, count]) => ({ method, count })),
  );
  const workedMs = thinkingMs + codeMs + execMs;
  let lastLiveKind: "reasoning" | "code" | "output" | null = null;
  let lastLiveStartedAt = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (isLiveReasoning(it)) {
      lastLiveKind = "reasoning";
      lastLiveStartedAt = it.reasoningStartedAt ?? 0;
      break;
    }
    if (isLiveCode(it)) {
      lastLiveKind = "code";
      lastLiveStartedAt = it.codeStartedAt ?? 0;
      break;
    }
    if (isLiveExecution(it)) {
      lastLiveKind = "output";
      lastLiveStartedAt = it.execStartedAt ?? 0;
      break;
    }
  }
  return { total: items.length, thinking, code, output, turns: Math.max(thinking, code, output), systemPrompts, compactSummaries, memories, agentMessages, systemNotes, thinkingMs, codeMs, execMs, sdkCalls, workedMs, lastLiveKind, lastLiveStartedAt };
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
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
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

  const promotedChat = findLastVisibleAgentChat(items);
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
    const cls = item === promotedChat ? "primary" : classifyItem(item);
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
