"use client";

import { useQuery } from "@tanstack/react-query";
import { CheckCheck, EyeOff } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { WindowSelect, type WindowOption } from "@/components/agents/window-select";
import { ChatMarkdown } from "@/components/content/markdown";
import type { TaskSummaryRow } from "@/lib/contracts/types";
import { useWallDate } from "@/lib/format/now/use-wall-date";
import { formatRelative, formatUptime } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import { PRIORITY_BG } from "@/lib/notifications/notices";
import type { TaskWindow } from "@/lib/notifications/use-tasks";

import { taskFill } from "./presentation/task-palette";
import { useApi } from "@/lib/transport/transport-context";

function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={MIN_W_0}>
      <p className="text-[9px] font-medium uppercase tracking-wider text-muted-foreground/60">
        {label}
      </p>
      <p className="truncate text-[11px] text-popover-foreground">{children}</p>
    </div>
  );
}

export function TaskHoverCard({
  task,
  ownerLabel,
  parentTitle,
}: {
  task: TaskSummaryRow;
  ownerLabel: string;
  parentTitle: string | null;
}) {
  const client = useApi();
  const t = useTranslations("fleet.task");
  const now = useWallDate();
  // meiva:note
  // The list carries no text columns; the card fetches the one row it shows and
  // renders its description/result once it lands. A failed read says so in one
  // line instead of an empty card.
  // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
  const full = useQuery({
    queryKey: ["task", task.id],
    queryFn: () => client.getTask(task.id),
    staleTime: Infinity,
  });
  const statusLabels: Record<string, string> = {
    in_progress: t("status.inProgress"),
    done: t("status.done"),
    cancelled: t("status.canceled"),
  };
  const creator =
    task.created_by === "user"
      ? t("user")
      : task.created_by === "system"
        ? t("system")
        : t("agent", { id: task.created_by });
  const reminder =
    task.remind_interval_seconds == null
      ? "—"
      : t("every", { duration: formatUptime(task.remind_interval_seconds) });
  return (
    <div className="w-80 max-w-[80vw] rounded-lg border border-border bg-popover/95 p-3 shadow-xl backdrop-blur">
      <div className={cn(FLEX, "items-start gap-2")}>
        <span
          className={cn("mt-1 size-2 shrink-0 rounded-full bg-current", taskFill(task))}
        />
        <div className={cn(MIN_W_0, FLEX_1)}>
          <p className="break-words text-xs font-semibold leading-snug text-popover-foreground">
            {task.title}
          </p>
          <p className="mt-0.5 text-[10px] tabular-nums text-muted-foreground">
            {t("task", { id: task.id })}
          </p>
        </div>
        <span
          className={cn(
            "shrink-0 self-start rounded px-1 py-0.5 text-[9px] font-bold leading-none text-white",
            PRIORITY_BG[task.priority],
          )}
        >
          {task.priority}
        </span>
      </div>

      <div className="mt-2.5 grid grid-cols-2 gap-x-3 gap-y-1.5">
        <MetaRow label={t("meta.status")}>{statusLabels[task.status] ?? task.status}</MetaRow>
        <MetaRow label={t("meta.priority")}>{task.priority}</MetaRow>
        <MetaRow label={t("meta.owner")}>{ownerLabel}</MetaRow>
        <MetaRow label={t("meta.createdBy")}>{creator}</MetaRow>
        <MetaRow label={t("meta.parent")}>{parentTitle ?? "—"}</MetaRow>
        <MetaRow label={t("meta.reminder")}>{reminder}</MetaRow>
        <MetaRow label={t("meta.created")}>{formatRelative(task.created_at, now, { capitalize: true })}</MetaRow>
        <MetaRow label={t("meta.updated")}>{formatRelative(task.updated_at, now, { capitalize: true })}</MetaRow>
      </div>

      {task.reminder_count > 0 || task.last_reminded_at != null ? (
        <p className="mt-1.5 text-[10px] text-muted-foreground/70">
          {t("reminders", { count: task.reminder_count })}
          {task.last_reminded_at != null ? ` · ${t("lastReminder", { time: formatRelative(task.last_reminded_at, now) })}` : ""}
        </p>
      ) : null}

      {full.data?.description ? (
        <div className="mt-2 border-t border-border pt-2">
          <p className="text-[9px] font-medium uppercase tracking-wider text-muted-foreground/60">
            {t("meta.description")}
          </p>
          <div className="mt-0.5 break-words text-[11px] leading-snug text-popover-foreground/90">
            <ChatMarkdown content={full.data.description} />
          </div>
        </div>
      ) : null}
      {full.data?.results ? (
        <div className="mt-2 border-t border-border pt-2">
          <p className="text-[9px] font-medium uppercase tracking-wider text-muted-foreground/60">
            {t("meta.result")}
          </p>
          <div className="mt-0.5 break-words text-[11px] leading-snug text-popover-foreground/90">
            <ChatMarkdown content={full.data.results} />
          </div>
        </div>
      ) : null}

      {full.isError ? (
        <p data-testid="task-detail-failed" className="mt-2 border-t border-border pt-2 text-[10px] text-muted-foreground">
          {t("fullFailed")}
        </p>
      ) : null}

      <p className="mt-2 border-t border-border pt-1.5 text-[9px] text-muted-foreground/60">
        {t("openOwnerConversation")}
      </p>
    </div>
  );
}

