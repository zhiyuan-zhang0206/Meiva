"use client";

// Overlays follow their owning interaction without intercepting pointer input.

import { ArrowDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";

// meiva:note
// Two presentations, one ring: it turns while a load is in flight, and a
// reader who asked for reduced motion gets the same ring breathing (opacity
// only) instead of a frozen one.
function LoadingRing({ testId, className }: {
  testId?: string; className?: string;
}) {
  const circumference = 2 * Math.PI * 8;
  const arc = 0.72;
  return (
    <svg data-testid={testId} viewBox="0 0 24 24" aria-hidden="true"
      className={cn("size-5 shrink-0 animate-spin motion-reduce:animate-pulse pointer-events-none", className)}>
      <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="1.75" opacity="0.15" />
      <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round"
        strokeDasharray={`${circumference * arc} ${circumference * (1 - arc)}`} />
    </svg>
  );
}

// meiva:note
// The reader's request owns this overlay. Cold fill and compact-history
// retention run in the background without presenting another ring.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/loading-ring.md
export function LoadOlderSpinner({ loadingOlder }: { loadingOlder: boolean }) {
  const t = useTranslations("timeline");
  return (
    <div
      role="status"
      aria-hidden={!loadingOlder}
      data-testid="load-older-spinner"
      data-state={loadingOlder ? "loading" : "idle"}
      className={cn(
        "absolute right-5 top-14 z-20 h-8 w-8 rounded-full bg-background pointer-events-none",
        "items-center justify-center text-muted-foreground",
        loadingOlder ? "opacity-100" : "opacity-0",
        FLEX,
      )}
    >
      {loadingOlder ? <LoadingRing /> : null}
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
      <LoadingRing testId="cold-load-ring" />
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
