// Frontend type surface — HTTP wire schemas (Pydantic models) are derived from
// types-generated.ts; operator-facing projections are explicit overlays. The SSE event union (an Annotated
// discriminator union that OpenAPI can't fully express) stays hand-written,
// kept in sync via the task #11 wire-format roundtrip tests.
//
// Edit a backend Pydantic model → `./scripts/codegen/codegen-types.sh` regenerates
// types-generated.ts → consumers don't need to change import paths to
// pick up the new schema (re-export names here are stable). The
// pre-commit hook enforces codegen ↔ schema sync; drift fails the commit.

import type { TimelineResponse } from "./types-events";
import type { components } from "./types-generated";

export {
  type AgentSnapshot,
  type AgentSpawnedEvent,
  type AgentUpdatedEvent,
  type BackendTimelineItem,
  type CancelledEvent,
  type ChatDeltaEvent,
  type ChatStartEvent,
  type CodeDeltaEvent,
  type CodeStartEvent,
  type CompactDoneEvent,
  type CompactFinishedEvent,
  type CompactRequestEvent,
  type CompactStartedEvent,
  type ConnectionEvent,
  type ConnectionState,
  type ErrorEvent,
  type ExecOutputChunkEvent,
  type ExecOutputEvent,
  type ExecStartEvent,
  type ImpersonationChangedEvent,
  type InboundArrivedEvent,
  type InboundCommittedEvent,
  type LLMDoneEvent,
  type LabelUpdatedEvent,
  type NoticePostedEvent,
  type NoticeResolvedEvent,
  type NotificationEvent,
  type PageClosedEvent,
  type PageOpenedEvent,
  type ReasoningDeltaEvent,
  type ReasoningStartEvent,
  type SystemEvent,
  type TaskCreatedEvent,
  type TaskUpdatedEvent,
  type TimelineResponse,
  type TimelineSnapshotEvent,
  type ToUserChangedEvent,
  type TokenUsageEvent,
} from "./types-events";

export {
  COLOR_FAMILIES,
  COLOR_SLOT_IDS,
  DEFAULT_DATE_FORMAT,
  TIMELINE_COLOR_ITEMS,
  USER_SETTING_DEFAULTS,
  type ColorFamily,
  type ColorItemSpec,
  type ColorShape,
  type ColorSlotId,
  type ContextMeterWidth,
  type DateFormat,
  type FamilyClasses,
  type NotificationCategoriesResponse,
  type NotificationCategoryItem,
  type TaskFields,
  type TaskGraphRow,
  type TaskListResponse,
  type TaskPriority,
  type TaskRow,
  type TaskSummaryRow,
  type TimeMode,
  type ToUserDocument,
  type UserSettingListResponse,
  type UserSettingRow,
} from "./types-settings";

// --- HTTP schemas (gateway/schemas.py) — re-export from generated ---

type Schemas = components["schemas"];

export type TaskStatus = Schemas["TaskStatus"];
export type CancelAgentResponse = Schemas["CancelAgentResponse"];
export type CompactAgentResponse = Schemas["CompactAgentResponse"];
export type RetryLaunchAccepted = Schemas["RetryLaunchAccepted"];

/** The persisted lifecycle vocabulary carried on the gateway wire. These are
 *  control-plane states, not the status vocabulary the console presents. */
export type WireAgentStatus = Schemas["AgentStatus"];
export type WireAgentRow = Schemas["AgentRow"];
export type WireAgentCard = Schemas["AgentCard"];
export type AgentLineage = Schemas["AgentLineage"];
export interface AgentRoster { agents: AgentRow[]; ancestors: AgentLineage[] }
export interface AgentDirectoryPage { agents: AgentRow[]; next_cursor: number | null }
export type WireAgentRoster = Schemas["AgentRoster"];
export type WireAgentDirectoryPage = Schemas["AgentDirectoryPage"];

