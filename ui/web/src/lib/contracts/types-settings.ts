import type { components } from "./types-generated";

type Schemas = components["schemas"];

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
// meiva:link ui/web/src/lib/format/docs/time.md
export type DateFormat = "relative" | "absolute";

export const DEFAULT_DATE_FORMAT: DateFormat = "absolute";

export const USER_SETTING_DEFAULTS: Record<string, unknown> = {

  "display.language": "en",
  "behavior.navFallbackMs": 5000,
  "display.show_terminated": false,
  "display.date_format": DEFAULT_DATE_FORMAT,
  "display.show_agent_status": false,
  "display.show_timestamp_weekday": true,
  "display.clock_format": "24h",

  "display.timeline_width_px": 800,
  "display.expand_working_block": false,

  "display.inspector_open": false,

  "display.sidebar_collapsed": false,
  "display.sidebar_view_mode": "flat",

  "display.sidebar_sort": { key: "id", dir: "desc" },

  "display.stats_window_hours": 24,



  "display.shell_terminal_theme": "system",
  "display.theme": "system",

  "display.theme_pack": null,

  "behavior.confirm_terminate": true,
  "behavior.confirm_restart": true,
  "behavior.confirm_force_kill": true,

  "behavior.replySuggestions": true,
  "behavior.spawn_model": null,
  "behavior.spawn_reasoning_effort": null,
  "feature.reloadReadingRestore": true,
  "web.sseSingleStream": false,

  "models.hidden": [],

  "models.hidden_providers": [],

} as const;



export type OngoingDocument = Schemas["OngoingDocument"];
export type ToUserDocument = Schemas["ToUserDocument"];
export type WorkspaceFileResponse = Schemas["WorkspaceFileResponse"];
export type WorkspaceDirListing = Schemas["WorkspaceDirListing"];
