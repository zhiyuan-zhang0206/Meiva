"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { useCallback, useMemo } from "react";

import { AgentSidebar } from "@/components/agent-sidebar";
import { Composer } from "@/components/conversation/composer";
import { UploadButton } from "@/components/conversation/upload-button";
import { InspectorPanelSkeleton } from "@/components/inspector/inspector-panel-skeleton";
import { HeaderBar } from "@/components/shell/header-bar";
import { AgentEventStreamProvider } from "@/lib/transport/useEventStream";
import { TimelineView } from "@/components/timeline";
import { useAgents } from "@/lib/agents/use-agents";
import { useSidebarCollapsed } from "@/lib/agents/sidebar";
import { useContextPanelOpen } from "@/lib/context/context-panel-open";
import { useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import { useTokenUsage } from "@/lib/inspector/use-token-usage";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { useAlerts } from "@/lib/notifications/use-alerts";
import { useStore } from "@/lib/state/store";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { HomePageView } from "./home-view";

const SharedContextPanel = dynamic(
  () => import("@/components/context/shared-context-panel").then((module) => module.SharedContextPanel),
  { loading: () => <InspectorPanelSkeleton /> },
);

// meiva:note
// This route is the home page's composition root: every read the surface needs is taken here,
// once, and handed down as a value. The surface (`home-view.tsx`) therefore renders from its
// props alone, and a test drives it by passing the values and the children it wants to watch
// instead of replacing a module. Keep hook reads here; keep anything that decides what the
// surface shows out of this function.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export default function HomePage() {
  return (
    <AgentEventStreamProvider>
      <HomePageWiring />
    </AgentEventStreamProvider>
  );
}

// meiva:note
// The wiring sits under <AgentEventStreamProvider>: the timeline, token usage and pending-message
// reads subscribe to the active agent's event stream, and a provider cannot supply a read its own
// component makes.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
function HomePageWiring() {
  const showToast = useStore((state) => state.showToast);
  const focusComposer = useStore((state) => state.focusComposer);
  const composerFocusToken = useStore((state) => state.composerFocusToken);
  const setMobileSidebarOpen = useStore((state) => state.setMobileSidebarOpen);
  const requestScrollToBottom = useTimelineStore((state) => state.requestScrollToBottom);
  const { bfcacheId } = useRouter();
  const showError = useCallback((msg: string) => showToast(msg), [showToast]);
  const breakpoint = useBreakpoint();
  const sidebar = useSidebarCollapsed();
  const agents = useAgents(showError);
  const inspector = useInspectorOpen();
  const settings = useUserSettings();
  const [contextPanelOpen, setContextPanelOpen] = useContextPanelOpen();
  const alerts = useAlerts();
  const timeline = useTimeline(agents.activeId, showError, agents.activeAgent?.status);
  const tokenUsage = useTokenUsage(agents.activeId, showError);
  const store = useMemo(
    () => ({ focusComposer, composerFocusToken, setMobileSidebarOpen }),
    [focusComposer, composerFocusToken, setMobileSidebarOpen],
  );
  const timelineStore = useMemo(() => ({ requestScrollToBottom }), [requestScrollToBottom]);
  const contextPanel = useMemo(
    () => ({ open: contextPanelOpen, setOpen: setContextPanelOpen }),
    [contextPanelOpen, setContextPanelOpen],
  );
  return (
    <HomePageView
      showError={showError}
      store={store}
      breakpoint={breakpoint}
      sidebar={sidebar}
      agents={agents}
      inspector={inspector}
      settings={settings}
      timeline={timeline}
      timelineStore={timelineStore}
      tokenUsage={tokenUsage}
      bfcacheId={bfcacheId}
      unresolvedAlerts={alerts.data?.meta.unresolved_count ?? 0}
      contextPanel={contextPanel}
      AgentSidebar={AgentSidebar}
      ContextPanel={SharedContextPanel}
      HeaderBar={HeaderBar}
      TimelineView={TimelineView}
      Composer={Composer}
      UploadButton={UploadButton}
    />
  );
}
