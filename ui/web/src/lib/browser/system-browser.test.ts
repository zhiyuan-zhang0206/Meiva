// meiva:note
// The real implementation, driven by a page object a test builds: every branch — a browser that
// refuses storage, a page without the async clipboard, a frame with no page at all — is a value
// here, so nothing stubs a global.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { describe, expect, it } from "vitest";

import type { LiveDocument, LiveNavigator, LivePage } from "./system-browser";
import {
  beaconOver,
  browserOver,
  clipboardOver,
  mediaOver,
  navigationOver,
  storageOver,
  viewOver,
} from "./system-browser";

function warnings(): string[] {
  return [];
}

function storageOf(entries: Record<string, string> = {}): Storage {
  const values = new Map(Object.entries(entries));
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => [...values.keys()][index] ?? null,
    removeItem: (key: string) => void values.delete(key),
    setItem: (key: string, value: string) => void values.set(key, value),
  };
}

function navigatorOf(overrides: Partial<LiveNavigator> = {}): LiveNavigator {
  return {
    clipboard: { writeText: () => Promise.resolve() },
    sendBeacon: () => true,
    ...overrides,
  };
}

function documentOf(overrides: Partial<LiveDocument> = {}): LiveDocument {
  const box = {
    value: "",
    style: {} as CSSStyleDeclaration,
    select: () => undefined,
  } as unknown as HTMLTextAreaElement;
  return {
    createElement: () => box,
    body: { appendChild: () => undefined, removeChild: () => undefined },
    execCommand: () => true,
    ...overrides,
  };
}

function pageOf(overrides: Partial<LivePage> = {}): LivePage {
  const listeners = new Set<() => void>();
  return {
    location: { href: "http://gateway.test/shell/7#inbox", assign: () => undefined },
    history: {
      state: null,
      pushState: () => undefined,
      replaceState: () => undefined,
      back: () => undefined,
    },
    addEventListener: (_type, listener) => void listeners.add(listener),
    removeEventListener: (_type, listener) => void listeners.delete(listener),
    localStorage: () => storageOf(),
    sessionStorage: () => storageOf(),
    matchMedia: (query) =>
      ({
        matches: query === "(min-width: 768px)",
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      }) as unknown as MediaQueryList,
    navigator: navigatorOf(),
    document: documentOf(),
    ...overrides,
  };
}

describe("storageOver", () => {
  it("tells a found value from an empty one", () => {
    const storage = storageOver(() => storageOf({ "meiva.tab": "tasks" }), "local", warnings().push);
    expect(storage.read("meiva.tab")).toEqual({ state: "found", value: "tasks" });
    expect(storage.read("meiva.absent")).toEqual({ state: "absent" });
  });

  it("writes and removes through the area", () => {
    const backing = storageOf();
    const storage = storageOver(() => backing, "local", warnings().push);
    expect(storage.write("meiva.tab", "inbox")).toBe("stored");
    expect(backing.getItem("meiva.tab")).toBe("inbox");
    expect(storage.remove("meiva.tab")).toBe("removed");
    expect(backing.getItem("meiva.tab")).toBeNull();
  });

  it("answers unavailable — not absent — when the frame has no area", () => {
    const storage = storageOver(() => null, "local", warnings().push);
    expect(storage.read("meiva.tab")).toEqual({ state: "unavailable" });
    expect(storage.write("meiva.tab", "inbox")).toBe("unavailable");
    expect(storage.remove("meiva.tab")).toBe("unavailable");
  });

  it("answers unavailable and warns once when the browser refuses storage", () => {
    const said: string[] = [];
    const storage = storageOver(
      () => {
        throw new Error("blocked by the browser");
      },
      "local",
      (message) => said.push(message),
    );
    expect(storage.read("a")).toEqual({ state: "unavailable" });
    expect(storage.write("a", "1")).toBe("unavailable");
    expect(storage.remove("a")).toBe("unavailable");
    expect(said).toHaveLength(1);
    expect(said[0]).toContain("local storage is unavailable");
  });

  it("says nothing in a frame with no storage area (a server render)", () => {
    const said: string[] = [];
    const storage = storageOver(() => null, "local", (message) => said.push(message));
    storage.read("a");
    storage.write("a", "1");
    expect(said).toEqual([]);
  });
});

describe("mediaOver", () => {
  it("answers a query and follows its changes", () => {
    const listeners = new Set<() => void>();
    let matches = true;
    const media = mediaOver(
      () =>
        ({
          get matches() {
            return matches;
          },
          addEventListener: (_type: string, listener: () => void) => void listeners.add(listener),
          removeEventListener: (_type: string, listener: () => void) => void listeners.delete(listener),
        }) as unknown as MediaQueryList,
    );
    const query = media.query("(min-width: 768px)");
    expect(query.matches()).toBe(true);
    let changes = 0;
    const stop = query.subscribe(() => {
      changes += 1;
    });
    expect(listeners.size).toBe(1);
    matches = false;
    for (const listener of listeners) listener();
    expect(query.matches()).toBe(false);
    expect(changes).toBe(1);
    stop();
    expect(listeners.size).toBe(0);
  });

  it("throws when asked in a frame with no page rather than inventing an answer", () => {
    const query = mediaOver(null).query("(min-width: 768px)");
    expect(() => query.matches()).toThrow(/server render/);
    expect(() => query.subscribe(() => undefined)).toThrow(/server render/);
  });
});

