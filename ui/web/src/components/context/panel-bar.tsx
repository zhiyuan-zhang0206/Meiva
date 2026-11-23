"use client";

import { Maximize2, Minimize2, PanelLeft, PanelRight, ScanSearch, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { machineOf, ongoingTab, sameFile, useWorkspaceFileStore, type WorkspaceFileTarget } from "@/lib/context/workspace-file-store";
import { cn } from "@/lib/format/utils";
import { COLUMN_TITLE_BAR_CLASS, FLEX, MIN_W_0 } from "@/lib/layout/layout";

const EMPTY: WorkspaceFileTarget[] = [];

export function fileName(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? path;
}

const tabClass = (active: boolean) => cn(
  "group h-7 shrink-0 items-center gap-1 rounded-md px-2 text-[11px]",
  active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
  FLEX,
);
const iconButton = "shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground";

// meiva:note
// The panel's one bar, the height of the conversation's header bar so both rules meet in one line
// (user report 2026-11-21: the edges did not line up). Left: the files as tabs, the conversation's
// to-user document first and always there (it is the panel's home), then each opened file,
// closable; or the inspector's title while it is on. Right: fill the page, the inspector, and
// collapsing the panel. Selecting a tab is a history entry like opening a file; Back and Forward
// are the browser's.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function PanelBar({ agentId, mobile, inspecting, hasOngoing = false, onInspect, onCollapse, onOpenSidebar }: {
  agentId: number;
  mobile: boolean;
  inspecting: boolean;
  hasOngoing?: boolean;
  onInspect?: () => void;
  onCollapse?: () => void;
  onOpenSidebar?: () => void;
}) {
  const tNav = useTranslations("nav");
  const t = useTranslations("toUser");
  const tInspector = useTranslations("inspector");
  const tabs = useWorkspaceFileStore((s) => s.tabs[agentId] ?? EMPTY);
  const activeFile = useWorkspaceFileStore((s) => s.activeFile);
  const maximized = useWorkspaceFileStore((s) => s.maximized);
  const { selectTab, closeTab, setMaximized } = useWorkspaceFileStore.getState();
  const shownFile = activeFile?.agentId === agentId ? activeFile : null;
  const ongoing = ongoingTab(agentId);
  const showingOngoing = sameFile(shownFile, ongoing);
  return (
    <div data-testid="panel-bar" className={cn("shrink-0 items-center gap-1 px-2", COLUMN_TITLE_BAR_CLASS, FLEX)}>
      {onOpenSidebar ? (
        <button type="button" aria-label={tNav("openSidebar")} onClick={onOpenSidebar} className={iconButton}>
          <PanelLeft aria-hidden className="size-3.5" />
        </button>
      ) : null}
      {inspecting ? (
        <span className="px-1 text-[11px] text-muted-foreground">{tInspector("title")}</span>
      ) : (
        <div role="tablist" aria-label={t("filesSharedContext")} className={cn("items-center gap-0.5 overflow-x-auto", MIN_W_0, FLEX)}>
          <button type="button" role="tab" aria-selected={shownFile === null} onClick={() => selectTab(null)} className={tabClass(shownFile === null)}>
            <span className="whitespace-nowrap">{t("filesSharedContext")}</span>
          </button>
          <button type="button" role="tab" aria-selected={showingOngoing} onClick={() => selectTab(ongoing)} className={tabClass(showingOngoing)}>
            <span className="whitespace-nowrap">{t("ongoing")}</span>
            {hasOngoing ? <span role="img" aria-label={t("ongoingPresent")} className="size-1.5 rounded-full bg-current opacity-60" /> : null}
          </button>
          {tabs.map((tab) => {
            const active = sameFile(shownFile, tab);
            const name = fileName(tab.path);
            return (
              <div key={`${machineOf(tab)}:${tab.path}`} className={tabClass(active)}>
                <button type="button" role="tab" aria-selected={active} onClick={() => selectTab(tab)} className={cn("items-center", MIN_W_0, FLEX)}>
                  <span className="max-w-40 truncate" title={tab.path}>{name}</span>
                </button>
                <button
                  type="button"
                  aria-label={t("filesClose", { name })}
                  onClick={() => closeTab(tab)}
                  className="rounded p-0.5 opacity-60 hover:bg-foreground/10 hover:opacity-100"
                >
                  <X aria-hidden className="size-3" />
                </button>
              </div>
            );
          })}
        </div>
      )}
      <div className={cn("ml-auto shrink-0 items-center gap-0.5", FLEX)}>
        {mobile || inspecting ? null : (
          <button
            type="button"
            aria-label={t(maximized ? "filesRestore" : "filesMaximize")}
            aria-pressed={maximized}
            onClick={() => setMaximized(!maximized)}
            className={iconButton}
          >
            {maximized ? <Minimize2 aria-hidden className="size-3.5" /> : <Maximize2 aria-hidden className="size-3.5" />}
          </button>
        )}
        {onInspect ? (
          <button
            type="button"
            data-testid="open-inspector"
            aria-label={tInspector(inspecting ? "closeInspector" : "openInspector")}
            aria-pressed={inspecting}
            onClick={onInspect}
            className={cn("shrink-0 rounded p-1", inspecting ? "bg-foreground/20 text-foreground" : "text-muted-foreground")}
          >
            <ScanSearch aria-hidden className="size-3.5" />
          </button>
        ) : null}
        {onCollapse && !mobile ? (
          <button type="button" data-testid="context-panel-collapse" aria-label={t("hideContextPanel")} onClick={onCollapse} className={iconButton}>
            <PanelRight aria-hidden className="size-3.5" />
          </button>
        ) : null}
      </div>
    </div>
  );
}
