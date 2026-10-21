"use client";

import type { Harness } from "@/lib/harness/harness";
import { newOperationKey } from "@/lib/transport/operation-key";

import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AgentSidebar } from "@/components/agent-sidebar";
import { CodeHighlighterPreloader } from "@/components/content/code-highlighter-preloader";
import { ErrorBoundary } from "@/components/connection/error-boundary";
import { Composer } from "@/components/conversation/composer";
import { PermissionBanner } from "@/components/permissions/permission-banner";
import { HeaderBar } from "@/components/shell/header-bar";
import { ContextPanelToggle } from "@/components/shell/context-panel-toggle";
import { HomeLayout } from "@/components/shell/home-layout";
import { InspectorPanelSkeleton } from "@/components/inspector/inspector-panel-skeleton";
import { useToUser } from "@/lib/context/use-to-user";
import { useAlerts } from "@/lib/notifications/use-alerts";
import { useNotices } from "@/lib/notifications/use-notices";
import { PRIORITY_RANK } from "@/lib/notifications/notices";
import { PendingStrip } from "@/components/conversation/pending-strip";
import { UploadButton } from "@/components/conversation/upload-button";
import { TimelineView } from "@/components/timeline";
import { Button } from "@/components/ui/button";
import { api, MessageDeliveryUnknownError } from "@/lib/transport/api";
import { errMsg } from "@/lib/contracts/errors";
import { useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { useContextPanelOpen } from "@/lib/context/context-panel-open";
import { useSidebarCollapsed } from "@/lib/agents/sidebar";
import { useStore } from "@/lib/state/store";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import type { AgentRow, ContentBlock } from "@/lib/contracts/types";
import { useAgents } from "@/lib/agents/use-agents";
import { usePendingMessages, withoutTimelineDuplicates } from "@/lib/agents/use-pending-messages";
import { useReplySuggestion } from "@/lib/composer/use-reply-suggestion";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { timelineMaxWidthCss } from "@/lib/timeline/timeline-width";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { useTokenUsage } from "@/lib/inspector/use-token-usage";
import { AgentEventStreamProvider } from "@/lib/transport/useEventStream";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

const SharedContextPanel = dynamic(
  () => import("@/components/context/shared-context-panel").then((module) => module.SharedContextPanel),
  { loading: () => <InspectorPanelSkeleton /> },
);

export default function HomePage() {
  const showToast = useStore((s) => s.showToast);

  const showError = useCallback(
    (msg: string) => showToast(msg),
    [showToast],
  );

  return (
    // meiva:note
    // The page stacks the permission banner above the shell, and both wrapper layers carry FLEX explicitly: the outer one as `flex flex-col` (banner row plus shell row) and the inner one as `flex` (HomeLayout's narrow branch is a fragment whose children are flex items of it). A wrapper built from FLEX_COL alone is display:block and the chain collapses to content height -- measured 2026-10-17 at 320/390/768: the timeline surface grew to 4920px inside a 664px viewport, I6 failed, and the composer was pushed off screen. The banner adds one row when the host is restricted and renders nothing otherwise, so the geometry with no banner is the pre-banner geometry.
    // meiva:link ui/web/src/app/docs/layout-chain.meiva.okf.md
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0)}>
      <CodeHighlighterPreloader />
      <div className={cn(FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0)}>
        <PermissionBanner />
        <div className={cn(FLEX, FLEX_1, MIN_H_0, MIN_W_0)}>
          <HomeShell showError={showError} />
        </div>
      </div>
    </main>
  );
}

interface HomeShellProps {
  showError: (msg: string) => void;
}

