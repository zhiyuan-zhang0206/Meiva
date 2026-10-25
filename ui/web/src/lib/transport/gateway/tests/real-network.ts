export const REAL_NETWORK_KEY = Symbol.for("meiva.test.realNetwork");

interface RealNetwork {
  readonly fetch: typeof fetch;
  readonly XMLHttpRequest: typeof XMLHttpRequest;
}

function captured(): RealNetwork {
  const value: unknown = Reflect.get(globalThis, REAL_NETWORK_KEY);
  if (typeof value !== "object" || value === null) {
    throw new Error("the test setup file did not capture the platform's network before installing its guards");
  }
  return value as RealNetwork;
}

// meiva:note
// The platform's own fetch and XMLHttpRequest, captured by the test setup file
// before it installs the guards that make every unmocked request throw. A test that
// owns a real network exchange (a local server on port 0) takes them from here and
// passes them to a transport explicitly.
export const realNetwork: RealNetwork = captured();
