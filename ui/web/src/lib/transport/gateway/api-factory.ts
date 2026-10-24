import type {
  NoticesFeed,
  AgentInspectStatistics,
  AgentInspectLive,
  InspectWidget,
  AgentMessageEnqueued,
  AgentRow,
  WireAgentRow,
  WireAgentRoster,
  WireAgentDirectoryPage,
  AgentRoster,
  AgentDirectoryPage,
  ConversationSnapshotResponse,
  ContextBreakdownResponse,
  CommandItem,
  ContentBlock,
  TaskListResponse,
  TaskFields,
  TaskRow,
  TaskSummaryRow,
  PendingInbound,
  RunTimelineContext,
  RunTimelineLinkContent,
  RunTimelineLinks,
  RunTimelineMessages,
  RunTimelineResponse,
  ShellCapture,
  TimelineResponse,
  TokenUsageResponse,
  ReplySuggestionResponse,
  UploadedBatch,
} from "../../contracts/types";
import { projectAgentStatus } from "../../contracts/types";
import { agentShellPath, commandsPath, inspectStatisticsPath, listAgentsPath, tasksPath } from "./routes/agent-routes";
import { createControlApi } from "./api-control";
import { noticesPath } from "./routes/control-routes";
import type { Http } from "./http";
import { sendMessageWithReconciliation } from "./message-delivery";
import { INSPECT_REQUEST_TIMEOUT_MS } from "./plans";
import {
  runTimelineContextPath,
  runTimelineLinkContentPath,
  runTimelineLinksPath,
  runTimelineMessagesPath,
  runTimelinePath,
  timelinePath,
  type RunTimelineWindow,
} from "./routes/timeline-routes";
import { submitUploadedFiles, uploadFiles } from "./upload-delivery";

export type Api = ReturnType<typeof createApi>;

