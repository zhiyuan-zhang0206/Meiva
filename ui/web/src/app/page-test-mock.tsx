import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup } from "@testing-library/react";

import { renderWithBrowser } from "@/test-support/render";
import { useEffect } from "react";
import type { ComponentProps } from "react";
import { vi } from "vitest";

import type { AgentSidebar as AgentSidebarComponent } from "@/components/agent-sidebar";
import type { UploadButton as UploadButtonComponent } from "@/components/conversation/upload-button";
import type { SharedContextPanel as ContextPanelComponent } from "@/components/context/shared-context-panel";
import type { HeaderBar as HeaderBarComponent } from "@/components/shell/header-bar";
import type { TimelineView as TimelineViewComponent } from "@/components/timeline";
import type { AgentRow, BackendTimelineItem, ReplySuggestionResponse } from "@/lib/contracts/types";
import type { ConnectionState } from "@/lib/timeline/use-timeline";
import { HomePageView } from "./home-view";
import type { HomePageProps } from "./home-view";
import { realNetwork, startGateway, type TestGateway } from "@/lib/transport/tests/api/api-provider";
import type { Api } from "@/lib/transport/api";
import { gatewayRig, type HttpRig } from "@/lib/transport/tests/api/api-provider";
import { resetTransport, transport } from "@/test-support/fake-api";
import { fakeStreamHub, resetStreamHandlers, TestStreamProvider } from "@/test-support/fake-stream";
import { AuthProvider } from "@/lib/auth/auth-context";
import { ApiProvider, StreamHubProvider, WallClockProvider } from "@/lib/transport/transport-context";

// meiva:note
// The values a test steers. Each one becomes a required input of the home surface
// (`homePageProps` below); no test replaces a module to reach them.
export const hooksState = {
  isLarge: true,
  isDesktop: true,
  agents: [] as AgentRow[],
  activeId: null as number | null,
  toast: null as string | null,
  timelineItems: [] as BackendTimelineItem[],
  timelineError: null as Error | null,
  timelineFetching: false,
  retryTimeline: vi.fn(),
  loadOlder: vi.fn(),
  connectionState: "open" as ConnectionState,
  turnActive: false,
  spawn: vi.fn().mockResolvedValue(7),
  fork: vi.fn().mockResolvedValue(8),
  terminate: vi.fn().mockResolvedValue(undefined),
  forceExpire: vi.fn().mockResolvedValue(undefined),
  restart: vi.fn().mockResolvedValue(undefined),
  resurrect: vi.fn().mockResolvedValue(undefined),
  compact: vi.fn().mockResolvedValue(undefined),
  showToast: vi.fn(),
  focusComposer: vi.fn(),
  setMobileSidebarOpen: vi.fn(),
  setMobileInspectorOpen: vi.fn(),
  setInspectorHours: vi.fn(),
  requestScrollToBottom: vi.fn(),
  setSetting: vi.fn(),
  settings: { "display.timeline_width_px": 768 } as Record<string, unknown>,
  settingsLoading: false,
  sidebarCollapsed: false,
  sidebarLoading: false,
  inspectorOpen: false,
  inspectorToggle: vi.fn(),
  contextPanelOpen: false,
  contextPanelSetOpen: vi.fn(),
  unresolvedAlerts: 0,
  bfcacheId: "_b_test_",
};

export const inspectorMockState = {
  shouldThrow: false,
  mounts: 0,
};

export function ComposerMock({
  mode,
  onSend,
  onStop,
  stageQueue,
  children,
  details,
  maxWidthCss,
  suggestions,
}: {
  suggestions?: readonly string[] | null;
  mode: string;
  onSend: (s: string, fileIds: string[], clientMessageId: string) => Promise<boolean>;
  onStop: () => void;
  stageQueue?: { seq: number; files: readonly File[] } | null;
  children?: React.ReactNode;
  details?: React.ReactNode;
  maxWidthCss?: string;
}) {
  return (
    <div
      data-testid="composer"
      data-mode={mode}
      data-max-width={maxWidthCss ?? "undefined"}
      data-suggestion={suggestions?.join("|") ?? ""}
      data-stage-seq={String(stageQueue?.seq ?? 0)}
      data-stage-files={stageQueue?.files.map((f) => f.name).join(",") ?? ""}
    >
      <textarea data-testid="composer-paste-target" />
      <button data-testid="composer-send" onClick={() => void onSend("hi", [], "test-client-message-id")}>send</button>
      <button data-testid="composer-send-multi" onClick={() => void onSend("/compact /update", [], "test-client-message-id")}>send multi</button>
      <button data-testid="composer-send-multi-args" onClick={() => void onSend("/search hello world /compact", [], "test-client-message-id")}>send multi args</button>
      <button data-testid="composer-stop" onClick={() => onStop()}>stop</button>
      <button data-testid="composer-send-plain" onClick={() => void onSend("plain text", [], "test-client-message-id")}>send plain</button>
      {details}
      {children}
    </div>
  );
}