/** The lease phase behind an open impersonation session (only present on the
 *  card-shaped roster/directory rows — the selected-agent detail read has no
 *  impersonation projection at all). Only `active` means the agent is
 *  actually taken over; `requested`/`accepted` still run the native agent
 *  until activation reaches its next safe boundary. */
export type OpenImpersonationStatus = NonNullable<WireAgentCard["open_impersonation_status"]>;

/** The console's complete, user-facing agent status model. Liveness remains a
 *  separate `AgentRow.liveness_state` axis, so an idling agent whose runner
 *  is unreachable still renders as `idling` + `offline`, rather than hiding
 *  the outage.
 *  `impersonated` is a card-level projection (see `projectAgentStatus`), not
 *  a wire lifecycle state — the backend `AgentStatus` enum never changes. */
export type PublicAgentStatus =
  | Extract<WireAgentStatus, "running" | "idling" | "terminated">
  | "impersonated";
export type AgentRow = Omit<
  WireAgentCard,
  "status" | "observation" | "open_impersonation_session_id" | "open_impersonation_status"
> & {
  readonly status: PublicAgentStatus;
  readonly observation?: WireAgentCard["observation"] | null;
  readonly open_impersonation_session_id?: number | null;
  readonly open_impersonation_status?: OpenImpersonationStatus | null;
};

/** Collapse every known wire lifecycle state into the public three-state model.
 *  The switch is deliberately exhaustive: adding a backend enum member fails
 *  type-checking here, and an unknown runtime value throws instead of silently
 *  inventing a display fallback. Never yields `impersonated` — that
 *  projection needs the card's lease status, not just the lifecycle enum
 *  (see `projectAgentStatus`). */
export function projectAgentStatusValue(status: WireAgentStatus): Extract<WireAgentStatus, "running" | "idling" | "terminated"> {
  switch (status) {
    case "running":
      return "running";
    case "terminated":
      return "terminated";
    case "idling":
      return "idling";
    default: {
      const unknownStatus: never = status;
      throw new Error(`unknown internal agent status: ${String(unknownStatus)}`);
    }
  }
}

/** Project one gateway card or selected detail before it enters frontend state.
 *  A card whose open lease has reached `active` projects to `impersonated`
 *  instead of the lifecycle-derived status — an external tool has taken the
 *  agent over, so `idling` would misrepresent it. `requested`/`accepted`
 *  leases (the native agent still runs until activation) and the
 *  selected-agent detail read (no lease field at all) fall through to the
 *  ordinary lifecycle projection. */
export function projectAgentStatus(row: WireAgentCard | WireAgentRow): AgentRow {
  const openImpersonationStatus = "open_impersonation_status" in row ? row.open_impersonation_status : null;
  const status: PublicAgentStatus =
    openImpersonationStatus === "active" && row.status !== "terminated"
      ? "impersonated"
      : projectAgentStatusValue(row.status);
  if ("awaiting_response_count" in row) {
    return status === row.status ? row : { ...row, status };
  }
  // Keep future wire fields by default; selected-detail bodies and lineage/probe
  // evidence are deliberately excluded from the operator-facing card.
  const {
    notices_awaiting_response,
    fork_source_checkpoint_id: _forkSourceCheckpointId,
    last_probe_at: _lastProbeAt,
    ...cardFields
  } = row;
  return {
    ...cardFields,
    status,
    awaiting_response_count: notices_awaiting_response.length,
    highest_notice_priority: notices_awaiting_response.map((n) => n.priority).sort()[0] ?? null,
    open_impersonation_session_id: null,
    open_impersonation_status: openImpersonationStatus,
  };
}
// OpenNotice rides the agent snapshot (notices_awaiting_response — the open
// require_response worklist). NoticeItem is the standalone feed element (the FYI
// queue + the resolved history). ResolveNoticeIn is the resolve request body.
export type OpenNotice = Schemas["OpenNotice"];
export type NoticeItem = Schemas["NoticeItem"];
// The unified inbox feed (Task #1024, R4 layer 2, Q1=A): open (FYI) +
// awaiting (require_response) + one keyset page of the resolved history.
export type NoticesFeed = Schemas["NoticesFeed"];
export type ResolveNoticeIn = Schemas["ResolveNoticeIn"];
export type NoticeQuestion = NonNullable<OpenNotice["question"]>;
export type NoticeAnswer = Schemas["NoticeAnswer"];
export type AgentMessageEnqueued = Schemas["AgentMessageEnqueued"];
export type PendingInbound = Schemas["PendingInbound"];


