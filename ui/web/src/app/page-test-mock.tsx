import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { useState } from "react";
import { vi } from "vitest";

import type { AgentRow, ReplySuggestionResponse } from "@/lib/contracts/types";
import type { ConnectionState } from "@/lib/timeline/use-timeline";
import { realNetwork, startGateway, type TestGateway } from "@/lib/transport/tests/api/api-provider";
import type { Api } from "@/lib/transport/api";
import { gatewayRig, type HttpRig } from "@/lib/transport/tests/api/api-provider";
import { resetTransport, transport } from "@/test-support/fake-api";
import { fakeStreamHub, resetStreamHandlers, TestStreamProvider } from "@/test-support/fake-stream";
import { AuthProvider } from "@/lib/auth/auth-context";
import { ApiProvider, StreamHubProvider } from "@/lib/transport/transport-context";

export const hooksState = {
  isLarge: true,
  isDesktop: true,
  agents: [] as AgentRow[],
  activeId: null as number | null,
  toast: null as string | null,
  timelineItems: [] as unknown[],
  timelineError: null as Error | null,
  timelineFetching: false,
  retryTimeline: vi.fn(),
  connectionState: "open" as ConnectionState,
  turnActive: false,
  spawn: vi.fn().mockResolvedValue(7),
  fork: vi.fn().mockResolvedValue(8),
  terminate: vi.fn().mockResolvedValue(undefined),
  restart: vi.fn().mockResolvedValue(undefined),
  resurrect: vi.fn().mockResolvedValue(undefined),
  showToast: vi.fn(),
  focusComposer: vi.fn(),
  setMobileSidebarOpen: vi.fn(),
  setMobileInspectorOpen: vi.fn(),
  setInspectorHours: vi.fn(),
  requestScrollToBottom: vi.fn(),
  setSetting: vi.fn(),
  settings: { "display.timeline_width_ratio": 0.4 } as Record<string, unknown>,
};

export const inspectorMockState = {
  shouldThrow: false,
  mounts: 0,
};

export function ComposerMock({
  mode,
  onSend,
  onStop,
  onUploadFiles,
  onAttachImage,
  showContextMeter,
  children,
  details,
  maxWidthCss,
  suggestions,
}: {
  suggestions?: readonly string[] | null;
  mode: string;
  onSend: (s: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>;
  onStop: () => Promise<void>;
  onUploadFiles?: (files: File[]) => void;
  onAttachImage?: (file: File) => Promise<string>;
  showContextMeter?: boolean;
  children?: React.ReactNode;
  details?: React.ReactNode;
  maxWidthCss?: string;
}) {
  const [thumbnail, setThumbnail] = useState<string | null>(null);
  const pasteImage = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const image = Array.from(event.clipboardData.files).find((file) =>
      file.type.startsWith("image/"),
    );
    if (!image) return;
    event.preventDefault();
    if (onAttachImage) void onAttachImage(image).then(setThumbnail);
    else onUploadFiles?.([image]);
  };
  return (
    <div
      data-testid="composer"
      data-mode={mode}
      data-context-meter={String(showContextMeter ?? false)}
      data-max-width={maxWidthCss ?? "undefined"}
      data-suggestion={suggestions?.join("|") ?? ""}
    >
      <textarea data-testid="composer-paste-target" onPaste={pasteImage} />
      {thumbnail ? <div data-testid="composer-image-thumbnail" data-url={thumbnail} /> : null}
      <button data-testid="composer-send" onClick={() => void onSend("hi", [], "test-client-message-id")}>send</button>
      <button data-testid="composer-send-multi" onClick={() => void onSend("/compact /update", [], "test-client-message-id")}>send multi</button>
      <button data-testid="composer-send-multi-args" onClick={() => void onSend("/search hello world /compact", [], "test-client-message-id")}>send multi args</button>
      <button data-testid="composer-stop" onClick={() => void onStop()}>stop</button>
      <button data-testid="composer-send-plain" onClick={() => void onSend("plain text", [], "test-client-message-id")}>send plain</button>
      <button data-testid="composer-send-multi-image" onClick={() => void onSend("/compact /update", ["/api/agents/5/uploads/a.png"], "test-client-message-id")}>send multi image</button>
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

export function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>, {
    wrapper: gatewayWrapper,
  });
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
  resetStreamHandlers();
  transport.sendMessage.mockResolvedValue(undefined);
  transport.cancel.mockResolvedValue(undefined);
  transport.uploadFiles.mockResolvedValue(undefined);
  transport.submitUploadedFiles.mockResolvedValue(undefined);
  transport.listPages.mockResolvedValue([]);
  homePageApi = {
    ...homePageRig.api,
    sendMessage: transport.sendMessage,
    cancel: transport.cancel,
    uploadFiles: transport.uploadFiles,
    submitUploadedFiles: transport.submitUploadedFiles,
    listPages: transport.listPages,
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
  hooksState.settings = { "display.timeline_width_ratio": 0.4 };
  hooksState.setSetting = vi.fn();
  hooksState.setMobileInspectorOpen = vi.fn();
  hooksState.setInspectorHours = vi.fn();
  hooksState.isLarge = true;
  hooksState.isDesktop = true;
}