export function makeAgent(overrides: Partial<AgentRow>): AgentRow {
  return {
    agent_id: 1,
    spawner: "user",
    harness: "meiva",
    fork_source_agent_id: null,
    status: "idling",
    spawned_at: "2026-05-15T00:00:00Z",
    last_active_at: "2026-05-15T00:00:00Z",
    last_inbound_at: "2026-05-15T00:00:00Z",
    label: null,
    machine: "test",
    supports_vision: true,
    awaiting_response_count: 0,
    highest_notice_priority: null,
    unread_notice_count: 0,
    heartbeat_paused_until: null,
    liveness_state: "online",
    ...overrides,
  };
}

// meiva:note
// A timeline row with the fields the surface reads; the rest are the contract's defaults.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export function makeItem(
  overrides: Pick<BackendTimelineItem, "item_id" | "kind" | "payload"> & Partial<BackendTimelineItem>,
): BackendTimelineItem {
  return { show_timestamp: true, source: null, created_at: null, inbound_id: null, ...overrides };
}

// meiva:note
// Hand-written stand-ins for the children the surface renders. The surface takes each one as a
// required prop, so a test watches the props it computed by looking at these instead of replacing
// the production module they came from.
export function AgentSidebarMock({ onSpawn }: ComponentProps<typeof AgentSidebarComponent>) {
  return (
    <button data-testid="sidebar-spawn" onClick={() => { onSpawn({}); }}>spawn</button>
  );
}

export function HeaderBarMock({ label, children, maxWidthCss }: ComponentProps<typeof HeaderBarComponent>) {
  return (
    <header data-testid="header-bar" data-label={label} style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}>
      {children}
    </header>
  );
}

export function TimelineViewMock({ turnActive, maxWidthCss, items }: ComponentProps<typeof TimelineViewComponent>) {
  return (
    <div
      data-testid="timeline"
      data-turn-active={turnActive ? "1" : "0"}
      data-item-count={items.length}
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
    />
  );
}

export function UploadButtonMock({ agentId, onUpload }: ComponentProps<typeof UploadButtonComponent>) {
  return (
    <button
      type="button"
      data-testid="upload-button"
      data-agent-id={agentId ?? "null"}
      onClick={() => void onUpload([new File(["x"], "picked.txt", { type: "text/plain" })])}
    />
  );
}

export function ContextPanelMock({ agentId }: ComponentProps<typeof ContextPanelComponent>) {
  useEffect(() => {
    inspectorMockState.mounts += 1;
  }, []);
  if (inspectorMockState.shouldThrow) {
    throw new Error("Simulated inspector render crash");
  }
  return <div data-testid="inspector-panel" data-agent-id={agentId} />;
}

// meiva:note
// Every value the surface reads arrives here, and every movable child is one of the stand-ins
// above. A test that wants a different reading sets the field on `hooksState` before rendering.
export function homePageProps(): HomePageProps {
  const state = hooksState;
  return {
    showError: state.showToast,
    store: {
      focusComposer: state.focusComposer,
      composerFocusToken: 0,
      setMobileSidebarOpen: state.setMobileSidebarOpen,
    },
    breakpoint: { isNarrow: !state.isDesktop, isLarge: state.isLarge },
    sidebar: {
      collapsed: state.sidebarCollapsed,
      setCollapsed: (collapsed: boolean) => { state.setSetting("display.sidebar_collapsed", collapsed); },
      isLoading: state.sidebarLoading,
    },
    agents: {
      agents: state.agents,
      ancestors: [],
      activeAgent: state.agents.find((agent) => agent.agent_id === state.activeId),
      activeId: state.activeId,
      pendingActions: {},
      pendingSpawnCount: 0,
      spawn: state.spawn,
      fork: state.fork,
      terminate: state.terminate,
      forceExpire: state.forceExpire,
      restart: state.restart,
      resurrect: state.resurrect,
      compact: state.compact,
      isLoading: false,
    },
    inspector: { open: state.inspectorOpen, toggle: state.inspectorToggle },
    settings: { settings: state.settings, isLoading: state.settingsLoading },
    timeline: {
      items: state.timelineItems,
      compactBuffer: null,
      streamingCode: false,
      turnActive: state.turnActive,
      hasMoreOlder: false,
      loadingOlder: false,
      loadOlder: state.loadOlder,
      isLoading: false,
      retainedItemsMax: 0,
      error: state.timelineError,
      isFetching: state.timelineFetching,
      loadingNewer: false,
      retryTimeline: state.retryTimeline,
    },
    timelineStore: { requestScrollToBottom: state.requestScrollToBottom },
    bfcacheId: state.bfcacheId,
    unresolvedAlerts: state.unresolvedAlerts,
    contextPanel: { open: state.contextPanelOpen, setOpen: state.contextPanelSetOpen },
    AgentSidebar: AgentSidebarMock,
    ContextPanel: ContextPanelMock,
    HeaderBar: HeaderBarMock,
    TimelineView: TimelineViewMock,
    Composer: ComposerMock,
    UploadButton: UploadButtonMock,
  };
}

