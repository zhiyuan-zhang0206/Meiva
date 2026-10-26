import { createServer } from "node:http";

export const REAL_NETWORK_KEY = Symbol.for("meiva.test.realNetwork");

interface RealNetwork {
  readonly createServer: typeof createServer;
  readonly fetch: typeof fetch;
  readonly XMLHttpRequest: typeof XMLHttpRequest;
}

function captured(): RealNetwork {
  const value: unknown = Reflect.get(globalThis, REAL_NETWORK_KEY);
  if (typeof value === "object" && value !== null) {
    return Object.freeze({ ...(value as RealNetwork), createServer });
  }
  const fetchFn = Reflect.get(globalThis, "fetch");
  const xhr = Reflect.get(globalThis, "XMLHttpRequest");
  return Object.freeze({ fetch: fetchFn, XMLHttpRequest: xhr, createServer });
}

// meiva:note
// The platform's own fetch and XMLHttpRequest, captured by the test setup file
// before it installs the guards that make every unmocked request throw. A test that
// owns a real network exchange (a local server on port 0) takes them from here and
// passes them to a transport explicitly.
export const realNetwork: RealNetwork = captured();