export const TASK_FORCE_KEY = "display.task_force_params.v2";

function StatusToggleButton({
  active,
  onClick,
  label,
  hiddenCount,
  icon,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  hiddenCount: number;
  icon: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
        "border transition-colors select-none",
        active
          ? "border-border bg-accent text-accent-foreground"
          : "border-transparent text-muted-foreground/50 hover:text-muted-foreground hover:border-border",
      )}
    >
      {icon}
      <span>{label}</span>
      {!active && hiddenCount > 0 && (
        <span className="rounded-full bg-muted px-1 text-[9px] tabular-nums text-muted-foreground">
          {hiddenCount}
        </span>
      )}
    </button>
  );
}

export function StaleBadge({ show }: { show: boolean }) {
  const t = useTranslations("fleet.task");
  if (!show) return null;
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] text-amber-600 dark:text-amber-400"
    >
      <span className="size-1.5 rounded-full bg-amber-500" />
      {t("stale")}
    </span>
  );
}

const TASK_WINDOW_VALUES: readonly TaskWindow[] = ["24h", "7d", "30d", "all"];

export function FilterCluster({
  taskWindow,
  setTaskWindow,
  showDone,
  setShowDone,
  hiddenDoneCount,
  showCanceled,
  setShowCanceled,
  hiddenCanceledCount,
}: {
  taskWindow: TaskWindow;
  setTaskWindow: (w: TaskWindow) => void;
  showDone: boolean;
  setShowDone: (v: boolean) => void;
  hiddenDoneCount: number;
  showCanceled: boolean;
  setShowCanceled: (v: boolean) => void;
  hiddenCanceledCount: number;
}) {
  const t = useTranslations("fleet.task");
  const taskWindowOptions: readonly WindowOption[] = TASK_WINDOW_VALUES.map((value) => ({
    value,
    label: value === "all" ? t("all") : value,
  }));
  return (
    <div className={cn("ml-auto flex-wrap items-center gap-1", FLEX)}>
      <WindowSelect
        value={taskWindow}
        options={taskWindowOptions}
        onChange={(v) => {
          const next = TASK_WINDOW_VALUES.find((window) => window === v);
          if (next !== undefined) setTaskWindow(next);
        }}
        ariaLabel={t("window")}
        className="cursor-pointer rounded border border-border bg-background/80 px-1.5 py-0.5 text-[10px] text-muted-foreground backdrop-blur hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      />
      <StatusToggleButton
        active={showDone}
        onClick={() => setShowDone(!showDone)}
        label={t("status.done")}
        hiddenCount={showDone ? 0 : hiddenDoneCount}
        icon={<CheckCheck className="size-3" />}
      />
      <StatusToggleButton
        active={showCanceled}
        onClick={() => setShowCanceled(!showCanceled)}
        label={t("status.canceled")}
        hiddenCount={showCanceled ? 0 : hiddenCanceledCount}
        icon={<EyeOff className="size-3" />}
      />
    </div>
  );
}
