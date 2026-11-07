"use client";

import { formatAbsolute, type ClockFormat } from "@/lib/format/time";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { useTimeFormat } from "@/lib/format/use-time-format";

// meiva:note
// Timestamp in a message card's hover footer (every kind whose config stamps one): `YYYY-MM-DD HH:MM:SS`, with the abbreviated weekday inserted when Show weekday is set and `2:53:29 AM` on the 12-hour clock. It renders the viewer's local timezone (Intl default) without appending a timezone suffix.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md

export interface FormatItemTimeOptions {
  showWeekday?: boolean;
  clockFormat?: ClockFormat;
}

export function formatItemTime(iso: string, opts: FormatItemTimeOptions = {}): string {
  const formatted = formatAbsolute(iso, {
    weekday: opts.showWeekday,
    clockFormat: opts.clockFormat,
  });
  return formatted;
}

// meiva:note
// The stamp is text, not a control: it carries no title tooltip and no button chrome, and it reads the viewer's own clock preferences (12/24-hour, weekday) instead of taking them as props, so every call site renders the same shape.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export function ItemTimestamp({ iso }: { iso: string }) {
  const { clockFormat } = useTimeFormat();
  const { settings } = useUserSettings();
  const showWeekday = settings["display.show_timestamp_weekday"] as boolean;
  const time = formatItemTime(iso, { showWeekday, clockFormat });
  if (!time) return null;
  return (
    <span className="text-[9px] leading-none text-muted-foreground/80 font-mono tabular-nums">
      {time}
    </span>
  );
}
