"use client";

import { ArrowLeft, ScanSearch } from "lucide-react";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { InspectorPanel } from "@/components/inspector/inspector-panel";
import { FileTabs } from "./file-tabs";
import { FileTree } from "./file-tree";
import { QuickOpen } from "./quick-open";
import { ToUserDocumentView } from "./to-user-document";
import { WorkspaceFileView } from "./workspace-file-view";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import type { useToUser } from "@/lib/context/use-to-user";
import { useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import { cn } from "@/lib/format/utils";

// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function SharedContextPanel({ agentId, document, mobile, onClose, onCollapse }: {
  agentId: number;
  document: ReturnType<typeof useToUser>;
  mobile: boolean;
  onClose: () => void;
  onCollapse?: () => void;
}) {
  const t = useTranslations("toUser");
  const tInspector = useTranslations("inspector");
  const { open: inspecting, toggle: toggleInspector } = useInspectorOpen();
  const activeFile = useWorkspaceFileStore((s) => s.activeFile);
  const isViewingFile = activeFile !== null && activeFile.agentId === agentId;
  const documentScroll = useRef<HTMLDivElement>(null);
  const readingPosition = useRef(0);
  const toggleInspection = () => {
    if (!inspecting) readingPosition.current = documentScroll.current?.scrollTop ?? 0;
    toggleInspector();
  };
  const resetReadingPosition = useCallback(() => {
    readingPosition.current = 0;
    if (documentScroll.current) documentScroll.current.scrollTop = 0;
  }, []);
  useEffect(() => {
    if (!inspecting && documentScroll.current) documentScroll.current.scrollTop = readingPosition.current;
  }, [inspecting]);
  const [quickOpen, setQuickOpen] = useState(false);
  const [treeOpen, setTreeOpen] = useState(false);
  // meiva:note
  // The tree browses another agent's workspace only for the conversation that chose it.
  const [workspaceChoice, setWorkspaceChoice] = useState<{ conversation: number; workspace: number } | null>(null);
  const workspaceAgentId = workspaceChoice?.conversation === agentId ? workspaceChoice.workspace : agentId;
  const setWorkspaceAgentId = (workspace: number) => setWorkspaceChoice({ conversation: agentId, workspace });
  return (
    <aside
      className={cn(
        "relative w-full bg-background",
        FLEX,
        MIN_H_0,
        FLEX_COL,
        mobile
          ? "fixed inset-x-0 top-0 z-50 w-[var(--visual-viewport-width,100%)] h-[var(--visual-viewport-height,100dvh)] pt-[env(safe-area-inset-top)] pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)] pb-[var(--safe-area-bottom,env(safe-area-inset-bottom,0px))]"
          : "h-full",
      )}
      aria-label={t("title")}
    >
      {mobile && <header className={cn("h-11 shrink-0 items-center justify-between border-b px-3 text-xs", FLEX)}>
        <button onClick={onClose} className={cn("items-center gap-2 rounded px-1 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground", FLEX)}>
          <ArrowLeft className="size-3.5" />{t("backToMessages")}
        </button>
        <button
          type="button"
          aria-label={tInspector(inspecting ? "closeInspector" : "openInspector")}
          aria-pressed={inspecting}
          onClick={toggleInspection}
          className={cn("rounded p-1", inspecting && "bg-foreground/20 text-foreground")}
        >
          <ScanSearch className="size-3.5" />
        </button>
      </header>}
      {inspecting ? null : (
        <FileTabs
          agentId={agentId}
          mobile={mobile}
          treeOpen={treeOpen}
          onToggleTree={() => setTreeOpen((open) => !open)}
          onQuickOpen={() => setQuickOpen(true)}
        />
      )}
      {quickOpen && !inspecting ? (
        <QuickOpen agentId={agentId} workspaceAgentId={workspaceAgentId} onClose={() => setQuickOpen(false)} />
      ) : null}
      <div className={cn(MIN_H_0, FLEX_1, FLEX)}>
      {treeOpen && !inspecting ? (
        <FileTree agentId={agentId} workspaceAgentId={workspaceAgentId} onWorkspaceChange={setWorkspaceAgentId} />
      ) : null}
      <div className={cn(MIN_W_0, MIN_H_0, FLEX_1, FLEX, FLEX_COL)}>
      {isViewingFile && !inspecting ? (
        <WorkspaceFileView
          key={`${agentId}:${activeFile.path}:${activeFile.lineRange ?? ""}`}
          agentId={agentId}
          target={activeFile}
          mobile={mobile}
          inspecting={inspecting}
          onInspect={mobile ? undefined : toggleInspection}
          onCollapse={onCollapse}
        />
      ) : (
        <ToUserDocumentView
          key={agentId}
          agentId={agentId}
          document={document.data}
          failure={document.error}
          pending={document.isFetching}
          refresh={() => void document.refetch()}
          mobile={mobile}
          inspecting={inspecting}
          bodyHidden={inspecting}
          onNavigate={resetReadingPosition}
          onInspect={mobile ? undefined : toggleInspection}
          onCollapse={onCollapse}
          viewportRef={documentScroll}
        />
      )}
      {inspecting && (
        <div data-testid="shared-inspector" className={cn(MIN_H_0, FLEX_1, FLEX)}>
          <InspectorPanel agentId={agentId} />
        </div>
      )}
      </div>
      </div>
    </aside>
  );
}