export type StatsDashboard = Schemas["StatsDashboard"];
export type StatsTokens = Schemas["StatsTokens"];
export type PluginStat = Schemas["PluginStat"];

// The sidebar card's warning/error classes: one row per (level, event_name, source, process),
// and the persisted dismissal a row's Dismiss action creates.
export type AlertClassRow = Schemas["AlertClassRow"];
export type AlertClassesResponse = Schemas["AlertClassesResponse"];
export type AlertClassSample = Schemas["AlertClassSample"];
export type EventResolutionRow = Schemas["EventResolutionRow"];

// --- Metrics (settings Metrics tab) ---
//
// The envelope (meta + the `metrics` map) comes from the generated schema,
// but `metrics` is `Record<string, unknown>` there by design: the backend
// types it as a free-form dict so adding a `@metric` unit is one function with
// no schema churn. The per-unit data shapes below mirror the `data` dicts the
// units in base/telemetry/metrics/report.py emit; consumers narrow `report.metrics[key]` to
// the matching interface. Adding a unit that reuses these shapes needs no TS
// change beyond a new key access.
export type AlertsWindow = "1h" | "6h" | "24h" | "7d";

// --- Alerts (the system→human alert store, Task #1224) ---
//
// Generated re-exports of the gateway's Pydantic models (AlertRow /
// AlertsListMeta / AlertsListResponse) — the pre-commit types-codegen-fresh
// gate fails on any drift, so the alert types can never silently diverge
// from the backend. Alert is fully separate from Notice.
export type Alert = Schemas["AlertRow"];
export type AlertsResponse = Schemas["AlertsListResponse"];
export type AlertSeverity = Alert["severity"];
export type AlertStatus = Alert["status"];


// SpawnAgentRequest: backend `spawner: str = Field(default="user")` is
// emitted in OpenAPI as "required + default" (Pydantic default behavior),
// so openapi-typescript marks it required. But the frontend's default
// calls never pass spawner — we override it to optional to match runtime
// behavior (the server falls back to default "user" when the field is missing).
//
// `prompt_source` is `str | None = None` + model_validator enforcing
// "must be present when prompt is given" on the server — so the
// generated type is already nullable optional; inherit it directly with
// no override.
export type SpawnAgentRequest = Omit<Schemas["SpawnAgentRequest"], "spawner"> & {
  readonly spawner?: string;
};
export type SpawnedAgent = Schemas["SpawnedAgent"];

export type ModelsResponse = Schemas["ModelsResponse"];
export type DefaultModelView = Schemas["DefaultModelView"];

export type MemoryNoteResponse = Schemas["MemoryNoteResponse"];

export type TerminateAgentResponse = Schemas["TerminateAgentResponse"];
export type OpenTasksHint = Schemas["OpenTasksHint"];
export type RestartAgentResponse = Schemas["RestartAgentResponse"];
export type ResurrectAgentResponse = Schemas["ResurrectAgentResponse"];

export type TokenUsageResponse = Schemas["TokenUsageResponse"];
export type ReplySuggestionResponse = Schemas["ReplySuggestionResponse"];

export type ContextBreakdownResponse = Schemas["ContextBreakdownResponse"];
export type ContextCategory = Schemas["ContextCategory"];
export type ContextSection = Schemas["ContextSection"];

