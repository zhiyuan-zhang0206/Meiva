import type { components } from "./types-generated";

type Schemas = components["schemas"];

export type BackendTimelineItem = Omit<
  Schemas["TimelineItem"],
  "source" | "created_at" | "inbound_id"
> & {
  readonly source: string | null;
  readonly created_at: string | null;
  readonly inbound_id: number | null;
  readonly partial?: boolean;
  readonly interrupted?: boolean;

  readonly reasoningStartedAt?: number;

  readonly reasoningElapsedMs?: number;

  readonly codeStartedAt?: number;

  readonly codeElapsedMs?: number;

  readonly execStartedAt?: number;
  readonly execElapsedMs?: number;
};

export interface TimelineResponse {
  readonly items: BackendTimelineItem[];
  readonly msg_count: number;
  readonly has_more: boolean;
}

export type ConnectionEvent =
  | { type: "open" }
  | { type: "reconnecting" }
  | { type: "closed" }
  | { type: "parse-failed"; raw: string; error: unknown };

export type ConnectionState = Exclude<ConnectionEvent["type"], "parse-failed">;

interface BaseEvent {
  readonly agent_id: number;
}

export interface ChatStartEvent extends BaseEvent {
  readonly role: "chat_start";
  readonly item_id: string;
}
export interface ChatDeltaEvent extends BaseEvent {
  readonly role: "chat_delta";
  readonly item_id: string;
  readonly content: string;
}
export interface CompactRequestEvent extends BaseEvent {
  readonly role: "compact_request";
  readonly content: string;
}
export interface CompactDoneEvent extends BaseEvent {
  readonly role: "compact_done";
}
export interface CompactStartedEvent extends BaseEvent {
  readonly role: "compact_started";
  readonly compact_id: string;
  readonly started_at: string;
  readonly mode: "request" | "auto";
}
export interface CompactFinishedEvent extends BaseEvent {
  readonly role: "compact_finished";
  readonly compact_id: string;
  readonly status: "success" | "failure" | "replaced";
  readonly finished_at: string;
}
export interface CodeStartEvent extends BaseEvent {
  readonly role: "code_start";
  readonly item_id: string;
}
export interface CodeDeltaEvent extends BaseEvent {
  readonly role: "code_delta";
  readonly item_id: string;
  readonly content: string;
}
export interface ReasoningStartEvent extends BaseEvent {
  readonly role: "reasoning_start";
  readonly item_id: string;
}
export interface ReasoningDeltaEvent extends BaseEvent {
  readonly role: "reasoning_delta";
  readonly item_id: string;
  readonly content: string;
}
export interface ExecStartEvent extends BaseEvent {
  readonly role: "exec_start";

  readonly item_id: string;
}
export interface ExecOutputChunkEvent extends BaseEvent {
  readonly role: "exec_output_chunk";
  readonly item_id: string;
  readonly content: string;
  readonly keepalive?: boolean;
}
export interface ExecOutputEvent extends BaseEvent {
  readonly role: "exec_output";
  readonly item_id: string;
  readonly content: string;
}
export interface ErrorEvent extends BaseEvent {
  readonly role: "error";
  readonly content: string;
  readonly error_class?: string | null;
  readonly provider?: string | null;
  readonly status?: number | null;
  readonly reason?: string | null;
  readonly blocked?: boolean;
  readonly recovery?: string | null;
}
export interface CancelledEvent extends BaseEvent {
  readonly role: "cancelled";
}
export interface ImpersonationChangedEvent extends BaseEvent {
  readonly role: "impersonation_changed";
}
export interface InboundArrivedEvent extends BaseEvent {
  readonly role: "inbound_arrived";
  readonly inbound_id: number;
  readonly kind: string;
  readonly source: string;
  readonly content: string;
}
export interface TokenUsageEvent extends BaseEvent {
  readonly role: "token_usage";
  readonly input_tokens: number;
  readonly output_tokens: number;
  readonly reasoning_tokens?: number;
}
export interface LLMDoneEvent extends BaseEvent {
  readonly role: "llm_done";
}
export interface InboundCommittedEvent extends BaseEvent {
  readonly role: "inbound_committed";
  readonly inbound_id: number;
}
export interface LabelUpdatedEvent extends BaseEvent {
  readonly role: "label_updated";
  readonly label: string | null;
}
export interface ToUserChangedEvent extends BaseEvent {
  role: "to_user_changed";
}

export interface PageOpenedEvent extends BaseEvent {
  readonly role: "page_opened";
  readonly page_id: number;
  readonly name: string;
  readonly port: number;
  readonly title: string | null;
  readonly url: string;
}
export interface PageClosedEvent extends BaseEvent {
  readonly role: "page_closed";
  readonly name: string;
}

export interface TimelineSnapshotEvent extends BaseEvent {
  readonly role: "timeline_snapshot";
  readonly items: readonly BackendTimelineItem[];

  readonly msg_count: number;
}

export type AgentSnapshot = Schemas["AgentRow"];

export interface AgentSpawnedEvent extends BaseEvent {
  readonly role: "agent_spawned";
}
export interface AgentUpdatedEvent extends BaseEvent {
  readonly role: "agent_updated";
}
export interface NoticePostedEvent extends BaseEvent {
  readonly role: "notice_posted";
  readonly notice_id: number;
  readonly priority: string;
  readonly title: string;

  readonly task_id: number | null;
}
export interface NoticeResolvedEvent extends BaseEvent {
  readonly role: "notice_resolved";
  readonly notice_id: number;
  readonly resolution?: string | null;
  readonly require_response?: boolean | null;
  readonly age_seconds?: number | null;
  readonly had_reply?: boolean | null;
}
export interface TaskCreatedEvent extends BaseEvent {
  readonly role: "task_created";
  readonly task_id: number;
}
export interface TaskUpdatedEvent extends BaseEvent {
  readonly role: "task_updated";
  readonly task_id: number;
}

export interface NotificationEvent extends BaseEvent {
  readonly role: "notification";
  readonly notification_id: number;
  readonly category: string;
  readonly subject: string;
  readonly body: string | null;
  readonly href: string | null;
  readonly importance: string;
}

export type SystemEvent =
  | ChatStartEvent
  | ChatDeltaEvent
  | CompactRequestEvent
  | CompactDoneEvent
  | CompactStartedEvent
  | CompactFinishedEvent
  | CodeStartEvent
  | CodeDeltaEvent
  | ReasoningStartEvent
  | ReasoningDeltaEvent
  | ExecStartEvent
  | ExecOutputChunkEvent
  | ExecOutputEvent
  | ErrorEvent
  | CancelledEvent
  | ImpersonationChangedEvent
  | InboundArrivedEvent
  | InboundCommittedEvent
  | LabelUpdatedEvent
  | ToUserChangedEvent
  | PageOpenedEvent
  | PageClosedEvent
  | TokenUsageEvent
  | LLMDoneEvent
  | TimelineSnapshotEvent
  | AgentSpawnedEvent
  | AgentUpdatedEvent
  | NoticePostedEvent
  | NoticeResolvedEvent
  | TaskCreatedEvent
  | TaskUpdatedEvent
  | NotificationEvent;
