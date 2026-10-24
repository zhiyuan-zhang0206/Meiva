import type { QueryClient } from "@tanstack/react-query";
import type React from "react";
import type { Mock } from "vitest";

import type {
  BackendTimelineItem,
  ConversationSnapshotResponse,
  SystemEvent,
  TimelineResponse,
} from "../../contracts/types";
import type { ApiError } from "../../transport/api";
import type { ConnectionEvent } from "../../transport/useEventStream";

export interface TimelineReadOptions {
  limit?: number;
  before?: string;
  signal?: AbortSignal;
}

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

export type Answer<Args extends unknown[], Reply> = (...args: Args) => Promise<Reply>;

export interface AnswerScript<Args extends unknown[], Reply> {
  queue: (...answers: Answer<Args, Reply>[]) => void;
  always: (answer: Answer<Args, Reply>) => void;
}

export interface UseTimelineTestContext {
  ApiError: typeof ApiError;
  timeline: AnswerScript<[agentId: number, options: TimelineReadOptions | undefined], TimelineResponse>;
  snapshot: AnswerScript<[agentId: number, signal: AbortSignal | undefined], ConversationSnapshotResponse>;
  timelineReads: TimelineRead[];
  snapshotReads: SnapshotRead[];
  turnStarts: number[];
  resolves: <T>(value: T) => () => Promise<T>;
  rejects: (error: Error) => () => Promise<never>;
  startTransitionSpy: Mock<(cb: () => void) => void>;
  pushEvent: (ev: SystemEvent) => void;
  pushBatch: (evs: SystemEvent[]) => void;
  pushConnectionEvent: (ev: ConnectionEvent) => void;
  snapshotItem: (overrides: Partial<BackendTimelineItem>) => BackendTimelineItem;
  postWindow: () => BackendTimelineItem[];
  tlResp: (items?: BackendTimelineItem[], has_more?: boolean) => TimelineResponse;
  composedSnapshot: (timeline: TimelineResponse) => ConversationSnapshotResponse;
  getQueryClient: () => QueryClient;
  wrapper: ({ children }: { children: React.ReactNode }) => React.ReactElement;
  seedSettings: (overrides: Record<string, unknown>) => void;
  setVisibility: (visibility: DocumentVisibilityState) => void;
  delay: (ms?: number) => Promise<void>;
}
