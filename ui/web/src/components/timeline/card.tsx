"use client";

// meiva:note
// Unified message-card system for the timeline. Every timeline item renders as a MessageCard: a colored left-border box with a clickable CardHeader (chevron + icon + title/summary) and a collapsible body below it, plus a hover footer under the box that carries the item timestamp and Copy.
//
// messageCardConfig(item, colors) is the single source of the per-kind visual mapping — icon, title, border/background color (color families resolved from @/lib/timeline-colors; user-configurable via Display settings), which global toggle governs its default expanded state, and whether the card stamps a timestamp in its hover footer. It returns null for the ephemeral system markers (compact_done / cancelled / error / unrecognized), which render bare (no card, not collapsible) through their own renderers.
//
// Four kinds carry a rich, dynamic header summary instead of a static title — Thinking ("Thought for 8s"), Code (the SDK call histogram), Output ("ran in 1s"), and the System prompt line count. Those summaries own their own icon and live/error coloring, so the header renders the Summary component for them and a plain icon + title for every other kind.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md

import {
  Bot,
  ChevronDown,
  ChevronRight,
  FileText,
  Info,
  Paperclip,
  Settings,
  Sparkles,
  Terminal,
  User,
  type LucideIcon,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { memo, type ReactNode } from "react";

import { usePythonCodeHighlighter } from "@/components/content/python-code";
import { DEFAULT_TIMELINE_COLORS, type TimelineColors } from "@/lib/timeline/timeline-colors";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { useThrottledStreaming } from "@/lib/timeline/use-throttled-streaming";
import { cn } from "@/lib/format/utils";

import { markerVisual } from "./markers";
import { classifyMarker } from "./model/marker-classify";
import { inboundKind } from "./model/runs";
import { externalAgentName } from "@/lib/format/inbound-source";
import { isLiveCode, isLiveExecution, isLiveReasoning, useNow } from "./model/reasoning-clock";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { agentChatTitle, HEADER_CLS, STICKY_HEADER_CLS, Summary, toolCallIcon } from "./view/card-summary";


// Which of the four rich header summaries a card renders in place of a static
// title. null = a plain icon + title header.
export type SummaryKind = "reasoning" | "code" | "output" | "system_prompt";

export interface CardConfig {
  // Rich dynamic summary to render instead of icon + title (owns its own icon).
  readonly rich: SummaryKind | null;
  // Static header icon / title — used only when rich is null.
  readonly icon: LucideIcon | null;
  readonly title: string | null;
  // i18n key under the "timeline" namespace + interpolation values — the
  // header renders the translated title when titleKey is present, falling
  // back to `title` (English) otherwise. Keys are hand-kept in sync with
  // messages/en (canonical English source).
  readonly titleKey?: string;
  readonly titleValues?: Record<string, string | number>;
  // Extra classes on the icon + title span (marker color + uppercase tracking).
  readonly titleClass?: string;
  readonly border: string;
  readonly bg: string;
  // Card-wide text color (lifecycle / memory markers tint their whole card).
  readonly cardText?: string;
  // The kind's own default for a revealed turn child (None mode; All/Last are
  // decided by the mode table). False for nearly every kind — a turn opened by
  // hand shows its children COLLAPSED (user ruling 2026-09-26, task #4780, so
  // each child opens on its own click). True only where the content must be
  // visible the moment the block is revealed: attach thumbnails ARE the
  // attached media (open by default — user ruling 2026-08-26; kept open when
  // the 2026-08-30 ruling folded it into the details block).
  readonly fixedDefault: boolean;
  // meiva:note
  // Whether the card stamps the item's timestamp in its hover footer (right-aligned under the card, next to Copy).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  readonly showTimestamp: boolean;
  // meiva:note
  // Whether this KIND offers Copy at all — a whitelist decided here, in the one
  // per-kind table, so a kind added later must choose instead of inheriting a
  // button. Copy marks the messages a reader can quote: the human's message and
  // the agent's own reply. Thinking, tool calls, tool output, the system prompt,
  // attachments and system notes are the agent's machinery, not its
  // conversation, and offer none (user 2026-10-29).
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
  readonly copy: boolean;
}

// Pure per-item visual config. null → this item is an ephemeral system marker
// that renders bare (no card). Kept pure (no toggle state) so the timeline can
// memoize it over the items array alone.
export function messageCardConfig(
  item: BackendTimelineItem,
  colors: TimelineColors = DEFAULT_TIMELINE_COLORS,
): CardConfig | null {
  switch (item.kind) {
    case "system_prompt":
      return {
        rich: "system_prompt",
        icon: Settings,
        title: null,
        border: colors.system_prompt.border,
        bg: colors.system_prompt.bg ?? "",
        fixedDefault: false,
        showTimestamp: false,
        copy: false,
      };
    case "agent_reasoning":
      return {
        rich: "reasoning",
        icon: Sparkles,
        title: null,
        border: colors.reasoning.border,
        bg: colors.reasoning.bg ?? "",
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "agent_code":
      return {
        rich: "code",
        icon: toolCallIcon(item.source),
        title: null,
        // Deeper border than agent_chat — the tool call is the load-bearing action.
        border: colors.agent_code.border,
        bg: "bg-card",
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "code_output":
      return {
        rich: "output",
        icon: Terminal,
        title: null,
        border: colors.code_output.border,
        bg: colors.code_output.bg ?? "",
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "agent_chat": {
      const chatTitle = agentChatTitle(item);
      return {
        rich: null,
        icon: Bot,
        title: chatTitle.title,
        titleKey: `timeline.${chatTitle.titleKey}`,
        titleValues: chatTitle.titleValues,
        border: colors.agent_chat.border,
        bg: "bg-card",
        fixedDefault: item.user_facing !== false,
        showTimestamp: true,
        copy: true,
      };
    }
    case "attach":
      // Attached media (meiva.self.attach) — open by default so the image
      // thumbnails are visible without a click once the card is revealed
      // (user ruling 2026-08-26; kept open when the 2026-08-30 ruling folded
      // it into the details block).
      return {
        rich: null,
        icon: Paperclip,
        title: null,
        titleKey: "timeline.attachedFiles",
        border: colors.attach.border,
        bg: colors.attach.bg ?? "",
        fixedDefault: true,
        showTimestamp: true,
        copy: false,
      };
    case "inbound_chat": {
      // Inter-agent + system inbound default collapsed (secondary chatter — the
      // header label stays, the body folds); only a human message stays open.
      const kind = inboundKind(item.source);
      if (kind === "agent") {
        return {
          rich: null,
          icon: Bot,
          title: `Agent ${(item.source ?? "").slice("agent:".length)}`,
          titleKey: "timeline.agent",
          titleValues: { id: (item.source ?? "").slice("agent:".length) },
          border: colors.inbound_agent.border,
          bg: colors.inbound_agent.bg ?? "",
          fixedDefault: false,
          showTimestamp: true,
          copy: true,
        };
      }
      if (kind === "external_agent") {
        const name = externalAgentName(item.source ?? "");
        return {
          rich: null,
          icon: Bot,
          title: `External agent ${name}`,
          titleKey: "timeline.externalAgent",
          titleValues: { name },
          border: colors.inbound_agent.border,
          bg: colors.inbound_agent.bg ?? "",
          fixedDefault: false,
          showTimestamp: true,
          copy: true,
        };
      }
      if (kind === "human") {
        return {
          rich: null,
          icon: User,
          title: "Human",
          titleKey: "timeline.human",
          border: colors.inbound_human.border,
          bg: colors.inbound_human.bg ?? "",
          fixedDefault: true,
          showTimestamp: true,
          copy: true,
        };
      }
      return {
        rich: null,
        icon: Info,
        title: "System",
        titleKey: "timeline.system",
        border: colors.inbound_system.border,
        bg: colors.inbound_system.bg ?? "",
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    }
    case "inbound_compact_summary":
      return {
        rich: null,
        icon: FileText,
        title: "Compact summary",
        titleKey: "timeline.compactSummary",
        border: colors.inbound_system.border,
        bg: colors.inbound_system.bg ?? "",
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "inbound_compact_request":
      return {
        rich: null,
        icon: FileText,
        title: "Compact request",
        titleKey: "timeline.compactRequest",
        border: colors.inbound_system.border,
        bg: colors.inbound_system.bg ?? "",
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "system_marker": {
      const cls = classifyMarker(item.source);
      if (cls.kind === "ephemeral") return null; // bare render path
      const v = markerVisual(cls, colors);
      return {
        rich: null,
        icon: v.icon,
        title: v.title,
        titleKey: v.titleKey ? `markers.${v.titleKey}` : undefined,
        titleClass: v.titleClass,
        border: v.border,
        bg: v.bg,
        cardText: v.text,
        // Framework notes (lifecycle / memory / guidance) default collapsed —
        // the labeled header conveys the event; the payload folds.
        fixedDefault: false,
        // Events keep their timestamp; standing context / guidance notes drop it.
        showTimestamp: item.show_timestamp,
        copy: false,
      };
    }
  }
  // Exhaustiveness guard: TS narrows to never; runtime fallback renders bare.
  /* v8 ignore next 3 */
  const _exhaustive: never = item.kind;
  console.warn("[timeline] messageCardConfig: unknown item kind", _exhaustive);
  return null;
}

// Cap how often a streaming item's header re-derives its summary. The code /
// output summaries scan the full accumulated payload (regex + line split), so at
// delta-commit rate the scan would restart from zero dozens of times a second; a
// glanceable summary only needs a few refreshes per second.
const SUMMARY_PARSE_INTERVAL_MS = 250;

const STICKY_CHILD_HEADER_CLS =
  "sticky top-[calc(2.75rem_+_var(--turn-header-h,0px))] z-[5]";
// A single frosted pane covers the header and its 2px top overlap, preserving
// the translucent hover without an unblurred seam under HeaderBar/parent turn.
// The button stays transparent: painting the pane twice would flatten the glass.
// All edges are paint-only to preserve measured height and avoid a pin loop.
// Sticky z-index contains the pane behind the label; the 16px shadow seals the
// scrollbar gutter and the separator sits inside the pane's bottom edge.
// The pane's `::before` box is kept present (same `content-['']`, geometry,
// and transition declarations) in BOTH stuck and unstuck states — only its
// background/blur *values* differ. `content` flipping between `normal` (no
// box) and `''` (a box) is a hard boundary for CSS transitions: a
// pseudo-element that didn't exist in the previous style has no prior frame
// to interpolate from, so the very first stick (and every un-stick/re-stick
// after) would snap straight to the settled frosted look instead of fading in
// over `duration-150`. Keeping the box alive at a transparent, unblurred
// resting value while unstuck gives the transition a real "before" state.
const HEADER_PANE_GEOMETRY_CLS =
  "before:absolute before:inset-x-0 before:-top-[2px] before:bottom-0 before:-z-10 before:content-[''] " +
  "before:pointer-events-none " +
  "before:transition-[background-color,backdrop-filter] before:duration-150 before:ease-out " +
  "motion-reduce:before:transition-none";
export const STUCK_HEADER_CLS =
  "bg-transparent hover:bg-transparent rounded-none " +
  "shadow-[16px_0_0_0_var(--background)] " +
  HEADER_PANE_GEOMETRY_CLS + " " +
  "before:bg-background/95 hover:before:bg-accent/30 before:backdrop-blur-md " +
  "after:absolute after:inset-x-0 after:bottom-0 after:h-px after:content-[''] " +
  "after:bg-border after:pointer-events-none " +
  "transition-[color] duration-150 ease-out motion-reduce:transition-none";
export const UNSTUCK_HEADER_CLS =
  "bg-transparent transition-[background-color,box-shadow,border-color] duration-150 ease-out motion-reduce:transition-none " +
  HEADER_PANE_GEOMETRY_CLS + " " +
  "before:bg-transparent before:backdrop-blur-none";

// meiva:note
// The hover chrome (timestamp, Copy) lives in the gap BELOW a block, not on the
// block: it reads as a separator between two messages instead of eating the
// card's own bottom padding (user 2026-10-21). `.timeline-item` owns that gap —
// it pads its bottom by the same 12px the row list already separates rows by and
// gives the height back as a negative margin (globals.css), so block-to-block
// spacing is unchanged and the band is drawn inside the row rather than past it,
// where content-visibility paint containment would clip it. A reserved line
// existed once and made the gap depend on which kinds stamped a time; this band
// costs no layout height at rest. Invisible and click-through until the pointer
// hovers the block, focus enters it, or a touch reveals it for a few seconds
// (`TimelineRow` adds that class: a coarse pointer has no hover to gate on, and
// leaving it painted put a time and Copy under every message for good).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
export const CARD_FOOTER_CLS = cn(
  "absolute right-2 bottom-0 h-3 items-center justify-end gap-1 sm:right-3",
  "pointer-events-none opacity-0 transition-opacity motion-reduce:transition-none",
  "group-hover/msgcard:pointer-events-auto group-hover/msgcard:opacity-100",
  "group-focus-within/msgcard:pointer-events-auto group-focus-within/msgcard:opacity-100",
  FLEX,
);

export function MessageCard({
  config,
  children,
}: {
  config: CardConfig;
  children: ReactNode;
}) {
  return (
    <div className={cn("relative rounded-r-sm", config.bg, config.cardText)}>
      <span
        aria-hidden
        data-testid="card-accent"
        className={cn("pointer-events-none absolute top-0 left-0 h-7 w-0 rounded-full border-l-2", config.border)}
      />
      {children}
    </div>
  );
}

// The clickable card header — always visible, toggles the body below it. Renders
// the rich Summary for the four summary kinds, else a plain icon + title.
// Render a card's static header title: translated via titleKey when present
// (falling back to the English `title`). titleKey is a full dotted key
// ("timeline.agentChat", "markers.terminated", …) resolved through the
// root translator; the agent-chat suffix rides inside titleValues as its own
// dotted key, resolved here.
function cardTitle(config: CardConfig, t: ReturnType<typeof useTranslations>): string {
  if (!config.titleKey) return config.title ?? "";
  const suffixKey = config.titleValues?.suffixKey;
  // Always pass `suffix` to the message: the agent-chat title is "Meiva ▸{suffix}",
  // and next-intl renders the raw key when an interpolation variable is
  // missing (MissingValueError → key fallback), which leaked "timeline.agentChat"
  // for plain (non-interrupted, non-partial) chat messages. Empty suffix is a
  // valid value — it renders the bare "Meiva ▸".
  const values = suffixKey
    ? { ...config.titleValues, suffix: t(suffixKey as Parameters<typeof t>[0]) }
    : { ...config.titleValues, suffix: "" };
  return t(config.titleKey as Parameters<typeof t>[0], values);
}

export const CardHeader = memo(function CardHeader({
  item,
  config,
  expanded,
  onToggle,
  stickyHeader = false,
  isStuck = false,
}: {
  item: BackendTimelineItem;
  config: CardConfig;
  expanded: boolean;
  onToggle: () => void;
  /** Where this card's header pins while expanded: `"top"` below the HeaderBar
   *  (top-level message cards, task #3136), or `"nested"` under its expanded
   *  work block's header (that block's child cards, task #3215 —
   *  STICKY_CHILD_HEADER_CLS). false / omitted: no pin. */
  stickyHeader?: "top" | "nested" | false;
  /** The header is currently pinned at the sticky line — drives the elevated
   *  mask styling (same contract as the work-block header). */
  isStuck?: boolean;
}) {
  const t = useTranslations();
  const live = isLiveReasoning(item) || isLiveCode(item) || isLiveExecution(item);
  const now = useNow(live);
  const summaryPayload = useThrottledStreaming(
    item.payload,
    !!item.partial,
    SUMMARY_PARSE_INTERVAL_MS,
  );
  const Icon = config.icon;
  const highlighter = usePythonCodeHighlighter();
  // Sticky only while expanded: a collapsed card has nothing to scroll under
  // its header, and the collapsed rest position must stay in flow.
  const sticky = !!stickyHeader && expanded;
  const stickyCls = stickyHeader === "nested" ? STICKY_CHILD_HEADER_CLS : STICKY_HEADER_CLS;

  return (
    // No title attribute — deliberately no hover tooltip on this button; the
    // chevron + click affordance is enough for sighted mouse users.
    // aria-expanded carries the same collapsed/expanded state to assistive
    // tech (the disclosure-widget ARIA pattern) — title was never a reliable
    // way to expose that anyway. data-testid/data-expanded are the test hook
    // (tests used to select via the tooltip text).
    <button
      type="button"
      onClick={onToggle}
      // Expand "intent" signal (task: keep the highlighter lazy but warm it
      // ahead of need — python-code.tsx): a collapsed card's body may hold a
      // python code block, so hovering/focusing its toggle starts the same
      // dynamic import the eventual expand would trigger anyway, ahead of
      // the click. Idempotent — a no-op once the chunk is loading/loaded.
      onPointerEnter={() => void highlighter.preload()}
      onFocus={() => void highlighter.preload()}
      className={cn(
        HEADER_CLS,
        "flex-wrap",
        sticky && stickyCls,
        sticky && (isStuck ? STUCK_HEADER_CLS : UNSTUCK_HEADER_CLS),
      )}
      aria-expanded={expanded}
      data-testid="card-toggle"
      data-expanded={expanded}
      data-stuck={sticky && isStuck ? "true" : "false"}
    >
      {expanded ? (
        <ChevronDown className="size-3 shrink-0 opacity-60" />
      ) : (
        <ChevronRight className="size-3 shrink-0 opacity-60" />
      )}
      {config.rich ? (
        <Summary item={item} payload={summaryPayload} now={now} live={live} />
      ) : (
        <span className={cn("items-center gap-1.5", config.titleClass, FLEX, MIN_W_0)}>
          {Icon ? <Icon className="size-3.5 shrink-0" /> : null}
          <span className="truncate">{cardTitle(config, t)}</span>
        </span>
      )}
      {/* No impersonation badge on purpose — the timeline does not surface the
          external takeover executor (user ruling 2026-09-16, task #3660). The
          item keeps its impersonation metadata for envelope semantics. */}
    </button>
  );
});