// meiva:note
// The home surface as a test renders it: the real one, with the values `hooksState` holds.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export function HomePageBound() {
  return <HomePageView {...homePageProps()} />;
}

export let homePageGateway: TestGateway;
export let homePageRig: HttpRig;
let homePageApi: Api;
const { hub } = fakeStreamHub();
export let replySuggestions: ReplySuggestionResponse;

export function setReplySuggestions(value: ReplySuggestionResponse): void {
  replySuggestions = value;
}

// meiva:note
// The page's reads go through the rig's gateway client (a test asserts the requests it made);
// its writes are the fake's `vi.fn()`s, so a test can answer them and count them.
export function gatewayWrapper({ children }: { children: React.ReactNode }) {
  return (
    <ApiProvider api={homePageApi}>
      <StreamHubProvider hub={hub}>
        <AuthProvider>
          <TestStreamProvider>{children}</TestStreamProvider>
        </AuthProvider>
      </StreamHubProvider>
    </ApiProvider>
  );
}

export async function cleanupHomePageTest(): Promise<void> {
  cleanup();
  await homePageGateway.close();
}

// meiva:note
// The home surface renders the shared-context column, which ages its document on the page's clock;
// the real one keeps these asserts as they were.
export function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return renderWithBrowser(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </WallClockProvider>,
    { wrapper: gatewayWrapper },
  );
}

export async function resetHomePageTestState() {
  replySuggestions = { suggestions: [], reason: null };
  homePageGateway = await startGateway(realNetwork.createServer, (request) => {
    if (/^\/api\/agents\/\d+\/reply-suggestion$/.test(request.path)) return { kind: "json", body: replySuggestions };
    if (/^\/api\/agents\/\d+\/pending$/.test(request.path)) return { kind: "json", body: [] };
    if (request.path === "/api/notices") return { kind: "json", body: { open: [], awaiting: [], resolved_page: [], next_cursor: null } };
    if (request.path === "/api/alerts") return { kind: "json", body: { alerts: [], meta: { window: "24h", total: 0, unresolved_count: 0 } } };
    return { kind: "text", status: 404, body: "unexpected route" };
  });
  homePageRig = gatewayRig(homePageGateway);
  resetTransport();
  transport.getReplySuggestion.mockImplementation(() => Promise.resolve(replySuggestions));
  resetStreamHandlers();
  transport.sendMessage.mockResolvedValue(undefined);
  transport.cancel.mockResolvedValue(undefined);
  transport.stageUploadFile.mockResolvedValue(undefined);
  transport.deleteStagedUploadFile.mockResolvedValue(undefined);
  transport.submitUploadedFiles.mockResolvedValue(undefined);
  transport.getUploadDeliveryStatus.mockResolvedValue(undefined);
  transport.listPages.mockResolvedValue([]);
  homePageApi = {
    ...homePageRig.api,
    // meiva:note
    // The page's key source is the fake the test answers, like every other member below: a test
    // that needs the plain-HTTP fallback answers `newOperationKey` itself instead of replacing the
    // page's `crypto` with a stub.
    newOperationKey: transport.newOperationKey,
    sendMessage: transport.sendMessage,
    cancel: transport.cancel,
    stageUploadFile: transport.stageUploadFile,
    deleteStagedUploadFile: transport.deleteStagedUploadFile,
    submitUploadedFiles: transport.submitUploadedFiles,
    getUploadDeliveryStatus: transport.getUploadDeliveryStatus,
    listPages: transport.listPages,
    getReplySuggestion: transport.getReplySuggestion,
  };
  vi.clearAllMocks();
  inspectorMockState.shouldThrow = false;
  inspectorMockState.mounts = 0;
  hooksState.agents = [];
  hooksState.activeId = null;
  hooksState.toast = null;
  hooksState.timelineItems = [];
  hooksState.timelineError = null;
  hooksState.timelineFetching = false;
  hooksState.connectionState = "open";
  hooksState.turnActive = false;
  hooksState.settings = { "display.timeline_width_px": 768 };
  hooksState.setSetting = vi.fn();
  hooksState.setMobileInspectorOpen = vi.fn();
  hooksState.setInspectorHours = vi.fn();
  hooksState.isLarge = true;
  hooksState.isDesktop = true;
  hooksState.settingsLoading = false;
  hooksState.sidebarCollapsed = false;
  hooksState.sidebarLoading = false;
  hooksState.inspectorOpen = false;
  hooksState.contextPanelOpen = false;
  hooksState.unresolvedAlerts = 0;
}
