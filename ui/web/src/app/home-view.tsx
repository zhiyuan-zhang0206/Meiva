"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps, ComponentType } from "react";

import type { AgentSidebar as AgentSidebarComponent } from "@/components/agent-sidebar";
import { ErrorBoundary } from "@/components/connection/error-boundary";
import { CodeHighlighterPreloader } from "@/components/content/code-highlighter-preloader";
import type { Composer as ComposerComponent } from "@/components/conversation/composer";
import { PendingStrip } from "@/components/conversation/pending-strip";
import type { UploadButton as UploadButtonComponent } from "@/components/conversation/upload-button";
import type { SharedContextPanel as ContextPanelComponent } from "@/components/context/shared-context-panel";
import { ContextPanelToggle } from "@/components/shell/context-panel-toggle";
import type { HeaderBar as HeaderBarComponent } from "@/components/shell/header-bar";
import { HomeLayout } from "@/components/shell/home-layout";
import type { TimelineView as TimelineViewComponent } from "@/components/timeline";
import { Button } from "@/components/ui/button";
import { usePendingMessages, withoutTimelineDuplicates } from "@/lib/agents/use-pending-messages";
import type { useAgents } from "@/lib/agents/use-agents";
import type { useSidebarCollapsed } from "@/lib/agents/sidebar";
import { useReplySuggestion } from "@/lib/composer/use-reply-suggestion";
import { useToUser } from "@/lib/context/use-to-user";
import type { AgentRow, ContentBlock } from "@/lib/contracts/types";
import { errMsg } from "@/lib/contracts/errors";
import { cn } from "@/lib/format/utils";
import type { Harness } from "@/lib/harness/harness";
import { PRIORITY_RANK } from "@/lib/notifications/notices";
import { useNotices } from "@/lib/notifications/use-notices";
import type { useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import type { TokenUsageState } from "@/lib/inspector/use-token-usage";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import type { useBreakpoint } from "@/lib/layout/breakpoint";
import type { Store } from "@/lib/state/store";
import type { useUserSettings } from "@/lib/state/use-user-settings";
import { timelineMaxWidthCss } from "@/lib/timeline/timeline-width";
import type { UseTimelineResult } from "@/lib/timeline/use-timeline";
import { MessageDeliveryUnknownError } from "@/lib/transport/api";
import { useApi, useStreamHub } from "@/lib/transport/transport-context";

// meiva:note
// The home route splits in two: `page.tsx` is the composition root that reads every hook and
// hands the values down, and this module is the surface that renders them. A test renders the
// surface with the values it wants and the children it wants to watch; nothing here reads a
// store, a router or a breakpoint on its own, so no test has to patch a module to steer it.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md

type AgentSidebarSlot = ComponentType<ComponentProps<typeof AgentSidebarComponent>>;
type ContextPanelSlot = ComponentType<ComponentProps<typeof ContextPanelComponent>>;
type HeaderBarSlot = ComponentType<ComponentProps<typeof HeaderBarComponent>>;
type TimelineViewSlot = ComponentType<ComponentProps<typeof TimelineViewComponent>>;
type ComposerSlot = ComponentType<ComponentProps<typeof ComposerComponent>>;
type UploadButtonSlot = ComponentType<ComponentProps<typeof UploadButtonComponent>>;

type StoreView = Pick<Store, "focusComposer" | "composerFocusToken" | "setMobileSidebarOpen">;
type AgentsView = Pick<
  ReturnType<typeof useAgents>,
  | "agents"
  | "ancestors"
  | "activeAgent"
  | "activeId"
  | "pendingActions"
  | "pendingSpawnCount"
  | "spawn"
  | "fork"
  | "terminate"
  | "forceExpire"
  | "restart"
  | "resurrect"
  | "compact"
  | "isLoading"
>;
type BreakpointView = Pick<ReturnType<typeof useBreakpoint>, "isNarrow" | "isLarge">;
type SidebarView = ReturnType<typeof useSidebarCollapsed>;
type InspectorView = ReturnType<typeof useInspectorOpen>;
type SettingsView = Pick<ReturnType<typeof useUserSettings>, "settings" | "isLoading">;
interface TimelineStoreView {
  requestScrollToBottom: () => void;
}
type TimelineViewState = Pick<
  UseTimelineResult,
  | "items"
  | "compactBuffer"
  | "streamingCode"
  | "turnActive"
  | "hasMoreOlder"
  | "loadingOlder"
  | "loadOlder"
  | "isLoading"
  | "retainedItemsMax"
  | "error"
  | "isFetching"
  | "loadingNewer"
  | "retryTimeline"
>;
type TokenUsageView = Pick<
  TokenUsageState,
  "contextTokens" | "maxContextTokens" | "softCompactTokens" | "hardCompactTokens" | "contextPending"
>;

// meiva:note
// Every field is one source's value, read once by the route: a hook's result narrowed to what the
// surface renders, a store selection, or one of the child components the surface draws. `bfcacheId`
// is the session-history entry the timeline keys its scroll memory on, and `unresolvedAlerts` is the
// 24-hour alert count in the header badge -- both values, so the surface never looks either up.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export interface HomePageProps {
  showError: (msg: string) => void;
  store: StoreView;
  breakpoint: BreakpointView;
  sidebar: SidebarView;
  agents: AgentsView;
  inspector: InspectorView;
  settings: SettingsView;
  timeline: TimelineViewState;
  timelineStore: TimelineStoreView;
  tokenUsage: TokenUsageView;
  bfcacheId: string;
  unresolvedAlerts: number;
  contextPanel: { open: boolean; setOpen: (open: boolean) => void };
  AgentSidebar: AgentSidebarSlot;
  ContextPanel: ContextPanelSlot;
  HeaderBar: HeaderBarSlot;
  TimelineView: TimelineViewSlot;
  Composer: ComposerSlot;
  UploadButton: UploadButtonSlot;
}

