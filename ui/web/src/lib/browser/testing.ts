// meiva:note
// The browser port a test hands in: a page whose address, storage, media answers, clipboard,
// beacon and scrolling are values the test sets, with a record of what the component asked for.
//
// By default the fake is the page the test itself runs in — its address, its two storage areas and
// its media queries — so a test that sets `window.innerWidth` or seeds `localStorage` keeps the
// meaning it had before the port existed, while a test that wants a specific answer passes one
// (`{ media: { "(pointer: coarse)": true } }`) instead of stubbing a global. What is new is the
// record: `browser.actions` says what the component asked the page to do.
//
// It resolves a relative address against the page the test is on, and it can refuse a whole area
// (`refuseStorage`) or one operation while reads still work (`failStorage`), so the `unavailable`
// answer is constructed rather than assumed.
//
// It lives beside the port because the hooks in lib/ are the first thing a test drives through it,
// and lib/ may not import src/test-support/ (that would close a cycle: test-support already
// reaches lib). `src/test-support/browser.ts` re-exports it for component tests.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { createElement, type ReactNode } from "react";

import type { Browser, MediaQuery, PageLocation, StorageRead } from "./browser";
import { pageLocation } from "./browser";
import { BrowserProvider } from "./browser-provider";

export interface FakeActions {
  readonly assigned: string[];
  readonly pushed: { readonly url: string; readonly state: unknown }[];
  readonly replaced: { readonly url: string; readonly state: unknown }[];
  readonly scrolled: { readonly target: Element; readonly block: ScrollLogicalPosition }[];
  readonly copied: string[];
  readonly beaconed: { readonly url: string; readonly body: string }[];
  traversals: number;
  backs: number;
}

export interface FakeStore {
  get(key: string): string | undefined;
  has(key: string): boolean;
}

export interface FakeBrowser extends Browser {
  href(): string;
  moveTo(href: string): void;
  traverse(href: string): void;
  answerMedia(query: string, matches: boolean): void;
  refuseStorage(refused: boolean): void;
  failStorage(area: "local" | "session", operations: "write" | "remove" | "both"): void;
  clipboardAccepts(accepts: boolean): void;
  beaconAccepts(accepts: boolean): void;
  readonly localValues: FakeStore;
  readonly sessionValues: FakeStore;
  readonly actions: FakeActions;
}

export interface FakeBrowserOptions {
  readonly href?: string;
  readonly local?: Readonly<Record<string, string>>;
  readonly session?: Readonly<Record<string, string>>;
  readonly media?: Readonly<Record<string, boolean>>;
}

interface Backing {
  get(key: string): string | null;
  set(key: string, value: string): void;
  remove(key: string): void;
}

function memoryBacking(seed: Readonly<Record<string, string>>): Backing {
  const values = new Map(Object.entries(seed));
  return {
    get: (key) => values.get(key) ?? null,
    set: (key, value) => void values.set(key, value),
    remove: (key) => void values.delete(key),
  };
}

function liveStorage(area: "localStorage" | "sessionStorage"): Backing | null {
  if (typeof window === "undefined") return null;
  try {
    const storage = window[area];
    return {
      get: (key) => storage.getItem(key),
      set: (key, value) => storage.setItem(key, value),
      remove: (key) => storage.removeItem(key),
    };
  } catch {
    return null;
  }
}

function view(backing: Backing | null): FakeStore {
  return {
    get: (key) => backing?.get(key) ?? undefined,
    has: (key) => backing?.get(key) != null,
  };
}

export function pageWrapper(browser: FakeBrowser) {
  return function Page({ children }: { children: ReactNode }) {
    return createElement(BrowserProvider, { browser }, children);
  };
}

