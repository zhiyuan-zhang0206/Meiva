import type { ReactNode } from "react";
import type { Mock, MockInstance } from "vitest";

export interface MockEventSourceLike {
  url: string;
  init?: EventSourceInit;
  readyState: number;
  fireOpen(): void;
  fireMessage(data: unknown): void;
  fireErrorWithReadyState(state: number): void;
}

export interface EventStreamTestContext {
  checkAuthMock: Mock<() => Promise<{ authenticated: boolean }>>;
  MockEventSource: {
    readonly CONNECTING: 0;
    readonly OPEN: 1;
    readonly CLOSED: 2;
  };
  getLastInstance: () => MockEventSourceLike | null;
  getInstances: () => MockEventSourceLike[];
  resetInstances: () => void;
  deferredAuthResult: () => {
    promise: Promise<{ authenticated: boolean }>;
    resolve: (result: { authenticated: boolean }) => void;
  };
  expectInstance: () => MockEventSourceLike;
  waitForInstance: () => Promise<void>;
  withProvider: () => ({ children }: { children: ReactNode }) => ReactNode;
  withAgentProvider: () => ({ children }: { children: ReactNode }) => ReactNode;
  useAuth: () => { status: string };
  getStoreState: () => { reconnectNonce: number; activeId: number | null };
  setStoreState: (partial: { reconnectNonce?: number; activeId?: number | null }) => void;
  setDocumentVisibility: (state: DocumentVisibilityState) => void;
  spyConsoleError: () => MockInstance;
}
