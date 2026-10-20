"use client";

import { type ReactNode } from "react";
import { useDefaultLayout } from "react-resizable-panels";

import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { FLEX, FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { panelLayoutStorage } from "@/lib/layout/panel-layout-storage";
import { useClientReady } from "@/lib/layout/use-client-ready";
import { cn } from "@/lib/format/utils";

// Panel sizes are written as explicit percent strings: v4 reads a bare number
// as PIXELS and a unit-less string as percent (v3 read numbers as percent).
interface ColumnFrame {
  layoutId: string;
  expanded: readonly [sidebar: string, main: string];
  expandedMinimums: readonly [sidebar: string, main: string];
}

const DESKTOP_COLUMNS: ColumnFrame = {
  layoutId: "meiva.home.columns.context",
  expanded: ["16%", "84%"],
  expandedMinimums: ["12%", "60%"],
};

const MOBILE_COLUMNS: ColumnFrame = {
  layoutId: "meiva.home.columns.mobile",
  expanded: ["40%", "60%"],
  expandedMinimums: ["30%", "40%"],
};

const INSPECTOR_LAYOUT_ID = "meiva.home.context.desktop";
const INSPECTOR_OPEN_SIZES = ["57.143%", "42.857%"] as const;

// v4 stores a group's layout as { <panelId>: percent }, so panel ids are part
// of the persisted state (the library also writes them to id/data-testid on
// the panel node — keep them unique across the app).
const PANEL_SIDEBAR = "panel-sidebar";
const PANEL_MAIN = "panel-main";
const PANEL_TIMELINE = "panel-timeline";
const PANEL_INSPECTOR = "panel-inspector";

// SSR-safe group-split storage (see lib/panel-layout-storage.ts).
const HOME_SPLIT_STORAGE = panelLayoutStorage();


interface Props {
  isNarrow: boolean;
  isLarge: boolean;
  sidebarCollapsed: boolean;
  sidebar: ReactNode;
  main: ReactNode;
  inspector: ReactNode;
}

// meiva:note
// Both column dividers paint the handle's own full-height line: an inset segment
// stopped above the composer and below the title bar, which read as a broken
// line next to dividers that do run edge to edge (user report 2026-10-20).
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function HomeDividerHandle() {
  return <ResizableHandle />;
}

function DesktopMain({ main, inspector }: Pick<Props, "main" | "inspector">) {
  const inspectorVisible = inspector !== null && inspector !== undefined && inspector !== false;
  const mainDefaultSize = inspectorVisible ? INSPECTOR_OPEN_SIZES[0] : "100%";

  // Persist only user-driven layout commits: a mount / shape-change /
  // constraint commit must not overwrite this shape's stored split with a
  // normalized transient frame (the job the removed v3 mount guard did).
  // The closed-inspector shape keeps its own panel set, so toggling the
  // inspector never touches the dragged two-panel split.
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: INSPECTOR_LAYOUT_ID,
    storage: HOME_SPLIT_STORAGE,
    onlySaveAfterUserInteractions: true,
  });

  return (
    // Keep this group horizontal for its whole lifetime. Re-orienting (or
    // re-shaping) a mounted react-resizable-panels group can normalize and
    // commit a breakpoint frame over the user's saved desktop split.
    <ResizablePanelGroup
      orientation="horizontal"
      defaultLayout={defaultLayout}
      onLayoutChanged={onLayoutChanged}
      className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
    >
      <ResizablePanel
        id={PANEL_TIMELINE}
        defaultSize={mainDefaultSize}
        minSize={inspectorVisible ? "50%" : "100%"}
        className={cn(FLEX, MIN_H_0, MIN_W_0)}
      >
        {main}
      </ResizablePanel>
      {inspectorVisible ? (
        <>
          <HomeDividerHandle />
          <ResizablePanel
            id={PANEL_INSPECTOR}
            defaultSize={INSPECTOR_OPEN_SIZES[1]}
            minSize="25%"
            className={cn(FLEX, MIN_H_0, MIN_W_0)}
          >
            {inspector}
          </ResizablePanel>
        </>
      ) : null}
    </ResizablePanelGroup>
  );
}

export function HomeLayout({
  isNarrow,
  isLarge,
  sidebarCollapsed,
  sidebar,
  main,
  inspector,
}: Props) {
  // A hydrating load starts in the SSR-safe mobile frame; Delay PanelGroup
  // registration until the breakpoint values are the client's own — otherwise
  // the transient frame registers a group that immediately remounts onto the
  // breakpoint's keyed group. The full-size placeholder reserves the page box,
  // so the gate itself does not move surrounding layout.
  //
  // The gate keys on the snapshot's ORIGIN (useClientReady), not on a
  // post-mount flag: a client-side navigation already has the real breakpoint
  // values in its first render, so it renders the real frame directly — a
  // back/forward into the home page must not paint a blank placeholder frame
  // first. Hydration keeps the old two-phase order (server snapshot → real).
  const clientReady = useClientReady();

  const frame = isLarge ? DESKTOP_COLUMNS : MOBILE_COLUMNS;
  // Only user-driven commits persist (see DesktopMain) — the breakpoint's
  // frame swap must never rewrite the other frame's saved ratios.
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: frame.layoutId,
    storage: HOME_SPLIT_STORAGE,
    onlySaveAfterUserInteractions: true,
  });

  if (!clientReady) {
    return (
      <div
        data-testid="home-layout-placeholder"
        aria-hidden="true"
        className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
      />
    );
  }

  // Phones retain the existing full-screen sidebar and inspector overlays.
  // They do not mount a split group, so their interaction model cannot be
  // constrained by a desktop panel's minimum width.
  if (isNarrow) {
    return (
      <>
        {sidebar}
        {main}
        {inspector}
      </>
    );
  }

  if (sidebarCollapsed) {
    return (
      <>
        <div className={cn("h-full w-full", FLEX, MIN_H_0, MIN_W_0)}>
          {isLarge && inspector !== null && inspector !== undefined && inspector !== false ? (
            <DesktopMain main={main} inspector={inspector} />
          ) : (
            main
          )}
        </div>
        {isLarge ? null : inspector}
      </>
    );
  }

  return (
    <>
      {/* Desktop and compact frames have different layout ids, so breakpoint
          transitions cannot overwrite each other's saved ratios. */}
      <ResizablePanelGroup
        key={frame.layoutId}
        orientation="horizontal"
        defaultLayout={defaultLayout}
        onLayoutChanged={onLayoutChanged}
        className={cn(FLEX_1, MIN_H_0, MIN_W_0)}
      >
        <ResizablePanel
          id={PANEL_SIDEBAR}
          defaultSize={frame.expanded[0]}
          minSize={frame.expandedMinimums[0]}
          maxSize="50%"
          className={cn(FLEX, MIN_H_0, MIN_W_0)}
        >
          {sidebar}
        </ResizablePanel>
        <HomeDividerHandle />
        <ResizablePanel
          id={PANEL_MAIN}
          defaultSize={frame.expanded[1]}
          minSize={frame.expandedMinimums[1]}
          className={cn(FLEX, MIN_H_0, MIN_W_0)}
        >
          {isLarge ? <DesktopMain main={main} inspector={inspector} /> : main}
        </ResizablePanel>
      </ResizablePanelGroup>
      {/* Below lg the inspector remains the existing fixed overlay. Keeping it
          outside the md-width split prevents a hidden panel from consuming
          timeline width and avoids clipping position:fixed under a panel. */}
      {isLarge ? null : inspector}
    </>
  );
}
