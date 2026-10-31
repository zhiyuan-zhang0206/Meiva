"use client";

import { modelLabel } from "@/lib/contracts/models";

import { useQueryClient } from "@tanstack/react-query";
import {
  Bell,
  ExternalLink,
  HeartPulse,
  LayoutPanelTop,
  SlidersHorizontal,
  Terminal,
} from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";

import { Metric, Section } from "@/components/inspector/inspector-section";
import { OpenNoticeDetail } from "@/components/notifications/open-notice-detail";
import { useAgentRoster } from "@/lib/agents/use-agents";
import type {
  AgentInspectLive,
  HeartbeatInfo,
  OpenNotice,
  PageRow,
  ShellInfo,
} from "@/lib/contracts/types";
import { formatRelative, formatUptime } from "@/lib/format/time";
import { useNow } from "@/lib/format/now/use-now";
import { useTimeFormat } from "@/lib/format/use-time-format";
import { cn } from "@/lib/format/utils";
import { harnessLabel } from "@/lib/harness/harness";
import { inspectLiveQueryKey } from "@/lib/inspector/inspector-queries";
import { fleetNoticeHref } from "@/lib/inspector/inspector-widgets";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import { useWallNow } from "@/lib/transport/transport-context";

import { formatInterval } from "./inspector-cost-section";

export function PageSection({ pages }: { pages: PageRow[] }) {
  const t = useTranslations("inspector");
  if (pages.length === 0) return null;

  return (
    <Section icon={<LayoutPanelTop className="size-3" />} title={t("sectionPage")}>
      {pages.map((p) => (
        <a
          key={p.name}
          href={p.url}
          target="_blank"
          rel="noopener noreferrer"
          className={cn("items-center gap-2 rounded bg-sidebar-accent/40 px-2 py-1.5 font-mono text-[11px] hover:bg-sidebar-accent group", FLEX)}
        >
          <ExternalLink className="size-3 shrink-0 text-muted-foreground group-hover:text-foreground" />
          <span className={cn(FLEX, FLEX_COL, MIN_W_0, FLEX_1)}>
            <span className="truncate text-foreground">{p.title ?? p.name}</span>
            <span className="truncate text-[10px] text-muted-foreground">
              {p.url}
            </span>
          </span>
        </a>
      ))}
    </Section>
  );
}

