"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Loader2,
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
import { Input } from "@/components/ui/input";
import { TableCell, TableRow } from "@/components/ui/table";
import { errMsg } from "@/lib/contracts/errors";
import type { ScheduleCreate, ScheduleSummary, ScheduleUpdate } from "@/lib/contracts/types";
import { formatRelative } from "@/lib/format/time";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";
import { useStore } from "@/lib/state/store";

import { useSectionVisible } from "../visibility";
import { useApi } from "@/lib/transport/transport-context";

const STATUS_DOT: Record<string, string> = {
  running: "bg-green-500",
  stopped: "bg-muted-foreground/40",
  error: "bg-red-500",
  completed: "bg-blue-500",
};

export function ScheduleRow({
  schedule,
  expanded,
  onExpand,
  onEnsureExpanded,
  onToggle,
  onRestart,
  onDelete,
  onEdited,
}: {
  schedule: ScheduleSummary;
  expanded: boolean;
  onExpand: () => void;
  onEnsureExpanded: () => void;
  onToggle: () => void;
  onRestart: () => void;
  onDelete: () => void;
  onEdited: () => void;
}) {
  const t = useTranslations("schedules");
  const highlighter = usePythonCodeHighlighter();
  const [editing, setEditing] = useState(false);

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
          <span className={cn("items-center gap-1.5 text-xs text-muted-foreground", FLEX)}>
            <span
              className={`inline-block size-2 rounded-full ${STATUS_DOT[schedule.status] ?? "bg-muted-foreground/40"}`}
            />
            {schedule.status}
          </span>
        </TableCell>
        <TableCell className="font-mono text-xs font-medium">
          {schedule.name}
          {!schedule.enabled && (
            <span className="ml-1.5 text-muted-foreground/60">{t("off")}</span>
          )}
        </TableCell>
        <TableCell
          className="text-xs max-w-[280px] truncate text-muted-foreground"
          title={schedule.description ?? undefined}
        >
          {schedule.description ?? "—"}
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {formatRelative(schedule.updated_at, new Date(), { capitalize: true })}
        </TableCell>
        <TableCell>
          <div className={cn("items-center gap-0.5", FLEX)}>
            <IconButton
              label={schedule.enabled ? "Stop" : "Start"}
              onClick={onToggle}
              icon={
                schedule.enabled ? (
                  <Pause className="size-3.5" />
                ) : (
                  <Play className="size-3.5" />
                )
              }
            />
            <IconButton label={t("restart")} onClick={onRestart} icon={<RotateCcw className="size-3.5" />} />
            <IconButton
              label={t("edit")}
              onClick={() => {
                onEnsureExpanded();
                setEditing(true);
              }}
              icon={<Pencil className="size-3.5" />}
            />
            <IconButton label={t("delete")} onClick={onDelete} icon={<Trash2 className="size-3.5" />} />
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
    return <Loader2 className="size-4 animate-spin text-muted-foreground my-3 mx-auto" />;
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
              className="mt-1 w-full rounded border border-border bg-background p-2 font-mono text-xs"
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
              {updateMutation.isPending ? <Loader2 className="size-3.5 animate-spin mr-1" /> : null}
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
              <div className="font-medium text-red-500">{t("lastError")}</div>
              <pre className="mt-1 max-h-40 overflow-auto rounded bg-background p-2 font-mono text-[11px] text-red-400">
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
                    {formatRelative(r.ran_at, new Date(), { capitalize: true })} · {r.ok === null ? "…" : r.ok ? "✓" : "✗"}
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
      <div className="grid grid-cols-2 gap-3">
        <label className="text-xs">
          {t("nameRequired")}
          <Input
            className="mt-1 h-8 text-xs font-mono"
            placeholder="memory-arbiter"
            value={value.name}
            onChange={(e) => onChange({ ...value, name: e.target.value })}
          />
        </label>
        <label className="text-xs">
          {t("command")}
          <Input
            className="mt-1 h-8 text-xs font-mono"
            placeholder="python schedule.py"
            value={value.command}
            onChange={(e) => onChange({ ...value, command: e.target.value })}
          />
        </label>
        <label className="text-xs col-span-2">
          {t("description")}
          <Input
            className="mt-1 h-8 text-xs"
            placeholder={t("whatThisDoes")}
            value={value.description ?? ""}
            onChange={(e) => onChange({ ...value, description: e.target.value || null })}
          />
        </label>
        <label className="text-xs col-span-2">
          {t("scriptRequired")}
          <textarea
            className="mt-1 w-full rounded border border-border bg-background p-2 font-mono text-xs"
            rows={12}
            placeholder={"import time\nwhile True:\n    ...\n    time.sleep(60)"}
            value={value.script}
            onChange={(e) => onChange({ ...value, script: e.target.value })}
          />
        </label>
      </div>
      <div className={cn("items-center gap-2", FLEX)}>
        <Button type="button" size="sm" disabled={!canSubmit || submitting} onClick={onSubmit}>
          {submitting ? <Loader2 className="size-3.5 animate-spin mr-1" /> : null}
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
}: {
  label: string;
  onClick: () => void;
  icon: React.ReactNode;
}) {
  return (
    <Button
      type="button"
      size="icon"
      variant="ghost"
      className="size-7 text-muted-foreground"
      onClick={onClick}
      aria-label={label}
    >
      {icon}
    </Button>
  );
}