export function fakeBrowser(options: FakeBrowserOptions = {}): FakeBrowser {
  const live = typeof window === "undefined" ? null : window;
  // meiva:note
  // A caller may write the page it wants as a path (`/#ticket=x`); it is resolved against the
  // page the test runs on, so `pageLocation` always receives an absolute address.
  const base = live?.location.href ?? "http://fake.local/";
  let href = options.href === undefined ? base : new URL(options.href, base).href;
  let historyState: unknown = null;
  let refused = false;
  let acceptsClipboard = true;
  let acceptsBeacon = true;
  const localValues =
    options.local === undefined ? liveStorage("localStorage") : memoryBacking(options.local);
  const sessionValues =
    options.session === undefined ? liveStorage("sessionStorage") : memoryBacking(options.session);
  const answers = new Map<
    string,
    { matches: boolean; explicit: boolean; listeners: Set<() => void> }
  >(
    Object.entries(options.media ?? {}).map(([query, matches]) => [
      query,
      { matches, explicit: true, listeners: new Set() },
    ]),
  );
  const traverseListeners = new Set<() => void>();
  const resolve = (url: string): string => new URL(url, href).href;
  const actions: FakeActions = {
    assigned: [],
    pushed: [],
    replaced: [],
    scrolled: [],
    copied: [],
    beaconed: [],
    traversals: 0,
    backs: 0,
  };

  const failures = { local: { write: false, remove: false }, session: { write: false, remove: false } };

  const area = (name: "local" | "session", backing: Backing | null) => ({
    read(key: string): StorageRead {
      if (refused || backing === null) return { state: "unavailable" };
      try {
        const value = backing.get(key);
        return value === null ? { state: "absent" } : { state: "found", value };
      } catch {
        return { state: "unavailable" };
      }
    },
    write(key: string, value: string): "stored" | "unavailable" {
      if (refused || backing === null || failures[name].write) return "unavailable";
      try {
        backing.set(key, value);
        return "stored";
      } catch {
        return "unavailable";
      }
    },
    remove(key: string): "removed" | "unavailable" {
      if (refused || backing === null || failures[name].remove) return "unavailable";
      try {
        backing.remove(key);
        return "removed";
      } catch {
        return "unavailable";
      }
    },
  });

  const answer = (query: string) => {
    const known = answers.get(query);
    if (known !== undefined) return known;
    const heard = { matches: false, explicit: false, listeners: new Set<() => void>() };
    answers.set(query, heard);
    return heard;
  };

  const fake: FakeBrowser = {
    navigation: {
      location: (): PageLocation => pageLocation(href),
      state: () => historyState,
      assign(url) {
        actions.assigned.push(url);
        href = resolve(url);
      },
      push(url, state) {
        actions.pushed.push({ url, state });
        href = resolve(url);
        historyState = state;
      },
      replace(url, state) {
        actions.replaced.push({ url, state });
        href = resolve(url);
        historyState = state;
      },
      back() {
        actions.backs += 1;
      },
      onTraverse(listener) {
        traverseListeners.add(listener);
        actions.traversals = traverseListeners.size;
        return () => {
          traverseListeners.delete(listener);
          actions.traversals = traverseListeners.size;
        };
      },
    },
    local: area("local", localValues),
    session: area("session", sessionValues),
    media: {
      query(query: string): MediaQuery {
        const heard = answer(query);
        const list = live?.matchMedia(query);
        return {
          matches: () => (heard.explicit ? heard.matches : list?.matches === true),
          subscribe: (onChange) => {
            heard.listeners.add(onChange);
            list?.addEventListener("change", onChange);
            return () => {
              heard.listeners.delete(onChange);
              list?.removeEventListener("change", onChange);
            };
          },
        };
      },
    },
    clipboard: {
      write(text) {
        if (!acceptsClipboard) return Promise.resolve(false);
        actions.copied.push(text);
        return Promise.resolve(true);
      },
    },
    beacon: {
      send(url, body) {
        if (!acceptsBeacon) return false;
        actions.beaconed.push({ url, body });
        return true;
      },
    },
    view: {
      scrollIntoView(target, block) {
        actions.scrolled.push({ target, block });
      },
    },
    href: () => href,
    moveTo(next) {
      href = resolve(next);
    },
    traverse(next) {
      href = resolve(next);
      for (const listener of [...traverseListeners]) listener();
    },
    answerMedia(query, matches) {
      const heard = answer(query);
      heard.matches = matches;
      heard.explicit = true;
      for (const listener of [...heard.listeners]) listener();
    },
    refuseStorage(value) {
      refused = value;
    },
    failStorage(areaName, operations) {
      failures[areaName].write = operations === "write" || operations === "both";
      failures[areaName].remove = operations === "remove" || operations === "both";
    },
    clipboardAccepts(value) {
      acceptsClipboard = value;
    },
    beaconAccepts(value) {
      acceptsBeacon = value;
    },
    localValues: view(localValues),
    sessionValues: view(sessionValues),
    actions,
  };
  return fake;
}
