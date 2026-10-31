---
type: doc
title: Browser capability port
description: How the frontend reaches the page — the browser port's capabilities, the three answers a read gives, the composition root that builds the real one, and the fake a test hands in.
tags: [frontend, state, architecture]
---

# Browser capability port

A page's address, its two storage areas, its media queries, its clipboard, its
unload-safe beacon, the view it scrolls, the viewport it is drawn into, the sizes
it reports, the idle slices it offers and the object URLs it holds a blob under
are the world this frontend runs in. They are read once, at the composition root,
and handed down as one value.

`lib/browser/browser.ts` declares the port. `lib/browser/system-browser.ts` is
the only file that names `window`: it builds the port from the live page, and
`meizel` records it as a composition root ("the composition root where the
frontend's world enters"). `lib/browser/browser-provider.tsx` carries the value
down through `<BrowserProvider>`, and `useBrowser()` reads it.

```tsx
<BrowserProvider browser={browser}>   {/* browser: lib/browser/system-browser.ts */}
  <App />
</BrowserProvider>
```

## No provider is a composition error

`useBrowser()` throws `useBrowser must be used inside <BrowserProvider>` when no
provider is mounted — there is no fallback to the real window, because a fallback
is exactly the ambient read this port replaces. A test that renders a component
bare therefore throws on purpose: that throw is the design, not a regression, and
the fix is to mount the provider with `fakeBrowser()`, not to add a default. The
same contract as `useApi()` in `lib/transport/transport-context.tsx`.

## A read answers which of the three things happened

`BrowserStorage.read` returns `found`, `absent` or `unavailable` instead of
`string | null`. They are different facts and callers treat them differently:

| answer | means | what a caller does |
|---|---|---|
| `found` | the value is stored | use it |
| `absent` | this browser never stored one | the product default, and the caller says why in a comment |
| `unavailable` | this frame has no storage: a server render, or a browser that refuses site data | keep working without remembering; the first refusal logs one warning per area |

Collapsing them — `?? "default"` — is what this port exists to end: a caller that
cannot tell "never set" from "cannot store" cannot say what it did either.

A media query is asked again whenever it may have changed (`matches()` plus
`subscribe`), so `useMediaQuery` can keep reading it through
`useSyncExternalStore`. A frame with no page (a server render) answers the hook's
declared server snapshot instead: asking the port for a media answer there throws.

## Actions, not only reads

`scrollIntoView`, `assign`, `push`, `replace` and `back` are on the port too: a
component asks the browser to move or scroll, and the port is the one place that
happens, so a test asserts what was scrolled where without a DOM stub. The slash
menu, the inbox queue's route anchor, both anchor-nav pages and the config page's
focus jump scroll through `browser.view`; the shared-context panel moves through
`browser.navigation`.

## The viewport is a measurement, not a stored fact

`browser.viewport.read()` answers a fresh `ViewportReading`: the layout height
the shell's `100%` resolves against, and the visual band the reader sees (or
`null` where the browser has none). `subscribe` follows only the visual viewport,
because that is the one a keyboard, a toolbar or a pinch moves. A phone keyboard
shrinks only that band, so `lib/browser/viewport.ts` decides whether the shell is
pinned to it — `pinnedShellHeight` — and `VisualViewportHeightSync`
applies the answer to the document. That decision is a pure function of the
reading, so a test asserts it directly and never installs a stub on `innerHeight`
or `visualViewport`.

## A settling page reports the size that moved

`browser.size.observe(target, onChange)` asks the page to report when an element's
box changes, and answers the function that ends the watch; a frame with no page —
or a browser without `ResizeObserver` — watches nothing and says so by doing
nothing. `useSettledAnchorScroll` is the caller: the section bodies above a
deep-linked target arrive after the first scroll, the content wrapper grows, and
the hook re-applies the scroll until the height settles. A test fires
`fakeBrowser().sizeWatches[0].fire()` instead of installing a fake
`ResizeObserver` class on the global.

## Idle time is a value, with the caller's timing

`browser.idle.schedule(work, { deadlineMs, fallbackDelayMs })` runs `work` in an
idle slice — no later than the deadline, and through a timer of `fallbackDelayMs`
on a browser without `requestIdleCallback` (Safari) — and answers the function
that drops work which has not run yet. Both numbers belong to the caller because
they differ: the deferred-mount queue releases one subtree per slice and must not
wait (`fallbackDelayMs: 0`), while the syntax-highlighter prefetch may
(`fallbackDelayMs: 2000`). A test chooses the fake's mode: `idle: "manual"`
records the slices it fires by hand, `idle: "immediate"` runs each at once for a
page that must settle in one commit, and the default schedules on the page the
test runs in.

## The app router is not this port

`browser.navigation` is the page's history object. The app's own routing lives beside this package in
`lib/browser/navigation/` (`AppNavigation`: `pathname`, `push`, `replace`), and it is nested here as
a capacity compromise — `lib/` is at its twenty-entry cap — not because routing belongs to the
browser. The two values are unrelated and neither is built from the other; a `lib/` regrouped around
a routing layer of its own should take that package with it.

## A preview lives in an object URL, and the port releases it

`browser.objectUrls.create(file)` answers the URL a pending image preview renders
from, and `revoke(url)` releases the blob. The composer keeps that URL as the
attachment's identity while it is pending, so releasing it is also the moment the
attachment leaves. A test reads `fakeBrowser().actions.createdUrls` and
`revokedUrls`: a preview that is never released shows up as a created URL with no
matching revoke, and no test replaces the URL API to see that.

## What a test does

`lib/browser/testing.ts` is the fake: storage in two maps, an address the test
moves, media answers it sets, size watchers in `sizeWatches`, idle slices in
`idleRequests`, and an `actions` record of everything the component asked for. A component test renders the provider around the component and reads
the same value the app reads:

```tsx
const browser = fakeBrowser({ href: "http://fake.local/shell/7#tasks" });
render(<BrowserProvider browser={browser}><Component /></BrowserProvider>);
expect(browser.actions.pushed).toEqual([{ url: "/fleet", state: {} }]);
```

`src/test-support/browser.ts` re-exports the fake for component tests; production
code never imports either path.
