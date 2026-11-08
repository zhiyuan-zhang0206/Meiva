"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Pause,
  Pencil,
  Play,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { PythonCode, usePythonCodeHighlighter } from "@/components/content/python-code";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Input } from "@/components/ui/input";
import { TableCell, TableRow } from "@/components/ui/table";
import { errMsg } from "@/lib/contracts/errors";
import type { ScheduleCreate, ScheduleSummary, ScheduleUpdate } from "@/lib/contracts/types";
import { useWallDate } from "@/lib/format/now/use-wall-date";
import { formatRelative } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";
import { useStore } from "@/lib/state/store";

import { useSectionVisible } from "../visibility";
import { useApi } from "@/lib/transport/transport-context";

function schedulePresentation(schedule: ScheduleSummary) {
  if (schedule.placement_status !== "ok") return { label: "needsAttention", tone: "bg-destructive/10 text-destructive" } as const;
  if (schedule.status === "error") return { label: "needsAttention", tone: "bg-destructive/10 text-destructive" } as const;
  if (schedule.status === "completed") return { label: "completedState", tone: "bg-muted text-muted-foreground" } as const;
  if (!schedule.enabled && schedule.status === "running") return { label: "stopUnconfirmed", tone: "bg-amber-500/10 text-amber-700 dark:text-amber-300" } as const;
  if (!schedule.enabled && schedule.status === "stopped") return { label: "stoppedState", tone: "bg-muted text-muted-foreground" } as const;
  if (schedule.enabled && schedule.status === "running") return { label: "enabledState", tone: "bg-blue-500/10 text-blue-700 dark:text-blue-300" } as const;
  if (schedule.enabled && schedule.status === "stopped") return { label: "startUnconfirmed", tone: "bg-amber-500/10 text-amber-700 dark:text-amber-300" } as const;
  return { label: "unknownState", tone: "bg-muted text-muted-foreground" } as const;
}

