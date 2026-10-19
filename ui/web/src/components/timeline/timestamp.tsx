"use client";

import { formatAbsolute, type ClockFormat } from "@/lib/format/time";
import { useTimeFormat } from "@/lib/format/use-time-format";

// meiva:note
// Timestamp on agent_reasoning / agent_chat / agent_code headers: `[YYYY-MM-DD HH:MM:SS]`, with the abbreviated weekday inserted when showWeekday is set, `2:53:29 AM` on the 12-hour clock, and the zone name (`[2026-05-06 16:56:29 PDT]`) only when Show timezone is on. It renders the viewer's local timezone (Intl default) and may disagree with backend `settings.timezone` on cross-tz deployments — the accepted trade-off for needing no server-side tz config. The shape mirrors the agent-facing stamp but can carry the trailing zone the backend dropped: the agent is told its cluster timezone once and every stamp it reads is in it, while a viewer has no such declaration, so the label is offered as a preference instead of always riding along.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md

export interface FormatItemTimeOptions {
  showWeekday?: boolean;
  clockFormat?: ClockFormat;
  showTimeZone?: boolean;
}

export function formatItemTime(iso: string, opts: FormatItemTimeOptions = {}): string {
  const formatted = formatAbsolute(iso, {
    weekday: opts.showWeekday,
    clockFormat: opts.clockFormat,
    showTimeZone: opts.showTimeZone,
  });
  return formatted ? `[${formatted}]` : "";
}

export function ItemTimestamp({ iso, showWeekday = false }: { iso: string; showWeekday?: boolean }) {
  const { clockFormat, showTimeZone } = useTimeFormat();
  const time = formatItemTime(iso, { showWeekday, clockFormat, showTimeZone });
  if (!time) return null;
  return (
    <span className="ml-2 text-[10px] text-muted-foreground/60 font-mono tabular-nums">
      {time}
    </span>
  );
}
