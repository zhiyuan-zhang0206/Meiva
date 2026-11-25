// meiva:note
// The browser capability port's real implementation and this frontend's composition root for the
// page: the one file that reads `window`. It builds the port from the live page and hands it down
// through <BrowserProvider>; nothing below reads the browser itself.
//
// Each capability is built by a small function that takes the page's own objects, so every branch
// — a browser that refuses storage, a page without a clipboard API, a frame with no page at all —
// is constructed by a test with a plain object, never by stubbing a global.
// meiva:link ui/web/src/docs/frontend-state/browser-port.md

import type {
  Browser,
  BrowserBeacon,
  BrowserClipboard,
  BrowserDialogs,
  BrowserIdle,
  BrowserMedia,
  BrowserNavigation,
  BrowserObjectUrls,
  BrowserSize,
  BrowserSelection,
  BrowserStorage,
  BrowserViewport,
  MediaQuery,
  ViewportCause,
} from "./browser";
import { pageLocation } from "./browser";

export interface LiveNavigator {
  readonly clipboard?: { writeText(text: string): Promise<void> } | undefined;
  sendBeacon(url: string, data?: BodyInit | null): boolean;
}

export interface LiveDocument {
  getSelection?(): Selection | null;
  addEventListener?(type: "selectionchange" | "visibilitychange", listener: () => void): void;
  removeEventListener?(type: "selectionchange" | "visibilitychange", listener: () => void): void;
  createElement(tag: "textarea"): HTMLTextAreaElement;
  readonly body: { appendChild(node: Node): unknown; removeChild(node: Node): unknown };
  execCommand(command: "copy"): boolean;
  readonly visibilityState?: string;
}

export interface LiveViewport {
  readonly height: number;
  readonly width?: number;
  readonly scale: number;
  addEventListener(type: "resize" | "scroll", listener: () => void): void;
  removeEventListener(type: "resize" | "scroll", listener: () => void): void;
}

// meiva:note
// One size observer as the page hands it over: the port only ever attaches a listener to the target
// and ends the watch, so this is the whole surface it needs from `ResizeObserver`.
export interface LiveResizeObserver {
  observe(target: Element): void;
  disconnect(): void;
}

export type LivePageEventType = "popstate" | "resize" | "scroll" | "orientationchange";

export interface LiveOrientation {
  addEventListener(type: "change", listener: () => void): void;
  removeEventListener(type: "change", listener: () => void): void;
}

export interface LiveViewportMeta {
  content: string;
}

export interface LivePage {
  readonly location: { readonly href: string; assign(url: string): void };
  readonly history: {
    readonly state: unknown;
    pushState(state: unknown, unused: string, url: string): void;
    replaceState(state: unknown, unused: string, url: string): void;
    back(): void;
  };
  addEventListener(type: LivePageEventType, listener: () => void): void;
  removeEventListener(type: LivePageEventType, listener: () => void): void;
  localStorage(): Storage | null;
  createObjectUrl(file: Blob): string;
  revokeObjectUrl(url: string): void;
  sessionStorage(): Storage | null;
  matchMedia(query: string): MediaQueryList;
  readonly innerHeight: number;
  readonly innerWidth?: number;
  readonly visualViewport: LiveViewport | null;
  readonly screenOrientation?: LiveOrientation | null | undefined;
  viewportMeta(): LiveViewportMeta | null;
  scrollTo(x: number, y: number): void;
  readonly navigator: LiveNavigator;
  readonly document: LiveDocument;
  confirm(message: string): boolean;
  // meiva:note
  // Absent when the browser has no ResizeObserver; a page that has one builds it with the port's
  // change callback.
  readonly newResizeObserver?: ((onChange: () => void) => LiveResizeObserver) | undefined;
  // meiva:note
  // Present only where the browser has requestIdleCallback (Safari has none); `idleOver` reaches
  // for the timer pair below when it is absent.
  readonly requestIdleCallback?: ((work: () => void, options: { timeout: number }) => number) | undefined;
  readonly cancelIdleCallback?: ((handle: number) => void) | undefined;
  setTimeout(work: () => void, delayMs: number): number;
  clearTimeout(handle: number): void;
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
    highlight(target) {
      target.animate([{ outline: "2px solid currentColor", outlineOffset: "4px" }, { outline: "2px solid transparent", outlineOffset: "4px" }], { duration: 1600, easing: page?.matchMedia("(prefers-reduced-motion: reduce)").matches ? "steps(1, end)" : "ease-out" });
    },
    scrollIntoView(target, block) {
      if (page === null) throw new Error("a server render has no view to scroll");
      target.scrollIntoView({ block });
    },
  };
}

export const ROTATION_COALESCE_MS = 50;

export const ROTATION_RESAMPLE_MS = [60, 180, 350, 600] as const;
export const ROTATION_RECHECK_MS = 180;

export const SCALE_LOCK_MS = 60;

export function withoutScaleLimits(content: string): string[] {
  return content
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry !== "" && !/^(minimum|maximum)-scale\s*=/i.test(entry));
}

