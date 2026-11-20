"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, File, Folder } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Spinner } from "@/components/ui/spinner";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { useApi } from "@/lib/transport/transport-context";

// meiva:note
// One folder of the tree, read when it is first opened. Entries come back folders first.
function TreeFolder({ agentId, workspaceAgentId, path, depth }: {
  agentId: number;
  workspaceAgentId: number;
  path: string | undefined;
  depth: number;
}) {
  const t = useTranslations("toUser");
  const client = useApi();
  const activeFile = useWorkspaceFileStore((s) => s.activeFile);
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());
  const listing = useQuery({
    queryKey: ["workspace-dir", workspaceAgentId, path ?? ""],
    queryFn: ({ signal }) => client.listWorkspaceDir(workspaceAgentId, path, signal),
    staleTime: 10_000,
  });
  const indent = { paddingLeft: `${8 + depth * 12}px` };
  if (listing.isLoading) {
    return <div style={indent} className="py-1"><Spinner className="size-3" label={t("loading")} /></div>;
  }
  if (listing.error || !listing.data) {
    return <div style={indent} className="py-1 text-[11px] text-destructive">{t("unreadable")}</div>;
  }
  if (listing.data.entries.length === 0) {
    return <div style={indent} className="py-1 text-[11px] text-muted-foreground">{t("filesEmptyFolder")}</div>;
  }
  return (
    <ul role={depth === 0 ? "tree" : "group"} aria-label={depth === 0 ? t("filesWorkspace", { id: workspaceAgentId }) : undefined}>
      {listing.data.entries.map((entry) => {
        const isDir = entry.kind === "dir";
        const expanded = open.has(entry.path);
        const selected = !isDir && activeFile?.path === entry.path;
        return (
          <li key={entry.path} role="treeitem" aria-expanded={isDir ? expanded : undefined} aria-selected={selected}>
            <button
              type="button"
              style={indent}
              onClick={() => {
                if (isDir) {
                  setOpen((state) => {
                    const next = new Set(state);
                    if (expanded) next.delete(entry.path);
                    else next.add(entry.path);
                    return next;
                  });
                }
                else useWorkspaceFileStore.getState().openFile({ agentId, path: entry.path });
              }}
              className={cn(
                "w-full items-center gap-1 rounded-sm py-0.5 pr-2 text-left text-[12px] hover:bg-accent/60",
                selected ? "bg-accent text-foreground" : "text-foreground/80",
                FLEX,
              )}
            >
              {isDir ? (
                <ChevronRight aria-hidden className={cn("size-3 shrink-0 opacity-60 transition-transform", expanded && "rotate-90")} />
              ) : (
                <span aria-hidden className="w-3 shrink-0" />
              )}
              {isDir ? <Folder aria-hidden className="size-3.5 shrink-0 opacity-60" /> : <File aria-hidden className="size-3.5 shrink-0 opacity-50" />}
              <span className={cn("truncate", MIN_W_0)}>{entry.name}</span>
            </button>
            {isDir && expanded ? (
              <TreeFolder agentId={agentId} workspaceAgentId={workspaceAgentId} path={entry.path} depth={depth + 1} />
            ) : null}
          </li>
        );
      })}
      {listing.data.truncated ? (
        <li style={indent} className="py-1 text-[10px] text-muted-foreground">{t("filesTruncated", { count: listing.data.entries.length })}</li>
      ) : null}
    </ul>
  );
}

// meiva:note
// The workspace as a tree beside the open file. It starts at this conversation's agent; typing
// another agent's number browses that agent's workspace, with the same read access as opening a
// linked file (user ruling 2026-11-20). Files open in this conversation's tabs.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function FileTree({ agentId, workspaceAgentId, onWorkspaceChange }: {
  agentId: number;
  workspaceAgentId: number;
  onWorkspaceChange: (agentId: number) => void;
}) {
  const t = useTranslations("toUser");
  const [draft, setDraft] = useState(String(workspaceAgentId));
  return (
    <nav data-testid="file-tree" aria-label={t("filesWorkspace", { id: workspaceAgentId })} className={cn("w-56 shrink-0 border-r", MIN_H_0, FLEX, FLEX_COL)}>
      <form
        className={cn("shrink-0 items-center gap-1 border-b px-2 py-1 text-[11px] text-muted-foreground", FLEX)}
        onSubmit={(event) => {
          event.preventDefault();
          const id = Number(draft.replace(/^#/, ""));
          if (Number.isInteger(id) && id > 0) onWorkspaceChange(id);
          else setDraft(String(workspaceAgentId));
        }}
      >
        <span aria-hidden>#</span>
        <input
          aria-label={t("filesSwitchWorkspace")}
          inputMode="numeric"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => setDraft(String(workspaceAgentId))}
          className="w-16 bg-transparent font-mono text-foreground outline-none"
        />
      </form>
      <div className={cn("overflow-y-auto py-1", MIN_H_0)}>
        <TreeFolder key={workspaceAgentId} agentId={agentId} workspaceAgentId={workspaceAgentId} path={undefined} depth={0} />
      </div>
    </nav>
  );
}
