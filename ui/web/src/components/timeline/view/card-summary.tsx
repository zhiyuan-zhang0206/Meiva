"use client";

import {
  Bot,
  Code2,
  FilePen,
  FileText,
  Search,
  Sparkles,
  Terminal,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import {
  formatDuration,
  summarizeCode,
  type SdkCall,
} from "@/lib/format/item-summary";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

import { isLiveCode, isLiveExecution } from "../model/reasoning-clock";

export function agentChatTitle(item: BackendTimelineItem): {
  title: string;
  titleKey: string;
  titleValues: Record<string, string>;
} {
  const suffixKey = item.interrupted
    ? "timeline.streamingInterruptedSuffix"
    : item.partial
      ? "timeline.streamingSuffix"
      : "";
  const suffix = item.interrupted
    ? " (streaming interrupted)"
    : item.partial
      ? " (streaming…)"
      : "";
  return {
    title: `Meiva ▸${suffix}`,
    titleKey: "agentChat",
    titleValues: { suffixKey },
  };
}

export function toolCallIcon(source: string | null | undefined): LucideIcon {
  if (!source || source === "execute_code") return Code2;
  switch (source) {
    case "command":
    case "Bash":
    case "run_command":
      return Terminal;
    case "patch":
    case "Edit":
    case "Write":
    case "MultiEdit":
    case "write_file":
    case "edit_file":
      return FilePen;
    case "Read":
    case "read_file":
      return FileText;
    case "Grep":
    case "Glob":
    case "web_search":
    case "search_web":
      return Search;
    case "Task":
    case "Agent":
    case "invoke_subagent":
      return Bot;
    default:
      return Wrench;
  }
}

function renderToolIcon(source: string | null | undefined, className: string): ReactNode {
  switch (source) {
    case "command":
    case "Bash":
    case "run_command":
      return <Terminal className={className} />;
    case "patch":
    case "Edit":
    case "Write":
    case "MultiEdit":
    case "write_file":
    case "edit_file":
      return <FilePen className={className} />;
    case "Read":
    case "read_file":
      return <FileText className={className} />;
    case "Grep":
    case "Glob":
    case "web_search":
    case "search_web":
      return <Search className={className} />;
    case "Task":
    case "Agent":
    case "invoke_subagent":
      return <Bot className={className} />;
    default:
      return <Wrench className={className} />;
  }
}

export function Summary({
  item,
  now,
  live,
}: {
  item: BackendTimelineItem;
  now: number;
  live: boolean;
}) {
  switch (item.kind) {
    case "agent_reasoning":
      return <ThinkingSummary item={item} now={now} live={live} />;
    case "agent_code":
      return <CodeSummary item={item} now={now} />;
    case "code_output":
      return <OutputSummary item={item} now={now} />;
    default:
      return null;
  }
}

function ThinkingSummary({
  item,
  now,
  live,
}: {
  item: BackendTimelineItem;
  now: number;
  live: boolean;
}) {
  const t = useTranslations("timeline");

  let label: string;
  if (live && item.reasoningStartedAt != null) {
    label = t("thinkingFor", { duration: formatDuration(now - item.reasoningStartedAt) });
  } else if (item.reasoning_ms != null) {
    label = t("thoughtFor", { duration: formatDuration(item.reasoning_ms) });
  } else if (item.reasoningElapsedMs != null) {
    label = t("thoughtFor", { duration: formatDuration(item.reasoningElapsedMs) });
  } else {
    label = t("thinking");
  }

  return (
    <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
      <Sparkles className={cn("size-3.5 shrink-0", live && "text-sky-500")} />
      <span className="truncate tabular-nums">{label}</span>
    </span>
  );
}

function CodeSummary({
  item,
  now,
}: {
  item: BackendTimelineItem;
  now: number;
}) {
  const t = useTranslations("timeline");
  const isExternalTool = Boolean(item.source && item.source !== "execute_code");
  const { calls } = summarizeCode(isExternalTool ? null : item.sdk_calls);

  let timing: string | null = null;
  const liveCode = isLiveCode(item);
  const codeElapsed = item.code_elapsed_ms ?? item.codeElapsedMs;
  if (liveCode && item.codeStartedAt != null) {
    timing = t("writingCodeFor", { duration: formatDuration(now - item.codeStartedAt) });
  } else if (codeElapsed != null) {
    timing = t("wroteCodeFor", { duration: formatDuration(codeElapsed) });
  }

  if (isExternalTool) {
    return (
      <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
        {renderToolIcon(item.source, cn("size-3.5 shrink-0", liveCode && "text-sky-500"))}
        <span className="truncate tabular-nums">
          {timing ? <>{timing}<span className="opacity-60">{" · "}</span></> : null}
          <span className="text-foreground/80">{item.source}</span>
        </span>
      </span>
    );
  }

  return (
    <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
      <Code2 className={cn("size-3.5 shrink-0", liveCode && "text-sky-500")} />
      <span className="truncate tabular-nums">
        {calls.length > 0 ? (
          <>
            {timing ? <>{timing}<span className="opacity-60">{" · "}</span></> : null}
            {calls.map((c, i) => (
              <span key={c.method}>
                {i > 0 ? <span className="opacity-40"> · </span> : null}
                <CallBadge call={c} />
              </span>
            ))}
          </>
        ) : (
          timing ?? t("code")
        )}
      </span>
    </span>
  );
}

// meiva:note
// Shared by the TurnBlock aggregate header so a collapsed turn reads as the same
// clickable summary row as a card header.
export const HEADER_CLS =
  "w-full flex items-center gap-1.5 px-2 sm:px-3 py-1.5 text-[11px] font-mono text-left " +
  "text-muted-foreground/80 hover:text-foreground hover:bg-accent/30 " +
  "transition-colors select-none rounded-tr-sm";

export function CallBadge({ call }: { call: SdkCall }) {
  return (
    <span>
      <span className="text-foreground/80">{call.method}</span>
      <span className="opacity-60">{` × ${call.count}`}</span>
    </span>
  );
}

function OutputSummary({ item, now }: { item: BackendTimelineItem; now: number }) {
  const t = useTranslations("timeline");
  const liveExec = isLiveExecution(item);
  const execMs = item.exec_ms ?? item.execElapsedMs;
  let ran: string | null = null;
  if (execMs != null) {
    ran = t("ranIn", { duration: formatDuration(execMs) });
  } else if (liveExec && item.execStartedAt != null) {
    ran = t("runningFor", { duration: formatDuration(now - item.execStartedAt) });
  }
  return (
    <span className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>
      <Terminal className={cn("size-3.5 shrink-0", liveExec && "text-sky-500")} />
      <span className="truncate tabular-nums">{ran ?? t("output")}</span>
    </span>
  );
}