function HomeShell({ showError }: HomeShellProps) {
  const focusComposer = useStore((s) => s.focusComposer);
  const { isNarrow, isLarge } = useBreakpoint();
  const {
    collapsed: sidebarCollapsed,
    setCollapsed: setSidebarCollapsed,
    isLoading: sidebarSettingsLoading,
  } = useSidebarCollapsed();
  const sidebarEntryResetDone = useRef(false);

  useEffect(() => {
    if (sidebarSettingsLoading || sidebarEntryResetDone.current) return;
    sidebarEntryResetDone.current = true;
    if (sidebarCollapsed) setSidebarCollapsed(false);
  }, [sidebarCollapsed, sidebarSettingsLoading, setSidebarCollapsed]);

  const {
    agents,
    ancestors,
    activeAgent,
    activeId,
    pendingActions,
    pendingSpawnCount,
    spawn,
    fork,
    terminate,
    forceExpire,
    restart,
    resurrect,
    compact,
    isLoading: agentsLoading,
  } = useAgents(showError);

  const { open: inspectorOpen, toggle: toggleInspector } = useInspectorOpen();
  const toUser = useToUser(activeId);
  const noticeFeed = useNotices();
  const [mobileContextOpen, setMobileContextOpen] = useState(false);
  const activeNotices = [...noticeFeed.awaiting, ...noticeFeed.open]
    .filter((notice) => notice.agent_id === activeId)
    .sort((a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] || b.created_at.localeCompare(a.created_at));
  // meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
  const [contextPanelOpen, setContextPanelOpen] = useContextPanelOpen();
  // meiva:note
  // The mode owns the panel: entering inspector while the shared-context column
  // is closed still shows it, because otherwise the mode would be on with
  // nothing to inspect. On mobile the inspector keeps its own sheet.
  // meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
  const showContext = isLarge
    ? activeId != null && (contextPanelOpen || inspectorOpen)
    : mobileContextOpen || inspectorOpen;

  const handleSpawn = useCallback(
    async ({
      machine,
      model,
      reasoning_effort,
      harness,
    }: {
      machine?: string;
      model?: string;
      reasoning_effort?: string;
      harness?: Harness;
    }) => {
      const newId = await spawn(machine, model, reasoning_effort, harness);
      if (newId !== null) focusComposer();
    },
    [spawn, focusComposer],
  );

  useQuery({
    queryKey: ["agent-pages", activeId] as const,
    queryFn: () => {
      if (activeId == null) throw new Error("pages preload: activeId is null");
      return api.listPages(activeId);
    },
    enabled: activeId != null,
    staleTime: 10_000,
  });

  const sidebar = (
    <ErrorBoundary>
      <AgentSidebar
        agents={agents}
        ancestors={ancestors}
        isLoading={agentsLoading}
        pendingActions={pendingActions}
        pendingSpawnCount={pendingSpawnCount}
        onSpawn={handleSpawn}
        onTerminate={terminate}
        onForceExpire={forceExpire}
        onRestart={restart}
        onResurrect={resurrect}
        onFork={fork}
        onCompact={compact}
      />
    </ErrorBoundary>
  );
  const main = (
    <AgentEventStreamProvider>
      <HomeContent
        activeId={activeId}
        activeAgent={activeAgent}
        showError={showError}
        contextAvailable={!isLarge && activeId != null}
        openContext={() => { setMobileContextOpen(true); if (inspectorOpen) toggleInspector(); }}
        contextPanelOpen={contextPanelOpen}
        openContextPanel={() => setContextPanelOpen(true)}
      />
    </AgentEventStreamProvider>
  );
  const inspector =
    showContext && activeId != null ? (
      <ErrorBoundary resetKey={activeId}>
        <SharedContextPanel
          agentId={activeId}
          document={toUser}
          notices={activeNotices}
          noticesFailed={noticeFeed.error}
          mobile={!isLarge}
          onClose={() => { setMobileContextOpen(false); if (inspectorOpen) toggleInspector(); }}
          onCollapse={isLarge ? () => setContextPanelOpen(false) : undefined}
        />
      </ErrorBoundary>
    ) : null;

  return (
    <HomeLayout
      isNarrow={isNarrow}
      isLarge={isLarge}
      sidebarCollapsed={sidebarCollapsed}
      sidebar={sidebar}
      main={main}
      inspector={inspector}
    />
  );
}

interface HomeContentProps {
  activeId: number | null;
  activeAgent: AgentRow | undefined;
  showError: (msg: string) => void;
  contextAvailable: boolean;
  openContext: () => void;
  contextPanelOpen: boolean;
  openContextPanel: () => void;
}

