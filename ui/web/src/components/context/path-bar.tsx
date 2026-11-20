"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { resolveWorkspaceFile } from "@/components/content/markdown-image-url";
import { Spinner } from "@/components/ui/spinner";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { useWallDate } from "@/lib/format/now/use-wall-date";
import { formatRelative } from "@/lib/format/time";
import { cn, formatFileSize } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { useApi } from "@/lib/transport/transport-context";

import { fileName } from "./panel-bar";

interface Folder {
  workspace: number;
  // meiva:note
  // A workspace-relative or absolute directory; undefined is the workspace root.
  path: string | undefined;
}

// meiva:note
// One folder's entries under a path segment, like an address bar's drop-down: a folder opens in
// place (with a way back up), a file opens as a tab. On the root it also takes another agent's
// number, to browse that agent's workspace with the read access links already have.
function FolderMenu({ agentId, start, onClose }: { agentId: number; start: Folder; onClose: () => void }) {
  const t = useTranslations("toUser");
  const client = useApi();
  const [trail, setTrail] = useState<Folder[]>([start]);
  const [draft, setDraft] = useState(String(start.workspace));
  const folder = trail[trail.length - 1];
  const listing = useQuery({
    queryKey: ["workspace-dir", folder.workspace, folder.path ?? ""],
    queryFn: ({ signal }) => client.listWorkspaceDir(folder.workspace, folder.path, signal),
    staleTime: 10_000,
  });
  return (
    <div data-testid="path-menu" role="menu" className="absolute left-0 top-full z-40 mt-1 w-72 rounded-lg border border-border bg-background py-1 shadow-lg">
      {folder.path === undefined ? (
        <form
          className={cn("items-center gap-1 border-b px-3 pb-1.5 pt-0.5 text-[11px] text-muted-foreground", FLEX)}
          onSubmit={(event) => {
            event.preventDefault();
            const id = Number(draft.replace(/^#/, ""));
            if (Number.isInteger(id) && id > 0) setTrail([{ workspace: id, path: undefined }]);
          }}
        >
          <span>{t("filesSwitchWorkspace")}</span>
          <span aria-hidden>#</span>
          <input aria-label={t("filesSwitchWorkspace")} inputMode="numeric" value={draft} onChange={(event) => setDraft(event.target.value)} className="w-16 bg-transparent font-mono text-foreground outline-none" />
        </form>
      ) : null}
      {trail.length > 1 ? (
        <button type="button" role="menuitem" onClick={() => setTrail(trail.slice(0, -1))} className={cn("w-full items-center gap-1 px-3 py-1 text-left text-[12px] text-muted-foreground hover:bg-accent", FLEX)}>
          <ChevronLeft aria-hidden className="size-3" />..
        </button>
      ) : null}
      <div className="max-h-72 overflow-y-auto">
        {listing.isLoading ? <div className="px-3 py-1.5"><Spinner className="size-3" label={t("loading")} /></div> : null}
        {listing.error ? <div className="px-3 py-1.5 text-[11px] text-destructive">{t("unreadable")}</div> : null}
        {listing.data?.entries.length === 0 ? <div className="px-3 py-1.5 text-[11px] text-muted-foreground">{t("filesEmptyFolder")}</div> : null}
        {listing.data?.entries.map((entry) => (
          <button
            key={entry.path}
            type="button"
            role="menuitem"
            onClick={() => {
              if (entry.kind === "dir") {
                setTrail([...trail, { workspace: folder.workspace, path: entry.path }]);
                return;
              }
              useWorkspaceFileStore.getState().openFile({ agentId, path: entry.path });
              onClose();
            }}
            className={cn("w-full items-center justify-between gap-2 px-3 py-1 text-left text-[12px] hover:bg-accent", FLEX)}
          >
            <span className={cn("truncate", MIN_W_0)}>{entry.name}</span>
            {entry.kind === "dir" ? <ChevronRight aria-hidden className="size-3 shrink-0 opacity-50" /> : null}
          </button>
        ))}
        {listing.data?.truncated ? <div className="px-3 py-1 text-[10px] text-muted-foreground">{t("filesTruncated", { count: listing.data.entries.length })}</div> : null}
      </div>
    </div>
  );
}

// meiva:note
// The file picker of the side panel, like a file manager's address bar (user ruling 2026-11-21):
// `#63 › project › src › main.py`. Each folder segment drops down that folder's entries; the
// last segment is the file on show, followed by what it says about itself (size, or when the
// to-user document last changed). The full-page viewer shows the tree on the left instead.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function PathBar({ agentId, documentModifiedAt }: { agentId: number; documentModifiedAt: string | null | undefined }) {
  const t = useTranslations("toUser");
  const client = useApi();
  const now = useWallDate();
  const activeFile = useWorkspaceFileStore((s) => s.activeFile);
  const shown = activeFile?.agentId === agentId ? activeFile : null;
  const { ownerAgentId, requestPath } = resolveWorkspaceFile(shown?.path ?? "to_user.md", agentId);
  const inWorkspace = !requestPath.startsWith("/") && !requestPath.startsWith("~");
  const parts = inWorkspace ? requestPath.split("/").filter(Boolean) : [];
  const folders = parts.slice(0, -1);
  const file = useQuery({
    queryKey: ["workspace-file", ownerAgentId, requestPath],
    queryFn: ({ signal }) => client.getWorkspaceFile(ownerAgentId, requestPath, signal),
    enabled: shown !== null,
  });
  const [open, setOpen] = useState<number | null>(null);
  const barRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open === null) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !barRef.current?.contains(event.target as Node)) setOpen(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  const segment = "shrink-0 rounded px-1 py-0.5 hover:bg-accent hover:text-foreground";
  const detail = shown === null
    ? (documentModifiedAt ? t("updated", { time: formatRelative(documentModifiedAt, now) }) : null)
    : (file.data ? formatFileSize(file.data.size) : null);
  return (
    <div ref={barRef} data-testid="path-bar" className={cn("relative shrink-0 items-center gap-0.5 border-b px-2 py-1 font-mono text-[11px] text-muted-foreground", FLEX)}>
      <div className="relative">
        <button type="button" aria-haspopup="menu" aria-expanded={open === 0} onClick={() => setOpen(open === 0 ? null : 0)} className={segment}>
          #{ownerAgentId}
        </button>
        {open === 0 ? <FolderMenu agentId={agentId} start={{ workspace: ownerAgentId, path: undefined }} onClose={() => setOpen(null)} /> : null}
      </div>
      {inWorkspace ? folders.map((name, index) => (
        <div key={`${index}:${name}`} className={cn("relative items-center", FLEX)}>
          <ChevronRight aria-hidden className="size-3 shrink-0 opacity-50" />
          <button type="button" aria-haspopup="menu" aria-expanded={open === index + 1} onClick={() => setOpen(open === index + 1 ? null : index + 1)} className={segment}>
            {name}
          </button>
          {open === index + 1 ? (
            <FolderMenu agentId={agentId} start={{ workspace: ownerAgentId, path: folders.slice(0, index + 1).join("/") }} onClose={() => setOpen(null)} />
          ) : null}
        </div>
      )) : null}
      <ChevronRight aria-hidden className="size-3 shrink-0 opacity-50" />
      <span className={cn("truncate px-1 text-foreground", MIN_W_0)} title={inWorkspace ? undefined : requestPath}>
        {inWorkspace ? parts[parts.length - 1] : fileName(requestPath)}
      </span>
      {shown?.lineRange ? <span data-testid="file-line-range-badge" className="shrink-0 rounded bg-primary/20 px-1.5 text-[10px] text-primary">#L{shown.lineRange}</span> : null}
      {detail ? <span className="ml-auto shrink-0 whitespace-nowrap pl-2 font-sans text-[10px]">{detail}</span> : null}
    </div>
  );
}
