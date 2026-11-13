"use client";

// meiva:note
// Per-kind metadata and secondary step shell for the timeline. Primary messages
// (human inbound_chat and user-facing agent_chat) render directly in TimelineRow
// as a right-aligned user bubble and unboxed canvas Markdown prose respectively,
// while secondary work items inside TurnBlocks render through borderless
// MessageCard + CardHeader (icon, title or summary, and inline chevron).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.md

import {
  Bot,
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
import { memo, useContext, type ReactNode } from "react";

import { usePythonCodeHighlighter } from "@/components/content/python-code";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";

import { markerVisual } from "./markers";
import { classifyMarker } from "./model/marker-classify";
import { inboundKind } from "./model/runs";
import { externalAgentName } from "@/lib/format/inbound-source";
import { isLiveCode, isLiveExecution, isLiveReasoning, LiveClockGate, useNow } from "./model/reasoning-clock";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { agentChatTitle, DisclosureChevron, DISCLOSURE_TITLE_CLS, Summary, toolCallIcon } from "./view/card-summary";

export type SummaryKind = "reasoning" | "code" | "output";

export interface CardConfig {
  readonly rich: SummaryKind | null;
  readonly icon: LucideIcon | null;
  readonly title: string | null;
  readonly titleKey?: string;
  readonly titleValues?: Record<string, string | number>;
  // meiva:note
  // Default body visibility within an opened settled TurnBlock.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
  readonly fixedDefault: boolean;
  readonly expandWhileWorking?: false;
  // meiva:note
  // Whether the card stamps the item's timestamp in its right-aligned hover footer.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.md
  readonly showTimestamp: boolean;
  // meiva:note
  // Explicit per-kind Copy whitelist: only human/agent inbound_chat and agent_chat
  // offer Copy; machinery kinds (thinking, tool calls/output, system prompt,
  // attach, system notes) offer none.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.md
  readonly copy: boolean;
}

// meiva:note
// Work a CLI's own sub-agent did (its text, and the tool calls and outputs it made) names the call that
// started it in `parent_item_id`; the main agent's own tool activity names none. The row nests it
// under that call and the tool header says whose it is.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.md
export function isSubagentWork(item: BackendTimelineItem): boolean {
  return (
    !!item.parent_item_id &&
    (item.kind === "agent_code" || item.kind === "code_output" || item.kind === "subagent_text")
  );
}

export function messageCardConfig(item: BackendTimelineItem): CardConfig | null {
  switch (item.kind) {
    case "system_prompt":
      return {
        rich: null,
        icon: Settings,
        title: "System prompt",
        titleKey: "timeline.systemPrompt",
        fixedDefault: false,
        showTimestamp: false,
        copy: false,
      };
    case "agent_reasoning":
      return {
        rich: "reasoning",
        icon: Sparkles,
        title: null,
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "agent_code":
      return {
        rich: "code",
        icon: toolCallIcon(item.source),
        title: null,
        fixedDefault: false,
        showTimestamp: false,
        copy: false,
      };
    case "code_output":
      return {
        rich: "output",
        icon: Terminal,
        title: null,
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "subagent_text":
      return {
        rich: null,
        icon: Bot,
        title: item.source ? `Sub-agent · ${item.source}` : "Sub-agent",
        titleKey: item.source ? "timeline.subagentNamed" : "timeline.subagent",
        titleValues: item.source ? { name: item.source } : undefined,
        fixedDefault: false,
        expandWhileWorking: false,
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
        fixedDefault: true,
        showTimestamp: true,
        copy: true,
      };
    }
    case "attach":
      return {
        rich: null,
        icon: Paperclip,
        title: null,
        titleKey: "timeline.attachedFiles",
        fixedDefault: true,
        showTimestamp: true,
        copy: false,
      };
    case "inbound_chat": {
      const kind = inboundKind(item.source);
      if (kind === "agent") {
        return {
          rich: null,
          icon: Bot,
          title: `Agent ${(item.source ?? "").slice("agent:".length)}`,
          titleKey: "timeline.agent",
          titleValues: { id: (item.source ?? "").slice("agent:".length) },
          fixedDefault: false,
          expandWhileWorking: false,
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
          fixedDefault: false,
          expandWhileWorking: false,
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
        fixedDefault: true,
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
        fixedDefault: false,
        showTimestamp: true,
        copy: false,
      };
    case "system_marker": {
      const cls = classifyMarker(item.source);
      if (cls.kind === "ephemeral") return null;
      const v = markerVisual(cls);
      return {
        rich: null,
        icon: v.icon,
        title: v.title,
        titleKey: v.titleKey ? `markers.${v.titleKey}` : undefined,
        fixedDefault: cls.kind === "note",
        showTimestamp: item.show_timestamp,
        copy: false,
      };
    }
  }
  const _exhaustive: never = item.kind;
  console.warn("[timeline] messageCardConfig: unknown item kind", _exhaustive);
  return null;
}

// meiva:note
// Hover chrome (timestamp + Copy) sits in the 20px block-to-block gap below
// primary messages (`.timeline-item` 1rem bottom padding + -1rem margin hand-back
// in globals.css), right-aligned for both human bubbles and agent prose, and
// stays invisible/click-through until hovered, focused, or touch-revealed.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.md
export const CARD_FOOTER_CLS = cn(
  "absolute bottom-0 right-1 h-3 items-center justify-end gap-1.5 sm:right-1.5",
  "pointer-events-none opacity-0 transition-opacity duration-150 motion-reduce:transition-none",
  "group-hover/msgcard:pointer-events-auto group-hover/msgcard:opacity-100",
  "group-focus-within/msgcard:pointer-events-auto group-focus-within/msgcard:opacity-100",
  FLEX,
);

export function MessageCard({
  children,
}: {
  config?: CardConfig;
  children: ReactNode;
}) {
  return <div className="relative rounded-md">{children}</div>;
}

function cardTitle(config: CardConfig, t: ReturnType<typeof useTranslations>): string {
  if (!config.titleKey) return config.title ?? "";
  const suffixKey = config.titleValues?.suffixKey;
  // meiva:note
  // Always supply `suffix` (empty string when unset) so next-intl never falls back
  // to rendering the raw "timeline.agentChat" key on plain agent_chat items.
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
}: {
  item: BackendTimelineItem;
  config: CardConfig;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useTranslations();
  const gate = useContext(LiveClockGate);
  const live = gate && (isLiveReasoning(item) || isLiveCode(item) || isLiveExecution(item));
  const now = useNow(live);
  const Icon = config.icon;
  const highlighter = usePythonCodeHighlighter();

  return (
    <button
      type="button"
      onClick={onToggle}
      onPointerEnter={() => void highlighter.preload()}
      onFocus={() => void highlighter.preload()}
      className={cn("group/disclosure w-full flex-wrap items-center gap-1.5 rounded-md px-1 py-1 text-left font-mono text-[11px] text-muted-foreground select-none", FLEX)}
      aria-expanded={expanded}
      data-testid="card-toggle"
      data-expanded={expanded}
    >
      <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
        {config.rich ? <Summary item={item} now={now} live={live} /> : (
          <>
            {Icon ? <Icon className="size-3.5 shrink-0 opacity-80" /> : null}
            <span className={DISCLOSURE_TITLE_CLS}>{cardTitle(config, t)}</span>
          </>
        )}
        <DisclosureChevron expanded={expanded} />
      </span>
      {config.rich && isSubagentWork(item) ? (
        <span
          data-testid="subagent-badge"
          className="shrink-0 rounded-sm bg-muted px-1 text-[10px] leading-4 text-muted-foreground"
        >
          {t("timeline.subagent")}
        </span>
      ) : null}
    </button>
  );
});
