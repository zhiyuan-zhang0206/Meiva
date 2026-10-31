"use client";

// meiva:note
// Deep-link anchor scrolling for the two vertical anchor-nav pages (Control
// and Insights). A mount-time hash scroll is not enough on its own: section
// bodies fetch asynchronously (live status data and section lists), so content
// ABOVE the target grows after the first
// scrollIntoView and pushes the anchored section below where it landed — on
// /insights#alerts the section ended up at the bottom of the viewport (or
// below the fold entirely, when an async section above it settled last). This hook
// re-applies the scroll while the scroll container's content height is still
// settling, then detaches — the user's own scrolling is never fought.

import { useEffect } from "react";

import { useBrowser } from "@/lib/browser/browser-provider";

// meiva:note
// Re-scroll only while the content height keeps changing; stop once it has
// been stable this long (the async sections above the target have landed).
const SETTLE_STABLE_MS = 500;
// meiva:note
// Hard cap — never keep re-scrolling for longer than this after mount.
const SETTLE_CAP_MS = 5000;
// meiva:note
// A genuine user scroll during the settle window moves the container by far
// more than this; a content-height change without user input leaves it (or
// clamps it by a couple of px). Past this slack we stop fighting.
const USER_SCROLL_SLACK_PX = 32;

// meiva:note
// Scroll `targetId` to the top of the `scrollId` container on mount, and
// re-apply the scroll whenever the container's content height changes until
// it settles (or the cap elapses). A no-op when `targetId` is null or the
// target element is missing — the caller resolves the URL hash.
export function useSettledAnchorScroll(scrollId: string, targetId: string | null): void {
  const browser = useBrowser();
  const view = browser.view;
  useEffect(() => {
    if (!targetId) return;
    const container = document.getElementById(scrollId);
    const scroll = () => {
      const target = document.getElementById(targetId);
      if (target) view.scrollIntoView(target, "start");
    };
    scroll();
    // meiva:note
    // Nothing to watch — no container, or one whose content has not rendered — and no size
    // notifications at all (a frame with no page): the initial scroll is the whole behavior, which
    // is also the correct result when the content is already settled.
    const content = container?.firstElementChild ?? null;
    if (container === null || content === null) return;

    let lastHeight = container.scrollHeight;
    let lastScrollTop = container.scrollTop;
    let stopped = false;
    let stableTimer: number | undefined;
    let unwatch: () => void = () => undefined;

    const stop = (): void => {
      if (stopped) return;
      stopped = true;
      unwatch();
      window.clearTimeout(stableTimer);
      window.clearTimeout(cap);
    };

    // meiva:note
    // Watch the content wrapper: the container's own box does not change as
    // its content grows — only the wrapper's height does.
    unwatch = browser.size.observe(content, () => {
      if (stopped) return;
      const height = container.scrollHeight;
      if (height === lastHeight) return;
      lastHeight = height;
      // meiva:note
      // The user took over scrolling during the settle window — stop.
      if (Math.abs(container.scrollTop - lastScrollTop) > USER_SCROLL_SLACK_PX) {
        stop();
        return;
      }
      scroll();
      lastScrollTop = container.scrollTop;
      window.clearTimeout(stableTimer);
      stableTimer = window.setTimeout(stop, SETTLE_STABLE_MS);
    });
    const cap = window.setTimeout(stop, SETTLE_CAP_MS);
    return stop;
  }, [browser, scrollId, targetId, view]);
}
