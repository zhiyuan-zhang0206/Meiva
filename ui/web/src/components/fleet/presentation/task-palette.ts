// meiva:note
// Task rendering, chrome and contracts share these module-owned palette records and legend values.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md

import type { TaskStatus, TaskSummaryRow } from "@/lib/contracts/types";

export const STATUS_FILL: Record<TaskStatus, string> = {
  in_progress: "text-slate-400",
  done: "text-emerald-500",
  cancelled: "text-destructive",
};

export const ROOT_NODE_STATUS = "root";
export const ROOT_FILL = "text-violet-500";

export const NODE_FILL: Record<string, string> = {
  ...STATUS_FILL,
  [ROOT_NODE_STATUS]: ROOT_FILL,
};

export function taskFill(task: TaskSummaryRow): string {
  return task.parent_id == null ? ROOT_FILL : STATUS_FILL[task.status];
}

export const TASK_LEGEND_ENTRIES = [
  { key: "in_progress", fill: STATUS_FILL.in_progress, labelKey: "status.inProgress" },
  { key: ROOT_NODE_STATUS, fill: ROOT_FILL, labelKey: "root" },
] as const;
