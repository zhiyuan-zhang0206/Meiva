"use client";

import { FolderTree, Maximize2, Minimize2, Search, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { useWorkspaceFileStore, type WorkspaceFileTarget } from "@/lib/context/workspace-file-store";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

const EMPTY: WorkspaceFileTarget[] = [];

export function fileName(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? path;
}

const tabClass = (active: boolean) => cn(
  "group h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-[11px]",
  active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
  FLEX,
);

// meiva:note
// The panel's files as tabs: the conversation's to-user document first and always there, then every
// file opened in this conversation, each closable. Selecting a tab is a history entry like opening a
// file. The bar also opens quick open, the file tree and the full-page view.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function FileTabs({ agentId, mobile, treeOpen, onToggleTree, onQuickOpen }: {
  agentId: number;
  mobile: boolean;
  treeOpen: boolean;
  onToggleTree: () => void;
  onQuickOpen: () => void;
}) {
  const t = useTranslations("toUser");
  const tabs = useWorkspaceFileStore((s) => s.tabs[agentId] ?? EMPTY);
  const activeFile = useWorkspaceFileStore((s) => s.activeFile);
  const maximized = useWorkspaceFileStore((s) => s.maximized);
  const { selectTab, closeTab, setMaximized } = useWorkspaceFileStore.getState();
  const shownFile = activeFile?.agentId === agentId ? activeFile : null;
  const onDocument = shownFile === null;
  const iconButton = "shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground";
  return (
    <div data-testid="file-tabs" className={cn("h-9 shrink-0 items-center gap-1 border-b px-2", FLEX)}>
      <div role="tablist" aria-label={t("filesSharedContext")} className={cn("items-center gap-0.5 overflow-x-auto", MIN_W_0, FLEX)}>
        <button
          type="button"
          role="tab"
          aria-selected={onDocument}
          onClick={() => selectTab(null)}
          className={tabClass(onDocument)}
        >
          <span className="whitespace-nowrap">{t("filesSharedContext")}</span>
        </button>
        {tabs.map((tab) => {
          const active = shownFile?.path === tab.path;
          const name = fileName(tab.path);
          return (
            <div key={tab.path} className={tabClass(active)}>
              <button
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => selectTab(tab)}
                className={cn("items-center gap-1.5", MIN_W_0, FLEX)}
              >
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
      <div className={cn("ml-auto shrink-0 items-center gap-0.5", FLEX)}>
        <button type="button" aria-label={t("filesOpen")} onClick={onQuickOpen} className={iconButton}>
          <Search aria-hidden className="size-3.5" />
        </button>
        <button
          type="button"
          aria-label={t(treeOpen ? "filesHideTree" : "filesShowTree")}
          aria-pressed={treeOpen}
          onClick={onToggleTree}
          className={cn(iconButton, treeOpen && "bg-accent text-foreground")}
        >
          <FolderTree aria-hidden className="size-3.5" />
        </button>
        {mobile ? null : (
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
      </div>
    </div>
  );
}
