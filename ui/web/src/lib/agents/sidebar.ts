// Sidebar state hooks: collapsed / view mode / sort / stats window are
// DB-backed user settings (display.sidebar_*), so the choice survives refreshes
// and stays in sync across every frontend entrypoint via the shared
// ["user-settings"] cache. Homepage panel widths are device-local layout ratios
// owned by react-resizable-panels, not durable user settings.
//
// Agent labels don't live here — the backend `threads.label` column is
// the source of truth; the gateway BackgroundTask runs an LLM in the
// background to generate a short label when spawn carries a prompt;
// user overrides go through `api.patchAgentLabel`.

import { useCallback } from "react";

import { useUserSettings } from "../state/use-user-settings";

// Collapse remains DB-backed (display.sidebar_collapsed) and syncs across
// frontends within a session, but resets to expanded on every entry (#723).
// The reset waits for settings to load in the stable HomeShell owner.
export function useSidebarCollapsed(): {
  collapsed: boolean;
  setCollapsed: (c: boolean) => void;
  isLoading: boolean;
} {
  const { settings, setSetting, isLoading } = useUserSettings();
  const collapsed = settings["display.sidebar_collapsed"] === true;
  const setCollapsed = useCallback(
    (c: boolean) => setSetting("display.sidebar_collapsed", c),
    [setSetting],
  );
  return { collapsed, setCollapsed, isLoading };
}

// Whether terminated agents are shown lives in the DB-backed user setting
// `display.show_terminated` (read via useUserSettings), not here — that keeps
// the sidebar toggle and the Display settings page pointed at one source of
// truth instead of a localStorage copy that could drift.

// Whether agents are shown as a flat sortable list (default) or a spawn-lineage
// tree. Flat + sort-by-id is the default so the list has a stable, predictable
// order; the tree remains available via the toggle. DB-backed
// (display.sidebar_view_mode).
export function useSidebarViewMode(): {
  viewMode: "tree" | "flat";
  setViewMode: (v: "tree" | "flat") => void;
} {
  const { settings, setSetting } = useUserSettings();
  const viewMode: "tree" | "flat" =
    settings["display.sidebar_view_mode"] === "tree" ? "tree" : "flat";
  const setViewMode = useCallback(
    (v: "tree" | "flat") => setSetting("display.sidebar_view_mode", v),
    [setSetting],
  );
  return { viewMode, setViewMode };
}

// Flat-list sort: which key + direction. DB-backed (display.sidebar_sort).
// Default is id descending (a stable, predictable order); switching to
// last_active defaults to most-recent-first.
export type FlatSortKey = "id" | "last_active" | "status";
export type SortDir = "asc" | "desc";
export interface SidebarSort {
  key: FlatSortKey;
  dir: SortDir;
}
export const SIDEBAR_SORT_DEFAULT: SidebarSort = { key: "id", dir: "desc" };
// Direction a key snaps to when first selected (id/status ascending, last_active
// most-recent-first); clicking the already-active key reverses it.
export const SORT_DEFAULT_DIR: Record<FlatSortKey, SortDir> = {
  id: "desc",
  last_active: "desc",
  status: "asc",
};

function isFlatSortKey(v: unknown): v is FlatSortKey {
  return v === "id" || v === "last_active" || v === "status";
}

function isSidebarSort(v: unknown): v is SidebarSort {
  if (typeof v !== "object" || v === null) return false;
  return "key" in v && isFlatSortKey(v.key) && "dir" in v && (v.dir === "asc" || v.dir === "desc");
}

export function useSidebarSort(): {
  sort: SidebarSort;
  setSort: (s: SidebarSort) => void;
} {
  const { settings, setSetting } = useUserSettings();
  const raw = settings["display.sidebar_sort"];
  const sort = isSidebarSort(raw) ? raw : SIDEBAR_SORT_DEFAULT;
  const setSort = useCallback(
    (s: SidebarSort) => setSetting("display.sidebar_sort", s),
    [setSetting],
  );
  return { sort, setSort };
}

export {
  STATS_WINDOW_DEFAULT,
  STATS_WINDOW_LABELS,
  STATS_WINDOWS,
  useStatsDashboard,
  useStatsWindow,
  type StatsWindowHours,
} from "./sidebar-stats";

// Relative time formatting for sidebar rows (and, transitively, every other
// consumer of this re-export — alerts, inbox-queue). The exact date is in
// the hover tooltip (ISO string passed as title). Delegates to the shared
// helper (`@/lib/time`) so this and every other relative-time surface in the
// app read the same wording scheme (tz audit, 2026-08: this used to be one
// of 5 independently-drifted implementations).
export { formatRelative as formatRelativeTime } from "../format/time";
