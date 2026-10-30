// meiva:note
// The browser capability port's real implementation and this frontend's composition root for the
// page: the one file that reads `window`. It builds the port from the live page and hands it down
// through <BrowserProvider>; nothing below reads the browser itself.
//
// Each capability is built by a small function that takes the page's own objects, so every branch
// — a browser that refuses storage, a page without a clipboard API, a frame with no page at all —
// is constructed by a test with a plain object, never by stubbing a global.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import type {
  Browser,
  BrowserBeacon,
  BrowserClipboard,
  BrowserMedia,
  BrowserNavigation,
  BrowserObjectUrls,
  BrowserStorage,
  BrowserViewport,
  MediaQuery,
} from "./browser";
import { pageLocation } from "./browser";

export interface LiveNavigator {
  readonly clipboard?: { writeText(text: string): Promise<void> } | undefined;
  sendBeacon(url: string, data?: BodyInit | null): boolean;
}

export interface LiveDocument {
  createElement(tag: "textarea"): HTMLTextAreaElement;
  readonly body: { appendChild(node: Node): unknown; removeChild(node: Node): unknown };
  execCommand(command: "copy"): boolean;
}

export interface LiveViewport {
  readonly height: number;
  readonly scale: number;
  addEventListener(type: "resize" | "scroll", listener: () => void): void;
  removeEventListener(type: "resize" | "scroll", listener: () => void): void;
}

export interface LivePage {
  readonly location: { readonly href: string; assign(url: string): void };
  readonly history: {
    readonly state: unknown;
    pushState(state: unknown, unused: string, url: string): void;
    replaceState(state: unknown, unused: string, url: string): void;
    back(): void;
  };
  addEventListener(type: "popstate", listener: () => void): void;
  removeEventListener(type: "popstate", listener: () => void): void;
  localStorage(): Storage | null;
  createObjectUrl(file: Blob): string;
  revokeObjectUrl(url: string): void;
  sessionStorage(): Storage | null;
  matchMedia(query: string): MediaQueryList;
  readonly innerHeight: number;
  readonly visualViewport: LiveViewport | null;
  readonly navigator: LiveNavigator;
  readonly document: LiveDocument;
}

// meiva:note
// One storage area, guarded once here so no caller repeats the try/catch a browser forces on it —
// reading the area itself can throw, not only the call. A refused read is `unavailable`, never
// `absent`: a private window and an empty area answer differently, and the caller decides what each
// means. The first refusal warns, because a page whose selections silently stop being remembered is
// worth one line in the console.
export function storageOver(
  area: () => Storage | null,
  name: string,
  warn: (message: string) => void,
): BrowserStorage {
  let warned = false;
  const refuse = (operation: string): void => {
    if (warned) return;
    warned = true;
    warn(`[browser] ${name} storage is unavailable: ${operation} will not be remembered`);
  };
  return {
    read(key) {
      try {
        const storage = area();
        if (storage === null) return { state: "unavailable" };
        const value = storage.getItem(key);
        return value === null ? { state: "absent" } : { state: "found", value };
      } catch {
        refuse("a value");
        return { state: "unavailable" };
      }
    },
    write(key, value) {
      try {
        const storage = area();
        if (storage === null) return "unavailable";
        storage.setItem(key, value);
        return "stored";
      } catch {
        refuse("a value");
        return "unavailable";
      }
    },
    remove(key) {
      try {
        const storage = area();
        if (storage === null) return "unavailable";
        storage.removeItem(key);
        return "removed";
      } catch {
        refuse("a value");
        return "unavailable";
      }
    },
  };
}

// meiva:note
// The page's media queries. A frame with no page cannot answer one, and saying so is the point: a
// server render takes the server snapshot its hook declares instead of asking the port.
export function mediaOver(match: ((query: string) => MediaQueryList) | null): BrowserMedia {
  return {
    query(query) {
      let followed: MediaQueryList | null = null;
      const live = (): MediaQueryList => {
        if (match === null) {
          throw new Error("a server render has no media answer: render the hook's server snapshot");
        }
        followed ??= match(query);
        return followed;
      };
      const answer: MediaQuery = {
        matches: () => live().matches,
        subscribe: (onChange) => {
          const list = live();
          list.addEventListener("change", onChange);
          return () => list.removeEventListener("change", onChange);
        },
      };
      return answer;
    },
  };
}

// meiva:note
// The page's address and history. Reading the address is allowed in a frame without one (it
// answers null, the server's honest "no page yet"); moving it is not, and says so.
export function navigationOver(page: LivePage | null): BrowserNavigation {
  const live = (action: string): LivePage => {
    if (page === null) throw new Error(`a server render cannot ${action}`);
    return page;
  };
  return {
    location: () => (page === null ? null : pageLocation(page.location.href)),
    state: () => live("read history state").history.state,
    assign: (url) => live("navigate").location.assign(url),
    push: (url, state) => live("push a history entry").history.pushState(state, "", url),
    replace: (url, state) => live("replace a history entry").history.replaceState(state, "", url),
    back: () => live("go back").history.back(),
    onTraverse: (listener) => {
      const target = live("listen for traversal");
      target.addEventListener("popstate", listener);
      return () => target.removeEventListener("popstate", listener);
    },
  };
}

