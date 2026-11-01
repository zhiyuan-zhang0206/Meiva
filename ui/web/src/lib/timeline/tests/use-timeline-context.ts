import type { QueryClient } from "@tanstack/react-query";
import type React from "react";
import type { Mock } from "vitest";

import type {
  BackendTimelineItem,
  ConversationSnapshotResponse,
  SystemEvent,
  TimelineResponse,
} from "../../contracts/types";
import type { GatewayReply } from "../../transport/tests/api/api-provider";
import type { ApiError } from "../../transport/api";
import type { ConnectionEvent } from "../../transport/useEventStream";

export interface TimelineRead {
  agentId: number;
  before: string | undefined;
  limit: number | undefined;
  signal: AbortSignal | undefined;
}

export interface SnapshotRead {
  agentId: number;
  signal: AbortSignal | undefined;
}

export type Answer = GatewayReply | Promise<GatewayReply>;

export interface AnswerScript {
  queue: (...answers: Answer[]) => void;
  always: (answer: Answer) => void;
}

export interface UseTimelineTestContext {
  ApiError: typeof ApiError;
  timeline: AnswerScript;
  snapshot: AnswerScript;
  wireReplies: Map<string, Answer[]>;
  timelineReads: TimelineRead[];
  snapshotReads: SnapshotRead[];
  turnStarts: number[];
  resolves: (value: unknown) => GatewayReply;
  rejects: (error: Error) => GatewayReply;
  startTransitionSpy: Mock<(cb: () => void) => void>;
  pushEvent: (ev: SystemEvent) => void;
  pushBatch: (evs: SystemEvent[]) => void;
  pushConnectionEvent: (ev: ConnectionEvent) => void;
  snapshotItem: (overrides: Partial<BackendTimelineItem>) => BackendTimelineItem;
  postWindow: () => BackendTimelineItem[];
  tlResp: (items?: BackendTimelineItem[], has_more?: boolean) => TimelineResponse;
  composedSnapshot: (timeline: TimelineResponse) => ConversationSnapshotResponse;
  getQueryClient: () => QueryClient;
  wallClock: { now: () => number; advance: (ms: number) => void };
  wrapper: ({ children }: { children: React.ReactNode }) => React.ReactElement;
  setVisibility: (visibility: DocumentVisibilityState) => void;
  delay: (ms?: number) => Promise<void>;
}
