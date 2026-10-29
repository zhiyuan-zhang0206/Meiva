"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ScanSearch } from "lucide-react";
import dynamic from "next/dynamic";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef } from "react";

import { InspectorPanelSkeleton } from "@/components/inspector/inspector-panel-skeleton";
import { ScrollArea } from "@/components/ui/scroll-area";
import { OpenNoticeDetail } from "@/components/notifications/open-notice-detail";
import { ToUserDocumentView } from "./to-user-document";
import type { NoticeItem } from "@/lib/contracts/types";
import type { useToUser } from "@/lib/context/use-to-user";
import { useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import { dropOpenNotices } from "@/lib/notifications/use-notices";
import { cn } from "@/lib/format/utils";

const Inspector = dynamic(() => import("@/components/inspector/inspector-panel").then((m) => m.InspectorPanel), {
  loading: () => <InspectorPanelSkeleton />,
});

// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
export function SharedContextPanel({ agentId, document, notices, noticesFailed, mobile, onClose, onCollapse }: {
  agentId: number;
  document: ReturnType<typeof useToUser>;
  notices: NoticeItem[];
  noticesFailed: boolean;
  mobile: boolean;
  onClose: () => void;
  onCollapse?: () => void;
}) {
  const t = useTranslations("toUser");
  const tInspector = useTranslations("inspector");
  const { open: inspecting, toggle: toggleInspector } = useInspectorOpen();
  const queryClient = useQueryClient();
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
  const notice = notices.at(0);
  // meiva:note
  // The column reads top to bottom as title bar, reading area, question: the
  // question is the column's own bottom region, pinned under the one scrolling
  // area rather than travelling inside it, and it stays there in Inspector mode
  // (user report 2026-10-30 — a pending question must not disappear with the
  // body it used to sit in). Inspector replaces the reading area above it; its
  // toggle stays in the desktop document bar or narrow top bar so the active
  // mode always has an exit.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  return (
    <aside className={cn("relative w-full bg-background", FLEX, MIN_H_0, FLEX_COL, mobile ? "fixed inset-0 z-50" : "h-full")} aria-label={t("title")}>
      {mobile && <header className={cn("h-11 shrink-0 items-center justify-between border-b px-3 text-xs", FLEX)}>
        <button onClick={onClose} className={cn("items-center gap-2 rounded px-1 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground", FLEX)}>
          <ArrowLeft className="size-3.5" />{t("backToMessages")}
        </button>
        <button
          type="button"
          title={tInspector(inspecting ? "closeInspector" : "openInspector")}
          aria-label={tInspector(inspecting ? "closeInspector" : "openInspector")}
          aria-pressed={inspecting}
          onClick={toggleInspection}
          className={cn("rounded p-1", inspecting && "bg-foreground/20 text-foreground")}
        >
          <ScanSearch className="size-3.5" />
        </button>
      </header>}
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
      {inspecting && (
        <div data-testid="shared-inspector" className={cn(MIN_H_0, FLEX_1, FLEX)}>
          <Inspector agentId={agentId} embedded />
        </div>
      )}
      {noticesFailed && <p role="status" className="shrink-0 border-t border-border/60 px-5 py-2 text-xs text-muted-foreground">{t("noticesUnavailable")}</p>}
      {notice && (
        <section data-testid="notice-region" className="shrink-0 border-t border-border/60" aria-label={t("notification")}>
          <ScrollArea type="auto" className={QUESTION_REGION_MAX_HEIGHT_CLASS}>
            <div className="px-5 py-3">
              <OpenNoticeDetail key={notice.id} agentId={agentId} notice={notice} onResolved={() => dropOpenNotices(queryClient, [notice.id])} />
            </div>
          </ScrollArea>
        </section>
      )}
    </aside>
  );
}