function scaleLockOver(page: LivePage | null, onRestored: () => void) {
  let pristine: string | null = null;
  let timer: number | null = null;
  const restore = (): void => {
    if (timer !== null && page !== null) page.clearTimeout(timer);
    timer = null;
    if (pristine === null) return;
    const meta = page?.viewportMeta();
    if (meta) meta.content = pristine;
    pristine = null;
  };
  return {
    lock(): void {
      if (page === null) return;
      const meta = page.viewportMeta();
      if (meta === null) return;
      pristine ??= meta.content;
      if (timer !== null) page.clearTimeout(timer);
      meta.content = [...withoutScaleLimits(pristine), "minimum-scale=1", "maximum-scale=1"].join(", ");
      timer = page.setTimeout(() => {
        timer = null;
        restore();
        page.scrollTo(0, 0);
        onRestored();
      }, SCALE_LOCK_MS);
    },
    release: restore,
  };
}

function followViewport(
  page: LivePage,
  visual: LiveViewport,
  emit: (cause: ViewportCause) => void,
): () => void {
  const samples = new Set<number>();
  let coalescing: number | null = null;
  const moved = () => emit("moved");
  let portrait: Partial<MediaQueryList> | null = null;
  try {
    portrait = page.matchMedia("(orientation: portrait)");
  } catch {
    portrait = null;
  }
  let lastPortrait: boolean | null = portrait?.matches ?? null;
  const noteOrientation = () => {
    lastPortrait = portrait?.matches ?? lastPortrait;
  };
  const onRotation = () => {
    noteOrientation();
    if (coalescing !== null) return;
    coalescing = page.setTimeout(() => {
      coalescing = null;
    }, ROTATION_COALESCE_MS);
    for (const timer of samples) page.clearTimeout(timer);
    samples.clear();
    emit("rotated");
    for (const delayMs of ROTATION_RESAMPLE_MS) {
      const timer = page.setTimeout(() => {
        samples.delete(timer);
        noteOrientation();
        emit(delayMs === ROTATION_RECHECK_MS ? "rotation-recheck" : "moved");
      }, delayMs);
      samples.add(timer);
    }
  };
  const onVisibility = () => {
    if (page.document.visibilityState === "hidden") return;
    const now = portrait?.matches;
    if (now !== undefined && lastPortrait !== null && now !== lastPortrait) {
      onRotation();
      return;
    }
    emit("recheck");
  };
  const orientation = page.screenOrientation ?? null;
  visual.addEventListener("resize", moved);
  visual.addEventListener("scroll", moved);
  page.addEventListener("resize", moved);
  page.addEventListener("scroll", moved);
  page.addEventListener("orientationchange", onRotation);
  page.document.addEventListener?.("visibilitychange", onVisibility);
  orientation?.addEventListener("change", onRotation);
  portrait?.addEventListener?.("change", onRotation);
  return () => {
    for (const timer of samples) page.clearTimeout(timer);
    samples.clear();
    if (coalescing !== null) page.clearTimeout(coalescing);
    coalescing = null;
    visual.removeEventListener("resize", moved);
    visual.removeEventListener("scroll", moved);
    page.removeEventListener("resize", moved);
    page.removeEventListener("scroll", moved);
    page.removeEventListener("orientationchange", onRotation);
    page.document.removeEventListener?.("visibilitychange", onVisibility);
    orientation?.removeEventListener("change", onRotation);
    portrait?.removeEventListener?.("change", onRotation);
  };
}

// meiva:note
// The page's viewport. Every read is a fresh measurement — a height is not a stored fact — and the
// subscription follows the visual viewport (which a keyboard, a toolbar or a pinch moves), the
// page's own resize and scroll (which `WKWebView` keyboard insets move), every rotation signal the
// page has, and the tab returning to the foreground; the page's listeners are attached once for
// all subscribers. `resetScroll` returns the root page scroll to `(0, 0)`; `resetScale` re-asks
// WebKit for scale 1 in the current orientation through a short-lived viewport-meta lock. A frame
// with no page (a server render) has none of this, and says so with a zero reading.
export function viewportOver(page: LivePage | null): BrowserViewport {
  const subscribers = new Set<(cause: ViewportCause) => void>();
  const emit = (cause: ViewportCause) => {
    for (const subscriber of [...subscribers]) subscriber(cause);
  };
  const scale = scaleLockOver(page, () => emit("moved"));
  let unfollow: (() => void) | null = null;
  return {
    read: () =>
      page === null
        ? { layoutHeight: 0, visual: null }
        : {
            layoutHeight: page.innerHeight,
            ...(page.innerWidth === undefined ? {} : { layoutWidth: page.innerWidth }),
            visual:
              page.visualViewport === null
                ? null
                : {
                    height: page.visualViewport.height,
                    ...(page.visualViewport.width === undefined
                      ? {}
                      : { width: page.visualViewport.width }),
                    scale: page.visualViewport.scale,
                  },
          },
    resetScroll() {
      page?.scrollTo(0, 0);
    },
    resetScale: () => scale.lock(),
    subscribe(onChange) {
      const visual = page?.visualViewport ?? null;
      if (page === null || visual === null) return () => undefined;
      const subscriber = (cause: ViewportCause) => onChange(cause);
      subscribers.add(subscriber);
      unfollow ??= followViewport(page, visual, emit);
      return () => {
        subscribers.delete(subscriber);
        if (subscribers.size > 0) return;
        unfollow?.();
        unfollow = null;
        scale.release();
      };
    },
  };
}