// --- Run timeline (GET /api/agents/{id}/run-timeline) ---

export type RunTimelineResponse = Schemas["RunTimelineResponse"];
export type RunTimelineNode = Schemas["RunTimelineNode"];
export type RunTimelineUnit = Schemas["RunTimelineUnit"];
export type RunTimelineLink = Schemas["RunTimelineLink"];
export type RunTimelineLinks = Schemas["RunTimelineLinks"];
export type RunTimelineLinkContent = Schemas["RunTimelineLinkContent"];
export type RunTimelineUsage = Schemas["RunTimelineUsage"];
export type RunTimelineGeneration = Schemas["RunTimelineGeneration"];
export type RunTimelineContext = Schemas["RunTimelineContext"];
export type SessionOut = Schemas["SessionOut"];
export type RunTimelineMessages = Schemas["RunTimelineMessages"];
export type RunTimelineMessage = Schemas["RunTimelineMessage"];
export type RunTimelineMessagePart = Schemas["RunTimelineMessagePart"];

// --- Per-agent inspector panel (GET /api/agents/{id}/inspect/statistics) ---

export type AgentInspectStatistics = Schemas["AgentInspectStatistics"];
export type AgentInspectLive = Schemas["AgentInspectLive"];
export type ShellInfo = Schemas["ShellInfo"];
export type ShellCapture = Schemas["ShellCaptureResponse"];
export type AgentCost = Schemas["AgentCost"];
export type AgentStats = Schemas["AgentStats"];
export type AgentTps = Schemas["AgentTps"];
export type HeartbeatInfo = Schemas["HeartbeatInfo"];
export type HeartbeatLastPause = Schemas["HeartbeatLastPause"];
// Plugin widgets embedded in the panel (GET /api/agents/{id}/inspect/widgets):
// closed-set data rendered by the console's own components — a kind the
// console does not know is skipped at render time, never interpreted.
export type InspectWidget = Schemas["InspectWidgetResult"];
export type InspectWidgetTask = Schemas["InspectWidgetTask"];

export const inspectLiveQueryKey = (agentId: number) =>
  ["agent-inspect-live", agentId] as const;

// --- Shell monitor page (GET /api/agents/{id}/shell/{sid}) ---
//
// --- meiva.ui.show panel ---

export type PageRow = Schemas["PageRow"];
export type PageRegisterRequest = Schemas["PageRegisterRequest"];

// GET /api/agents/{id}/conversation-snapshot response (task #3900 batch 2) —
// the three conversation read models composed into one switch-refresh read
// (agent-reconcile.ts). Sections are the same frontend types the standalone
// readers consume; the gateway composes its standalone payloads server-side.
export interface ConversationSnapshotResponse {
  readonly timeline: TimelineResponse;
  readonly token_usage: TokenUsageResponse;
  readonly pending: PendingInbound[];
}

// --- Config (runtime config panel) ---
//
// `ConfigFieldView` is the per-field view returned by GET /api/config;
// `ConfigView` is the top-level schema (field list + raw_overrides).
// Same pattern as ExtensionsView: raw_overrides is what the frontend
// uses for delta updates.
//
// Hand-written mirror of the generated ConfigFieldView (types-generated.ts).
// The generated type carries `scope`; kept here so consumers import from
// the stable `@/lib/types` path without touching the generated layer directly.