// meiva:note
// Every endpoint is written against the Http client it is given, so the same
// definitions run against the real gateway in the app and against a local server
// in tests. Paths and bodies come from the *-routes and request-plan modules;
// this file joins a plan to the response type it decodes. The timeout and caller
// cancellation stay connected through headers and body consumption, the Inspector
// client budget leaves serialization and network headroom above the backend work
// budget, and HTTP rejection keeps its status separate from network failure.
// meiva:link ui/web/src/docs/frontend-state/api-requests.meiva.okf.md
export function createApi(http: Http) {
  const { send: f, ok, jsonTimed: jsonWithTimeout } = http;
  return {
    head: (path: string): Promise<Response> => f(path, { method: "HEAD" }),

    getTimeline: (
      agentId: number,
      opts?: { limit?: number; before?: string; signal?: AbortSignal },
    ): Promise<TimelineResponse> => {
      return f(timelinePath(agentId, opts), { signal: opts?.signal }).then(ok<TimelineResponse>);
    },

    getTokenUsage: (agentId: number, signal?: AbortSignal): Promise<TokenUsageResponse> => {
      return f(`/api/agents/${agentId}/token-usage`, { signal }).then(ok<TokenUsageResponse>);
    },

    getReplySuggestion: (agentId: number, signal: AbortSignal): Promise<ReplySuggestionResponse> => {
      return f(`/api/agents/${agentId}/reply-suggestion`, { method: "POST", signal }).then(
        ok<ReplySuggestionResponse>,
      );
    },

    getConversationSnapshot: (
      agentId: number,
      signal?: AbortSignal,
    ): Promise<ConversationSnapshotResponse> => {
      return f(`/api/agents/${agentId}/conversation-snapshot`, { signal }).then(
        ok<ConversationSnapshotResponse>,
      );
    },

    getContextBreakdown: (agentId: number): Promise<ContextBreakdownResponse> => {
      return f(`/api/agents/${agentId}/context-breakdown`).then(ok<ContextBreakdownResponse>);
    },

    getRunTimeline: (
      agentId: number,
      options?: RunTimelineWindow & { signal?: AbortSignal },
    ): Promise<RunTimelineResponse> => {
      return jsonWithTimeout<RunTimelineResponse>(
        runTimelinePath(agentId, options),
        {},
        INSPECT_REQUEST_TIMEOUT_MS,
        options?.signal,
        "run timeline",
      );
    },

    getRunTimelineLinks: (
      agents: readonly number[],
      timeWindow: { from: string; to: string },
    ): Promise<RunTimelineLinks> => {
      return f(runTimelineLinksPath(agents, timeWindow)).then(ok<RunTimelineLinks>);
    },

    getRunTimelineLinkContent: (
      ref: { inbound_id: number } | { notice_id: number },
    ): Promise<RunTimelineLinkContent> => {
      return f(runTimelineLinkContentPath(ref)).then(ok<RunTimelineLinkContent>);
    },

    getRunTimelineContext: (agentId: number, at: number, snapshot?: string): Promise<RunTimelineContext> => {
      return f(runTimelineContextPath(agentId, at, snapshot)).then(ok<RunTimelineContext>);
    },

    getRunTimelineMessages: (
      agentId: number,
      range: { start: number; end: number; limit?: number; full?: boolean; snapshot?: string },
    ): Promise<RunTimelineMessages> => {
      return f(runTimelineMessagesPath(agentId, range)).then(ok<RunTimelineMessages>);
    },

    getAgentInspectStatistics: (
      agentId: number,
      hours?: number | null,
      signal?: AbortSignal,
    ): Promise<AgentInspectStatistics> => {
      return jsonWithTimeout<AgentInspectStatistics>(
        inspectStatisticsPath(agentId, hours),
        {},
        INSPECT_REQUEST_TIMEOUT_MS,
        signal,
        "Inspector request",
      );
    },

    getAgentInspectLive: (
      agentId: number,
      signal?: AbortSignal,
    ): Promise<AgentInspectLive> => {
      return jsonWithTimeout<AgentInspectLive>(
        `/api/agents/${agentId}/inspect/live`,
        {},
        INSPECT_REQUEST_TIMEOUT_MS,
        signal,
        "Inspector live request",
      );
    },

    getAgentInspectWidgets: (
      agentId: number,
      signal?: AbortSignal,
    ): Promise<InspectWidget[]> => {
      return jsonWithTimeout<InspectWidget[]>(
        `/api/agents/${agentId}/inspect/widgets`,
        {},
        INSPECT_REQUEST_TIMEOUT_MS,
        signal,
        "Inspector widgets request",
      );
    },

    getAgentShell: (agentId: number, sessionId: number, lines?: number): Promise<ShellCapture> => {
      return f(agentShellPath(agentId, sessionId, lines)).then(ok<ShellCapture>);
    },

    getPendingMessages: (agentId: number, signal?: AbortSignal): Promise<PendingInbound[]> => {
      return f(`/api/agents/${agentId}/pending`, { signal }).then(ok<PendingInbound[]>);
    },

    getCommands: (agentId?: number | null): Promise<CommandItem[]> => {
      return f(commandsPath(agentId)).then(ok<CommandItem[]>);
    },

    sendMessage: (
      agentId: number,
      content: string | ContentBlock[],
      clientMessageId: string,
    ): Promise<AgentMessageEnqueued> => {
      return sendMessageWithReconciliation(http, agentId, content, clientMessageId);
    },

    getNotices: (params?: {
      limit?: number;
      resolvedLimit?: number;
      beforeAt?: string;
      beforeId?: number;
    }): Promise<NoticesFeed> => {
      return f(noticesPath(params)).then(ok<NoticesFeed>);
    },

    getAgentRoster: (signal?: AbortSignal): Promise<AgentRoster> =>
      f("/api/agents/roster", { signal }).then(ok<WireAgentRoster>).then((roster) => ({
        agents: roster.agents.map(projectAgentStatus), ancestors: roster.ancestors,
      })),

    listAgents: (options: { scope?: "live" | "terminated" | "all"; query?: string; beforeId?: number; limit?: number; signal?: AbortSignal } = {}): Promise<AgentDirectoryPage> => {
      return f(listAgentsPath(options), { signal: options.signal }).then(ok<WireAgentDirectoryPage>)
        .then((page) => ({ ...page, agents: page.agents.map(projectAgentStatus) }));
    },

    getAgent: (agentId: number, signal?: AbortSignal): Promise<AgentRow> =>
      f(`/api/agents/${agentId}`, { signal }).then(ok<WireAgentRow>).then(projectAgentStatus),

    getTasks: <T extends TaskFields = "full">(
      opts?: { window?: string; fields?: T },
    ): Promise<TaskListResponse<T extends "full" ? TaskRow : TaskSummaryRow>> => {
      return f(tasksPath(opts)).then(
        ok<TaskListResponse<T extends "full" ? TaskRow : TaskSummaryRow>>,
      );
    },

    submitUploadedFiles: (
      agentId: number,
      files: File[],
      operationKey: string,
      onProgress?: (pct: number) => void,
    ) => submitUploadedFiles(http, agentId, files, operationKey, onProgress),

    uploadFiles: (
      agentId: number,
      files: File[],
      onProgress?: (pct: number) => void,
      operationKey: string = http.newOperationKey(),
    ): Promise<UploadedBatch> => uploadFiles(http, agentId, files, onProgress, operationKey),

    newOperationKey: http.newOperationKey,

    ...createControlApi(http),
  };
}
