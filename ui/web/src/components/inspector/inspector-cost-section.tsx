"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, Gauge, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { WindowSelect } from "@/components/agents/window-select";
import { ContextButton } from "@/components/inspector/context-breakdown";
import { SectionSkeleton } from "@/components/inspector/inspector-panel-skeleton";
import { Metric, Section } from "@/components/inspector/inspector-section";
import { useAgentRoster } from "@/lib/agents/use-agents";
import type { AgentInspectStatistics, AgentRow } from "@/lib/contracts/types";
import { AGENT_DETAIL_QUERY_KEY } from "@/lib/fold/agents";
import { formatTokens } from "@/lib/format/format-number";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { useApi } from "@/lib/transport/transport-context";

// meiva:note
// `labelKey` carries the translator's own key type: a window whose label key is missing fails at
// this table, not at a cast on the render path.
// meiva:link ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md
type InspectorKey = Parameters<ReturnType<typeof useTranslations<"inspector">>>[0];

const WINDOWS: { labelKey: InspectorKey; value: number | null }[] = [
  { labelKey: "windowAll", value: null },
  { labelKey: "window5m", value: 0 },
  { labelKey: "window1h", value: 1 },
  { labelKey: "window24h", value: 24 },
  { labelKey: "window7d", value: 168 },
];

export function InspectReadError({ message, retryLabel, pending, onRetry }: {
  message: string;
  retryLabel: string;
  pending: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="space-y-2 font-mono text-[11px] text-destructive" role="alert">
      <p>{message}</p>
      <button
        type="button"
        onClick={onRetry}
        disabled={pending}
        className="rounded border border-destructive/40 px-2 py-1 hover:bg-destructive/10 disabled:opacity-50"
      >
        {retryLabel}
      </button>
    </div>
  );
}

