import type { SseChannel } from "./sse-plan";

const WIRE_CHANNEL: Record<SseChannel, string> = {
  system: "global",
  systemAll: "all",
  alerts: "alerts",
};
const MERGED_ORDER: readonly SseChannel[] = ["system", "systemAll", "alerts"];

export function mergedChannels(merged: ReadonlySet<SseChannel>): readonly SseChannel[] {
  return MERGED_ORDER.filter((ch) => merged.has(ch));
}

export function mergedStreamUrl(base: string, merged: ReadonlySet<SseChannel>, union: string): string {
  const channels = mergedChannels(merged).map((ch) => WIRE_CHANNEL[ch]).join(",");
  const agents = merged.has("systemAll") ? `&agents=${union === "" ? "0" : union}` : "";
  return `${base}/api/stream?channels=${channels}${agents}`;
}

export function detailUnion(interests: Iterable<{ activeId: number | null }>): string {
  const ids = [...new Set([...interests].map(({ activeId }) => activeId).filter((id): id is number => id !== null))]
    .sort((a, b) => a - b);
  return ids.join(",");
}

export function staleTabs(
  interests: ReadonlyMap<string, { seen: number }>,
  ownTabId: string,
  cutoff: number,
): string[] {
  return [...interests]
    .filter(([tabId, interest]) => tabId !== ownTabId && interest.seen <= cutoff)
    .map(([tabId]) => tabId);
}

const LOGICAL_CHANNEL: Record<string, SseChannel | undefined> = {
  global: "system",
  all: "systemAll",
  alerts: "alerts",
};

export interface MergedFrame {
  ch: SseChannel;
  events: unknown[];
  dropped: number;
  error?: string;
}

export function parseMergedFrame(raw: string): MergedFrame | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  if (!("ch" in parsed) || typeof parsed.ch !== "string") return null;
  const ch = LOGICAL_CHANNEL[parsed.ch];
  if (ch === undefined) return null;
  if ("error" in parsed && typeof parsed.error === "string") {
    return { ch, events: [], dropped: 0, error: parsed.error };
  }
  if (!("events" in parsed) || !Array.isArray(parsed.events)) return null;
  const dropped =
    "dropped" in parsed && typeof parsed.dropped === "number" && parsed.dropped > 0
      ? parsed.dropped
      : 0;
  return { ch, events: parsed.events, dropped };
}
