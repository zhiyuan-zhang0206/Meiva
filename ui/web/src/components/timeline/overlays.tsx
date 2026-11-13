"use client";

// Overlays follow their owning interaction without intercepting pointer input.

import { ArrowDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/format/utils";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout/layout";

// meiva:note
// The ring's natural box: the pull strip scales it, so this is the height at
// which the whole circle is on screen.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
const RING_SIZE = 20;

// meiva:note
// Two presentations, one ring: it turns while a load is in flight, and a
// reader who asked for reduced motion gets the same ring breathing (opacity
// only) instead of a frozen one.
// meiva:note
// The scale rides the `scale` property, never `transform`. An inline transform
// makes the spin keyframes interpolate as matrices, and `rotate(360deg)`
// decomposes to 0deg — the ring then paints the same frame forever while
// getAnimations() reports it running (user report 2026-10-29: "it doesn't spin
// any more, it's a static animation").
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
function LoadingRing({ arc = 0.72, scale = 1, testId, className }: {
  arc?: number; scale?: number; testId?: string; className?: string;
}) {
  const circumference = 2 * Math.PI * 8;
  return (
    <svg data-testid={testId} viewBox="0 0 24 24" style={{ scale: `${scale}` }} aria-hidden="true"
      className={cn("size-5 shrink-0 animate-spin motion-reduce:animate-pulse pointer-events-none", className)}>
      <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="1.75" opacity="0.15" />
      <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round"
        strokeDasharray={`${circumference * arc} ${circumference * (1 - arc)}`} />
    </svg>
  );
}

// meiva:note
// The circle sits inside the revealed native scroll space and scales with it.
// No visible instructions compete with the conversation; the label remains
// accessible.
// meiva:note
// `loadingOlder` is the reader's own older-page request, not the store's
// in-flight flag: the cold first-viewport fill and compact-history retention
// page backwards on their own, and presenting them lit this strip for history
// nobody pulled for (user report 2026-10-29). The strip keeps one ring element
// for the whole request, so its spin never restarts mid-load.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/loading-ring.md
export function LoadOlderSpinner({ loadingOlder, pullProgress = 0, pullOffset = 0, returning = false }: {
  loadingOlder: boolean; pullProgress?: number; pullOffset?: number; returning?: boolean;
}) {
  const t = useTranslations("timeline");
  const visible = loadingOlder || pullOffset > 0;
  const state = loadingOlder ? "loading" : pullProgress >= 1 ? "ready" : "pulling";
  const height = pullOffset || (loadingOlder ? 40 : 0);
  // meiva:note
  // The strip is the revealed pull space and it is `overflow-hidden`, so a
  // fixed-size ring is sliced into a bar for the first few pixels of a pull
  // (user report 2026-10-20: "a spinner with no bottom"). Scaling it with the
  // strip keeps the whole ring on screen at every pull distance.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
  const ringScale = height > 0 ? Math.min(1, height / RING_SIZE) : 1;
  const arc = loadingOlder ? 0.72 : 0.18 + 0.54 * pullProgress;
  return (
    <div
      role="status"
      aria-hidden={!visible}
      data-testid="load-older-spinner"
      data-state={visible ? state : "idle"}
      data-pull-offset={pullOffset}
      style={{ height }}
      className={cn(
        "absolute top-11 inset-x-0 z-20 pointer-events-none text-muted-foreground justify-center items-center",
        returning && "transition-[height,opacity] duration-200 ease-out motion-reduce:transition-none",
        visible ? "opacity-100" : "opacity-0",
        OVERFLOW_HIDDEN, FLEX,
      )}
    >
      {visible ? (
        <LoadingRing arc={arc} scale={ringScale} />
      ) : null}
      <span className="sr-only">{t("loadingEarlier")}</span>
    </div>
  );
}

// Gesture paging remains available to keyboard and assistive-technology users.
// Reveal this control only when focused, without adding idle reading chrome.
export function LoadOlderKeyboardButton({ loading, onClick }: { loading: boolean; onClick: () => boolean }) {
  const t = useTranslations("timeline");
  return (
    <button
      type="button"
      data-testid="load-older-keyboard"
      disabled={loading}
      onClick={onClick}
      className="sr-only focus:not-sr-only focus:absolute focus:top-14 focus:left-1/2 focus:-translate-x-1/2 focus:z-30 focus:rounded-md focus:border focus:bg-background focus:px-3 focus:py-2 focus:text-xs"
    >
      {t("loadEarlier")}
    </button>
  );
}

// meiva:note
// Cold load draws the same ring as every other loading state in the timeline, at the
// top of the pane, instead of its own spinner glyph plus a "Loading conversation…"
// label: one loading vocabulary, and nothing to re-translate (user 2026-10-21). It
// only appears when this thread has no items yet AND a fetch is in flight — a warm
// switch or a background refetch keeps its items on screen and never shows this.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function ColdLoadSpinner({ show }: { show: boolean }) {
  const t = useTranslations("timeline");
  if (!show) return null;
  return (
    <div
      role="status"
      data-testid="cold-load-spinner"
      className={cn("pointer-events-none absolute inset-x-0 top-11 z-10 justify-center py-12 text-muted-foreground", FLEX)}
    >
      <LoadingRing testId="cold-load-ring" arc={0.72} />
      <span className="sr-only">{t("loadingConversation")}</span>
    </div>
  );
}

// meiva:note
// A solid surface avoids repainting the content beneath the arrow in WebKit.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/sticky-scroll.md
export function ScrollToBottomButton({ atBottom, onClick }: { atBottom: boolean; onClick: () => void }) {
  const t = useTranslations("timeline");
  if (atBottom) return null;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={t("scrollDown")}
      data-testid="scroll-to-bottom"
      className={cn(
        "absolute bottom-4 left-1/2 -translate-x-1/2 z-30",
        "size-9 rounded-full",
        "bg-background border border-border",
        "shadow-md hover:shadow-lg",
        "items-center justify-center",
        "text-muted-foreground hover:text-foreground",
        "transition-colors duration-300 ease-out",
        FLEX,
      )}
    >
      <ArrowDown aria-hidden="true" data-testid="scroll-to-bottom-arrow" className="size-4" />
    </button>
  );
}