export function ScheduleRow({
  schedule,
  expanded,
  onExpand,
  onEnsureExpanded,
  onToggle,
  onRestart,
  onDelete,
  onEdited,
  pending,
}: {
  schedule: ScheduleSummary;
  expanded: boolean;
  onExpand: () => void;
  onEnsureExpanded: () => void;
  onToggle: () => void;
  onRestart: () => void;
  onDelete: () => void;
  onEdited: () => void;
  pending: boolean;
}) {
  const t = useTranslations("schedules");
  const now = useWallDate();
  const highlighter = usePythonCodeHighlighter();
  const [editing, setEditing] = useState(false);
  const presentation = schedulePresentation(schedule);

  return (
    <>
      <TableRow>
        <TableCell className="pr-0">
          <button
            type="button"
            onClick={onExpand}
            onPointerEnter={() => void highlighter.preload()}
            onFocus={() => void highlighter.preload()}
            className="text-muted-foreground hover:text-foreground"
            aria-label={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </button>
        </TableCell>
        <TableCell>
          <span className={cn("inline-flex whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium", presentation.tone)}>
            {pending ? t("updatingState") : t(presentation.label)}
          </span>
        </TableCell>
        <TableCell className="text-sm font-medium">
          {schedule.name}
        </TableCell>
        <TableCell
          className="text-xs max-w-[280px] truncate text-muted-foreground"
          title={schedule.description ?? undefined}
        >
          {schedule.description ?? "—"}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {formatRelative(schedule.updated_at, now, { capitalize: true })}
        </TableCell>
        <TableCell>
          <div className={cn("items-center gap-0.5", FLEX)}>
            <IconButton
              label={schedule.enabled ? t("stopAction") : t("startAction")}
              disabled={pending}
              onClick={onToggle}
              icon={
                schedule.enabled ? (
                  <Pause className="size-3.5" />
                ) : (
                  <Play className="size-3.5" />
                )
              }
            />
            <IconButton label={t("restart")} disabled={pending || !schedule.enabled} onClick={onRestart} icon={<RotateCcw className="size-3.5" />} />
            <IconButton
              label={t("edit")}
              disabled={pending}
              onClick={() => {
                onEnsureExpanded();
                setEditing(true);
              }}
              icon={<Pencil className="size-3.5" />}
            />
            <IconButton label={t("delete")} disabled={pending} onClick={onDelete} icon={<Trash2 className="size-3.5" />} />
          </div>
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow>
          <TableCell colSpan={6} className="bg-muted/30">
            <ScheduleDetail
              id={schedule.id}
              editing={editing}
              onDoneEditing={() => {
                setEditing(false);
                onEdited();
              }}
            />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function ScheduleDetail({
  id,
  editing,
  onDoneEditing,
}: {
  id: number;
  editing: boolean;
  onDoneEditing: () => void;
}) {
  const client = useApi();
  const t = useTranslations("schedules");
  const now = useWallDate();
  const showToast = useStore((s) => s.showToast);
  const visible = useSectionVisible();
  const full = useQuery({
    queryKey: ["schedule", id],
    queryFn: () => client.getSchedule(id),
    enabled: visible,
    staleTime: 0,
  });
  const logs = useQuery({
    queryKey: ["schedule-logs", id],
    queryFn: () => client.scheduleLogs(id),
    refetchInterval: 15_000,
    enabled: visible,
    staleTime: 0,
  });
  const runs = useQuery({
    queryKey: ["schedule-runs", id],
    queryFn: () => client.scheduleRuns(id),
    enabled: visible,
    staleTime: 0,
  });

  const [edit, setEdit] = useState<ScheduleUpdate | null>(null);
  const updateMutation = useMutation({
    mutationFn: (body: ScheduleUpdate) => client.updateSchedule(id, body),
    onSuccess: () => {
      showToast("Schedule updated");
      onDoneEditing();
    },
    onError: (err: unknown) => showToast(`Update failed: ${errMsg(err)}`),
  });

  if (full.isLoading || !full.data) {
    return <Spinner className="my-3 mx-auto size-4" />;
  }
  const s = full.data;

  const editForm: ScheduleUpdate = edit ?? {
    name: s.name,
    description: s.description,
    script: s.script,
    command: s.command,
  };

  return (
    <div className="space-y-3 py-2 text-xs">
      <p className="text-muted-foreground">{t("reportedState", { state: s.status, machine: s.machine })}</p>
      {editing ? (
        <div className="rounded-md border border-border p-3 space-y-2">
          <label htmlFor={`schedule-edit-desc-${String(s.id)}`} className="block text-muted-foreground">
            Description
            <Input
              id={`schedule-edit-desc-${String(s.id)}`}
              className="mt-1 h-8 text-xs"
              value={editForm.description ?? ""}
              onChange={(e) => setEdit({ ...editForm, description: e.target.value || null })}
            />
          </label>
          <label className="block text-muted-foreground">
            Script
            <textarea
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              data-form-type="other"
              data-1p-ignore=""
              data-lpignore="true"
              className="mt-1 w-full rounded border border-border bg-background p-2 font-mono text-xs pointer-coarse:text-base"
              rows={12}
              value={editForm.script ?? ""}
              onChange={(e) => setEdit({ ...editForm, script: e.target.value })}
            />
          </label>
          <div className={cn("items-center gap-2", FLEX)}>
            <Button
              type="button"
              size="sm"
              disabled={updateMutation.isPending}
              onClick={() => updateMutation.mutate(editForm)}
            >
              {updateMutation.isPending ? <Spinner className="mr-1 size-3.5" /> : null}
              Save
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onDoneEditing}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <>
          {s.last_error && (
            <div>
              <div className="font-medium text-destructive">{t("lastError")}</div>
              <pre className="mt-1 max-h-40 overflow-auto rounded bg-background p-2 font-mono text-[11px] text-destructive">
                {s.last_error}
              </pre>
            </div>
          )}
          <div>
            <div className="font-medium text-muted-foreground">{t("script")}</div>
            <div className="mt-1 max-h-64 overflow-auto rounded bg-background p-2">
              <PythonCode code={s.script} />
            </div>
          </div>
          <div>
            <div className="font-medium text-muted-foreground">
              Logs {logs.data ? `(${logs.data.source})` : ""}
            </div>
            <pre className="mt-1 max-h-48 overflow-auto rounded bg-background p-2 font-mono text-[11px] text-muted-foreground">
              {logs.data && logs.data.lines.length > 0 ? logs.data.lines.join("\n") : "—"}
            </pre>
          </div>
          <div>
            <div className="font-medium text-muted-foreground">
              Run history ({runs.data?.length ?? 0})
            </div>
            {runs.data && runs.data.length > 0 ? (
              <ul className="mt-1 space-y-0.5 text-muted-foreground">
                {runs.data.map((r) => (
                  <li key={r.id}>
                    {formatRelative(r.ran_at, now, { capitalize: true })} · {r.ok === null ? "…" : r.ok ? "✓" : "✗"}
                    {r.note ? ` · ${r.note}` : ""}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-muted-foreground/60">{t("noRuns")}</p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export function ScheduleForm({
  title,
  value,
  onChange,
  submitting,
  submitLabel,
  canSubmit,
  onSubmit,
  onCancel,
}: {
  title: string;
  value: ScheduleCreate;
  onChange: (v: ScheduleCreate) => void;
  submitting: boolean;
  submitLabel: string;
  canSubmit: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations("schedules");
  return (
    <div className="rounded-md border border-border p-4 space-y-3">
      <h4 className="text-xs font-medium text-muted-foreground">{title}</h4>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="text-xs">
          {t("nameRequired")}
          <Input
            autoCorrect="off"
            autoCapitalize="none"
            spellCheck={false}
            className="mt-1 h-8 text-xs font-mono"
            placeholder="memory-arbiter"
            value={value.name}
            onChange={(e) => onChange({ ...value, name: e.target.value })}
          />
        </label>
        <label className="text-xs">
          {t("command")}
          <Input
            autoCorrect="off"
            autoCapitalize="none"
            spellCheck={false}
            className="mt-1 h-8 text-xs font-mono"
            placeholder="python schedule.py"
            value={value.command}
            onChange={(e) => onChange({ ...value, command: e.target.value })}
          />
        </label>
        <label className="text-xs sm:col-span-2">
          {t("description")}
          <Input
            className="mt-1 h-8 text-xs"
            placeholder={t("whatThisDoes")}
            value={value.description ?? ""}
            onChange={(e) => onChange({ ...value, description: e.target.value || null })}
          />
        </label>
        <label className="text-xs sm:col-span-2">
          {t("scriptRequired")}
          <textarea
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="none"
            spellCheck={false}
            data-form-type="other"
            data-1p-ignore=""
            data-lpignore="true"
            className="mt-1 w-full rounded border border-border bg-background p-2 font-mono text-xs pointer-coarse:text-base"
            rows={12}
            placeholder={"import time\nwhile True:\n    ...\n    time.sleep(60)"}
            value={value.script}
            onChange={(e) => onChange({ ...value, script: e.target.value })}
          />
        </label>
      </div>
      <div className={cn("items-center gap-2", FLEX)}>
        <Button type="button" size="sm" disabled={!canSubmit || submitting} onClick={onSubmit}>
          {submitting ? <Spinner className="mr-1 size-3.5" /> : null}
          {submitLabel}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function IconButton({
  label,
  onClick,
  icon,
  disabled,
}: {
  label: string;
  onClick: () => void;
  icon: React.ReactNode;
  disabled: boolean;
}) {
  return (
    <Button
      type="button"
      size="icon"
      variant="ghost"
      className="size-7 text-muted-foreground"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
    >
      {icon}
    </Button>
  );
}
