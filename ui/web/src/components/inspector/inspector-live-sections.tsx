"use client";

import { modelLabel } from "@/lib/contracts/models";

import {
  CalendarClock,
  HeartPulse,
  SlidersHorizontal,
  Terminal,
} from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";

import { Metric, Section } from "@/components/inspector/inspector-section";
import { useAgentRoster } from "@/lib/agents/use-agents";
import type {
  AgentInspectLive,
  HeartbeatInfo,
  InspectorScheduleInfo,
  ShellInfo,
} from "@/lib/contracts/types";
import { formatRelative, formatUptime } from "@/lib/format/time";
import { useNow } from "@/lib/format/now/use-now";
import { useTimeFormat } from "@/lib/format/use-time-format";
import { cn } from "@/lib/format/utils";
import { harnessLabel } from "@/lib/harness/harness";
import { useWallNow } from "@/lib/transport/transport-context";

import { formatInterval, InspectReadError } from "./inspector-cost-section";

export function ShellsSection({ inspect }: { inspect: AgentInspectLive }) {
  const { shells } = inspect;
  const now = useNow(1_000, useWallNow());
  const t = useTranslations("inspector");
  if (inspect.shells_available === true && shells.length === 0) return null;

  return (
    <Section
      icon={<Terminal className="size-3" />}
      title={t("sectionShells")}
      badge={inspect.shells_available === true ? String(shells.length) : "?"}
    >
      {inspect.shells_available !== true ? (
        <p className="font-mono text-[10.5px] text-muted-foreground">Shell observation unavailable</p>
      ) : (
        <ul className="space-y-1">
          {shells.map((s) => (
            <ShellRow key={s.id} agentId={inspect.agent_id} shell={s} now={now} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function ShellRow({
  agentId,
  shell,
  now,
}: {
  agentId: number;
  shell: ShellInfo;
  now: Date;
}) {
  const t = useTranslations("inspector");
  const validAgent = Number.isFinite(agentId) && agentId >= 0;
  const validShell = Number.isFinite(shell.id) && shell.id >= 0;
  const createdMs = shell.created_at != null ? new Date(shell.created_at).getTime() : NaN;
  const runtimeSeconds =
    Number.isFinite(createdMs)
      ? Math.max(0, Math.floor((now.getTime() - createdMs) / 1000))
      : shell.uptime_seconds;
  const rowClass =
    "flex items-center gap-2 rounded border border-black/[0.06] dark:border-white/[0.08] bg-sidebar-accent/40 dark:bg-[#33322C] px-2.5 py-1.5 font-mono text-[11px] leading-[16px]";
  const content = (
    <>
      <span className="tabular-nums text-muted-foreground">#{shell.id}</span>
      <span className="truncate text-foreground dark:text-[#f0ede7]">{shell.name ?? t("unnamed")}</span>
      <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
        {formatUptime(runtimeSeconds)}
      </span>
    </>
  );

  if (!validAgent || !validShell) {
    return (
      <li>
        <span className={rowClass}>{content}</span>
      </li>
    );
  }

  return (
    <li>
      <Link
        href={`/shell/${agentId}/${shell.id}`}
        className={`${rowClass} hover:bg-sidebar-accent`}
      >
        {content}
      </Link>
    </li>
  );
}

type InspectorKey = Parameters<ReturnType<typeof useTranslations<"inspector">>>[0];

const SCHEDULE_STATUS_KEY: Readonly<Record<string, InspectorKey | undefined>> = {
  running: "scheduleStatusRunning",
  scheduled: "scheduleStatusScheduled",
  error: "scheduleStatusError",
  stopped: "scheduleStatusStopped",
  completed: "scheduleStatusCompleted",
  cancelled: "scheduleStatusCancelled",
};

function scheduleDotClass(status: string): string {
  if (status === "running") return "bg-sky-500 animate-pulse";
  if (status === "scheduled") return "bg-emerald-500";
  if (status === "error") return "bg-destructive";
  return "bg-muted-foreground/50";
}

function scheduleTriggerLabel(schedule: InspectorScheduleInfo): string {
  if (schedule.trigger_kind === "cron" && schedule.cron_expr) {
    return `cron ${schedule.cron_expr}`;
  }
  if (schedule.trigger_kind === "every" && schedule.interval_seconds != null) {
    return `every ${formatInterval(schedule.interval_seconds)}`;
  }
  if (schedule.trigger_kind === "at" && schedule.run_at) {
    return `at ${schedule.run_at}`;
  }
  return schedule.trigger_kind;
}

export function SchedulesSection({ inspect }: { inspect: AgentInspectLive }) {
  const schedules = inspect.schedules ?? [];
  const now = useNow(1_000, useWallNow());
  const t = useTranslations("inspector");
  if (schedules.length === 0) return null;

  return (
    <Section
      icon={<CalendarClock className="size-3" />}
      title={t("sectionSchedules")}
      badge={String(schedules.length)}
    >
      <ul className="space-y-1">
        {schedules.map((s) => (
          <ScheduleItemRow key={s.id} schedule={s} now={now} />
        ))}
      </ul>
    </Section>
  );
}

function ScheduleItemRow({
  schedule,
  now,
}: {
  schedule: InspectorScheduleInfo;
  now: Date;
}) {
  const t = useTranslations("inspector");
  const { formatFuture } = useTimeFormat();
  const validId = Number.isFinite(schedule.id) && schedule.id > 0;
  const isCluster = schedule.owner_agent_id == null;
  const statusKey = SCHEDULE_STATUS_KEY[schedule.status];
  const statusLabel = statusKey ? t(statusKey) : schedule.status;
  const rightLabel =
    schedule.status === "scheduled" && schedule.next_launch_at != null
      ? formatFuture(schedule.next_launch_at, now)
      : statusLabel;
  const tooltipParts = [
    schedule.name,
    scheduleTriggerLabel(schedule),
    statusLabel,
    schedule.machine,
  ];
  if (isCluster) tooltipParts.push(t("scheduleScopeCluster"));
  const tooltip = tooltipParts.join(" · ");

  const rowClass =
    "flex items-center gap-2 rounded border border-black/[0.06] dark:border-white/[0.08] bg-sidebar-accent/40 dark:bg-[#33322C] px-2.5 py-1.5 font-mono text-[11px] leading-[16px]";
  const content = (
    <>
      <span
        aria-hidden
        data-testid={`schedule-dot-${schedule.status}`}
        className={cn("size-1.5 shrink-0 rounded-full", scheduleDotClass(schedule.status))}
      />
      <span className="truncate text-foreground dark:text-[#f0ede7]">{schedule.name}</span>
      {isCluster ? (
        <span
          data-testid="schedule-scope-cluster"
          className="shrink-0 rounded bg-muted px-1 py-0.5 text-[9.5px] leading-none text-muted-foreground"
        >
          {t("scheduleScopeCluster")}
        </span>
      ) : null}
      <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">{rightLabel}</span>
    </>
  );

  if (!validId) {
    return (
      <li>
        <span className={rowClass} title={tooltip}>
          {content}
        </span>
      </li>
    );
  }

  return (
    <li>
      <Link
        href={`/control/schedules?schedule=${schedule.id}`}
        title={tooltip}
        className={`${rowClass} hover:bg-sidebar-accent`}
      >
        {content}
      </Link>
    </li>
  );
}

const SKILL_LIST_KEYS = new Set([
  "skills_to_inject_into_system_prompt",
  "skills_to_expand_at_start",
]);

function displaySkillName(name: string): string {
  return name === "*" ? name : name.replace(/_/g, "-");
}

// meiva:note
// llm_model is not an override a reader compares — it is the agent's own state, shown in
// Liveness beside its harness (user 2026-10-21). Every other key stays.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
const CONFIG_OVERLAY_HIDDEN_KEYS = new Set(["llm_model"]);

export function ConfigOverlaySection({ inspect }: { inspect: AgentInspectLive }) {
  const entries = Object.entries(inspect.config_overlay).filter(([key]) => !CONFIG_OVERLAY_HIDDEN_KEYS.has(key));
  const t = useTranslations("inspector");
  if (entries.length === 0) return null;

  return (
    <Section icon={<SlidersHorizontal className="size-3" />} title={t("sectionConfigOverlay")}>
      <dl className="space-y-1">
        {entries.map(([k, v]) => (
          <div
            key={k}
            className="rounded border border-black/[0.06] dark:border-white/[0.08] bg-sidebar-accent/40 dark:bg-[#33322C] px-2.5 py-1.5 font-mono text-[11px] leading-[16px]"
          >
            <dt className="break-all text-[10.5px] text-muted-foreground">{k}</dt>
            <dd className="mt-0.5 break-all text-foreground dark:text-[#f0ede7]">{formatValue(SKILL_LIST_KEYS.has(k) && Array.isArray(v) ? v.map(displaySkillName) : v)}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

// meiva:note
// The values are keys of the agent-row catalog this section renders with, so the table is checked
// against that catalog instead of cast at the call site.
// meiva:link ui/web/src/docs/frontend-components/inspector/inspector.md
type AgentRowKey = Parameters<ReturnType<typeof useTranslations<"agentRow">>>[0];

const STATUS_LABEL_KEY: Readonly<Record<string, AgentRowKey | undefined>> = {
  running: "statusRunning",
  idling: "statusIdling",
  terminated: "statusTerminated",
};

export function LivenessSection({
  agentId,
  inspect,
  pending,
  errorMessage,
  fetching,
  onRetry,
}: {
  agentId: number;
  inspect: AgentInspectLive | undefined;
  pending: boolean;
  errorMessage: string;
  fetching: boolean;
  onRetry: () => void;
}) {
  const { data: roster } = useAgentRoster();
  const rosterRow = roster?.agents.find((agent) => agent.agent_id === agentId);
  // meiva:note
  // Which harness runs the agent and which model it was born on belong to the agent's
  // current state, not to its configuration overrides: the title bar stopped naming the
  // harness (user 2026-10-21), and the overlay's llm_model row is hidden below, so the
  // two facts a reader checks first sit together here.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
  const harness = rosterRow?.harness;
  const model = typeof inspect?.config_overlay.llm_model === "string" ? inspect.config_overlay.llm_model : undefined;
  const offline = inspect?.liveness_state === "offline";
  const t = useTranslations("inspector");
  const tStatus = useTranslations("agentRow");
  const now = useNow(1_000, useWallNow());
  const { formatAbsolute, formatFuture } = useTimeFormat();
  const next = inspect
    ? nextHeartbeatTime(inspect.heartbeat, now, formatFuture)
    : undefined;
  const lastPause = inspect?.heartbeat.last_pause;
  const status = inspect?.status;
  const statusKey = status !== undefined ? STATUS_LABEL_KEY[status] : undefined;
  return (
    <Section
      icon={<HeartPulse className={cn("size-3", offline && "text-destructive")} />}
      title={t("sectionLiveness")}
      loading={!inspect && pending}
    >
      {!inspect ? (
        pending ? null : (
          <InspectReadError
            message={errorMessage}
            retryLabel={t("retryInspector")}
            pending={fetching}
            onRetry={onRetry}
          />
        )
      ) : (
        <div className="grid grid-cols-2 gap-1">
          <Metric
            className="col-span-2"
            label={t("metricBirth")}
            value={`${formatRelative(inspect.spawned_at, now, { capitalize: true })}, ${formatAbsolute(inspect.spawned_at)}`}
          />
          <Metric label={t("metricNextHeartbeat")} value={next ?? "—"} />
          <Metric
            label={t("metricLastPause")}
            value={
              lastPause
                ? `${formatRelative(lastPause.at, now, { capitalize: true })} · ${formatInterval(Math.round(lastPause.duration_s))}`
                : t("neverPaused")
            }
          />
          <Metric label={t("metricHarness")} value={harness ? harnessLabel(harness) : "—"} />
          <Metric label={t("metricModel")} value={model === undefined ? "—" : modelLabel(model)} />
          <Metric label={t("metricMachine")} value={inspect.machine} />
          <Metric
            label={t("metricStatus")}
            value={statusKey ? tStatus(statusKey) : inspect.status}
          />
        </div>
      )}
    </Section>
  );
}

// meiva:note
// The next-heartbeat cell answers one question — when does the heartbeat look at this agent
// again — and prints only that instant: no `paused` / `pending` / `due` qualifier rides beside
// it (user ruling 2026-11-08). An active pause is the daemon's floor whatever the agent's
// status is, so its end is the answer while it runs; otherwise the projected check-in is. The
// value follows the reader's `display.date_format` ("Tomorrow 15:21" or "in 22h"), absolute by
// default (user 2026-10-29). With neither instant there is nothing to print but an em dash.
// meiva:link ui/web/src/docs/frontend-components/inspector/section-details/liveness.md
function nextHeartbeatTime(
  hb: HeartbeatInfo,
  now: Date,
  formatFuture: (iso: string, now: Date) => string,
): string {
  const pausedUntil = hb.paused_until ?? null;
  const pausedNow = pausedUntil !== null && new Date(pausedUntil).getTime() > now.getTime();
  const at = pausedNow ? pausedUntil : (hb.next_at ?? null);
  return at === null ? "—" : formatFuture(at, now);
}

function formatValue(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}
