import type { HarnessAvailability } from "../../contracts/models";
import type {
  AgentMachineRow,
  AlertClassRow,
  AlertClassesResponse,
  AlertClassSample,
  AlertsResponse,
  AlertsWindow,
  CancelAgentResponse,
  ClusterStatus,
  CompactAgentResponse,
  ConfigView,
  ConfigWriteResult,
  DefaultModelView,
  EventResolutionRow,
  GuideDraftResponse,
  HostPermissionsPayload,
  InventoryAggregate,
  InventoryWriteResult,
  MemoryNoteResponse,
  ModelsResponse,
  NotificationCategoriesResponse,
  PackageDraftResponse,
  PackageKind,
  PageRow,
  ResolvedConfigView,
  ResolveNoticeIn,
  RestartAgentResponse,
  ResurrectAgentResponse,
  RetryLaunchAccepted,
  ScheduleCreate,
  ScheduleDraftResponse,
  ScheduleLogsView,
  ScheduleRunView,
  ScheduleSummary,
  ScheduleUpdate,
  ScheduleView,
  SkillView,
  SkillsView,
  SpawnAgentRequest,
  SpawnedAgent,
  StatsDashboard,
  SystemStatus,
  TaskRow,
  TerminateAgentResponse,
  ToUserDocument,
  UiContributionsResponse,
  UserSettingListResponse,
  UserSettingRow,
  WireFleetGraph,
} from "../../contracts/types";
import { toUserPath } from "./routes/agent-routes";
import { configPath, harnessAvailabilityPath, inventoryPath, resolvedConfigPath, settingPath } from "./routes/config-routes";
import { alertClassSamplesPath, alertsPath, fleetGraphPath, memoryNotePath } from "./routes/control-routes";
import type { Http } from "./http";
import { keyedJson, keyedPost, patchJson, postInit, postJson, putJson } from "./plans";
import { confirmDraftReceipt } from "./plans";
import { STATS_REQUEST_TIMEOUT_MS } from "./plans";

