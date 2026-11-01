"use client";

import { useTranslations } from "next-intl";

import { formatTokens } from "@/lib/format/format-number";
import { cn } from "@/lib/format/utils";
import { OVERFLOW_HIDDEN } from "@/lib/layout/layout";

export interface ContextMeterProps {
  /** Current context-window occupancy (the last LLM call's input_tokens). */
  contextTokens: number;
  /** Model context window ceiling; 0 = unknown (gauge + thresholds hidden). */
  maxContextTokens: number;
  /** Wind-down-reminder threshold (soft) — 0 when unknown. */
  softCompactTokens: number;
  /** Force-compact ceiling (hard) — 0 when unknown. */
  hardCompactTokens: number;
  showText?: boolean;
  // meiva:note
  // Extra classes for the numeric text.
  textClassName?: string;
  className?: string;
}

// meiva:note
// The inspector's context readout: a fixed-width (`w-32`) gauge whose fill
// turns amber past the soft (wind-down) threshold and red past the hard
// (force-compact) ceiling, with tick marks at both, plus an optional numeric
// summary. The soft/hard values are the agent model's own — a fraction of its
// context window (see `base/lm/context_budget.py`), so a 200K-window model
// shows smaller marks than a 1M-window one.
//
// The caller only mounts this once `contextTokens > 0` (an agent that has run at
// least one LLM call). When `maxContextTokens` is 0 (a model with no known
// window) the gauge and thresholds are omitted and only the raw count shows.
// meiva:link ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md
export function ContextMeter({
  contextTokens,
  maxContextTokens,
  softCompactTokens,
  hardCompactTokens,
  showText = true,
  textClassName,
  className,
}: ContextMeterProps) {
  const t = useTranslations("contextMeter");
  const hasWindow = maxContextTokens > 0;
  const hasThresholds = hasWindow && hardCompactTokens > 0;

  // Position everything as a percentage of the full window; clamp so a value
  // over the ceiling (occupancy briefly past hard before compaction fires)
  // still renders inside the bar.
  const pct = (v: number) => Math.min(100, Math.max(0, (v / maxContextTokens) * 100));
  const fillPct = hasWindow ? pct(contextTokens) : 0;

  // Fill color communicates urgency: neutral below soft, amber in the
  // wind-down band, red once past the force-compact ceiling.
  const overHard = hasThresholds && contextTokens > hardCompactTokens;
  const overSoft = hasThresholds && contextTokens > softCompactTokens;
  const fillColor = overHard
    ? "bg-destructive"
    : overSoft
      ? "bg-amber-500"
      : "bg-muted-foreground/60";

  const label = hasThresholds
    ? t("contextWindDown", {
        used: formatTokens(contextTokens),
        max: formatTokens(maxContextTokens),
        soft: formatTokens(softCompactTokens),
        hard: formatTokens(hardCompactTokens),
      })
    : hasWindow
      ? t("context", {
          used: formatTokens(contextTokens),
          max: formatTokens(maxContextTokens),
        })
      : t("contextUsed", { used: formatTokens(contextTokens) });

  return (
    <span
      data-testid="context-meter"
      className={cn("inline-flex items-center gap-1.5", className)}
    >
      {hasWindow ? (
        <span
          role="img"
          aria-label={label}
          className={cn(
            "relative inline-block h-1.5 w-32 shrink-0 rounded-full bg-muted",
            OVERFLOW_HIDDEN
          )}
        >
          <span
            data-testid="context-meter-fill"
            className={cn("absolute inset-y-0 left-0 rounded-full", fillColor)}
            style={{ width: `${fillPct}%` }}
          />
          {hasThresholds ? (
            <>
              <span
                data-testid="context-meter-soft-mark"
                className="absolute inset-y-0 w-px bg-amber-500/80"
                style={{ left: `${pct(softCompactTokens)}%` }}
              />
              <span
                data-testid="context-meter-hard-mark"
                className="absolute inset-y-0 w-px bg-destructive/80"
                style={{ left: `${pct(hardCompactTokens)}%` }}
              />
            </>
          ) : null}
        </span>
      ) : null}
      {showText ? (
        <span className={cn("truncate tabular-nums", textClassName)}>
          Context: {formatTokens(contextTokens)}
          {hasWindow ? `/${formatTokens(maxContextTokens)}` : ""}
          {hasThresholds
            ? t("soft", { soft: formatTokens(softCompactTokens), hard: formatTokens(hardCompactTokens) })
            : " tokens"}
        </span>
      ) : null}
    </span>
  );
}
