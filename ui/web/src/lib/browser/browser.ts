// meiva:note
// The browser a component runs in, as one value.
//
// A page's address, the two storage areas it keeps, the media it reports, the clipboard, the
// unload-safe beacon and the view it scrolls are the world this frontend reads. They enter the app
// at exactly one place — `system-browser.ts`, the composition root — and travel down through
// `<BrowserProvider>`; a component never reaches for `window` itself, so a test hands in
// `fakeBrowser()` and steers every answer by value.
//
// A read says which of the three things happened instead of collapsing them: `found`, `absent`
// (the user never set it) and `unavailable` (this frame has no storage: a server render, or a
// browser that refuses it). Each caller decides what each state means; none of them substitutes a
// default for an unknown answer.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

export interface PageLocation {
  readonly href: string;
  readonly pathname: string;
  readonly search: string;
  readonly hash: string;
}

export type StorageRead =
  | { readonly state: "found"; readonly value: string }
  | { readonly state: "absent" }
  | { readonly state: "unavailable" };

export type StorageWrite = "stored" | "unavailable";

export type StorageRemoval = "removed" | "unavailable";

export interface BrowserStorage {
  read(key: string): StorageRead;
  write(key: string, value: string): StorageWrite;
  remove(key: string): StorageRemoval;
}

export interface MediaQuery {
  matches(): boolean;
  subscribe(onChange: () => void): () => void;
}

export interface BrowserMedia {
  query(query: string): MediaQuery;
}

export interface BrowserNavigation {
  location(): PageLocation | null;
  state(): unknown;
  assign(url: string): void;
  push(url: string, state: unknown): void;
  replace(url: string, state: unknown): void;
  back(): void;
  onTraverse(listener: () => void): () => void;
}

export interface BrowserClipboard {
  write(text: string): Promise<boolean>;
}

export interface BrowserBeacon {
  send(url: string, body: string): boolean;
}

// meiva:note
// The page's own question: `confirm` puts a message in front of the reader and answers what they
// chose. It is a capability rather than a prop because it is the page's chrome — every surface that
// asks uses the same one — and because a caller must not fall back to "proceed" when the page cannot
// ask: a frame with no page (a server render) has nobody to ask and says so.
export interface BrowserDialogs {
  confirm(message: string): boolean;
}

export interface BrowserView {
  scrollIntoView(target: Element, block: ScrollLogicalPosition): void;
}

// meiva:note
// The viewport the page is drawn into. `layoutHeight` is what the shell's `100%` resolves against;
// `visual` is the band the reader actually sees — a phone keyboard shrinks only that one, and a
// pinch zoom reports a scale other than 1. A frame with no page has no visual viewport at all.
export interface ViewportReading {
  readonly layoutHeight: number;
  readonly visual: { readonly height: number; readonly scale: number } | null;
}

export interface BrowserViewport {
  read(): ViewportReading;
  subscribe(onChange: () => void): () => void;
}

// meiva:note
// Object URLs: the page's own way of holding a blob — an image the reader just pasted, before it
// has a server address. `create` answers the URL a preview renders from, and `revoke` releases it;
// a component that drops a preview must call `revoke`, because the page keeps the blob alive until
// it does.
export interface BrowserObjectUrls {
  create(file: Blob): string;
  revoke(url: string): void;
}

// meiva:note
// Size notifications for one element. A section body above a deep link settles after the first
// scroll, so the container's content grows and pushes the target down; whoever re-applies its work
// asks to be told, and the returned function ends the watch. A frame with no page — or a browser
// without ResizeObserver — watches nothing, and says so by doing nothing rather than by throwing.
export interface BrowserSize {
  observe(target: Element, onChange: () => void): () => void;
}

// meiva:note
// When a caller's timing for `BrowserIdle.schedule` differs: `deadlineMs` is the idle deadline (the
// work runs then even if the page never goes idle), `fallbackDelayMs` is the timer used by a
// browser that has no requestIdleCallback at all (Safari). Both are the caller's, because the
// deferred-mount queue releases one subtree per slice and must not wait, while the highlighter
// prefetch may.
export interface IdleTiming {
  readonly deadlineMs: number;
  readonly fallbackDelayMs: number;
}

// meiva:note
// The page's spare time, as a value. `schedule` asks for a slice after paint and returns the
// function that drops work which has not run yet; a frame with no page has no main thread to be
// idle and never runs the work.
export interface BrowserIdle {
  schedule(work: () => void, timing: IdleTiming): () => void;
}

export interface Browser {
  readonly navigation: BrowserNavigation;
  readonly local: BrowserStorage;
  readonly session: BrowserStorage;
  readonly media: BrowserMedia;
  readonly clipboard: BrowserClipboard;
  readonly beacon: BrowserBeacon;
  readonly dialogs: BrowserDialogs;
  readonly view: BrowserView;
  readonly viewport: BrowserViewport;
  readonly size: BrowserSize;
  readonly idle: BrowserIdle;

  readonly objectUrls: BrowserObjectUrls;
}

// meiva:note
// The address parts a caller reads, from the absolute URL a browser reports: the port never hands
// out a partial address, because a relative one would resolve against whatever base the caller
// happened to have.
export function pageLocation(href: string): PageLocation {
  const url = new URL(href);
  return { href: url.href, pathname: url.pathname, search: url.search, hash: url.hash };
}
