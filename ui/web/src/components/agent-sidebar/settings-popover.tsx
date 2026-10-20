"use client";

import * as Popover from "@radix-ui/react-popover";
import { ArrowUpDown, Eye, EyeOff, LayoutList, ListTree, SlidersHorizontal } from "lucide-react";
import { useTranslations } from "next-intl";

import { SORT_DEFAULT_DIR, useSidebarSort, type FlatSortKey } from "@/lib/agents/sidebar";
import { useUserSettings } from "@/lib/state/use-user-settings";

import { SidebarIconButton } from "./icon-button";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1 } from "@/lib/layout/layout";

// meiva:note
// The sidebar's display controls live behind one sliders button: view mode, sort,
// status colours and the terminated archive were a permanent toolbar row under
// the tree, which cost a whole row of a 230px column for settings a user picks
// once (user 2026-10-20). Not a gear: the footer's Control entry already is one,
// and two gears in one column read as the same destination (user 2026-10-20).
// The waiting count is NOT here - it is a live signal, and it moved to the
// header badge.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md

const SORT_KEYS: readonly FlatSortKey[] = ["id", "last_active", "status"];
// meiva:note
// The keys are wire field names (`last_active`), not words: each one carries a
// label so the menu never shows a raw key.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md
const SORT_LABEL: Record<FlatSortKey, (t: (key: "sortId" | "sortActivity" | "status") => string) => string> = {
  id: (t) => t("sortId"),
  last_active: (t) => t("sortActivity"),
  status: (t) => t("status"),
};
const ITEM = "w-full items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-sidebar-accent/60";

interface Props {
  viewMode: "tree" | "flat";
  onToggleViewMode: () => void;
  showTerminated: boolean;
  onToggleTerminated: () => void;
}

export function SidebarSettingsPopover({
  viewMode,
  onToggleViewMode,
  showTerminated,
  onToggleTerminated,
}: Props) {
  const t = useTranslations("sidebar");
  const { sort, setSort } = useSidebarSort();
  const { settings, setSetting } = useUserSettings();
  const showAgentStatus = settings["display.show_agent_status"] === true;

  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <SidebarIconButton label={t("sidebarSettings")} testid="sidebar-settings">
          <SlidersHorizontal className="size-4" />
        </SidebarIconButton>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={6}
          align="end"
          className="z-50 w-56 rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md outline-none"
        >
          <div className="px-2 pt-1 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70">
            {t("view")}
          </div>
          <div className={cn("gap-1 p-0.5 rounded bg-sidebar-accent/40", FLEX)} role="group" aria-label={t("view")}>
            {([["tree", t("tree"), ListTree], ["flat", t("flat"), LayoutList]] as const).map(([mode, label, Icon]) => (
              <button
                key={mode}
                type="button"
                onClick={viewMode === mode ? undefined : onToggleViewMode}
                aria-pressed={viewMode === mode}
                className={cn(
                  "items-center justify-center gap-1.5 rounded px-2 py-1 text-xs transition-colors",
                  FLEX,
                  FLEX_1,
                  viewMode === mode ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className="size-3.5" />
                <span>{label}</span>
              </button>
            ))}
          </div>

          <div className="px-2 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70">
            {t("sortBy")}
          </div>
          {SORT_KEYS.map((key) => {
            const active = sort.key === key;
            return (
              <button
                key={key}
                type="button"
                onClick={() =>
                  setSort(active ? { key, dir: sort.dir === "asc" ? "desc" : "asc" } : { key, dir: SORT_DEFAULT_DIR[key] })
                }
                aria-pressed={active}
                className={cn(ITEM, FLEX, active && "text-foreground")}
              >
                <ArrowUpDown className={cn("size-3.5", !active && "opacity-0")} />
                <span>{SORT_LABEL[key](t)}</span>
                {active ? <span className="ml-auto" aria-hidden>{sort.dir === "asc" ? "↑" : "↓"}</span> : null}
              </button>
            );
          })}

          <div className="px-2 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70">
            {t("display")}
          </div>
          <button
            type="button"
            onClick={() => setSetting("display.show_agent_status", !showAgentStatus)}
            aria-pressed={showAgentStatus}
            className={cn(ITEM, FLEX, showAgentStatus && "text-foreground")}
          >
            {showAgentStatus ? <Eye className="size-3.5" /> : <EyeOff className="size-3.5" />}
            <span>{showAgentStatus ? t("hideAgentStatus") : t("showAgentStatus")}</span>
          </button>
          <button
            type="button"
            onClick={onToggleTerminated}
            aria-pressed={showTerminated}
            className={cn(ITEM, FLEX, showTerminated && "text-foreground")}
          >
            {showTerminated ? <Eye className="size-3.5" /> : <EyeOff className="size-3.5" />}
            <span>{showTerminated ? t("hideTerminatedAgents") : t("showTerminatedAgents")}</span>
          </button>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
