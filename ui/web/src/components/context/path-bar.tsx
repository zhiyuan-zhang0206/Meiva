"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { resolveWorkspaceFile } from "@/components/content/markdown-image-url";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Spinner } from "@/components/ui/spinner";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { useWallDate } from "@/lib/format/now/use-wall-date";
import { formatRelative } from "@/lib/format/time";
import { cn, formatFileSize } from "@/lib/format/utils";
import { BAR_DIVIDER_CLASS, FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import { ApiError } from "@/lib/transport/api";
import { useApi } from "@/lib/transport/transport-context";

interface Folder {
  machineAgent: number;
  // meiva:note
  // An absolute directory on the machine of `machineAgent`.
  path: string;
}

// meiva:note
// One height whether the bar shows the path or edits it, so clicking it does not move the file below
// (user report 2026-11-21); its rule is the panel's divider, inset at both ends like every other.
const BAR_CLASS = cn("relative h-8 shrink-0 items-center px-2", BAR_DIVIDER_CLASS, FLEX);
// meiva:note
// The drop-down's width (`w-72`): it opens under what was clicked but keeps the bar's padding from
// either end.
const MENU_WIDTH_PX = 288;
const MENU_EDGE_PX = 8;

interface Root {
  kind: "workspace" | "home" | "machine";
  path: string;
  agent_id: number | null;
}

// meiva:note
// The address bar names a root by its real path: the workspace's directory, the home directory as
// `~`, the machine's root as `/` (user ruling 2026-11-21: an agent number says nothing there).
function rootLabel(root: Root): string {
  return root.kind === "home" ? "~" : root.path;
}

// meiva:note
// The path as the reader would type it: `~/…` under the home directory, absolute otherwise.
function typedPath(root: Root, absolute: string): string {
  if (root.kind === "home" && absolute.startsWith(`${root.path}/`)) return `~/${absolute.slice(root.path.length + 1)}`;
  return absolute;
}

function below(root: Root, absolute: string): string[] {
  const base = root.path.endsWith("/") ? root.path : `${root.path}/`;
  return (absolute.startsWith(base) ? absolute.slice(base.length) : absolute).split("/").filter(Boolean);
}

// meiva:note
// One folder's entries under a segment, like an address bar's drop-down: a folder opens in place
// (with a way back up), a file opens as a tab.
function FolderMenu({ agentId, start, onClose }: { agentId: number; start: Folder; onClose: () => void }) {
  const t = useTranslations("toUser");
  const client = useApi();
  const [trail, setTrail] = useState<Folder[]>([start]);
  const folder = trail[trail.length - 1];
  const listing = useQuery({
    queryKey: ["workspace-dir", folder.machineAgent, folder.path],
    queryFn: ({ signal }) => client.listWorkspaceDir(folder.machineAgent, folder.path, signal),
    staleTime: 10_000,
  });
  return (
    <div data-testid="path-menu" role="menu" className="absolute left-0 top-full z-40 mt-1 w-72 rounded-lg border border-border bg-background py-1 font-sans shadow-lg">
      {trail.length > 1 ? (
        <button type="button" role="menuitem" onClick={() => setTrail(trail.slice(0, -1))} className={cn("w-full items-center gap-1 px-3 py-1 text-left text-[12px] text-muted-foreground hover:bg-accent", FLEX)}>
          <ChevronLeft aria-hidden className="size-3" />..
        </button>
      ) : null}
      <ScrollArea viewportClassName="max-h-72 overscroll-y-contain">
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
                setTrail([...trail, { machineAgent: folder.machineAgent, path: entry.path }]);
                return;
              }
              useWorkspaceFileStore.getState().openFile({ agentId, machineAgentId: folder.machineAgent, path: entry.path });
              onClose();
            }}
            className={cn("w-full items-center justify-between gap-2 px-3 py-1 text-left text-[12px] hover:bg-accent", FLEX)}
          >
            <span className={cn("truncate", MIN_W_0)}>{entry.name}</span>
            {entry.kind === "dir" ? <ChevronRight aria-hidden className="size-3 shrink-0 opacity-50" /> : null}
          </button>
        ))}
        {listing.data?.truncated ? <div className="px-3 py-1 text-[10px] text-muted-foreground">{t("filesTruncated", { count: listing.data.entries.length })}</div> : null}
      </ScrollArea>
    </div>
  );
}