// meiva:note
// Size notifications for one element. The page hands over its own observer factory, so the port
// attaches and detaches without knowing which constructor built it; a frame with no page, or a
// browser without ResizeObserver, has nothing to watch and ends the watch immediately.
export function sizeOver(page: LivePage | null): BrowserSize {
  return {
    observe(target, onChange) {
      const observer = page?.newResizeObserver?.(onChange);
      if (observer === undefined) return () => undefined;
      observer.observe(target);
      return () => observer.disconnect();
    },
  };
}

// meiva:note
// The page's modal question. A frame with no page has nobody to ask, so it throws rather than
// answering "yes" on the reader's behalf.
export function dialogsOver(page: LivePage | null): BrowserDialogs {
  return {
    confirm(message) {
      if (page === null) throw new Error("a server render has no one to confirm");
      return page.confirm(message);
    },
  };
}

// meiva:note
// The page's spare time. The idle callback is the real one where the browser has it, with the
// caller's deadline so starved work still runs; everywhere else the caller's timer delay stands in
// (Safari), and cancelling releases whichever of the two was scheduled.
export function idleOver(page: LivePage | null): BrowserIdle {
  return {
    schedule(work, { deadlineMs, fallbackDelayMs }) {
      if (page === null) return () => undefined;
      const idle = page.requestIdleCallback;
      if (idle !== undefined) {
        const handle = idle(work, { timeout: deadlineMs });
        return () => page.cancelIdleCallback?.(handle);
      }
      const timer = page.setTimeout(work, fallbackDelayMs);
      return () => page.clearTimeout(timer);
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

// meiva:note
// A quote reader receives only a range whose endpoints belong to its own content scope.
export function selectionOver(doc: LiveDocument | null): BrowserSelection {
  return {
    read(scope) {
      const selection = doc?.getSelection?.();
      if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return null;
      const range = selection.getRangeAt(0);
      if (!scope.contains(range.startContainer) || !scope.contains(range.endContainer)) return null;
      return range.cloneRange();
    },
    subscribe(_scope, onChange) {
      doc?.addEventListener?.("selectionchange", onChange);
      return () => doc?.removeEventListener?.("selectionchange", onChange);
    },
  };
}

const warnToConsole = (message: string): void => console.warn(message);

export function browserOver(page: LivePage | null, warn: (message: string) => void): Browser {
  return {
    selection: selectionOver(page?.document ?? null),
    navigation: navigationOver(page),
    local: storageOver(() => (page === null ? null : page.localStorage()), "local", warn),
    session: storageOver(() => (page === null ? null : page.sessionStorage()), "session", warn),
    media: mediaOver(page === null ? null : (query) => page.matchMedia(query)),
    clipboard: clipboardOver(page?.navigator ?? null, page?.document ?? null),
    beacon: beaconOver(page?.navigator ?? null),
    dialogs: dialogsOver(page),
    view: viewOver(page),
    viewport: viewportOver(page),
    size: sizeOver(page),
    idle: idleOver(page),

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
        get innerHeight() {
          return window.innerHeight;
        },
        get innerWidth() {
          return window.innerWidth;
        },
        get visualViewport() {
          return window.visualViewport;
        },
        get screenOrientation() {
          return "orientation" in window.screen ? window.screen.orientation : null;
        },
        viewportMeta: () => window.document.querySelector<HTMLMetaElement>('meta[name="viewport"]'),
        scrollTo: (x, y) => window.scrollTo(x, y),

        newResizeObserver:
          typeof ResizeObserver === "undefined"
            ? undefined
            : (onChange) => {
                const observer = new ResizeObserver(() => onChange());
                return {
                  observe: (target) => observer.observe(target),
                  disconnect: () => observer.disconnect(),
                };
              },
        requestIdleCallback:
          typeof window.requestIdleCallback === "function"
            ? (work, options) => window.requestIdleCallback(work, options)
            : undefined,
        cancelIdleCallback:
          typeof window.cancelIdleCallback === "function"
            ? (handle) => window.cancelIdleCallback(handle)
            : undefined,
        setTimeout: (work, delayMs) => window.setTimeout(work, delayMs),
        clearTimeout: (handle) => window.clearTimeout(handle),

        confirm: (message) => window.confirm(message),
        createObjectUrl: (file) => URL.createObjectURL(file),
        revokeObjectUrl: (url) => URL.revokeObjectURL(url),
        navigator: window.navigator,
        document: window.document,
      }, warnToConsole);