export function createControlApi(http: Http) {
  const { send: f, ok, jsonTimed: jsonWithTimeout, newOperationKey } = http;

  const draftAgent = async (path: string, body: unknown, operationKey: string): Promise<GuideDraftResponse> => {
    const response = await f(path, keyedJson(body, operationKey));
    return confirmDraftReceipt(response.status, await ok<unknown>(response));
  };

  return {
    terminateAgent: (agentId: number, force = false): Promise<TerminateAgentResponse> => {
      return f(
        `/api/agents/${agentId}/terminate`,
        force ? postJson({ force: true }) : postInit,
      ).then(ok<TerminateAgentResponse>);
    },

    forceExpireImpersonation: (
      agentId: number,
      sessionId: number,
    ): Promise<{ session_id: number; status: "expired" | "not_open" }> =>
      f(`/api/agents/${agentId}/impersonation/force-expire`, postJson({ session_id: sessionId }))
        .then(ok<{ session_id: number; status: "expired" | "not_open" }>),

    restartAgent: (agentId: number): Promise<RestartAgentResponse> => {
      return f(`/api/agents/${agentId}/restart`, postInit).then(ok<RestartAgentResponse>);
    },

    resurrectAgent: (
      agentId: number,
      prompt?: string,
    ): Promise<ResurrectAgentResponse> => {
      return f(
        `/api/agents/${agentId}/resurrect`,
        postJson(prompt !== undefined ? { prompt } : {}),
      ).then(ok<ResurrectAgentResponse>);
    },

    patchAgentLabel: (agentId: number, label: string): Promise<void> => {
      return f(`/api/agents/${agentId}`, patchJson({ label })).then(async (res) => {
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
        }
      });
    },

    getToUser: (agentId: number, signal?: AbortSignal, references: string[] = []): Promise<ToUserDocument> => {
      return f(toUserPath(agentId, references), { signal }).then(ok<ToUserDocument>);
    },

    listPages: (agentId: number): Promise<PageRow[]> => {
      return f(`/api/agents/${agentId}/pages`).then(ok<PageRow[]>);
    },

    listAllPages: (): Promise<PageRow[]> => {
      return f(`/api/pages`).then(ok<PageRow[]>);
    },

    getFleetGraph: (opts?: { hours?: number; decayLambda?: number }): Promise<WireFleetGraph> => {
      return f(fleetGraphPath(opts)).then(ok<WireFleetGraph>);
    },

    getMemoryNote: (path: string): Promise<MemoryNoteResponse> => {
      return f(memoryNotePath(path)).then(ok<MemoryNoteResponse>);
    },

    getStatsDashboard: (hours: number, signal?: AbortSignal): Promise<StatsDashboard> => {
      return jsonWithTimeout<StatsDashboard>(
        `/api/stats/dashboard?hours=${hours}`,
        {},
        STATS_REQUEST_TIMEOUT_MS,
        signal,
        "stats dashboard",
      );
    },

    getAlertClasses: (hours: number, signal?: AbortSignal): Promise<AlertClassesResponse> => {
      return jsonWithTimeout<AlertClassesResponse>(
        `/api/stats/alert-classes?hours=${hours}`,
        {},
        STATS_REQUEST_TIMEOUT_MS,
        signal,
        "alert classes",
      );
    },

    getAlertClassSamples: (
      cls: Pick<AlertClassRow, "level" | "event_name" | "source" | "process">,
      hours: number,
      signal?: AbortSignal,
    ): Promise<AlertClassSample[]> => {
      return f(alertClassSamplesPath(cls, hours), { signal })
        .then(ok<{ samples: AlertClassSample[] }>)
        .then((body) => body.samples);
    },

    dismissAlertClass: (
      cls: Pick<AlertClassRow, "category" | "level" | "event_name" | "source" | "process">,
    ): Promise<EventResolutionRow> =>
      f("/api/event-resolutions", postJson(cls)).then(ok<EventResolutionRow>),

    reopenAlertClass: (dismissalId: number): Promise<EventResolutionRow> =>
      f(`/api/event-resolutions/${dismissalId}/reopen`, postInit).then(ok<EventResolutionRow>),

    getAlerts: (params: {
      window?: AlertsWindow;
      status?: string;
      severity?: string;
      limit?: number;
    } = {}): Promise<AlertsResponse> => {
      return f(alertsPath(params)).then(ok<AlertsResponse>);
    },

    getConfig: (machine?: string): Promise<ConfigView> => {
      return f(configPath(machine)).then(ok<ConfigView>);
    },

    getResolvedConfig: (model?: string): Promise<ResolvedConfigView> => {
      return f(resolvedConfigPath(model)).then(ok<ResolvedConfigView>);
    },

    getMachines: (): Promise<AgentMachineRow[]> => {
      return f("/api/cluster/machines").then(ok<AgentMachineRow[]>);
    },

    getModels: (): Promise<ModelsResponse> => {
      return f("/api/models").then(ok<ModelsResponse>);
    },

    getDefaultModel: (): Promise<DefaultModelView> => {
      return f("/api/config/default-model").then(ok<DefaultModelView>);
    },

    putDefaultModel: (model: string): Promise<DefaultModelView> => {
      return f("/api/config/default-model", putJson({ model })).then(ok<DefaultModelView>);
    },

    getSystemStatus: (): Promise<SystemStatus> => {
      return f("/api/status").then(ok<SystemStatus>);
    },

    getClusterStatus: (): Promise<ClusterStatus> => {
      return f("/api/cluster/status").then(ok<ClusterStatus>);
    },

    async putConfig(
      body: Record<string, unknown>,
      machine?: string,
    ): Promise<ConfigWriteResult> {
      const res = await f(configPath(machine), putJson(body));
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
      }
      return res.json() as Promise<ConfigWriteResult>;
    },

    getInventory: (): Promise<InventoryAggregate> => {
      return f("/api/inventory").then(ok<InventoryAggregate>);
    },

    async putInventory(
      body: { plugins?: Record<string, boolean>; mcp_servers?: Record<string, boolean> },
      machine: string,
    ): Promise<InventoryWriteResult> {
      const res = await f(inventoryPath(machine), putJson(body));
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
      }
      return res.json() as Promise<InventoryWriteResult>;
    },

    getSkills: (): Promise<SkillsView> => {
      return f("/api/skills").then(ok<SkillsView>);
    },

    putSkillEnabled: (name: string, enabled: boolean): Promise<SkillView> => {
      return f("/api/skills", putJson({ name, enabled })).then(ok<SkillView>);
    },

    login: (username: string, password: string): Promise<{ ok: boolean }> => {
      return f("/api/auth/login", postJson({ username, password })).then(ok<{ ok: boolean }>);
    },

    logout: (): Promise<{ ok: boolean }> => {
      return f("/api/auth/logout", postInit).then(ok<{ ok: boolean }>);
    },

    checkAuth: (): Promise<{ authenticated: boolean }> => {
      return f("/api/auth/check").then(ok<{ authenticated: boolean }>);
    },

    listSchedules: (): Promise<ScheduleSummary[]> => {
      return f("/api/schedules").then(ok<ScheduleSummary[]>);
    },

    getSchedule: (id: number): Promise<ScheduleView> => {
      return f(`/api/schedules/${id}`).then(ok<ScheduleView>);
    },

    createSchedule: (body: ScheduleCreate, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
      return f("/api/schedules", keyedJson(body, operationKey)).then(ok<ScheduleView>);
    },

    updateSchedule: (id: number, body: ScheduleUpdate, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
      return f(`/api/schedules/${id}`, keyedJson(body, operationKey, "PUT")).then(ok<ScheduleView>);
    },

    deleteSchedule: (id: number): Promise<{ status: string }> => {
      return f(`/api/schedules/${id}`, { method: "DELETE" }).then(ok<{ status: string }>);
    },

    startSchedule: (id: number, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
      return f(`/api/schedules/${id}/start`, keyedPost(operationKey)).then(ok<ScheduleView>);
    },

    stopSchedule: (id: number, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
      return f(`/api/schedules/${id}/stop`, keyedPost(operationKey)).then(ok<ScheduleView>);
    },

    restartSchedule: (id: number, operationKey: string = newOperationKey()): Promise<ScheduleView> => {
      return f(`/api/schedules/${id}/restart`, keyedPost(operationKey)).then(ok<ScheduleView>);
    },

    scheduleLogs: (id: number, lines = 200): Promise<ScheduleLogsView> => {
      return f(`/api/schedules/${id}/logs?lines=${lines}`).then(ok<ScheduleLogsView>);
    },

    scheduleRuns: (id: number, limit = 50): Promise<ScheduleRunView[]> => {
      return f(`/api/schedules/${id}/runs?limit=${limit}`).then(ok<ScheduleRunView[]>);
    },

    draftSchedule: (nl: string, operationKey: string = newOperationKey()): Promise<ScheduleDraftResponse> => {
      return draftAgent("/api/keyed/v1/schedules/draft", { nl }, operationKey);
    },

    draftGuide: (nl: string, operationKey: string = newOperationKey()): Promise<GuideDraftResponse> => {
      return draftAgent("/api/keyed/v1/guide/draft", { nl }, operationKey);
    },

    draftPackage: (kind: PackageKind, nl: string, operationKey: string = newOperationKey()): Promise<PackageDraftResponse> => {
      return draftAgent("/api/keyed/v1/packages/draft", { kind, nl }, operationKey);
    },

    getSettings: (): Promise<UserSettingListResponse> => {
      return f("/api/settings").then(ok<UserSettingListResponse>);
    },

    getNotificationCategories: (): Promise<NotificationCategoriesResponse> => {
      return f("/api/notifications/categories").then(ok<NotificationCategoriesResponse>);
    },

    putSetting: (key: string, value: unknown): Promise<UserSettingRow> => {
      return f(settingPath(key), putJson({ value })).then(ok<UserSettingRow>);
    },

    getUiContributions: (): Promise<UiContributionsResponse> => {
      return f("/api/ui/contributions").then(ok<UiContributionsResponse>);
    },

    getHostPermissions: (signal?: AbortSignal): Promise<HostPermissionsPayload> =>
      f(`/api/host/permissions`, { signal }).then(ok<HostPermissionsPayload>),

    recheckHostPermissions: (): Promise<HostPermissionsPayload> =>
      f(`/api/host/permissions/recheck`, postInit).then(ok<HostPermissionsPayload>),

    requestHostPermissions: (): Promise<HostPermissionsPayload> =>
      f(`/api/host/permissions/request`, postInit).then(ok<HostPermissionsPayload>),

    deferHostPermissions: (): Promise<HostPermissionsPayload> =>
      f(`/api/host/permissions/defer`, postInit).then(ok<HostPermissionsPayload>),

    resolveNotice: (
      agentId: number,
      noticeId: number,
      body: ResolveNoticeIn,
      idempotencyKey: string = newOperationKey(),
    ): Promise<{ status: string }> => {
      return f(
        `/api/keyed/v1/agents/${agentId}/notices/${noticeId}/resolve`,
        keyedJson(body, idempotencyKey),
      ).then(ok<{ status: string }>);
    },

    compact: (agentId: number): Promise<CompactAgentResponse> =>
      f(`/api/agents/${agentId}/compact`, postInit).then(ok<CompactAgentResponse>),

    cancel: (agentId: number): Promise<CancelAgentResponse> =>
      f(`/api/agents/${agentId}/cancel`, postInit).then(ok<CancelAgentResponse>),

    spawnAgent: (req: SpawnAgentRequest = {}, operationKey: string = newOperationKey()): Promise<SpawnedAgent> =>
      f("/api/keyed/v1/agents", keyedJson(req, operationKey)).then(ok<SpawnedAgent>),

    getHarnessAvailability: (machine: string, signal?: AbortSignal): Promise<HarnessAvailability[]> =>
      f(harnessAvailabilityPath(machine), { signal })
        .then((res) => (res.status === 404 ? [] : ok<HarnessAvailability[]>(res))),

    retryAgentLaunch: (agentId: number, priorAttempt: string, operationKey: string): Promise<RetryLaunchAccepted> =>
      f(`/api/keyed/v1/agents/${agentId}/retry-launch`, keyedJson({ expected_prior_attempt_id: priorAttempt }, operationKey))
        .then(ok<RetryLaunchAccepted>),

    getTask: (id: number): Promise<TaskRow> => f(`/api/tasks/${id}`).then(ok<TaskRow>),
  };
}
