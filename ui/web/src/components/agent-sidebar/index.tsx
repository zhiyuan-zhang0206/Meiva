"use client";

// The sidebar renders one live tree with minimal ancestry. Opening history
// mounts an independent, bounded directory page; selected conversations remain
// addressable by ID regardless of directory membership.

import { useQueryClient } from "@tanstack/react-query";
import { PanelLeft, Search, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { api } from "@/lib/transport/api";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { errMsg } from "@/lib/contracts/errors";
import { useSidebarCollapsed, useSidebarViewMode } from "@/lib/agents/sidebar";
import { useStore } from "@/lib/state/store";
import { useUserSettings } from "@/lib/state/use-user-settings";

import { SidebarBody } from "./body";
import { SidebarFooter } from "./footer";
import { SidebarHeader } from "./header";
import { SearchOverlay } from "./search-overlay";
import { SidebarIconButton } from "./icon-button";
import { SidebarSettingsPopover } from "./settings-popover";
import { fleetHref } from "./links";
import Link from "next/link";
import { SpawnPopover } from "./spawn-popover";
import type { DesktopProps, MobileProps, Props } from "./types";
import { FLEX, FLEX_COL } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export function AgentSidebar(props: Props) {
  // R4 layer 4: one responsive mechanism — breakpoint + conditional render.
  // The desktop rail and the mobile drawer are mutually exclusive surfaces;
  // only the active one mounts (the old always-mounted pair hid the inactive
  // one with CSS media queries). Switch point preserved from the old CSS
  // `md` rule: the rail shows at >= 768px, the drawer below it.
  const { isNarrow } = useBreakpoint();
  const { collapsed: collapsedSetting, setCollapsed } = useSidebarCollapsed();
  // meiva:note
  // The rail stays mounted in inspector mode: the mode changes what the right
  // column shows, not whether the agent tree is reachable (user 2026-10-20,
  // reversing the earlier mode-hides-the-rail rule). `display.sidebar_collapsed`
  // remains the only thing that collapses it.
  // meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
  const collapsed = collapsedSetting;
  const { viewMode, setViewMode } = useSidebarViewMode();
  // Show-terminated is a DB-backed user setting (display.show_terminated), the
  // single source of truth shared with the Display settings page — toggling
  // either place updates the same ["user-settings"] cache, so they stay in
  // lockstep across the sidebar and settings. Default false
  // (USER_SETTING_DEFAULTS); opaque non-boolean DB values remain opt-out.
  // This setting opens the bounded archive; live ancestry is a separate read.
  const { settings: userSettings, setSetting } = useUserSettings();
  const showTerminated = userSettings["display.show_terminated"] === true;
  const setShowTerminated = (v: boolean) => setSetting("display.show_terminated", v);
  const queryClient = useQueryClient();

  // -- Read UI state from the store --
  const activeId = useStore((s) => s.activeId);
  const setActiveId = useStore((s) => s.setActiveId);
  const mobileSidebarOpen = useStore((s) => s.mobileSidebarOpen);
  const setMobileSidebarOpen = useStore((s) => s.setMobileSidebarOpen);
  const searchQuery = useStore((s) => s.searchQuery);
  const setSearchQuery = useStore((s) => s.setSearchQuery);
  const showToast = useStore((s) => s.showToast);
  const [searchOpen, setSearchOpen] = useState(false);
  const openSearch = useCallback(() => setSearchOpen(true), []);
  const closeSearch = useCallback(() => setSearchOpen(false), []);

  const { agents, pendingActions, pendingSpawnCount } = props;

  // Mobile: auto-close the drawer after selecting an agent.
  const handleSelect = useCallback(
    (id: number) => {
      setActiveId(id);
      setMobileSidebarOpen(false);
    },
    [setActiveId, setMobileSidebarOpen],
  );

  const handleRename = (id: number, label: string) => {
    api
      .patchAgentLabel(id, label)
      .then(() => queryClient.invalidateQueries({ queryKey: ["agents"] }))
      .catch((err: unknown) => {
        // Rename is a user-initiated action — a silent console.error left the
        // user thinking the new label stuck (Task #1051).
        showToast(`Failed to rename agent: ${errMsg(err)}`);
      });
  };

  return (
    <>
      {!isNarrow ? (
      <DesktopSidebar
        agents={agents}
        ancestors={props.ancestors}
        activeId={activeId}
        onSelect={handleSelect}
        onSpawn={props.onSpawn}
        onTerminate={props.onTerminate}
        onForceExpire={props.onForceExpire}
        onRestart={props.onRestart}
        onResurrect={props.onResurrect}
        onFork={props.onFork}
        onCompact={props.onCompact}
        pendingActions={pendingActions}
        pendingSpawnCount={pendingSpawnCount}
        isLoading={props.isLoading}
        onRename={handleRename}
        showTerminated={showTerminated}
        onToggleTerminated={() => setShowTerminated(!showTerminated)}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed(!collapsedSetting)}
        onSearchOpen={openSearch}
        setCollapsed={setCollapsed}
        viewMode={viewMode}
        onToggleViewMode={() => setViewMode(viewMode === "tree" ? "flat" : "tree")}
      />
      ) : (
      <MobileSidebar
        agents={agents}
        ancestors={props.ancestors}
        activeId={activeId}
        onSelect={handleSelect}
        onSpawn={props.onSpawn}
        onTerminate={props.onTerminate}
        onForceExpire={props.onForceExpire}
        onRestart={props.onRestart}
        onResurrect={props.onResurrect}
        onFork={props.onFork}
        onCompact={props.onCompact}
        pendingActions={pendingActions}
        pendingSpawnCount={pendingSpawnCount}
        isLoading={props.isLoading}
        onRename={handleRename}
        showTerminated={showTerminated}
        onToggleTerminated={() => setShowTerminated(!showTerminated)}
        viewMode={viewMode}
        onToggleViewMode={() => setViewMode(viewMode === "tree" ? "flat" : "tree")}
        mobileOpen={mobileSidebarOpen}
        onMobileClose={() => setMobileSidebarOpen(false)}
        onSearchOpen={openSearch}
      />
      )}
      {/* Floating search overlay — search lives here, not inline in the
          sidebar (#723); the query filters the sidebar list in sync. The
          overlay searches live cards. The archive has its own server search. */}
      <SearchOverlay
        open={searchOpen}
        onClose={closeSearch}
        searchQuery={searchQuery}
        onSearchQueryChange={setSearchQuery}
        agents={agents}
        onSelect={handleSelect}
      />
    </>
  );
}

