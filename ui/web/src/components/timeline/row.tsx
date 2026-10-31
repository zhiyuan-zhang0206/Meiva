"use client";

// Per-item card-config cache + the memoized row renderer. Split out of
// index.tsx (outlier cleanup, task #1010) so the view file stays under the
// 800-line budget; behavior is identical.

import { memo, useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

import { DEFAULT_TIMELINE_COLORS, type TimelineColors } from "@/lib/timeline/timeline-colors";
import type { BackendTimelineItem } from "@/lib/contracts/types";

import { CopyButton } from "./buttons";
import { ItemTimestamp } from "./timestamp";
import {
  CARD_FOOTER_CLS,
  CARD_FOOTER_LEFT_CLS,
  type CardConfig,
  CardHeader,
  MessageCard,
  messageCardConfig,
} from "./card";
import { copyPayloadFor, ItemView } from "./item";
import { ItemErrorBoundary } from "./item-error-boundary";
import { inboundKind } from "./model/runs";

// meiva:note
// How long a touch keeps the chrome band up: long enough to read the time and
// reach Copy, short enough that it is gone before the next message is read.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
const TOUCH_REVEAL_MS = 6000;

// Per-item card-config cache. messageCardConfig is pure over `item`, and the
// store preserves object identity for items unchanged across a commit (its
// slice()-based reducers replace only the changed item), so a WeakMap keyed on
// the item reference hands back a STABLE config object for every unchanged row.
// That stability — combined with the memoized TimelineRow below — is what lets
// an unchanged row skip re-render on every streaming chunk: the items ARRAY
// identity changes each chunk, but the individual item refs (and thus their
// configs) do not. Recomputed only when an item's reference — or the
// resolved color map — changes: the config now depends on the user's
// timeline-color settings, and `useTimelineColors` keeps its result
// reference-stable while no color setting changes.
const configCache = new WeakMap<
  BackendTimelineItem,
  { colors: TimelineColors; config: CardConfig | null }
>();
export function cardConfigFor(
  item: BackendTimelineItem,
  colors: TimelineColors = DEFAULT_TIMELINE_COLORS,
): CardConfig | null {
  const hit = configCache.get(item);
  if (hit?.colors === colors) return hit.config;
  const config = messageCardConfig(item, colors);
  configCache.set(item, { colors, config });
  return config;
}

// meiva:note
// One timeline row, memoized so an unchanged item skips its entire subtree (card container + header summary scan + body) on every streaming chunk. Every prop is either a primitive or a reference stable across commits for an unchanged item: `item` (store-preserved ref), `config` (WeakMap cache), `expanded`/`streaming`/`copyable` (booleans) and `onToggle` (parent's stable useCallback). config === null is the ephemeral system marker, which renders bare (no card, not collapsible).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
export const TimelineRow = memo(function TimelineRow({
  item,
  config,
  streaming,
  expanded,
  copyable,
  stickyHeader = false,
  isStuck,
  onToggle,
  timelineSource = "canonical",
  displayRank,
}: {
  item: BackendTimelineItem;
  config: CardConfig | null;
  streaming: boolean;
  expanded: boolean;
  // meiva:note
  // The body is expanded and settled. Copy joins the timestamp in the card's
  // hover footer only when the KIND also offers it (config.copy — the whitelist
  // in card.tsx: a human's or an agent's message, and the agent's own reply).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  copyable: boolean;
  /** Where this row's header pins while expanded: `"top"` below the HeaderBar
   *  (top-level message cards) or `"nested"` under its expanded work block's
   *  header (work-block children, task #3215; see runs.classifyItem). false
   *  for rows with no pin. */
  stickyHeader?: "top" | "nested" | false;
  /** The header is currently pinned at the sticky line (task #3136). */
  isStuck: boolean;
  onToggle: (id: string, kind: BackendTimelineItem["kind"]) => void;
  timelineSource?: "buffer" | "canonical";
  displayRank?: number;
}) {
  // Bound toggle for the header. Created inside the memoized row, so it stays
  // stable across the PARENT's re-renders (the row re-renders only when its own
  // props change) — that is what keeps CardHeader's own memo intact.
  const handleToggle = useCallback(
    () => onToggle(item.item_id, item.kind),
    [onToggle, item.item_id, item.kind],
  );

  // meiva:note
  // A touch pointer has no hover, so the band is revealed by touching the block
  // and hides itself again after a few seconds: leaving it painted put a
  // timestamp and Copy under every message on a phone, which is what the user
  // reported. A mouse never takes this path — hover and focus already gate it.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  const [touchRevealed, setTouchRevealed] = useState(false);
  const revealTimer = useRef<number | null>(null);
  const revealForTouch = useCallback((event: ReactPointerEvent) => {
    if (event.pointerType === "mouse") return;
    setTouchRevealed(true);
    if (revealTimer.current !== null) window.clearTimeout(revealTimer.current);
    revealTimer.current = window.setTimeout(() => {
      setTouchRevealed(false);
      revealTimer.current = null;
    }, TOUCH_REVEAL_MS);
  }, []);
  useEffect(() => {
    return () => {
      if (revealTimer.current !== null) window.clearTimeout(revealTimer.current);
    };
  }, []);

  if (config === null) {
    return (
      <div data-item-id={item.item_id} data-timeline-source={timelineSource} data-display-rank={displayRank} aria-live="off" className="timeline-item">
        <ItemErrorBoundary resetKey={item.payload}>
          <ItemView item={item} streaming={streaming} />
        </ItemErrorBoundary>
      </div>
    );
  }

  // meiva:note
  // Built inside the memoized row (not passed in from the parent's per-render `renderRow` closure) so the JSX element stays a fresh object only when THIS row's own props change — passing it down from renderRow would recreate the node on every TimelineView render and defeat TimelineRow's memo for every row, not just this one.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  //
  // meiva:note
  // Top-level primary messages (human inbound_chat bubble + user-facing agent_chat
  // canvas prose) mount the hover chrome band below the message carrying the
  // timestamp and Copy. Secondary work items inside TurnBlocks / the Live Activity
  // Window omit timestamps and render an inline top-right Copy button only when
  // `config.copy` is true (inter-agent messages and non-user-facing agent_chat).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  const isHumanBubble = item.kind === "inbound_chat" && inboundKind(item.source) === "human";
  const isAgentProse = item.kind === "agent_chat" && item.user_facing !== false;
  const isPrimaryMessage = isHumanBubble || isAgentProse;

  const stamp =
    isPrimaryMessage && config.showTimestamp && item.created_at ? (
      <ItemTimestamp iso={item.created_at} />
    ) : null;
  const canCopy = (isPrimaryMessage ? !item.partial : copyable) && config.copy;
  const copy = canCopy ? <CopyButton text={copyPayloadFor(item)} compact /> : null;
  const primaryFooter = isPrimaryMessage && (stamp ?? copy) ? <>{stamp}{copy}</> : null;

  if (isHumanBubble) {
    return (
      <div
        data-item-id={item.item_id}
        data-timeline-source={timelineSource}
        data-display-rank={displayRank}
        data-message-role="user"
        aria-live="off"
        className={cn("timeline-item", primaryFooter ? "group/msgcard" : null)}
        onPointerDown={primaryFooter ? revealForTouch : undefined}
      >
        <ItemErrorBoundary resetKey={item.payload}>
          <div className={cn("justify-end", FLEX, MIN_W_0)}>
            <div
              data-testid="user-message-bubble"
              className={cn(
                "max-w-[85%] sm:max-w-[min(70%,34rem)] rounded-[20px] rounded-br-md px-4 py-2.5 text-[15px] leading-[24px]",
                "bg-secondary text-foreground/92 border border-border/60 shadow-2xs",
                "dark:bg-[#2c2a27] dark:text-[#e5e2dc] dark:border-white/[0.065]",
                MIN_W_0,
              )}
            >
              <ItemView item={item} streaming={streaming} />
            </div>
          </div>
        </ItemErrorBoundary>
        {primaryFooter ? (
          <div
            data-testid="card-chrome"
            className={cn(CARD_FOOTER_CLS, touchRevealed && "pointer-events-auto opacity-100")}
          >
            {primaryFooter}
          </div>
        ) : null}
      </div>
    );
  }

  if (isAgentProse) {
    return (
      <div
        data-item-id={item.item_id}
        data-timeline-source={timelineSource}
        data-display-rank={displayRank}
        data-message-role="assistant"
        aria-live="off"
        className={cn("timeline-item", primaryFooter ? "group/msgcard" : null)}
        onPointerDown={primaryFooter ? revealForTouch : undefined}
      >
        <ItemErrorBoundary resetKey={item.payload}>
          <div
            data-testid="agent-message-prose"
            className={cn("text-[15px] leading-[25px] text-foreground/92 dark:text-[#e2dfd9]", MIN_W_0)}
          >
            <ItemView item={item} streaming={streaming} />
          </div>
        </ItemErrorBoundary>
        {primaryFooter ? (
          <div
            data-testid="card-chrome"
            className={cn(CARD_FOOTER_LEFT_CLS, touchRevealed && "pointer-events-auto opacity-100")}
          >
            {primaryFooter}
          </div>
        ) : null}
      </div>
    );
  }

  // Sticky only while expanded — a collapsed row has nothing to scroll under
  // its header. The level decides the scan bucket + the pin line: a work
  // block's child row joins the nested pass (data-turn-child) instead of the
  // level-1 scan (findClosestStuckHeaderId, run-block.tsx).
  const sticky = Boolean(stickyHeader) && expanded;
  const nestedSticky = stickyHeader === "nested";

  return (
    // data-card-sticky marks a row whose header may pin — the scan target for
    // findClosestStuckHeaderId (see run-block.tsx); expanded is required for
    // the pin, so a collapsed row drops out of the scan by itself.
    <div
      data-item-id={item.item_id}
      data-timeline-source={timelineSource}
      data-display-rank={displayRank}
      data-card-sticky={sticky}
      data-turn-child={nestedSticky ? "true" : undefined}
      aria-live="off"
      className={cn("timeline-item", copy ? "group/msgcard" : null)}
      onPointerDown={copy ? revealForTouch : undefined}
    >
      {/* Boundary wraps header + content together: the header's summary
          derivation parses the payload too, so a malformed payload must
          not escape the per-item fallback. */}
      <ItemErrorBoundary resetKey={item.payload}>
        <MessageCard config={config}>
          <CardHeader
            item={item}
            config={config}
            expanded={expanded}
            onToggle={handleToggle}
            stickyHeader={stickyHeader}
            isStuck={isStuck}
          />
          {copy ? (
            <div
              data-testid="card-chrome"
              className={cn(
                "absolute top-1 right-1.5 z-10 items-center",
                "pointer-events-none opacity-0 transition-opacity duration-150 motion-reduce:transition-none",
                "group-hover/msgcard:pointer-events-auto group-hover/msgcard:opacity-100",
                "group-focus-within/msgcard:pointer-events-auto group-focus-within/msgcard:opacity-100",
                touchRevealed && "pointer-events-auto opacity-100",
                FLEX,
              )}
            >
              {copy}
            </div>
          ) : null}
          {expanded ? (
            <div className="pl-5 pr-1 pb-1.5 pt-0.5">
              <ItemView item={item} streaming={streaming} />
            </div>
          ) : null}
        </MessageCard>
      </ItemErrorBoundary>
    </div>
  );
});