export function HomePageView(props: HomePageProps) {
  const {
    agents,
    AgentSidebar,
    bfcacheId,
    breakpoint,
    Composer,
    contextPanel,
    ContextPanel,
    HeaderBar,
    inspector,
    settings,
    showError,
    sidebar,
    store,
    TimelineView,
    timeline,
    timelineStore,
    tokenUsage,
    unresolvedAlerts,
    UploadButton,
  } = props;
  const { isNarrow, isLarge } = breakpoint;
  const {
    agents: roster,
    activeAgent,
    activeId,
    pendingActions,
    pendingSpawnCount,
    isLoading: agentsLoading,
  } = agents;
  const client = useApi();
  const diagnostics = useStreamHub().transport.diagnostics;
  const toUser = useToUser(activeId);
  const noticeFeed = useNotices();
  const [mobileContextOpen, setMobileContextOpen] = useState(false);
  const sidebarEntryResetDone = useRef(false);

  // meiva:note
  // Entering the home surface releases a collapse that was loaded from the preference: the
  // first screen after a load is the conversation, and the reader who wants the rail closed
  // closes it again here. Only the loaded value is released -- a collapse made after mount is
  // the reader's own choice and the reset below does not run twice.
  useEffect(() => {
    if (sidebar.isLoading || sidebarEntryResetDone.current) return;
    sidebarEntryResetDone.current = true;
    if (sidebar.collapsed) sidebar.setCollapsed(false);
  }, [sidebar]);

  const activeNotices = [...noticeFeed.awaiting, ...noticeFeed.open]
    .filter((notice) => notice.agent_id === activeId)
    .sort((a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] || b.created_at.localeCompare(a.created_at));
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  // meiva:note
  // The mode owns the panel: entering inspector while the shared-context column
  // is closed still shows it, because otherwise the mode would be on with
  // nothing to inspect. On mobile the inspector keeps its own sheet.
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md
  const showContext = isLarge
    ? activeId != null && (contextPanel.open || inspector.open)
    : mobileContextOpen || inspector.open;

  const spawn = agents.spawn;
  const focusComposer = store.focusComposer;
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
      return client.listPages(activeId);
    },
    enabled: activeId != null,
    staleTime: 10_000,
  });

  const sidebarColumn = (
    <ErrorBoundary diagnostics={diagnostics}>
      <AgentSidebar
        agents={roster}
        ancestors={agents.ancestors}
        isLoading={agentsLoading}
        pendingActions={pendingActions}
        pendingSpawnCount={pendingSpawnCount}
        onSpawn={handleSpawn}
        onTerminate={agents.terminate}
        onForceExpire={agents.forceExpire}
        onRestart={agents.restart}
        onResurrect={agents.resurrect}
        onFork={agents.fork}
        onCompact={agents.compact}
      />
    </ErrorBoundary>
  );
  const mainColumn = (
    <HomeContent
        activeId={activeId}
        activeAgent={activeAgent}
        showError={showError}
        contextAvailable={!isLarge && activeId != null}
        openContext={() => { setMobileContextOpen(true); if (inspector.open) inspector.toggle(); }}
        contextPanelOpen={contextPanel.open}
        openContextPanel={() => contextPanel.setOpen(true)}
        breakpoint={breakpoint}
        settings={settings}
        store={store}
        timeline={timeline}
        timelineStore={timelineStore}
        tokenUsage={tokenUsage}
        bfcacheId={bfcacheId}
        unresolvedAlerts={unresolvedAlerts}
        HeaderBar={HeaderBar}
        TimelineView={TimelineView}
        Composer={Composer}
        UploadButton={UploadButton}
        sidebar={sidebar}
      />
  );
  const inspectorColumn =
    showContext && activeId != null ? (
      <ErrorBoundary diagnostics={diagnostics} resetKey={activeId}>
        <ContextPanel
          agentId={activeId}
          document={toUser}
          notices={activeNotices}
          noticesFailed={noticeFeed.error}
          mobile={!isLarge}
          onClose={() => { setMobileContextOpen(false); if (inspector.open) inspector.toggle(); }}
          onCollapse={isLarge ? () => contextPanel.setOpen(false) : undefined}
        />
      </ErrorBoundary>
    ) : null;

  return (
    // meiva:note
    // Both wrapper layers carry FLEX explicitly: the outer one as `flex flex-col` and the inner one as `flex` (HomeLayout's narrow branch is a fragment whose children are flex items of it). A wrapper built from FLEX_COL alone is display:block and the chain collapses to content height -- measured 2026-10-17 at 320/390/768: the timeline surface grew to 4920px inside a 664px viewport, I6 failed, and the composer was pushed off screen.
    // meiva:link ui/web/src/app/docs/layout-chain.meiva.okf.md
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0)}>
      <CodeHighlighterPreloader />
      <div className={cn(FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0)}>
        <div className={cn(FLEX, FLEX_1, MIN_H_0, MIN_W_0)}>
          <HomeLayout
            isNarrow={isNarrow}
            isLarge={isLarge}
            sidebarCollapsed={sidebar.collapsed}
            sidebar={sidebarColumn}
            main={mainColumn}
            inspector={inspectorColumn}
          />
        </div>
      </div>
    </main>
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
  breakpoint: BreakpointView;
  settings: SettingsView;
  store: StoreView;
  timeline: TimelineViewState;
  timelineStore: TimelineStoreView;
  tokenUsage: TokenUsageView;
  bfcacheId: string;
  unresolvedAlerts: number;
  HeaderBar: HeaderBarSlot;
  TimelineView: TimelineViewSlot;
  Composer: ComposerSlot;
  UploadButton: UploadButtonSlot;
  sidebar: SidebarView;
}

// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
function HomeContent({
  activeId,
  activeAgent,
  showError,
  contextAvailable,
  openContext,
  contextPanelOpen,
  openContextPanel,
  breakpoint,
  settings,
  store,
  timeline,
  timelineStore,
  tokenUsage,
  bfcacheId,
  unresolvedAlerts,
  HeaderBar,
  TimelineView,
  Composer,
  UploadButton,
  sidebar,
}: HomeContentProps) {
  const client = useApi();
  const diagnostics = useStreamHub().transport.diagnostics;
  const t = useTranslations("common");
  const timelineText = useTranslations("timeline");
  const { isNarrow, isLarge } = breakpoint;
  const { settings: userSettings, isLoading: userSettingsLoading } = settings;
  const contentColumnMaxWidth = isNarrow
    ? undefined
    : timelineMaxWidthCss(userSettings["display.timeline_width_px"]);
  const composerMaxWidth = isNarrow
    ? undefined
    : timelineMaxWidthCss(userSettings["display.timeline_width_px"]);

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
        // meiva:note
        // The upload's idempotency key comes from the transport this page was handed: which origin
        // can mint one (crypto.randomUUID, or random bytes on plain HTTP) is the transport's
        // business, and a test that needs the fallback answers `newOperationKey` itself instead of
        // replacing the page's `crypto`.
        await client.submitUploadedFiles(activeId, files, client.newOperationKey(), setUploadProgress);
      } catch (e: unknown) {
        setUploadError(errMsg(e));
      } finally {
        setUploading(false);
      }
    },
    [activeId, client],
  );

  const {
    items,
    compactBuffer,
    streamingCode,
    turnActive,
    hasMoreOlder,
    loadingOlder,
    loadOlder,
    isLoading,
    retainedItemsMax,
    error: timelineError,
    isFetching,
    loadingNewer,
    retryTimeline,
  } = timeline;
  const { contextTokens, maxContextTokens, softCompactTokens, hardCompactTokens, contextPending } = tokenUsage;
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
          await client.sendMessage(activeId, content, clientMessageId);
        } else {
          const blocks: ContentBlock[] = [];
          if (content) blocks.push({ type: "text", text: content });
          for (const u of imageUrls) blocks.push({ type: "image_url", image_url: { url: u } });
          await client.sendMessage(activeId, blocks, clientMessageId);
        }
        timelineStore.requestScrollToBottom();
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
    [activeId, client, showError, timelineStore, t],
  );

  const attachImage = useCallback(
    async (file: File): Promise<string> => {
      if (activeId == null) throw new Error("no active agent");
      const batch = await client.uploadFiles(activeId, [file]);
      return batch.files[0].url;
    },
    [activeId, client],
  );

  const handleStop = useCallback(async () => {
    if (activeId == null) return;
    try {
      await client.cancel(activeId);
    } catch (e: unknown) {
      showError(`Cancel failed: ${errMsg(e)}`);
    }
  }, [activeId, client, showError]);

  // meiva:note
  // The bar names the agent in every mode, Inspector included: the mode changes what the
  // right column shows, not which conversation the reader is in, and a title reading
  // "Inspector" made the agent it still belonged to disappear (user 2026-10-21). The mode
  // stays visible in its own pressed toggle and in the column itself.
  // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
  const activeLabel = activeAgent
    ? `Agent #${activeAgent.agent_id}${activeAgent.label ? ` \u00b7 ${activeAgent.label}` : ""}`
    : "\u2026";
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
      <ErrorBoundary diagnostics={diagnostics}>
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
            onOpenSidebar={() => store.setMobileSidebarOpen(true)}
            sidebarCollapsed={sidebar.collapsed}
            alertCount={unresolvedAlerts}
            onToggleSidebar={() => {
              if (isNarrow) store.setMobileSidebarOpen(true);
              else sidebar.setCollapsed(!sidebar.collapsed);
            }}
            maxWidthCss={contentColumnMaxWidth}
            pinnedRight={
              isLarge && activeId != null && !contextPanelOpen ? (
                <ContextPanelToggle open={false} onToggle={openContextPanel} />
              ) : null
            }
          >
            {contextAvailable && <ContextPanelToggle open={false} onToggle={openContext} />}
          </HeaderBar>
          <div className="relative z-20 bg-background">
            <PendingStrip items={visiblePendingMessages} maxWidthCss={composerMaxWidth} />
            {activeAgent?.harness === "meiva" && activeAgent.supports_vision === null && (
              <p role="status" className="px-3 py-2 text-sm text-muted-foreground">
                {t("modelCapabilitiesUnavailable")}
              </p>
            )}
            <Composer
              diagnostics={diagnostics}
              maxWidthCss={composerMaxWidth}
              mode={composerMode}
              onSend={handleSend}
              onStop={handleStop}
              onUploadFiles={uploadHandler}
              onAttachImage={activeAgent?.harness === "meiva" && activeAgent.supports_vision === true ? attachImage : undefined}
              filesUploading={uploading}
              focusToken={store.composerFocusToken}
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