// meiva:note
// The side panel's address bar (user rulings 2026-11-21). Its root is the workspace the file lies
// in, else the home directory (`~`), else the machine's root (`/`), each shown by its path, and the
// path reads as one (`/`, not arrows, between its parts: user ruling 2026-11-21 03:09). Each folder
// drops down its entries (the last, the file's neighbours); clicking the bar's empty end turns it
// into a text field holding the path, where Enter opens what was typed (a folder opens its
// drop-down, a file opens as a tab) and Escape gives up. The full-page viewer shows the tree on the
// left instead.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function PathBar({ agentId, documentModifiedAt, ongoing = false }: { agentId: number; documentModifiedAt: string | null | undefined; ongoing?: boolean }) {
  const t = useTranslations("toUser");
  const client = useApi();
  const now = useWallDate();
  const activeFile = useWorkspaceFileStore((s) => s.activeFile);
  const shown = activeFile?.agentId === agentId ? activeFile : null;
  const { ownerAgentId, requestPath } = resolveWorkspaceFile(shown?.path ?? "to_user.md", shown?.machineAgentId ?? agentId, { preserveExplicit: true });
  const file = useQuery({
    queryKey: ["workspace-file", ownerAgentId, requestPath],
    queryFn: ({ signal }) => client.getWorkspaceFile(ownerAgentId, requestPath, signal),
  });
  const root: Root | null = file.data?.root ?? null;
  const absolute = file.data?.path ?? null;
  // meiva:note
  // Until the gateway names the root (or when the path does not resolve), the bar roots the path as
  // it was written: `~/…` at `~`, an absolute path at `/`, a workspace-relative one at no root.
  const writtenRoot = requestPath.startsWith("~") ? "~" : requestPath.startsWith("/") ? "/" : null;
  const parts = root && absolute ? below(root, absolute) : requestPath.replace(/^~/, "").split("/").filter(Boolean);
  const folders = parts.slice(0, -1);
  const folderPath = (count: number) => (root ? `${root.path.replace(/\/$/, "")}/${folders.slice(0, count).join("/")}` : null);

  const [open, setOpen] = useState<{ start: Folder; at: number; left: number } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // meiva:note
  // The field takes the caret as it appears, with the path selected, as an address bar does.
  const isEditing = editing !== null;
  useEffect(() => {
    if (isEditing) inputRef.current?.select();
  }, [isEditing]);
  useEffect(() => {
    if (open === null) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      const outside = event.target instanceof Node && !barRef.current?.contains(event.target);
      if (event instanceof KeyboardEvent ? event.key === "Escape" : outside) setOpen(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const go = async (typed: string) => {
    const text = typed.trim();
    if (!text) return;
    setEditing(null);
    // meiva:note
    // What is not a folder (the listing's 404) opens as a file, whose view reports whether it
    // exists; any other refusal opens the folder's drop-down, which reports it.
    try {
      const listing = await client.listWorkspaceDir(ownerAgentId, text);
      setOpen({ start: { machineAgent: ownerAgentId, path: listing.path }, at: -1, left: MENU_EDGE_PX });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        useWorkspaceFileStore.getState().openFile({ agentId, machineAgentId: ownerAgentId, path: text });
      } else {
        setOpen({ start: { machineAgent: ownerAgentId, path: text }, at: -1, left: MENU_EDGE_PX });
      }
    }
  };

  const segment = "shrink-0 rounded px-0.5 py-0.5 hover:bg-accent hover:text-foreground";
  const detail = shown === null || ongoing
    ? (documentModifiedAt ? t("updated", { time: formatRelative(documentModifiedAt, now) }) : null)
    : (file.data ? formatFileSize(file.data.size) : null);
  const toggle = (at: number, path: string | null, anchor: HTMLElement) => {
    if (path === null) return;
    const bar = barRef.current?.getBoundingClientRect();
    const left = bar ? Math.max(MENU_EDGE_PX, Math.min(anchor.getBoundingClientRect().left - bar.left, bar.width - MENU_WIDTH_PX - MENU_EDGE_PX)) : MENU_EDGE_PX;
    setOpen(open?.at === at ? null : { start: { machineAgent: ownerAgentId, path }, at, left });
  };

  if (editing !== null) {
    return (
      <div ref={barRef} data-testid="path-bar" className={BAR_CLASS}>
        <input
          ref={inputRef}
          aria-label={t("filesPath")}
          value={editing}
          onChange={(event) => setEditing(event.target.value)}
          onBlur={() => setEditing(null)}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            if (event.key === "Enter") { event.preventDefault(); void go(editing); }
            else if (event.key === "Escape") { event.preventDefault(); setEditing(null); }
          }}
          className="h-6 w-full rounded border border-input bg-transparent px-[3px] font-mono text-[11px] text-foreground outline-none focus:border-ring"
        />
      </div>
    );
  }

  const rootName = root ? rootLabel(root) : writtenRoot;
  const slash = <span aria-hidden className="shrink-0 opacity-50">/</span>;
  const segmentClass = (at: number) => cn(segment, open?.at === at && "bg-accent text-foreground");

  const crumbs = (
    <>
      {rootName === null ? null : (
        <>
          <button type="button" dir="rtl" aria-haspopup="menu" aria-expanded={open?.at === 0} onClick={(event) => toggle(0, root?.path ?? null, event.currentTarget)} className={cn(segmentClass(0), "max-w-56 shrink truncate", MIN_W_0)}>
            <bdi>{rootName}</bdi>
          </button>
          {rootName.endsWith("/") ? null : slash}
        </>
      )}
      {folders.map((name, index) => (
        // meiva:note
        // Folder segments give up width before the file name does: a deep path in a narrow panel
        // truncates its folders rather than overflowing the bar.
        <div key={`${index}:${name}`} className={cn("items-center", MIN_W_0, FLEX)}>
          <button type="button" aria-haspopup="menu" aria-expanded={open?.at === index + 1} onClick={(event) => toggle(index + 1, folderPath(index + 1), event.currentTarget)} className={cn(segmentClass(index + 1), "min-w-[2ch] max-w-32 truncate")}>
            {name}
          </button>
          {slash}
        </div>
      ))}
      <span className={cn("shrink-0 truncate px-0.5 text-foreground", MIN_W_0)}>{parts[parts.length - 1]}</span>
      {shown?.lineRange ? <span data-testid="file-line-range-badge" className="shrink-0 rounded bg-primary/20 px-1.5 text-[10px] text-primary">#L{shown.lineRange}</span> : null}
    </>
  );

  // meiva:note
  // The path appears once the gateway has answered, so it is never drawn from what was asked and
  // then redrawn from the real root. A long root keeps its end (`…/workspaces/63`) when the panel
  // is narrow: it is laid out right to left, with the path itself isolated so it reads left to right.
  return (
    <div ref={barRef} data-testid="path-bar" className={cn(BAR_CLASS, "font-mono text-[11px] text-muted-foreground", MIN_W_0)}>
      {file.isPending ? null : crumbs}
      <button
        type="button"
        aria-label={t("filesEditPath")}
        onClick={() => setEditing(root && absolute ? typedPath(root, absolute) : requestPath)}
        className={cn("h-5 cursor-text self-stretch", MIN_W_0, FLEX_1)}
      />
      {open ? (
        <div className="absolute top-full" style={{ left: open.left }}>
          <FolderMenu key={`${open.at}:${open.start.path}`} agentId={agentId} start={open.start} onClose={() => setOpen(null)} />
        </div>
      ) : null}
      {detail ? <span className="shrink-0 whitespace-nowrap pl-2 font-sans text-[10px]">{detail}</span> : null}
    </div>
  );
}