// meiva:note
// The clipboard: the async API when the page has it, `execCommand("copy")` through a hidden
// textarea when it does not (plain-HTTP LAN deployments have no secure context, so the async API
// is undefined there even though lib.dom.d.ts types it as always present). Never throws: a copy
// that fails is a copy the button reports as not done.
export function clipboardOver(
  navigator: LiveNavigator | null,
  document: LiveDocument | null,
): BrowserClipboard {
  return {
    async write(text) {
      if (navigator === null || document === null) {
        throw new Error("a server render has no clipboard");
      }
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const box = document.createElement("textarea");
          box.value = text;
          box.style.position = "fixed";
          box.style.opacity = "0";
          document.body.appendChild(box);
          box.select();
          document.execCommand("copy");
          document.body.removeChild(box);
        }
        return true;
      } catch {
        return false;
      }
    },
  };
}

// meiva:note
// The unload-safe delivery: `false` means the page refused the body (or has no beacon at all), and
// the caller falls back to a keepalive request. A frame with no page has no beacon.
export function beaconOver(navigator: LiveNavigator | null): BrowserBeacon {
  return {
    send(url, body) {
      if (navigator === null) throw new Error("a server render has no beacon");
      try {
        return navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
      } catch {
        return false;
      }
    },
  };
}

// meiva:note
// Scrolling an element into view is an action on the page, not a read of it: the element scrolls
// itself, and the port's job is to be the one place a component asks, so a test can see what was
// scrolled where without a DOM spy.
export function viewOver(page: LivePage | null): Browser["view"] {
  return {
    scrollIntoView(target, block) {
      if (page === null) throw new Error("a server render has no view to scroll");
      target.scrollIntoView({ block });
    },
  };
}

// meiva:note
// The page's viewport. Every read is a fresh measurement — a height is not a stored fact — and the
// subscription follows only the visual viewport: the one a keyboard, a toolbar or a pinch moves.
// A frame with no page (a server render) has neither, and says so with a zero reading.
export function viewportOver(page: LivePage | null): BrowserViewport {
  return {
    read: () =>
      page === null
        ? { layoutHeight: 0, visual: null }
        : {
            layoutHeight: page.innerHeight,
            visual:
              page.visualViewport === null
                ? null
                : { height: page.visualViewport.height, scale: page.visualViewport.scale },
          },
    subscribe(onChange) {
      const visual = page?.visualViewport ?? null;
      if (visual === null) return () => undefined;
      visual.addEventListener("resize", onChange);
      visual.addEventListener("scroll", onChange);
      return () => {
        visual.removeEventListener("resize", onChange);
        visual.removeEventListener("scroll", onChange);
      };
    },
  };
}

// meiva:note
// Object URLs of the page. A frame with no page has none: only a browser holds a blob under a URL,
// so a server render says so instead of inventing one.
export function objectUrlsOver(page: LivePage | null): BrowserObjectUrls {
  const live = (): LivePage => {
    if (page === null) throw new Error("a server render has no object URL");
    return page;
  };
  return {
    create: (file) => live().createObjectUrl(file),
    revoke: (url) => live().revokeObjectUrl(url),
  };
}

const warnToConsole = (message: string): void => console.warn(message);

export function browserOver(page: LivePage | null, warn: (message: string) => void): Browser {
  return {
    navigation: navigationOver(page),
    local: storageOver(() => (page === null ? null : page.localStorage()), "local", warn),
    session: storageOver(() => (page === null ? null : page.sessionStorage()), "session", warn),
    media: mediaOver(page === null ? null : (query) => page.matchMedia(query)),
    clipboard: clipboardOver(page?.navigator ?? null, page?.document ?? null),
    beacon: beaconOver(page?.navigator ?? null),
    view: viewOver(page),
    viewport: viewportOver(page),

    objectUrls: objectUrlsOver(page),
  };
}

// meiva:note
// The composition root: the production value is read here, once, and every part of the app takes
// it from <BrowserProvider> or from a caller that already holds it. `window` is named in this file
// and nowhere else in the frontend, so `meizel` sees the page enter here and nowhere else.
export const browser: Browser =
  typeof window === "undefined"
    ? browserOver(null, warnToConsole)
    : browserOver({
        location: window.location,
        history: window.history,
        addEventListener: (type, listener) => window.addEventListener(type, listener),
        removeEventListener: (type, listener) => window.removeEventListener(type, listener),
        localStorage: () => window.localStorage,
        sessionStorage: () => window.sessionStorage,
        matchMedia: (query) => window.matchMedia(query),
        innerHeight: window.innerHeight,
        visualViewport: window.visualViewport,

        createObjectUrl: (file) => URL.createObjectURL(file),
        revokeObjectUrl: (url) => URL.revokeObjectURL(url),
        navigator: window.navigator,
        document: window.document,
      }, warnToConsole);
