"use client";

import { useTranslations } from "next-intl";

import { STATUS_DOT } from "@/components/agents/agent-row";
import { ChatMarkdown } from "@/components/content/markdown";
import { PRIORITY_BG } from "@/lib/notifications/notices";
import { formatRelativeTime } from "@/lib/agents/sidebar";
import { useWallDate } from "@/lib/format/now/use-wall-date";
import type { PublicAgentStatus, OpenNotice } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";

export function PriorityBadge({ priority, muted }: { priority: OpenNotice["priority"]; muted?: boolean }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded px-1 text-[10px] font-semibold text-white",
        muted ? "bg-muted-foreground/40" : PRIORITY_BG[priority],
      )}
    >
      {priority}
    </span>
  );
}


export function AgentStatusDot({ status }: { status: PublicAgentStatus | null }) {
  if (!status) return null;
  return (
    <span
      className={cn("size-1.5 rounded-full shrink-0", STATUS_DOT[status])}
    />
  );
}

export function NoticeListRow({
  priority,
  blocking,
  title,
  agentLabel,
  agentId,
  agentStatus,
  createdAt,
  muted,
  indent,
  selected,
  onSelect,
  onSelectAgent,
  noticeKey,
  anchorHighlighted,
}: {
  priority: OpenNotice["priority"];
  blocking: boolean;
  title: string;
  agentLabel: string;
  agentId: number;
  agentStatus: PublicAgentStatus | null;
  createdAt?: string;
  muted?: boolean;
  indent?: boolean;
  selected: boolean;
  onSelect: () => void;
  onSelectAgent?: (agentId: number | null) => void;
  noticeKey?: string;
  anchorHighlighted?: boolean;
}) {
  const t = useTranslations("fleet.inboxPanel");
  const now = useWallDate();
  return (
    <li
      data-testid="inbox-row"
      data-notice-key={noticeKey}
      data-anchor-highlighted={anchorHighlighted ? "true" : undefined}
      className={cn(
        "items-stretch border-b border-border/50 transition-colors",
        selected && "bg-sidebar-accent",
        anchorHighlighted && "bg-primary/10 ring-1 ring-inset ring-primary/50",
        FLEX
      )}
    >
      <button
        type="button"
        onClick={() => {
          onSelect();
          onSelectAgent?.(agentId);
        }}
        className={cn(
          "text-left px-4 py-2.5 items-center gap-2 hover:bg-sidebar-accent/50",
          indent && "pl-7",
          muted && "opacity-55",
          MIN_W_0, FLEX_1, FLEX
        )}
      >
        <span className={cn("space-y-1.5", FLEX_1, MIN_W_0)}>
          <span className={cn("items-start gap-2", FLEX)}>
            <span data-testid="notice-title" className={cn("text-sm font-medium break-words", MIN_W_0, FLEX_1)}>
              <ChatMarkdown content={title} inline components={{ a: ({ children }) => <>{children}</> }} />
            </span>
            <PriorityBadge priority={priority} muted={muted} />
          </span>
          <span className={cn("flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground", FLEX)}>
            <span className={cn("items-center gap-1.5", FLEX)}>
              <AgentStatusDot status={agentStatus} />
              <span className="break-all">{agentLabel}</span>
            </span>
            {createdAt && <span>{formatRelativeTime(createdAt, now)}</span>}
            {blocking && <span className="text-destructive">{t("blocking")}</span>}
          </span>
        </span>
      </button>
    </li>
  );
}

// The detail header identifying the agent (status + label + relative time). The
// notice's own content and reply controls come from the shared
// <OpenNoticeDetail> below it.

export function AgentContextHeader({
  agentLabel,
  agentStatus,
  createdAt,
}: {
  agentLabel: string;
  agentStatus: PublicAgentStatus | null;
  createdAt?: string;
}) {
  const now = useWallDate();
  return (
    <div className="mb-3">
      <div className={cn("items-center gap-2", FLEX)}>
        <AgentStatusDot status={agentStatus} />
        <span className={cn("truncate text-xs text-muted-foreground", MIN_W_0)}>
          {agentLabel}
          {createdAt ? ` · ${formatRelativeTime(createdAt, now)}` : ""}
        </span>
      </div>
    </div>
  );
}