function DesktopSidebar(props: DesktopProps) {
  const t = useTranslations("sidebar");
  const { collapsed, onSearchOpen, onToggleCollapsed, viewMode, onToggleViewMode, showTerminated, onToggleTerminated } = props;
  // meiva:note
  // The waiting count is a live signal, not a setting: it moved from the removed
  // toolbar row to the header, next to the controls, and stays opt-in behind
  // notification.awaiting_reply (quiet defaults).
  // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
  const { settings } = useUserSettings();
  const waiting = props.agents.reduce((n, a) => n + a.awaiting_response_count, 0);
  const awaitingBadge =
    settings["notification.awaiting_reply"] === true && waiting > 0 ? (
      <Link
        href={fleetHref(props.activeId)}
        className="shrink-0 items-center gap-1 px-1 text-[11px] text-muted-foreground hover:text-foreground"
        title={String(waiting)}
        style={{ display: "inline-flex" }}
      >
        <span className="size-1.5 rounded-full bg-destructive" />
        <span>{waiting}</span>
      </Link>
    ) : null;

  if (collapsed) return null;

  return (
    <aside
      // FLEX + FLEX_COL: this aside is the I6-class vertical chain's sidebar
      // member — display:flex (R4-PR3 regression, Task #1053: `hidden md:flex`
      // was removed and only flex-col survived, so the aside fell back to
      // block layout, the agent-list ScrollArea's flex-1/min-h-0 stopped
      // bounding, and the WHOLE aside — header, spawn/toolbar bars and the
      // fixed footer — scrolled together). overflow-x-hidden is a hard
      // backstop: SidebarBody's own controls (spawn selects, sort buttons)
      // truncate to fit the available panel width, but this guarantees no
      // horizontal scrollbar can ever appear even if a control slips past that. h-full +
      // w-full let react-resizable-panels own the outer column dimensions.
      className={cn("relative h-full w-full bg-sidebar overflow-x-hidden", FLEX, FLEX_COL)}
    >
      <SidebarHeader
        trailing={
          <>
            {/* Search in the header (not inline in the body) — opens the
                floating overlay, #723. */}
            <SidebarIconButton label={t("searchAgents")} onClick={onSearchOpen}>
              <Search className="size-4" />
            </SidebarIconButton>
            {awaitingBadge}
            <SidebarSettingsPopover
              viewMode={viewMode}
              onToggleViewMode={onToggleViewMode}
              showTerminated={showTerminated}
              onToggleTerminated={onToggleTerminated}
            />
            <SpawnPopover variant="icon" onSpawn={props.onSpawn} />
            <SidebarIconButton
              label={t("hideSidebar")}
              onClick={onToggleCollapsed}
              testid="sidebar-collapse"
            >
              <PanelLeft className="size-4" />
            </SidebarIconButton>
          </>
        }
      />
      <SidebarBody {...props} wide />
      {/* Fixed bottom strip (user ruling 2026-08-05): Statistics popover +
          the four nav shortcuts (Memory Graph / Fleet / Insights / Control)
          live here — the spot an app's avatar row would occupy. */}
      <SidebarFooter activeAgentId={props.activeId} />
    </aside>
  );
}


function MobileSidebar(props: MobileProps) {
  const t = useTranslations("sidebar");
  // meiva:note
  // The drawer keeps the same four controls as the rail: the body is a list, so
  // create and the display settings live in its header on every width. Without
  // this the drawer lost both when the body's spawn bar and toolbar went away.
  // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
  const { mobileOpen, onMobileClose, onSearchOpen, viewMode, onToggleViewMode, showTerminated, onToggleTerminated } = props;
  if (!mobileOpen) return null;
  return (
    <div className={cn("fixed inset-0 z-50", FLEX)}>
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onMobileClose}
        aria-hidden="true"
      />
      <aside className={cn("relative w-full bg-sidebar", FLEX, FLEX_COL)}>
        <SidebarHeader
          trailing={
            <>
              <SidebarIconButton label={t("searchAgents")} onClick={onSearchOpen}>
                <Search className="size-4" />
              </SidebarIconButton>
              <SidebarSettingsPopover
                viewMode={viewMode}
                onToggleViewMode={onToggleViewMode}
                showTerminated={showTerminated}
                onToggleTerminated={onToggleTerminated}
              />
              <SpawnPopover variant="icon" onSpawn={props.onSpawn} />
              <SidebarIconButton label={t("closeSidebar")} onClick={onMobileClose}>
                <X className="size-4" />
              </SidebarIconButton>
            </>
          }
        />
        <SidebarBody {...props} wide />
        <SidebarFooter activeAgentId={props.activeId} />
      </aside>
    </div>
  );
}


// ── Collapsed sidebar: a completely blank rail ──
//
// Collapsed means "I looked away — tell me nothing": no mini agent list, no
// status dots, no spinners, no badges. The only affordance is expanding —
// static presence (and any opted-in signals) live behind that deliberate pull.
