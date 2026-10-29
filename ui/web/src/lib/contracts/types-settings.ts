import type { components } from "./types-generated";

type Schemas = components["schemas"];

export const COLOR_FAMILIES = [
  "emerald",
  "teal",
  "cyan",
  "sky",
  "blue",
  "indigo",
  "violet",
  "fuchsia",
  "pink",
  "rose",
  "red",
  "orange",
  "amber",
  "gray",
] as const;

export type ColorFamily = (typeof COLOR_FAMILIES)[number];

export interface FamilyClasses {
  readonly border500_50: string;
  readonly border500_70: string;
  readonly border500_40: string;
  readonly border400_40: string;
  readonly border400_50: string;
  readonly border400_60: string;
  readonly bg50_40_950_15: string;
  readonly bg50_60_900_20: string;
  readonly bg50_40_950_10: string;
  readonly bg50_950_20: string;
  readonly bg50_900_30: string;
  readonly bg50_950_30: string;
  readonly text700_300: string;
}

export type ColorShape = keyof FamilyClasses;

export const COLOR_SLOT_IDS = [
  "agent_chat",
  "agent_code",
  "code_output",
  "reasoning",
  "inbound_human",
  "inbound_agent",
  "inbound_system",
  "system_prompt",
  "attach",
  "note",
  "memory",
  "lifecycle_terminate",
  "lifecycle_restart",
  "lifecycle_resurrect",
  "lifecycle_fork",
] as const;

export type ColorSlotId = (typeof COLOR_SLOT_IDS)[number];

export interface ColorItemSpec {

  readonly key: string;
  readonly default: ColorFamily;
  readonly border: ColorShape;
  readonly bg: ColorShape | null;
  readonly text: ColorShape | null;
}

