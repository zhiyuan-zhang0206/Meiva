"use client";

import { memo, useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

import type { BackendTimelineItem } from "@/lib/contracts/types";

import { CopyButton } from "./buttons";
import { ItemTimestamp } from "./timestamp";
import {
  CARD_FOOTER_CLS,
  type CardConfig,
  CardHeader,
  MessageCard,
  messageCardConfig,
} from "./card";
import { copyPayloadFor, ItemView } from "./item";
import { ItemErrorBoundary } from "./item-error-boundary";
import { inboundKind, isSilentTimelineItem } from "./model/runs";

// meiva:note
// How long a touch keeps the chrome band up: long enough to read the time and
// reach Copy, short enough that it is gone before the next message is read.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
const TOUCH_REVEAL_MS = 6000;

// meiva:note
// Per-item card-config WeakMap cache keyed on item reference so unchanged rows
// receive a reference-stable CardConfig across streaming commits and preserve
// TimelineRow's memo.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/streaming-rows.meiva.okf.md
const configCache = new WeakMap<BackendTimelineItem, CardConfig | null>();
export function cardConfigFor(item: BackendTimelineItem): CardConfig | null {
  if (configCache.has(item)) return configCache.get(item) ?? null;
  const config = messageCardConfig(item);
  configCache.set(item, config);
  return config;
}

// meiva:note
// Memoized timeline row renderer: primary human messages render as right-aligned
// bubbles, user-facing agent_chat renders as unboxed canvas Markdown prose (both
// sharing right-aligned CARD_FOOTER_CLS below the block), secondary items render
// as collapsible MessageCard rows inside TurnBlocks, and config === null renders
// bare ephemeral markers.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
export const TimelineRow = memo(function TimelineRow({
  item,
  config,
  streaming,
  expanded,
  copyable,
  promotedPrimary = false,
  onToggle,
  timelineSource = "canonical",
  displayRank,
}: {
  item: BackendTimelineItem;
  config: CardConfig | null;
  streaming: boolean;
  expanded: boolean;
  // meiva:note
  // True when a secondary item's body is expanded and settled; combined with
  // config.copy to gate the inline Copy button on secondary message cards.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  copyable: boolean;
  promotedPrimary?: boolean;
  onToggle: (id: string, kind: BackendTimelineItem["kind"]) => void;
  timelineSource?: "buffer" | "canonical";
  displayRank?: number;
}) {
  const handleToggle = useCallback(
    () => onToggle(item.item_id, item.kind),
    [onToggle, item.item_id, item.kind],
  );

  // meiva:note
  // Coarse touch pointers lack hover, so touching a primary message or copyable
  // secondary card reveals its chrome band temporarily and hides it again after TOUCH_REVEAL_MS.
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
  // Top-level primary messages (human inbound_chat bubble + user-facing agent_chat
  // canvas prose) mount the right-aligned hover chrome band below the message
  // carrying the timestamp and Copy. Secondary work items inside TurnBlocks omit
  // below-card timestamps and float a hover-revealed Copy pill inside the
  // expanded body's bottom-right corner when `config.copy` is true (inter-agent
  // messages and non-user-facing agent_chat) so inter-card spacing stays uniform.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  const isUploadRow =
    item.kind === "inbound_chat" &&
    (item.source === "system:upload" || Boolean(item.files?.length));
  const isHumanBubble =
    item.kind === "inbound_chat" && (inboundKind(item.source) === "human" || isUploadRow);
  const isAgentProse =
    item.kind === "agent_chat" && (item.user_facing !== false || promotedPrimary);
  const isPrimaryMessage = isHumanBubble || isAgentProse;

  if (isSilentTimelineItem(item)) {
    return null;
  }

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
            className={cn(CARD_FOOTER_CLS, touchRevealed && "pointer-events-auto opacity-100")}
          >
            {primaryFooter}
          </div>
        ) : null}
      </div>
    );
  }

  const showExpandedBody =
    expanded && !(item.kind === "agent_reasoning" && item.payload === "" && !item.interrupted);
  const secondaryCopy = showExpandedBody ? copy : null;

  return (
    <div
      data-item-id={item.item_id}
      data-timeline-source={timelineSource}
      data-display-rank={displayRank}
      aria-live="off"
      className={cn("timeline-item", secondaryCopy ? "group/msgcard" : null)}
      onPointerDown={secondaryCopy ? revealForTouch : undefined}
    >
      <ItemErrorBoundary resetKey={item.payload}>
        <MessageCard config={config}>
          <CardHeader
            item={item}
            config={config}
            expanded={expanded}
            onToggle={handleToggle}
          />
          {showExpandedBody ? (
            <div className="relative pl-5 pr-1 pt-0.5 pb-1.5">
              <ItemView item={item} streaming={streaming} />
              {secondaryCopy ? (
                <div
                  data-testid="card-chrome"
                  className={cn(
                    "absolute bottom-1 right-1 z-10 items-center justify-end rounded border border-border/60 bg-background/90 px-1.5 py-1 shadow-2xs backdrop-blur-xs",
                    "pointer-events-none opacity-0 transition-opacity duration-150 motion-reduce:transition-none",
                    "group-hover/msgcard:pointer-events-auto group-hover/msgcard:opacity-100",
                    "group-focus-within/msgcard:pointer-events-auto group-focus-within/msgcard:opacity-100",
                    touchRevealed && "pointer-events-auto opacity-100",
                    FLEX,
                  )}
                >
                  {secondaryCopy}
                </div>
              ) : null}
            </div>
          ) : null}
        </MessageCard>
      </ItemErrorBoundary>
    </div>
  );
});