export function NoticeReplySection({
  agentId,
  notice,
}: {
  agentId: number;
  notice: OpenNotice;
}) {
  const queryClient = useQueryClient();
  const t = useTranslations("inspector");
  return (
    <Section
      icon={<Bell className="size-3" />}
      title={t("sectionNotice")}
      action={
        <Link
          href={fleetNoticeHref(notice.id)}
          className="inline-flex items-center gap-1 hover:text-foreground"
        >
          <ExternalLink className="size-3" aria-hidden />
          {t("jumpNotice")}
        </Link>
      }
    >
      <OpenNoticeDetail
        key={notice.id}
        agentId={agentId}
        notice={notice}
        showTimestamp
        onResolved={() => {
          void queryClient.invalidateQueries({ queryKey: inspectLiveQueryKey(agentId) });
        }}
      />
    </Section>
  );
}

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
        <p className="font-mono text-[11px] text-muted-foreground">Shell observation unavailable</p>
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
    "flex items-center gap-2 rounded bg-sidebar-accent/40 px-2 py-1 font-mono text-[11px]";
  const content = (
    <>
      <span className="tabular-nums text-muted-foreground">#{shell.id}</span>
      <span className="truncate text-foreground">{shell.name ?? t("unnamed")}</span>
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
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
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
            className="rounded bg-sidebar-accent/40 px-2 py-1 font-mono text-[11px]"
          >
            <dt className="break-all text-muted-foreground">{k}</dt>
            <dd className="mt-0.5 break-all text-foreground">{formatValue(SKILL_LIST_KEYS.has(k) && Array.isArray(v) ? v.map(displaySkillName) : v)}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

// meiva:note
// The values are keys of the agent-row catalog this section renders with, so the table is checked
// against that catalog instead of cast at the call site.
// meiva:link ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md
type AgentRowKey = Parameters<ReturnType<typeof useTranslations<"agentRow">>>[0];

const STATUS_LABEL_KEY: Readonly<Record<string, AgentRowKey | undefined>> = {
  running: "statusRunning",
  idling: "statusIdling",
  impersonated: "statusImpersonated",
  terminated: "statusTerminated",
};

export function LivenessSection({ inspect }: { inspect: AgentInspectLive }) {
  const { liveness_state: state, heartbeat, spawned_at } = inspect;
  const { data: roster } = useAgentRoster();
  const rosterRow = roster?.agents.find((agent) => agent.agent_id === inspect.agent_id);
  // meiva:note
  // Which harness runs the agent and which model it was born on belong to the agent's
  // current state, not to its configuration overrides: the title bar stopped naming the
  // harness (user 2026-10-21), and the overlay's llm_model row is hidden below, so the
  // two facts a reader checks first sit together here.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  const harness = rosterRow?.harness;
  const model = typeof inspect.config_overlay.llm_model === "string" ? inspect.config_overlay.llm_model : undefined;
  const offline = state === "offline";
  const t = useTranslations("inspector");
  const tStatus = useTranslations("agentRow");
  const now = useNow(1_000, useWallNow());
  const { formatAbsolute, formatFuture } = useTimeFormat();
  const next = nextHeartbeatCell(
    heartbeat,
    {
      pending: t("pending"),
      due: t("due"),
      paused: (time) => t("paused", { time }),
    },
    now,
    formatFuture,
  );
  const lastPause = heartbeat.last_pause;
  const impersonated = inspect.status !== "terminated" && rosterRow?.status === "impersonated";
  const status = impersonated ? "impersonated" : inspect.status;
  const statusKey = STATUS_LABEL_KEY[status];
  return (
    <Section icon={<HeartPulse className={cn("size-3", offline && "text-destructive")} />} title={t("sectionLiveness")}>
      <div className="grid grid-cols-2 gap-1">
        <Metric
          className="col-span-2"
          label={t("metricBirth")}
          value={`${formatRelative(spawned_at, now, { capitalize: true })}, ${formatAbsolute(spawned_at)}`}
        />
        <Metric label={t("metricNextHeartbeat")} value={next.value} />
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
          value={statusKey ? tStatus(statusKey) : status}
        />
      </div>
    </Section>
  );
}

// meiva:note
// The next-heartbeat cell answers "when does the heartbeat look at this agent again", so an
// active pause still says so instead of printing a bare time that reads like a scheduled
// check-in — but on one line: the relative span that used to sit under the value read as a
// second, smaller line, so the value itself now follows the reader's `display.date_format`
// ("Tomorrow 15:21" or "in 22h"), absolute by default (user 2026-10-29). The pause is the
// daemon's floor whatever the agent's status is — it stays the answer while the agent is busy
// (user report 2026-10-28). A pause that ran out between the last read and this tick keeps the
// cell's "due" wording instead of counting down at zero.
// meiva:link ui/web/src/docs/frontend-components/inspector/section-details/liveness.meiva.okf.md
function nextHeartbeatCell(
  hb: HeartbeatInfo,
  labels: {
    pending: string;
    due: string;
    paused: (time: string) => string;
  },
  now: Date,
  formatFuture: (iso: string, now: Date) => string,
): { value: string } {
  if (hb.paused_until) {
    const until = new Date(hb.paused_until).getTime();
    if (until <= now.getTime()) return { value: labels.due };
    return { value: labels.paused(formatFuture(hb.paused_until, now)) };
  }
  if (hb.heartbeat_pending) return { value: labels.pending };
  if (hb.next_at) {
    return {
      value:
        new Date(hb.next_at).getTime() <= now.getTime()
          ? labels.due
          : formatFuture(hb.next_at, now),
    };
  }
  return { value: "—" };
}

function formatValue(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}
