// meiva:note
// Tree-guide: a 1px vertical line per depth level (absolutely positioned,
// full row height), drawn only for ancestors that are not the last child —
// visually equivalent to the GitHub file tree. No ├/└ glyphs; hierarchy
// is expressed through indent + vertical lines, cleaner than ASCII.
// Default grey, no active / hover highlight swap (we tried path-stem
// highlighting, but it stole focus visually; the active row's background highlight is
// already distinctive enough).
// Row actions: alive actions remain in the context menu (long-press on touch).
// A terminated row keeps one always-on resurrect button; in-flight actions show a row spinner.
// The context menu's "with prompt..." items open a textarea dialog; the
// plain items fire immediately with no prompt. The destructive Kill
// (SIGKILL) sits behind a confirm so it can't be hit by accident.

import * as Popover from "@radix-ui/react-popover";
import { CircleQuestionMark, GitFork, Pencil, PowerOff, RotateCw, Shrink, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";

import { useBrowser } from "@/lib/browser/browser-provider";
import { useRef, useState } from "react";

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { useUserSettings } from "@/lib/state/use-user-settings";
import type { AgentRow } from "@/lib/contracts/types";
import type { PendingAction } from "@/lib/agents/use-agents";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import {
  AGENT_HOVER_OPEN_DELAY_MS,
  AGENT_HOVER_WARMUP_MS,
  AgentHoverCard,
  AgentHoverGroup,
  STATUS_DOT,
  useAgentRowHover,
} from "./agent-hover-card";
import { RowActions } from "./row-actions";

export {
  AGENT_HOVER_OPEN_DELAY_MS,
  AGENT_HOVER_WARMUP_MS,
  AgentHoverGroup,
  STATUS_DOT,
};

// Radix Dialog is needed only after an advanced prompt-taking menu action;
// closed rows keep no modal payload in the initial graph.
const LazyPromptDialog = dynamic(() =>
  import("@/components/agents/agent-prompt-dialog").then((module) => module.PromptDialog),
  { loading: () => null },
);

export interface AgentRowProps {
  agent: AgentRow;
  label: string | undefined; // undefined → default `#N`
  active: boolean;
  pending: PendingAction | undefined;
  /** Tree depth (top level = 0); drives left-side indent. */
  depth: number;
  /** Whether each ancestor on the chain is its parent's last child —
   *  ordered root → direct parent. index 0 = furthest ancestor (root),
   *  index depth-1 = direct parent. d-th ancestor isLast → don't draw a
   *  vertical line at that depth (the family branch has ended). */
  ancestorsIsLast: readonly boolean[];
  onSelect: () => void;
  onTerminate: () => void;
  /** Force kill — SIGKILLs the process immediately, for an agent stuck in a
   *  loop / hung call that the graceful terminate can't reach. Right-click
   *  menu only (guarded by a confirm), never a one-click button. */
  onForceKill: () => void;
  onRestart: (message: string) => void;
  /** Resurrect a terminated agent. The plain button / quick menu item is a
   *  pure lifecycle event — no prompt; the agent just wakes. An optional
   *  prompt (the "with prompt..." menu item) is delivered as the agent wakes. */
  onResurrect: (prompt?: string) => void;
  /** Fork a child agent from this agent's latest checkpoint. An optional
   *  prompt seeds the child's first turn (the "with prompt..." menu item);
   *  omit it for a plain fork. */
  onFork: (prompt?: string) => void;
  onRename: (label: string) => void;
  onCompact: () => void;
}

/** Per-level indent (px). 14 = 1px guide line + surrounding spacing. */
const INDENT = 14;
// meiva:note
// The guide aligns with the center of its ancestor's left marker when visible
// (16px row padding plus 3px marker radius).
const BAR_X_BASE = 19;

/** 1px vertical line per depth level spanning the full row height.
 *  Drawn for every ancestor depth — every child at every level gets a
 *  vertical bar so the lineage column is contiguous from top to bottom.
 *  In dark mode /20 alpha is too faint on the sidebar bg to be visible;
 *  bump to /40 so it stands out from the row. */
function TreeGuides({
  depth,
}: {
  depth: number;
  ancestorsIsLast: readonly boolean[];
}) {
  if (depth === 0) return null;
  const bars: React.ReactNode[] = [];
  for (let d = 0; d < depth; d++) {
    bars.push(
      <span
        key={d}
        aria-hidden
        data-testid="tree-guide"
        data-depth={d}
        style={{ left: BAR_X_BASE + d * INDENT }}
        className="z-10 pointer-events-none absolute inset-y-0 w-px bg-muted-foreground/20 dark:bg-muted-foreground/40"
      />,
    );
  }
  return <>{bars}</>;
}


export function AgentRow({
  agent,
  label,
  active,
  pending,
  depth,
  ancestorsIsLast,
  onSelect,
  onTerminate,
  onForceKill,
  onRestart,
  onResurrect,
  onFork,
  onRename,
  onCompact,
}: AgentRowProps) {
  const t = useTranslations("agentRow");
  const browser = useBrowser();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const pendingRenameRef = useRef(false);
  const focusRenameOnCloseRef = useRef(false);
  // Prompt dialogs for the "... with prompt" menu items. Two independent
  // flags (not one "which") so a closing dialog never flips its title to
  // the other action mid-fade.
  const [forkOpen, setForkOpen] = useState(false);
  const [resurrectOpen, setResurrectOpen] = useState(false);
  const [restartOpen, setRestartOpen] = useState(false);

  const { settings: userSettings } = useUserSettings();
  const showAgentStatus = userSettings["display.show_agent_status"] === true;
  const offline = agent.liveness_state === "offline";
  const working = agent.status === "running" && !offline;
  const statusMarker = offline
    ? "bg-muted-foreground/50"
    : showAgentStatus && agent.status === "terminated"
      ? STATUS_DOT[agent.status]
      : null;
  const replyLabel = agent.has_pending_question
    ? t("statusPendingQuestion")
    : agent.awaiting_response_count > 0
      ? t("statusAwaitingReply")
      : null;

  // #ID and label coexist, complementing each other: `#180 · screen-permission` or `#180` (when there's no label)
  const displayLabel = label ? `#${agent.agent_id} · ${label}` : `#${agent.agent_id}`;
  const indentPx = depth * INDENT;

  const submit = () => {
    onRename(draft);
    setEditing(false);
  };

  // meiva:note
  // The question goes through the browser port: a test answers it by value, and a frame with nobody
  // to ask refuses rather than proceeding on the reader's behalf.
  const askConfirm = (message: string): boolean => browser.dialogs.confirm(message);

  const confirmForceKill = () => {
    const confirmSetting = userSettings["behavior.confirm_force_kill"] !== false;
    if (!confirmSetting) {
      onForceKill();
      return;
    }
    if (
      askConfirm(
        t("forceKillConfirm", { id: agent.agent_id }),
      )
    ) {
      onForceKill();
    }
  };

  const confirmTerminate = () => {
    const confirmSetting = userSettings["behavior.confirm_terminate"] !== false;
    if (!confirmSetting) {
      onTerminate();
      return;
    }
    if (
      askConfirm(
        t("terminateConfirm", { id: agent.agent_id }),
      )
    ) {
      onTerminate();
    }
  };

  const confirmRestart = () => {
    setRestartOpen(true);
  };

  const { hoverOpen, onMouseEnter, onMouseLeave, dismissHover } = useAgentRowHover(
    agent.agent_id,
    editing,
  );

  const startRename = () => {
    dismissHover();
    setDraft(label ?? "");
    setEditing(true);
  };

  return (
    <Popover.Root open={hoverOpen}>
    <ContextMenu
      onOpenChange={(open) => {
        if (open) {
          dismissHover();
        } else if (pendingRenameRef.current) {
          pendingRenameRef.current = false;
          startRename();
        }
      }}
    >
      <Popover.Anchor asChild>
      <ContextMenuTrigger asChild>
        <li className="agent-row-item group relative">
          {/* Adjacent rows' bars at the same x naturally stack into a
              continuous vertical line — this is why the "path" looks
              contiguous, and the hard reason buttons can't be
              position:relative (positioned elements paint after bars,
              covering them). */}
          <TreeGuides depth={depth} ancestorsIsLast={ancestorsIsLast} />
          <button
            type="button"
            onClick={() => {
              dismissHover();
              onSelect();
            }}
            onMouseEnter={onMouseEnter}
            onMouseLeave={onMouseLeave}
            onContextMenu={dismissHover}
            className={cn(
              // No transition-colors — active switch must be instant,
              // otherwise the old-focused row's bg fades over 150ms while
              // the placeholder lights up, making it look like "the
              // placeholder gets focus first, then moves"
              // cursor-default: the global button cursor:pointer rule (#709)
              // gives this row a hand cursor; a switching row is a list item,
              // not a button — user ruling (#723) keeps the default arrow.
              "agent-row-pill w-full text-left pr-2.5 font-mono text-xs leading-4 items-center gap-2 cursor-default select-none [-webkit-touch-callout:none]",
              FLEX,
              active
                ? "agent-row-pill-active bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground",
            )}
            style={{ paddingLeft: `${10 + indentPx}px` }}
          >
            {statusMarker ? (
              <span
                title={offline ? t("statusOffline") : undefined}
                className={cn("size-1.5 rounded-full shrink-0", statusMarker)}
              />
            ) : null}
            {agent.availability?.reason.startsWith("launch_") && (
              <span className="shrink-0 rounded bg-destructive/15 px-1 text-[9px] text-destructive"
                title={t("launchFailed")}>
                {t("launchFailed")}
              </span>
            )}
            <span className={cn(FLEX_1, MIN_W_0, FLEX, FLEX_COL)}>
              {editing ? (
                <span className={cn("items-center gap-1", MIN_W_0, FLEX)}>
                  <span className="text-xs shrink-0 select-none">#{agent.agent_id} ·</span>
                  <input
                    ref={renameInputRef}
                    autoFocus
                    autoComplete="off"
                    autoCorrect="off"
                    autoCapitalize="none"
                    spellCheck={false}
                    enterKeyHint="done"
                    data-form-type="other"
                    data-1p-ignore=""
                    data-lpignore="true"
                    value={draft}
                    aria-label={t("rename", { id: agent.agent_id })}
                    onChange={(e) => setDraft(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    onBlur={submit}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        submit();
                      } else if (e.key === "Escape") {
                        e.preventDefault();
                        setEditing(false);
                      }
                    }}
                    className={cn("bg-background border border-primary rounded px-1 py-0 text-xs pointer-coarse:text-base focus:outline-none h-5 pointer-coarse:h-6", MIN_W_0)}
                    placeholder={label ?? "Label"}
                  />
                </span>
              ) : (
                <span
                  className={cn("items-center gap-1", MIN_W_0, FLEX)}
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    startRename();
                  }}
                >
                  <span className={cn("truncate", MIN_W_0, working && "agent-title-working")}>
                    {displayLabel}
                  </span>
                  {working ? <span className="sr-only">{t("statusWorking")}</span> : null}
                </span>
              )}
            </span>
            {/* Reserve room for the on-row action or pending spinner. */}
            {!editing && (agent.status === "terminated" || pending) ? (
              <span className="shrink-0 w-5" />
            ) : null}
            {replyLabel ? (
              <span
                role="img"
                aria-label={replyLabel}
                title={replyLabel}
                className="grid size-4 shrink-0 place-items-center"
              >
                {agent.has_pending_question ? (
                  <CircleQuestionMark aria-hidden className="size-3.5 text-[#ED8755]" />
                ) : (
                  <span aria-hidden className="size-1.5 rounded-full bg-[#ED8755]" />
                )}
              </span>
            ) : null}
          </button>
          {!editing ? (
            <RowActions
              agent={agent}
              pending={pending}
              hasReplyIndicator={replyLabel !== null}
              onResurrect={() => onResurrect()}
            />
          ) : null}
        </li>
      </ContextMenuTrigger>
      </Popover.Anchor>
      {hoverOpen ? (
        <AgentHoverCard agent={agent} displayLabel={displayLabel} />
      ) : null}
      <ContextMenuContent
        onCloseAutoFocus={(e) => {
          if (focusRenameOnCloseRef.current) {
            e.preventDefault();
            focusRenameOnCloseRef.current = false;
            renameInputRef.current?.focus();
          }
        }}
      >
        {/* onSelect hands Radix's Event to the callback; wrap the
            prompt-taking actions so the Event is not passed as a prompt. */}
        <ContextMenuItem
          onSelect={() => {
            pendingRenameRef.current = true;
            focusRenameOnCloseRef.current = true;
          }}
        >
          <Pencil /> {t("menuRename")}
        </ContextMenuItem>
        <ContextMenuSeparator />
        {agent.status === "terminated" ? (
          <>
            <ContextMenuItem onSelect={() => onResurrect()}>
              <RotateCw /> {t("resurrect")}
            </ContextMenuItem>
            <ContextMenuItem onSelect={() => setResurrectOpen(true)}>
              <RotateCw /> {t("resurrectWithPrompt")}
            </ContextMenuItem>
          </>
        ) : (
          <>
            {(agent.harness === "meiva" || agent.status === "idling") && (
              <ContextMenuItem
                onSelect={confirmRestart}
                disabled={pending !== undefined}
              >
                <RotateCw /> {t("restart")}
              </ContextMenuItem>
            )}
            <ContextMenuItem
              onSelect={confirmTerminate}
              disabled={pending !== undefined}
            >
              <PowerOff className="text-destructive" /> {t("terminate")}
            </ContextMenuItem>
            {agent.harness === "meiva" && (
              <>
                <ContextMenuItem onSelect={onCompact} disabled={pending !== undefined}>
                  <Shrink /> {t("compact")}
                </ContextMenuItem>
                <ContextMenuItem onSelect={() => onFork()}>
                  <GitFork /> {t("fork")}
                </ContextMenuItem>
                <ContextMenuItem onSelect={() => setForkOpen(true)}>
                  <GitFork /> {t("forkWithPrompt")}
                </ContextMenuItem>
              </>
            )}
            <ContextMenuSeparator />
            <ContextMenuItem
              variant="destructive"
              onSelect={confirmForceKill}
              disabled={pending !== undefined}
            >
              <X /> {t("kill")}
            </ContextMenuItem>
          </>
        )}
      </ContextMenuContent>
      {forkOpen && agent.harness === "meiva" ? (
        <LazyPromptDialog
          open
          onOpenChange={setForkOpen}
          title={t("forkAgent")}
          description={t("forkAgentDesc")}
          placeholder={t("forkPromptPlaceholder")}
          submitLabel={t("fork")}
          onSubmit={onFork}
        />
      ) : null}
      {resurrectOpen ? (
        <LazyPromptDialog
          open
          onOpenChange={setResurrectOpen}
          title={t("resurrectAgent")}
          description={t("resurrectDesc")}
          placeholder={t("resurrectPromptPlaceholder")}
          submitLabel={t("resurrect")}
          onSubmit={onResurrect}
        />
      ) : null}
      {restartOpen ? (
        <LazyPromptDialog
          open
          onOpenChange={setRestartOpen}
          title={t("restartAgent")}
          description={t("restartDesc")}
          placeholder={t("restartPromptPlaceholder")}
          submitLabel={t("restart")}
          onSubmit={(message: string) => {
            if (message.trim()) {
              onRestart(message.trim());
            }
          }}
        />
      ) : null}
      </ContextMenu>
    </Popover.Root>
  );
}
