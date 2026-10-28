// meiva:note
// The suite's fake transport: the real client's whole endpoint surface, every gateway
// call a `vi.fn()`. A test answers the calls it cares about and renders the page under
// `<ApiProvider api={transport}>` -- the same provider the app mounts -- so nothing
// replaces the transport module. An unanswered call rejects with its own name, so a test
// that forgot an answer fails at that call instead of on an undefined payload.
import { vi, type MockedFunction } from "vitest";
import type { ReactNode } from "react";

import type { Api } from "@/lib/transport/api";
import { cacheOnlyApi } from "@/lib/transport/tests/api/api-provider";
import { ApiProvider } from "@/lib/transport/transport-context";

// meiva:note
// Answers are values a test grabs from a fixture, not complete wire payloads, so the
// fake accepts whatever a fixture hands back. The argument list still comes from the
// real client, which keeps a misspelled call site and a misplaced argument a type error.
export type FakeApi = Api & {
  [K in keyof Api]: Api[K] extends (...args: infer A) => infer R
    ? MockedFunction<(...args: A) => Awaited<R>> & {
        mockResolvedValue(value: unknown): void;
        mockImplementation(fn: (...args: never[]) => unknown): void;
        mockReturnValue(value: unknown): void;
      }
    : never;
};

// meiva:note
// `newOperationKey` mints a key locally; every other member is a gateway call. An
// unanswered gateway call rejects, the same way a test says "no network in tests".
let mintedKeys = 0;

function answerFor(name: string): () => unknown {
  if (name === "newOperationKey") {
    return () => `test-operation-key-${++mintedKeys}`;
  }
  return () => Promise.reject(new Error(`unanswered transport call: ${name}`));
}

function blankTransport(): FakeApi {
  const fake: Record<string, unknown> = {};
  for (const name of Object.keys(cacheOnlyApi())) {
    fake[name] = vi.fn(answerFor(name));
  }
  return fake as unknown as FakeApi;
}

// meiva:note
// One fake per test file: the module registry is per file, so no test sees another's answers.
export const transport: FakeApi = blankTransport();

// meiva:note
// Clear every answer and recorded call, and put the unanswered call back.
export function resetTransport(): void {
  mintedKeys = 0;
  for (const [name, method] of Object.entries(transport)) {
    const fake = method as MockedFunction<(...args: never[]) => unknown>;
    fake.mockReset();
    fake.mockImplementation(answerFor(name));
  }
}

// meiva:note
// Render the page under the provider a test needs; the app mounts the same one.
export function ApiTestProvider({ children }: { children?: ReactNode }) {
  return <ApiProvider api={transport}>{children}</ApiProvider>;
}