export const TIMELINE_COLOR_ITEMS: Record<ColorSlotId, ColorItemSpec> = {
  agent_chat: { key: "display.color.agent_chat", default: "emerald", border: "border500_50", bg: null, text: null },
  agent_code: { key: "display.color.agent_code", default: "cyan", border: "border500_70", bg: null, text: null },
  code_output: { key: "display.color.code_output", default: "amber", border: "border500_50", bg: "bg50_40_950_10", text: null },
  reasoning: { key: "display.color.reasoning", default: "blue", border: "border400_40", bg: "bg50_60_900_20", text: null },
  inbound_human: { key: "display.color.inbound_human", default: "gray", border: "border400_60", bg: "bg50_900_30", text: null },
  inbound_agent: { key: "display.color.inbound_agent", default: "violet", border: "border400_60", bg: "bg50_950_20", text: null },
  inbound_system: { key: "display.color.inbound_system", default: "sky", border: "border400_60", bg: "bg50_950_20", text: null },
  system_prompt: { key: "display.color.system_prompt", default: "indigo", border: "border400_40", bg: "bg50_40_950_15", text: null },
  attach: { key: "display.color.attach", default: "sky", border: "border400_50", bg: "bg50_40_950_10", text: null },
  note: { key: "display.color.note", default: "teal", border: "border500_40", bg: "bg50_40_950_15", text: "text700_300" },
  memory: { key: "display.color.memory", default: "violet", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_terminate: { key: "display.color.lifecycle_terminate", default: "rose", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_restart: { key: "display.color.lifecycle_restart", default: "amber", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_resurrect: { key: "display.color.lifecycle_resurrect", default: "emerald", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
  lifecycle_fork: { key: "display.color.lifecycle_fork", default: "sky", border: "border400_60", bg: "bg50_950_30", text: "text700_300" },
};

export interface UserSettingRow {
  readonly key: string;
  readonly value: unknown;
  readonly updated_at: string;
}

export interface NotificationCategoryItem {
  readonly id: string;
  readonly label: string;
  readonly description: string;
  readonly importance: string;
  readonly enabled: boolean;
}
export interface NotificationCategoriesResponse {
  readonly categories: NotificationCategoryItem[];
}

export interface UserSettingListResponse {
  readonly settings: readonly UserSettingRow[];
}

// meiva:note
// The reader's one choice between the date-context absolute form and a relative
// span for a future instant (`formatFuture`), absolute by default: a bare span
// ("in 23h") never says which day it lands on (user 2026-10-29). The console
// prints every future instant through it, so the agent list and the Inspector
// agree on one answer.
// meiva:link ui/web/src/lib/format/docs/time.meiva.okf.md
export type DateFormat = "relative" | "absolute";

export const DEFAULT_DATE_FORMAT: DateFormat = "absolute";

export const USER_SETTING_DEFAULTS: Record<string, unknown> = {

  "display.language": "en",
  "behavior.navFallbackMs": 5000,
  "display.time_mode": "last_active",
  "display.show_terminated": false,
  "display.date_format": DEFAULT_DATE_FORMAT,
  "display.show_activity_line": false,
  "display.show_agent_status": false,
  "display.show_timestamp_weekday": true,
  "display.clock_format": "24h",
  "display.show_timezone": false,

  "display.render_reasoning_markdown": true,

  "display.context_meter_width": "comfortable",

  "display.timeline_width_ratio": 0.4,
  "display.expand_runs_mode": "none",
  "display.composer_context_meter": false,

  "display.inspector_open": false,

  "display.compact_history_sessions": 1,

  "display.sidebar_collapsed": false,
  "display.sidebar_view_mode": "flat",

  "display.sidebar_sort": { key: "id", dir: "desc" },

  "display.stats_window_hours": 24,

  "display.fleet_left_view": "graph",
  "display.fleet_queue_collapsed": false,
  "display.task_graph_mode": "graph",
  "display.task_show_done": false,
  "display.task_show_canceled": false,

  "display.task_window": "24h",

  "display.shell_terminal_theme": "system",

  "display.theme_pack": null,

  "display.color.agent_chat": TIMELINE_COLOR_ITEMS.agent_chat.default,
  "display.color.agent_code": TIMELINE_COLOR_ITEMS.agent_code.default,
  "display.color.code_output": TIMELINE_COLOR_ITEMS.code_output.default,
  "display.color.reasoning": TIMELINE_COLOR_ITEMS.reasoning.default,
  "display.color.inbound_human": TIMELINE_COLOR_ITEMS.inbound_human.default,
  "display.color.inbound_agent": TIMELINE_COLOR_ITEMS.inbound_agent.default,
  "display.color.inbound_system": TIMELINE_COLOR_ITEMS.inbound_system.default,
  "display.color.system_prompt": TIMELINE_COLOR_ITEMS.system_prompt.default,
  "display.color.attach": TIMELINE_COLOR_ITEMS.attach.default,
  "display.color.note": TIMELINE_COLOR_ITEMS.note.default,
  "display.color.memory": TIMELINE_COLOR_ITEMS.memory.default,
  "display.color.lifecycle_terminate": TIMELINE_COLOR_ITEMS.lifecycle_terminate.default,
  "display.color.lifecycle_restart": TIMELINE_COLOR_ITEMS.lifecycle_restart.default,
  "display.color.lifecycle_resurrect": TIMELINE_COLOR_ITEMS.lifecycle_resurrect.default,
  "display.color.lifecycle_fork": TIMELINE_COLOR_ITEMS.lifecycle_fork.default,
  "behavior.confirm_terminate": true,
  "behavior.confirm_restart": true,
  "behavior.confirm_force_kill": true,

  "behavior.replySuggestions": true,
  "behavior.spawn_model": null,
  "behavior.spawn_reasoning_effort": null,
  "notification.awaiting_reply": false,
  "web.sseSingleStream": false,

  "models.hidden": [],

  "models.hidden_providers": [],

} as const;

export type TimeMode = "last_active" | "spawned" | "hidden";

export type ContextMeterWidth = "compact" | "comfortable" | "wide";


export type ToUserDocument = Schemas["ToUserDocument"];

type TaskStatus = Schemas["TaskStatus"];

export type TaskPriority = "P0" | "P1" | "P2" | "P3";

export interface TaskSummaryRow {
  readonly id: number;
  readonly parent_id: number | null;
  readonly title: string;
  readonly status: TaskStatus;
  readonly priority: TaskPriority;
  readonly owner: number | null;
  readonly owner_label?: string | null;
  readonly created_by: string;
  readonly created_at: string;
  readonly updated_at: string;
  readonly remind_interval_seconds?: number | null;
  readonly last_reminded_at?: string | null;
  readonly reminder_count: number;
  readonly ghost?: boolean;
}

export interface TaskRow extends TaskSummaryRow {
  readonly description: string;
  readonly results: string | null;
}

export interface TaskGraphRow extends TaskSummaryRow {
  readonly description: string;
}

export type TaskFields = "full" | "summary" | "graph";

export interface TaskListResponse<T extends TaskSummaryRow = TaskSummaryRow> {
  readonly tasks: readonly T[];
}
