"use client";

// The page body: the timeline and its context on the left, the details of the
// selection on the right. Wide viewports split the two with a draggable
// handle (the repo's react-resizable-panels wrapper) whose side panel is
// bounded in pixels and whose ratio is remembered per device; narrow viewports
// stack them.

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import { useDefaultLayout } from "react-resizable-panels";

import { ScrollArea } from "@/components/ui/scroll-area";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { FLEX_1, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { usePanelLayoutStorage } from "@/lib/layout/panel-layout-storage";
import { useClientReady } from "@/lib/layout/use-client-ready";
import { cn } from "@/lib/format/utils";

export const RUN_TIMELINE_SPLIT_LAYOUT_ID = "meiva.run-timeline.split";
const PANEL_MAIN = "panel-run-timeline-main";
const PANEL_SIDE = "panel-run-timeline-side";

/** The side panel's width bounds, in pixels. */
export const SIDE_PANEL_DEFAULT = "440px";
export const SIDE_PANEL_MIN = "300px";
export const SIDE_PANEL_MAX = "760px";
const MAIN_PANEL_MIN = "420px";

export function RunTimelineWorkspace({ main, side }: { main: ReactNode; side: ReactNode }) {
  const t = useTranslations("runTimeline");
  const { isWide } = useBreakpoint();
  const clientReady = useClientReady();
  const splitStorage = usePanelLayoutStorage();
  const split = useDefaultLayout({
    id: RUN_TIMELINE_SPLIT_LAYOUT_ID,
    storage: splitStorage,
    onlySaveAfterUserInteractions: true,
  });

  const sideRegion = (
    <aside aria-label={t("detailRegion")} className={cn("h-full", MIN_H_0)}>
      <ScrollArea className="h-full" viewportTestId="run-timeline-reader">
        <div className="px-4 py-3.5 [overflow-wrap:anywhere]">{side}</div>
      </ScrollArea>
    </aside>
  );

  if (!clientReady || !isWide) {
    return (
      <ScrollArea className={cn(FLEX_1, MIN_H_0)} viewportTestId="run-timeline-workspace">
        <div className={cn("space-y-5 p-4", MIN_W_0)}>{main}</div>
        <div className="border-t border-border bg-card">{sideRegion}</div>
      </ScrollArea>
    );
  }

  return (
    <ResizablePanelGroup
      orientation="horizontal"
      defaultLayout={split.defaultLayout}
      onLayoutChanged={split.onLayoutChanged}
      className={cn(FLEX_1, MIN_H_0)}
      data-testid="run-timeline-workspace"
    >
      <ResizablePanel id={PANEL_MAIN} minSize={MAIN_PANEL_MIN}>
        <ScrollArea className={cn("h-full", MIN_W_0)} viewportTestId="run-timeline-main">
          <div className="space-y-5 p-6">{main}</div>
        </ScrollArea>
      </ResizablePanel>
      <ResizableHandle aria-label={t("resizeHandle")} />
      <ResizablePanel
        id={PANEL_SIDE}
        defaultSize={SIDE_PANEL_DEFAULT}
        minSize={SIDE_PANEL_MIN}
        maxSize={SIDE_PANEL_MAX}
        className="bg-card"
      >
        {sideRegion}
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}
