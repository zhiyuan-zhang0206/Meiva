import type { QueryClient } from "@tanstack/react-query";
import type React from "react";
import type { Mock, MockInstance } from "vitest";

import type {
  BackendTimelineItem,
  ConversationSnapshotResponse,
  SystemEvent,
  TimelineResponse,
  UserSettingListResponse,
} from "../../contracts/types";
import type { ApiError } from "../../transport/api";
import type { ConnectionEvent } from "../../transport/useEventStream";

export interface UseTimelineTestApi {
  getTimeline: (
    agentId: number,
    opts?: { limit?: number; before?: string; signal?: AbortSignal },
  ) => Promise<TimelineResponse>;
  getConversationSnapshot: (
    agentId: number,
    signal?: AbortSignal,
  ) => Promise<ConversationSnapshotResponse>;
  getSettings: () => Promise<UserSettingListResponse>;
}

export interface UseTimelineTestContext {
  api: UseTimelineTestApi;
  ApiError: typeof ApiError;
  noteTurnStart: (agentId: number) => void;
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
  spyInvalidateQueries: () => MockInstance<(opts?: { queryKey?: unknown }) => Promise<void>>;
  spyReloadSnapshot: () => MockInstance;
  spyDocumentVisibility: () => MockInstance<() => DocumentVisibilityState>;
  dispatchVisibilityChange: () => void;
  delay: (ms?: number) => Promise<void>;
}
