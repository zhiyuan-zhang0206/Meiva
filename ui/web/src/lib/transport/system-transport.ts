import type { HttpTransport, KeySource, LockPort, StreamTransport } from "./transport";

export function systemKeySource(): KeySource {
  return {
    randomUUID: typeof crypto.randomUUID === "function" ? () => crypto.randomUUID() : null,
    getRandomValues: (bytes) => crypto.getRandomValues(bytes),
  };
}

export function systemHttpTransport(): HttpTransport {
  return {
    fetch: (url, init) => fetch(url, init),
    now: () => performance.now(),
    openUpload: () => new XMLHttpRequest(),
    get keys() {
      return systemKeySource();
    },
  };
}

function navigatorLocks(): unknown {
  return typeof navigator === "undefined" ? undefined : Reflect.get(navigator, "locks");
}

function usableLocks(): LockPort | null {
  const locks = navigatorLocks();
  if (typeof locks === "object" && locks !== null && "request" in locks && typeof locks.request === "function") {
    return locks as LockPort;
  }
  return null;
}

export function systemStreamTransport(): StreamTransport {
  return {
    openEventSource: (url, init) => new EventSource(url, init),
    get openChannel() {
      return typeof BroadcastChannel === "undefined" ? null : (name: string) => new BroadcastChannel(name);
    },
    get locks() {
      return usableLocks();
    },
    get page() {
      return document;
    },
    wallNow: () => Date.now(),
    newTabId: () => `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    diagnostics: {
      warn: (message) => {
        console.warn(message);
      },
      error: (message, cause) => {
        console.error(message, cause);
      },
    },
    get facts() {
      const secure: unknown = typeof window === "undefined" ? false : Reflect.get(window, "isSecureContext");
      const locks = navigatorLocks();
      return { secureContext: secure === true, locksObject: typeof locks === "object" && locks !== null };
    },
  };
}