export function useAgentHarness(agentId: number) {
  const client = useApi();
  const { data: roster } = useAgentRoster();
  const selected = useQuery<AgentRow>({
    queryKey: [...AGENT_DETAIL_QUERY_KEY, agentId],
    queryFn: ({ signal }) => client.getAgent(agentId, signal),
    enabled: false,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
  return (roster?.agents.find((agent) => agent.agent_id === agentId) ?? selected.data)?.harness;
}

export function ContextSection({ agentId }: { agentId: number }) {
  const t = useTranslations("inspector");
  const harness = useAgentHarness(agentId);
  const [breakdownOpen, setBreakdownOpen] = useState(false);
  const contextTokens = useTimelineStore((s) => s.tokenUsage);
  const maxContextTokens = useTimelineStore((s) => s.maxContextTokens);
  const softCompactTokens = useTimelineStore((s) => s.softCompactTokens);
  const hardCompactTokens = useTimelineStore((s) => s.hardCompactTokens);
  if (harness !== "meiva" || contextTokens <= 0) return null;
  const hasWindow = maxContextTokens > 0;
  const percent = hasWindow
    ? `${((contextTokens / maxContextTokens) * 100).toFixed(1)}%`
    : "—";
  return (
    <Section icon={<Gauge className="size-3" />} title={t("sectionContext")} badge={percent}>
      <div className="space-y-1.5">
        <div className="relative px-0.5 py-1">
          <ContextButton
            agentId={agentId}
            open={breakdownOpen}
            onOpenChange={setBreakdownOpen}
            contextTokens={contextTokens}
            maxContextTokens={maxContextTokens}
            softCompactTokens={softCompactTokens}
            hardCompactTokens={hardCompactTokens}
            showText={false}
          />
        </div>
        <div className="grid grid-cols-2 gap-2">
          <Metric
            label={t("metricContextUsed")}
            value={hasWindow
              ? `${formatTokens(contextTokens)} / ${formatTokens(maxContextTokens)}`
              : formatTokens(contextTokens)}
            valueTestId="inspector-context-used"
          />
          {hardCompactTokens > 0 ? (
            <Metric
              label={t("metricContextLimits")}
              value={`${formatTokens(softCompactTokens)} / ${formatTokens(hardCompactTokens)}`}
              valueTestId="inspector-context-limits"
            />
          ) : null}
        </div>
      </div>
    </Section>
  );
}

export function CostActivitySection({
  agentId,
  inspect,
  failed,
  pending,
  hours,
  onHoursChange,
  fetching,
  onRefresh,
}: {
  agentId: number;
  inspect: AgentInspectStatistics | undefined;
  failed: boolean;
  pending: boolean;
  hours: number | null;
  onHoursChange: (hours: number | null) => void;
  fetching: boolean;
  onRefresh: () => void;
}) {
  const t = useTranslations("inspector");
  const harness = useAgentHarness(agentId);
  const isExternalHarness = harness != null && harness !== "meiva";
  const cost = inspect?.cost;
  const activity = inspect?.activity ?? null;
  const tps = inspect?.tps;
  const hasLife = activity !== null && activity.alive_seconds > 0;
  const idleSeconds = activity
    ? Math.max(0, activity.alive_seconds - activity.active_seconds)
    : 0;
  // meiva:note
  // The cost cell names a billing mode only when the number needs qualifying: a
  // metered (API) agent prices every call, so the amount is the whole story and
  // the cell stays silent; a subscription pays nothing per token, so its label
  // rides the amount in parentheses instead of taking a line of its own. An
  // unresolved harness gets the amount alone — the panel never guesses a mode
  // (user ruling 2026-10-28).
  // meiva:link ui/web/src/docs/frontend-components/inspector/activity.meiva.okf.md
  const billingLabel = isExternalHarness ? t("billingSubscription") : undefined;
  const unpricedSub =
    cost && cost.unpriced_calls > 0
      ? t("unpriced", { count: String(cost.unpriced_calls) })
      : undefined;
  return (
    <Section
      icon={<Activity className="size-3" />}
      title={t("sectionCostActivity")}
      action={
        <span className={cn("shrink-0 items-center gap-1", FLEX)}>
          <button
            type="button"
            onClick={onRefresh}
            disabled={fetching}
            aria-label={t("refreshCostActivity")}
            className="rounded p-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={cn("size-3.5", fetching && "animate-spin")} />
          </button>
          <WindowSelect
            value={hours == null ? "all" : String(hours)}
            options={WINDOWS.map((w) => {
              const label = t(w.labelKey);
              const appliedHours = inspect?.applied_window_hours;
              return {
                value: String(w.value ?? "all"),
                label:
                  w.value === hours &&
                  appliedHours != null &&
                  w.value != null &&
                  appliedHours < w.value
                    ? `${label} · ${appliedHours}h`
                    : label,
              };
            })}
            onChange={(v) => onHoursChange(v === "all" ? null : Number(v))}
            ariaLabel={t("windowAriaLabel")}
            className="cursor-pointer rounded border border-border bg-transparent px-1 py-0.5 text-[10px] text-muted-foreground hover:text-foreground focus:ring-1 focus:ring-ring focus:outline-none"
          />
        </span>
      }
    >
      {inspect ? (
        <div className="space-y-1">
          {cost ? (
            <div className="grid grid-cols-2 gap-1">
              <Metric
                label={t("metricCost")}
                value={costAmount(cost.cost_usd, billingLabel)}
                sub={unpricedSub}
                valueTestId="inspector-cost-amount"
              />
              <Metric label={t("metricLlmCalls")} value={String(cost.llm_calls)} />
              <Metric
                label={t("metricTokens")}
                value={`${formatTokens(cost.tokens_in)} / ${formatTokens(cost.tokens_out)}`}
              />
              <Metric
                label={t("metricCacheHit")}
                value={cost.cache_hit_pct === null ? "N/A" : `${cost.cache_hit_pct.toFixed(2)}%`}
              />
            </div>
          ) : null}
          <div className="grid grid-cols-2 gap-1">
            {!isExternalHarness ? (
              <>
                <Metric
                  label={t("metricTps")}
                  value={tps?.lm_stage_tps != null ? formatTps(tps.lm_stage_tps) : "—"}
                />
                <Metric
                  label={t("metricLlmOutput")}
                  value={
                    hasLife && activity.llm_seconds != null
                      ? formatInterval(Math.round(activity.llm_seconds))
                      : "—"
                  }
                />
                <Metric
                  label={t("metricCodeExecution")}
                  value={hasLife ? formatInterval(Math.round(activity.exec_seconds)) : "—"}
                />
              </>
            ) : null}
            <Metric
              label={t("metricIdle")}
              value={hasLife ? formatInterval(Math.round(idleSeconds)) : "—"}
            />
          </div>
        </div>
      ) : failed || !pending ? (
        <InspectReadError
          message={t("windowedUnavailable")}
          retryLabel={t("retryWindowed")}
          pending={fetching}
          onRetry={onRefresh}
        />
      ) : (
        <SectionSkeleton title={t("sectionCostActivity")} rows={4} />
      )}
    </Section>
  );
}

// meiva:note
// One text run: the amount and, for a subscription, its qualifier in
// parentheses — the cell must not split them across two rows.
// meiva:link ui/web/src/docs/frontend-components/inspector/activity.meiva.okf.md
function costAmount(usd: number, billing: string | undefined): string {
  const amount = `$${usd.toFixed(4)}`;
  return billing === undefined ? amount : `${amount} (${billing})`;
}

function formatTps(n: number): string {
  if (n === 0) return "—";
  return n.toFixed(1);
}

export function formatInterval(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH ? `${d}d ${remH}h` : `${d}d`;
}
