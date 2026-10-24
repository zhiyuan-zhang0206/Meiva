import type { ReactNode } from "react";

import type { AuthResult } from "../../stream/tests/scripted-auth";

export interface StreamSourceLike {
  url: string;
  init: EventSourceInit;
  readyState: number;
  open(): void;
  frame(data: unknown): void;
  error(state: number): void;
}

export interface EventStreamTestContext {
  auth: {
    always(result: AuthResult): void;
    once(result: AuthResult): void;
    onceDeferred(): { release: (result: AuthResult) => void };
    probes(): number;
  };
  READY: {
    readonly CONNECTING: 0;
    readonly OPEN: 1;
    readonly CLOSED: 2;
  };
  getLastInstance: () => StreamSourceLike | null;
  getInstances: () => StreamSourceLike[];
  resetInstances: () => void;
  expectInstance: () => StreamSourceLike;
  waitForInstance: () => Promise<void>;
  withProvider: () => ({ children }: { children: ReactNode }) => ReactNode;
  withAgentProvider: () => ({ children }: { children: ReactNode }) => ReactNode;
  useAuth: () => { status: string };
  getStoreState: () => { reconnectNonce: number; activeId: number | null };
  setStoreState: (partial: { reconnectNonce?: number; activeId?: number | null }) => void;
  setDocumentVisibility: (state: DocumentVisibilityState) => void;
  reportedErrors: () => { message: string; cause: unknown }[];
}
