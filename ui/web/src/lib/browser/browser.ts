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

export interface Browser {
  readonly navigation: BrowserNavigation;
  readonly local: BrowserStorage;
  readonly session: BrowserStorage;
  readonly media: BrowserMedia;
  readonly clipboard: BrowserClipboard;
  readonly beacon: BrowserBeacon;
  readonly view: BrowserView;
  readonly viewport: BrowserViewport;

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
