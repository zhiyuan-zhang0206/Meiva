import type { ReactNode } from "react";

import type { Api } from "../../gateway/api-factory";
import { ApiProvider } from "../../transport-context";

// meiva:note
// A gateway client for a test: the methods the test scripts and nothing else.
// Reading a method the test did not script fails at once, so a hook that reaches
// for an unexpected endpoint is a failing test rather than a silent no-op. The
// scripted methods are plain functions or small in-memory services, and the test
// asserts on what the hook shows or caches, not on how it was called.
export function fakeApi(methods: Partial<Api>): Api {
  return new Proxy(methods, {
    get(target, property) {
      const method: unknown = Reflect.get(target, property);
      if (method !== undefined) return method;
      throw new Error(`the test gateway client does not script api.${String(property)}`);
    },
  }) as Api;
}

export function apiWrapper(api: Api): ({ children }: { children: ReactNode }) => ReactNode {
  return function ApiWrapper({ children }) {
    return <ApiProvider api={api}>{children}</ApiProvider>;
  };
}
