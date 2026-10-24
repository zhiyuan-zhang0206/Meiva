import type { SystemEvent } from "../contracts/types";

const MAX_PARSE_FAILURES = 3;

export type SystemFrame =
  | { kind: "heartbeat" }
  | { kind: "events"; events: SystemEvent[] }
  | { kind: "malformed"; error: unknown };

// meiva:note
// A heartbeat (`{"role":"heartbeat"}`) is a liveness frame, not a business event,
// so it is classified and never fanned out. A batch frame (`[...]` from the
// all-events stream) keeps its object members and drops the rest instead of
// casting corrupt members into SystemEvents.
// meiva:link ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md
export function classifySystemFrame(raw: string): SystemFrame {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    return { kind: "malformed", error };
  }
  if (Array.isArray(parsed)) {
    const events: SystemEvent[] = [];
    for (const item of parsed as unknown[]) {
      if (typeof item !== "object" || item === null) continue;
      if ((item as { role?: unknown }).role === "heartbeat") continue;
      events.push(item as SystemEvent);
    }
    return { kind: "events", events };
  }
  if (typeof parsed === "object" && parsed !== null && (parsed as { role?: unknown }).role === "heartbeat") {
    return { kind: "heartbeat" };
  }
  return { kind: "events", events: [parsed as SystemEvent] };
}

export function selectedAgentEvents(events: SystemEvent[], activeId: number | null): SystemEvent[] {
  if (activeId === null) return [];
  return events.filter((event) => event.agent_id === 0 || event.agent_id === activeId);
}

export function nextParseFailures(failures: number): { failures: number; reopen: boolean } {
  const next = failures + 1;
  return next >= MAX_PARSE_FAILURES ? { failures: 0, reopen: true } : { failures: next, reopen: false };
}