export interface ConfigFieldView {
  readonly owner?: string | null; // None is Core; a plugin name owns its image.
  readonly name: string;
  readonly field_type: "bool" | "string" | "int" | "float" | "enum";
  readonly current_value: boolean | string | number | null;
  readonly default_value: boolean | string | number | null;
  readonly description: string;
  readonly group: string; // owning-domain label (e.g. "LLM", "Data plane") — the second-level bucket
  // Owning machine capability — the top-level config-panel section. "common" =
  // not owned by a single capability (cluster-wide policy or shared host identity).
  readonly capability: "gateway" | "agent-runner" | "common";
  readonly scope: "cluster-pinned" | "cluster-default" | "host" | "agent";
  readonly restart_required: "agent" | "ops" | "gateway" | "all" | "";
  // Whether a spawn/restart config overlay may override this field per agent —
  // drives the panel's "per-agent" tag and filter.
  readonly per_agent: boolean;
  readonly writable: boolean;
  readonly sensitive: boolean;
  readonly env_var: string;
  readonly remote_writable: boolean;
  // For an "enum" field, the allowed values rendered as a select; null for
  // every other field_type.
  readonly choices?: readonly string[] | null;
  // Host-side read-time capability hint; null for non-host fields and for
  // host fields with no static pre-grey gate. `reason` explains a false
  // `can_enable` (e.g. browser_enabled -> "no display detected on <host>").
  readonly can_enable: boolean | null;
  readonly reason: string | null;
}

export interface ConfigView {
  readonly fields: ConfigFieldView[];
  readonly raw_overrides: Record<string, unknown>;
  // The target machine's capability set: the gateway's own on the Cluster (self)
  // view, the ?machine= host's on a remote view. A subset of {gateway,
  // agent-runner} (never "common", a config bucket not a machine capability).
  // Drives which capability sections a remote view renders.
  readonly machine_capabilities: readonly ("gateway" | "agent-runner")[];
}

// PUT /api/config result — per-field verdicts + whether the write committed.
// Re-exported from the generated layer so the config page imports from the
// stable `@/lib/types` path.
export type ConfigFieldWriteResult = Schemas["ConfigFieldWriteResult"];
export type ConfigWriteResult = Schemas["ConfigWriteResult"];

// GET /api/config/resolved?model= — every per-model-defaultable setting resolved
// for one model, each row naming the layer its effective value came from
// (shared default < per-model default < explicit .env value). Read-only: a row's
// `name` is the same key GET/PUT /api/config uses, so the per-model view links
// back to that field's existing editor instead of carrying a write path.
export type ResolvedFieldView = Schemas["ResolvedFieldView"];
export type ResolvedConfigView = Schemas["ResolvedConfigView"];

// GET /api/cluster/machines row — name + free-text description + live status.
// Drives the config-page machine selector.
export type AgentMachineRow = Schemas["AgentMachineRow"];

// --- Inventory (Plugins & MCP, cross-machine matrix) ---
//
// GET /api/inventory -> InventoryAggregate (the column = machine, row =
// plugin/MCP-server matrix). Per-host cell state is InventoryItemHostState.
// PUT /api/inventory?machine= returns InventoryWriteResult (per-item ok/reason
// + atomic `applied`). Re-exported from the generated layer so the inventory
// page imports from the stable `@/lib/types` path.
export type InventoryAggregate = Schemas["InventoryAggregate"];
export type InventoryItemAggregate = Schemas["InventoryItemAggregate"];
export type InventoryItemHostState = Schemas["InventoryItemHostState"];
export type InventoryWriteResult = Schemas["InventoryWriteResult"];
export type InventoryItemWriteResult = Schemas["InventoryItemWriteResult"];

// GET /api/skills -> SkillsView: this gateway host's skills load dir correlated
// with the install registry (name / source layer / enabled / local drift).
export type SkillsView = Schemas["SkillsView"];
export type SkillView = Schemas["SkillView"];
export type SkillLayer = SkillView["layer"];


// --- System Status (GET /api/status) ---

// SystemStatus family — the /api/status set, re-exported from the
// generated layer to stay in sync with the backend.
export type ServiceItem = Schemas["ServiceItem"];
export type ServicesStatus = Schemas["ServicesStatus"];
export type MachineStatus = Schemas["MachineStatus"];
export type ResourceSample = Schemas["ResourceSample"];
export type ClusterPanel = Schemas["ClusterPanel"];
export type ClusterStatus = Schemas["ClusterStatus"];
export type SystemStatus = Schemas["SystemStatus"];