function HomeContent({
  activeId,
  activeAgent,
  showError,
  contextAvailable,
  openContext,
  contextPanelOpen,
  openContextPanel,
}: HomeContentProps) {
  const t = useTranslations("common");
  const timelineText = useTranslations("timeline");
  const composerFocusToken = useStore((s) => s.composerFocusToken);
  const { settings: userSettings, isLoading: userSettingsLoading } = useUserSettings();
  const unresolvedAlerts = useAlerts().data?.meta.unresolved_count ?? 0;
  const { isNarrow, isLarge } = useBreakpoint();
  const { collapsed: sidebarCollapsed, setCollapsed: setSidebarCollapsed } = useSidebarCollapsed();
  const contentColumnMaxWidth = isNarrow
    ? undefined
    : timelineMaxWidthCss(userSettings["display.timeline_width_ratio"]);
  const composerMaxWidth = isNarrow
    ? undefined
    : timelineMaxWidthCss(userSettings["display.timeline_width_ratio"]);
  const setMobileSidebarOpen = useStore((s) => s.setMobileSidebarOpen);
  const requestScrollToBottom = useTimelineStore((s) => s.requestScrollToBottom);
  const { bfcacheId } = useRouter();

  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadCount, setUploadCount] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const uploadHandler = useCallback(
    async (files: File[]) => {
      if (activeId == null || files.length === 0) return;
      setUploading(true);
      setUploadProgress(0);
      setUploadCount(files.length);
      setUploadError(null);
      try {
        await api.submitUploadedFiles(activeId, files, newOperationKey(), setUploadProgress);
      } catch (e: unknown) {
        setUploadError(errMsg(e));
      } finally {
        setUploading(false);
      }
    },
    [activeId],
  );

  const { items, compactBuffer, streamingCode, turnActive, hasMoreOlder, loadingOlder, loadOlder, isLoading, retainedItemsMax, error: timelineError, isFetching, loadingNewer, retryTimeline } =
    useTimeline(activeId, showError, activeAgent?.status);
  const { contextTokens, maxContextTokens, softCompactTokens, hardCompactTokens, contextPending } = useTokenUsage(
    activeId,
    showError,
  );
  const pendingMessages = usePendingMessages(activeId, showError);
  const visiblePendingMessages = useMemo(
    () => withoutTimelineDuplicates(pendingMessages, items),
    [pendingMessages, items],
  );

  const handleSend = useCallback(
    async (
      content: string,
      imageUrls: string[],
      clientMessageId: string,
    ): Promise<boolean> => {
      if (activeId == null) return false;
      try {
        if (imageUrls.length === 0) {
          await api.sendMessage(activeId, content, clientMessageId);
        } else {
          const blocks: ContentBlock[] = [];
          if (content) blocks.push({ type: "text", text: content });
          for (const u of imageUrls) blocks.push({ type: "image_url", image_url: { url: u } });
          await api.sendMessage(activeId, blocks, clientMessageId);
        }
        requestScrollToBottom();
        return true;
      } catch (e: unknown) {
        if (e instanceof MessageDeliveryUnknownError) {
          showError(t("deliveryUnconfirmedToast"));
          throw e;
        }
        showError(`Send failed: ${errMsg(e)}`);
        return false;
      }
    },
    [activeId, showError, requestScrollToBottom, t],
  );

  const attachImage = useCallback(
    async (file: File): Promise<string> => {
      if (activeId == null) throw new Error("no active agent");
      const batch = await api.uploadFiles(activeId, [file]);
      return batch.files[0].url;
    },
    [activeId],
  );

  const handleStop = useCallback(async () => {
    if (activeId == null) return;
    try {
      await api.cancel(activeId);
    } catch (e: unknown) {
      showError(`Cancel failed: ${errMsg(e)}`);
    }
  }, [activeId, showError]);

  // meiva:note
  // The bar names the agent in every mode, Inspector included: the mode changes what the
  // right column shows, not which conversation the reader is in, and a title reading
  // "Inspector" made the agent it still belonged to disappear (user 2026-10-21). The mode
  // stays visible in its own pressed toggle and in the column itself.
  // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
  const activeLabel = activeAgent
    ? `Agent #${activeAgent.agent_id}${activeAgent.label ? ` · ${activeAgent.label}` : ""}`
    : "…";
  const agentBusy = turnActive || activeAgent?.status === "running";
  const composerMode: "disabled" | "idle" | "busy" =
    activeId == null ? "disabled" : agentBusy ? "busy" : "idle";
  const replySuggestion = useReplySuggestion({
    agentId: activeId,
    status: activeAgent?.status,
    busy: agentBusy,
    items,
    enabled: !userSettingsLoading && userSettings["behavior.replySuggestions"] === true,
  });

  return (
    <section className={cn("relative", FLEX, MIN_H_0, FLEX_1, MIN_W_0, OVERFLOW_HIDDEN)}>
      <ErrorBoundary>
        <div data-testid="timeline-surface" className={cn("relative", FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0)}>
          {timelineError && (
            <div role="alert" className="mx-4 mt-14 mb-2 rounded-md border border-destructive/30 p-3 text-sm">
              <p>{timelineText(items.length > 0 ? "refreshFailed" : "loadFailed")}</p>
              <p className="mt-1 break-words text-xs text-muted-foreground">{errMsg(timelineError)}</p>
              <Button className="mt-2" variant="outline" size="sm" disabled={isFetching} onClick={retryTimeline}>
                {timelineText("retryLoad")}
              </Button>
            </div>
          )}
          <TimelineView
            key={activeId ?? "unselected"}
            items={items}
            harness={activeAgent?.harness ?? null}
            compactBuffer={compactBuffer}
            threadKey={activeId != null ? String(activeId) : undefined}
            scrollMemoryKey={`${bfcacheId}:agent:${activeId ?? "unselected"}`}
            streamingCode={streamingCode}
            turnActive={agentBusy}
            hasMoreOlder={hasMoreOlder}
            loadingOlder={loadingOlder}
            onLoadOlder={loadOlder}
            retainedItemsMax={retainedItemsMax}
            loading={isLoading}
            loadingNew={loadingNewer && items.length > 0}
            maxWidthCss={contentColumnMaxWidth}
          />
          <HeaderBar
            label={activeLabel}
            onOpenSidebar={() => setMobileSidebarOpen(true)}
            sidebarCollapsed={sidebarCollapsed}
            alertCount={unresolvedAlerts}
            onToggleSidebar={() => {
              if (isNarrow) setMobileSidebarOpen(true);
              else setSidebarCollapsed(!sidebarCollapsed);
            }}
            maxWidthCss={contentColumnMaxWidth}
            pinnedRight={
              isLarge && activeId != null && !contextPanelOpen ? (
                <ContextPanelToggle
                  open={false}
                  iconClass="size-4"
                  onToggle={openContextPanel}
                />
              ) : null
            }
          >
            {contextAvailable && <ContextPanelToggle open={false} onToggle={openContext} iconClass="size-4" />}
          </HeaderBar>
          <div className="relative z-20 bg-background">
            <PendingStrip items={visiblePendingMessages} maxWidthCss={composerMaxWidth} />
            {activeAgent?.harness === "meiva" && activeAgent.supports_vision === null && (
              <p role="status" className="px-3 py-2 text-sm text-muted-foreground">
                {t("modelCapabilitiesUnavailable")}
              </p>
            )}
            <Composer
              maxWidthCss={composerMaxWidth}
              mode={composerMode}
              onSend={handleSend}
              onStop={handleStop}
              onUploadFiles={uploadHandler}
              onAttachImage={activeAgent?.harness === "meiva" && activeAgent.supports_vision === true ? attachImage : undefined}
              filesUploading={uploading}
              focusToken={composerFocusToken}
              contextTokens={contextTokens}
              maxContextTokens={maxContextTokens}
              softCompactTokens={softCompactTokens}
              hardCompactTokens={hardCompactTokens}
              contextPending={contextPending}
              agentId={activeId}
              agentTerminated={activeAgent?.status === "terminated"}
              suggestions={replySuggestion}
              showContextMeter={activeAgent?.harness === "meiva" && userSettings["display.composer_context_meter"] === true}
            >
              <UploadButton
                agentId={activeId}
                onUpload={uploadHandler}
                uploading={uploading}
                progress={uploadProgress}
                count={uploadCount}
                error={uploadError}
              />
            </Composer>
          </div>
        </div>
      </ErrorBoundary>
    </section>
  );
}
