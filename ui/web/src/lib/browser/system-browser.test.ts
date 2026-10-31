// meiva:note
// The real implementation, driven by a page object a test builds: every branch — a browser that
// refuses storage, a page without the async clipboard, a frame with no page at all — is a value
// here, so nothing stubs a global.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { describe, expect, it } from "vitest";

import type { LiveDocument, LiveNavigator, LivePage, LiveViewport } from "./system-browser";
import {
  beaconOver,
  browserOver,
  clipboardOver,
  idleOver,
  mediaOver,
  navigationOver,
  objectUrlsOver,
  sizeOver,
  storageOver,
  viewOver,
  viewportOver,
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

// meiva:note
// A viewport a test can move: `height` is writable here (the port's reading is a snapshot, so the
// real interface has it read-only), and `fire` tells every subscriber what a browser would.
interface ViewportDouble extends LiveViewport {
  height: number;
  fire(): void;
  listening(): number;
}

function viewportOf(height: number, scale = 1): ViewportDouble {
  const listeners: Record<"resize" | "scroll", Set<() => void>> = {
    resize: new Set(),
    scroll: new Set(),
  };
  return {
    height,
    scale,
    addEventListener: (type, listener) => void listeners[type].add(listener),
    removeEventListener: (type, listener) => void listeners[type].delete(listener),
    fire: () => {
      for (const set of [listeners.resize, listeners.scroll]) for (const listener of [...set]) listener();
    },
    listening: () => listeners.resize.size + listeners.scroll.size,
  };
}

function pageOf(overrides: Partial<LivePage> = {}): LivePage {
  const listeners = new Set<() => void>();
  const page: LivePage = {
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
    createObjectUrl: () => "blob:page",
    revokeObjectUrl: () => undefined,
    sessionStorage: () => storageOf(),
    matchMedia: (query) => ({
      matches: query === "(min-width: 768px)",
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }) as unknown as MediaQueryList,
    innerHeight: 900,
    visualViewport: viewportOf(900),
    navigator: navigatorOf(),
    document: documentOf(),
    setTimeout: () => 0,
    clearTimeout: () => undefined,
  };
  return Object.assign(page, overrides);
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

describe("viewportOver", () => {
  it("reads the layout height and the visual band each time it is asked", () => {
    const visual = viewportOf(640, 1);
    const viewport = viewportOver(pageOf({ innerHeight: 900, visualViewport: visual }));
    expect(viewport.read()).toEqual({ layoutHeight: 900, visual: { height: 640, scale: 1 } });
    visual.height = 900;
    expect(viewport.read().visual).toEqual({ height: 900, scale: 1 });
  });

  it("tells a page without a visual viewport from one the keyboard moved", () => {
    const viewport = viewportOver(pageOf({ innerHeight: 900, visualViewport: null }));
    expect(viewport.read()).toEqual({ layoutHeight: 900, visual: null });
  });

  it("follows the visual viewport's resize and scroll, and stops on unsubscribe", () => {
    const visual = viewportOf(640);
    const viewport = viewportOver(pageOf({ visualViewport: visual }));
    let heard = 0;
    const unsubscribe = viewport.subscribe(() => {
      heard += 1;
    });
    expect(visual.listening()).toBe(2);
    visual.fire();
    expect(heard).toBe(2);

    unsubscribe();
    expect(visual.listening()).toBe(0);
    visual.fire();
    expect(heard).toBe(2);
  });

  it("has nothing to follow in a frame with no page", () => {
    const viewport = viewportOver(null);
    expect(viewport.read()).toEqual({ layoutHeight: 0, visual: null });
    const unsubscribe = viewport.subscribe(() => undefined);
    expect(unsubscribe()).toBeUndefined();
  });
});

describe("objectUrlsOver", () => {
  it("creates and releases through the page it was built from", () => {
    const created: string[] = [];
    const revoked: string[] = [];
    const objectUrls = objectUrlsOver(
      pageOf({
        createObjectUrl: () => {
          created.push("blob:one");
          return "blob:one";
        },
        revokeObjectUrl: (url) => revoked.push(url),
      }),
    );
    const url = objectUrls.create(new Blob(["x"]));
    objectUrls.revoke(url);
    expect(created).toEqual(["blob:one"]);
    expect(revoked).toEqual(["blob:one"]);
  });

  it("says a server render has no object URL instead of inventing one", () => {
    const objectUrls = objectUrlsOver(null);
    expect(() => objectUrls.create(new Blob(["x"]))).toThrow(/server render/);
    expect(() => objectUrls.revoke("blob:one")).toThrow(/server render/);
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

describe("sizeOver", () => {
  it("attaches the page's observer to the element and ends the watch", () => {
    const watched: Element[] = [];
    const changes: (() => void)[] = [];
    let disconnected = 0;
    const target = document.createElement("div");
    const size = sizeOver(
      pageOf({
        newResizeObserver: (onChange) => {
          changes.push(onChange);
          return {
            observe: (element) => void watched.push(element),
            disconnect: () => {
              disconnected += 1;
            },
          };
        },
      }),
    );

    const stop = size.observe(target, () => undefined);

    expect(watched).toEqual([target]);
    expect(changes).toHaveLength(1);
    stop();
    expect(disconnected).toBe(1);
  });

  it("watches nothing on a frame with no page, or a browser without ResizeObserver", () => {
    let heard = 0;
    const stopWithoutPage = sizeOver(null).observe(document.createElement("div"), () => {
      heard += 1;
    });
    const stopWithoutObserver = sizeOver(pageOf()).observe(
      document.createElement("div"),
      () => {
        heard += 1;
      },
    );
    stopWithoutPage();
    stopWithoutObserver();
    expect(heard).toBe(0);
  });
});

describe("idleOver", () => {
  it("asks the page for an idle slice with the caller's deadline, and cancels through the page", () => {
    const asked: { timeout: number }[] = [];
    const cancelled: number[] = [];
    const idle = idleOver(
      pageOf({
        requestIdleCallback: (_work, options) => {
          asked.push(options);
          return 11;
        },
        cancelIdleCallback: (handle) => void cancelled.push(handle),
      }),
    );

    const stop = idle.schedule(() => undefined, { deadlineMs: 50, fallbackDelayMs: 2000 });

    expect(asked).toEqual([{ timeout: 50 }]);
    expect(cancelled).toEqual([]);
    stop();
    expect(cancelled).toEqual([11]);
  });

  it("waits the caller's delay when the page has no idle callback (Safari)", () => {
    const delays: number[] = [];
    const cleared: number[] = [];
    const idle = idleOver(
      pageOf({
        setTimeout: (_work, delayMs) => {
          delays.push(delayMs);
          return 3;
        },
        clearTimeout: (handle) => void cleared.push(handle),
      }),
    );

    const stop = idle.schedule(() => undefined, { deadlineMs: 4000, fallbackDelayMs: 2000 });

    expect(delays).toEqual([2000]);
    stop();
    expect(cleared).toEqual([3]);
  });

  it("never runs the work on a frame with no page", () => {
    let ran = 0;
    const stop = idleOver(null).schedule(
      () => {
        ran += 1;
      },
      { deadlineMs: 0, fallbackDelayMs: 0 },
    );
    stop();
    expect(ran).toBe(0);
  });
});