// --- Plugin console contributions (GET /api/ui/contributions) ---
//
// What the cluster's enabled plugins declare under `contributions.ui` in their
// meiva-plugin.json. Declarations are data the console's own components render —
// no plugin JavaScript ever enters this bundle.

export type UiThemeContribution = Schemas["UiThemeContribution"];
export type UiNavContribution = Schemas["UiNavContribution"];
export type UiStatContribution = Schemas["UiStatContribution"];
export type UiContributionsResponse = Schemas["UiContributionsResponse"];

// --- File Upload ---

export type UploadedFile = Schemas["UploadedFile"];
export type UploadedBatch = Schemas["UploadedBatch"];
export type DeliveredUploadAcceptance = Schemas["Acceptance"];

// --- Multimodal message content (POST /api/agents/{id}/messages) ---

export type TextContentBlock = Schemas["TextContentBlock"];
export type ImageUrlContentBlock = Schemas["ImageUrlContentBlock"];
export type ContentBlock = TextContentBlock | ImageUrlContentBlock;

// --- Schedules (GET /api/schedules) ---

export type ScheduleSummary = Schemas["ScheduleSummary"];
export type ScheduleView = Schemas["ScheduleView"];
export type ScheduleCreate = Schemas["ScheduleCreate"];
export type ScheduleUpdate = Schemas["ScheduleUpdate"];
export type ScheduleRunView = Schemas["ScheduleRunView"];
export type ScheduleLogsView = Schemas["ScheduleLogsView"];
export type ScheduleDraftResponse = Schemas["ScheduleDraftResponse"];

// --- Meiva Guide (natural-language ops assistant; POST /api/keyed/v1/guide/draft) ---
export type GuideDraftResponse = Schemas["GuideDraftResponse"];

// --- Package install (POST /api/keyed/v1/packages/draft) ---
//
// Every skill / plugin / MCP install starts as a natural-language request that
// spawns an installer agent — there is no URL form, because the user is not
// expected to know which package is any good.
export type PackageKind = Schemas["PackageDraftRequest"]["kind"];
export type PackageDraftResponse = Schemas["PackageDraftResponse"];

// --- Composer commands (registered prompt templates) ---

export type CommandItem = Schemas["CommandItem"];

// --- Fleet graph (GET /api/fleet/graph) ---
//
// The weighted agent-relationship graph behind the fleet Graph View: nodes are
// agents, edges are permanent spawn/fork/resurrect lineage + decaying
// agent-to-agent message traffic (one edge per (from,to,event_type),
// event_count accumulating repeats, weight a recency*frequency score).
//
// The raw endpoint types come from OpenAPI below. The console overlays only
// its public lifecycle vocabulary and normalized edge-event vocabulary; every
// other field (including liveness, nullable machine, data staleness, telemetry
// health, and snapshot time) stays pinned to the generated wire contract.

// Lineage ties (spawn / fork / resurrect) + the message tie. The backend emits
// the raw event value `send_message`; normalizeGraph maps it to `message`
// (the graph's vocabulary), so the view keys on `message` here.
export type GraphEventType = "spawn" | "fork" | "resurrect" | "message";

export type WireFleetGraphNode = Schemas["FleetGraphNode"];
export type WireFleetGraphEdge = Schemas["FleetGraphEdge"];
export type WireFleetGraph = Schemas["FleetGraphResponse"];

export type FleetGraphNode = Omit<WireFleetGraphNode, "status"> & {
  readonly status: PublicAgentStatus;
};

export type FleetGraphEdge = Omit<WireFleetGraphEdge, "event_type"> & {
  readonly event_type: GraphEventType;
};

export type FleetGraph = Omit<WireFleetGraph, "nodes" | "edges"> & {
  readonly nodes: readonly FleetGraphNode[];
  readonly edges: readonly FleetGraphEdge[];
};
