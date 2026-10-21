"use client";

import { useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import dynamic from "next/dynamic";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef } from "react";

import { InspectorPanelSkeleton } from "@/components/inspector/inspector-panel-skeleton";
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

// meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
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
  const { open: inspecting, toggle: toggleInspector } = useInspectorOpen();
  const queryClient = useQueryClient();
  const documentScroll = useRef<HTMLDivElement>(null);
  const readingPosition = useRef(0);
  const resetReadingPosition = useCallback(() => {
    readingPosition.current = 0;
    if (documentScroll.current) documentScroll.current.scrollTop = 0;
  }, []);
  useEffect(() => {
    if (!inspecting && documentScroll.current) documentScroll.current.scrollTop = readingPosition.current;
  }, [inspecting]);
  const notice = notices.at(0);
  // meiva:note
  // Inspector is a mode, not a second surface: the document bar above already
  // says which one is on and carries the pressed magnifier that turns it off,
  // so the overlay below adds no header and no second exit (user 2026-10-20:
  // the panel should read like a mode being on).
  // meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
  return (
    <aside className={cn("relative w-full bg-background", FLEX, MIN_H_0, FLEX_COL, mobile ? "fixed inset-0 z-50" : "h-full")} aria-label={t("sharedContext")}>
      {mobile && <header className={cn("h-11 shrink-0 items-center justify-between border-b px-4 text-sm", FLEX)}>
        <span>{t("sharedContext")}</span><button aria-label={t("backToMessages")} onClick={onClose} className="rounded p-1 hover:bg-accent"><X className="size-4" /></button>
      </header>}
      <div ref={documentScroll} data-testid="shared-document" className={cn(MIN_H_0, FLEX_1, inspecting ? [FLEX_COL, FLEX] : "overflow-y-auto")}>
        <ToUserDocumentView
          key={agentId}
          agentId={agentId}
          document={document.data}
          failed={document.isError}
          pending={document.isFetching}
          refresh={() => void document.refetch()}
          mobile={mobile}
          inspecting={inspecting}
          bodyHidden={inspecting}
          onNavigate={resetReadingPosition}
          onInspect={() => {
            if (!inspecting) readingPosition.current = documentScroll.current?.scrollTop ?? 0;
            toggleInspector();
          }}
          onCollapse={onCollapse}
        >
          {noticesFailed && <p hidden={inspecting} role="status" className="px-5 py-2 text-xs text-muted-foreground">{t("noticesUnavailable")}</p>}
          {notice && <section hidden={inspecting} className="border-b border-border/60 px-5 py-4" aria-label={t("notification")}>
            <OpenNoticeDetail key={notice.id} agentId={agentId} notice={notice} onResolved={() => dropOpenNotices(queryClient, [notice.id])} />
          </section>}
        </ToUserDocumentView>
        {inspecting && (
          <div data-testid="shared-inspector" className={cn(MIN_H_0, FLEX_1, FLEX)}>
            <Inspector agentId={agentId} embedded />
          </div>
        )}
      </div>
    </aside>
  );
}