describe("navigationOver", () => {
  it("reads the address and the entry's state", () => {
    const navigation = navigationOver(pageOf({ history: { ...pageOf().history, state: { a: 1 } } }));
    expect(navigation.location()).toEqual({
      href: "http://gateway.test/shell/7#inbox",
      pathname: "/shell/7",
      search: "",
      hash: "#inbox",
    });
    expect(navigation.state()).toEqual({ a: 1 });
  });

  it("moves the page through the page's own history", () => {
    const calls: string[] = [];
    const page = pageOf({
      location: { href: "http://gateway.test/a", assign: (url) => void calls.push(`assign ${url}`) },
      history: {
        state: null,
        pushState: (state, _unused, url) => void calls.push(`push ${url} ${JSON.stringify(state)}`),
        replaceState: (state, _unused, url) => void calls.push(`replace ${url} ${JSON.stringify(state)}`),
        back: () => void calls.push("back"),
      },
    });
    const navigation = navigationOver(page);
    navigation.assign("http://gateway.test/b");
    navigation.push("http://gateway.test/c", { at: 1 });
    navigation.replace("http://gateway.test/d", { at: 2 });
    navigation.back();
    expect(calls).toEqual([
      "assign http://gateway.test/b",
      'push http://gateway.test/c {"at":1}',
      'replace http://gateway.test/d {"at":2}',
      "back",
    ]);
  });

  it("delivers a traversal to its listener until it is released", () => {
    const listeners = new Set<() => void>();
    const navigation = navigationOver(
      pageOf({
        addEventListener: (_type, listener) => void listeners.add(listener),
        removeEventListener: (_type, listener) => void listeners.delete(listener),
      }),
    );
    let traversed = 0;
    const stop = navigation.onTraverse(() => {
      traversed += 1;
    });
    for (const listener of listeners) listener();
    expect(traversed).toBe(1);
    stop();
    expect(listeners.size).toBe(0);
  });

  it("has no address in a frame with no page, and refuses to move one", () => {
    const navigation = navigationOver(null);
    expect(navigation.location()).toBeNull();
    expect(() => navigation.assign("/a")).toThrow(/server render cannot navigate/);
    expect(() => navigation.push("/a", null)).toThrow(/server render cannot push/);
    expect(() => navigation.replace("/a", null)).toThrow(/server render cannot replace/);
    expect(() => navigation.back()).toThrow(/server render cannot go back/);
    expect(() => navigation.state()).toThrow(/server render cannot read history state/);
    expect(() => navigation.onTraverse(() => undefined)).toThrow(/server render cannot listen/);
  });
});

describe("clipboardOver", () => {
  it("copies with the async API when the page has it", async () => {
    const written: string[] = [];
    const clipboard = clipboardOver(
      navigatorOf({ clipboard: { writeText: (text) => (void written.push(text), Promise.resolve()) } }),
      documentOf(),
    );
    await expect(clipboard.write("hello")).resolves.toBe(true);
    expect(written).toEqual(["hello"]);
  });

  it("falls back to execCommand when the page has no async clipboard", async () => {
    const executed: string[] = [];
    const clipboard = clipboardOver(
      navigatorOf({ clipboard: undefined }),
      documentOf({ execCommand: (command) => (void executed.push(command), true) }),
    );
    await expect(clipboard.write("hello")).resolves.toBe(true);
    expect(executed).toEqual(["copy"]);
  });

  it("reports a copy the page refused rather than throwing", async () => {
    const clipboard = clipboardOver(
      navigatorOf({ clipboard: { writeText: () => Promise.reject(new Error("denied")) } }),
      documentOf(),
    );
    await expect(clipboard.write("hello")).resolves.toBe(false);
  });

  it("rejects in a frame with no page", async () => {
    await expect(clipboardOver(null, null).write("hello")).rejects.toThrow(/server render/);
  });
});

describe("beaconOver", () => {
  it("reports what the page answered", () => {
    const accepted = beaconOver(navigatorOf({ sendBeacon: () => true }));
    const refused = beaconOver(navigatorOf({ sendBeacon: () => false }));
    expect(accepted.send("http://gateway.test/t", "{}")).toBe(true);
    expect(refused.send("http://gateway.test/t", "{}")).toBe(false);
  });

  it("reports a beacon the page threw on as refused", () => {
    const beacon = beaconOver(
      navigatorOf({
        sendBeacon: () => {
          throw new Error("too large");
        },
      }),
    );
    expect(beacon.send("http://gateway.test/t", "{}")).toBe(false);
  });

  it("throws in a frame with no page", () => {
    expect(() => beaconOver(null).send("http://gateway.test/t", "{}")).toThrow(/server render/);
  });
});

describe("viewOver", () => {
  it("scrolls the element itself into view", () => {
    const calls: ScrollIntoViewOptions[] = [];
    const target = { scrollIntoView: (options?: ScrollIntoViewOptions) => void calls.push(options ?? {}) };
    viewOver(pageOf()).scrollIntoView(target as unknown as Element, "nearest");
    expect(calls).toEqual([{ block: "nearest" }]);
  });

  it("throws in a frame with no page", () => {
    const target = { scrollIntoView: () => undefined } as unknown as Element;
    expect(() => viewOver(null).scrollIntoView(target, "start")).toThrow(/server render/);
  });
});

describe("browserOver", () => {
  it("builds every capability of a live page", () => {
    const browser = browserOver(pageOf(), warnings().push);
    expect(browser.navigation.location()?.pathname).toBe("/shell/7");
    expect(browser.local.read("missing")).toEqual({ state: "absent" });
    expect(browser.media.query("(min-width: 768px)").matches()).toBe(true);
  });

  it("builds the inert browser a server render sees", () => {
    const browser = browserOver(null, warnings().push);
    expect(browser.navigation.location()).toBeNull();
    expect(browser.local.read("a")).toEqual({ state: "unavailable" });
    expect(browser.session.write("a", "1")).toBe("unavailable");
  });
});
