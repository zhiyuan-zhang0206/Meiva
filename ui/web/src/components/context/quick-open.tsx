"use client";

import { useQuery } from "@tanstack/react-query";
import { CornerDownLeft, FileText } from "lucide-react";
import { useTranslations } from "next-intl";
import { useDeferredValue, useEffect, useId, useMemo, useRef, useState } from "react";

import { Spinner } from "@/components/ui/spinner";
import { useWorkspaceFileStore, type WorkspaceFileTarget } from "@/lib/context/workspace-file-store";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { useApi } from "@/lib/transport/transport-context";

import { fileName } from "./file-tabs";

const SEARCH_LIMIT = 30;

interface Choice {
  key: string;
  label: string;
  detail: string;
  target: WorkspaceFileTarget;
}

// meiva:note
// Something typed is a path to open as it is when it reads as one (a `/`, `~` or `.` start, or a
// directory separator); anything else only searches file names.
function asPath(query: string): string | null {
  const text = query.trim();
  if (!text) return null;
  return /^[~/.]/.test(text) || text.includes("/") ? text : null;
}

// meiva:note
// Quick open over the panel: empty, it lists the recently opened files; typed, it searches file
// names in the workspace the tree shows and offers to open the typed text as a path. Arrow keys
// choose, Enter opens, Escape closes.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function QuickOpen({ agentId, workspaceAgentId, onClose }: {
  agentId: number;
  workspaceAgentId: number;
  onClose: () => void;
}) {
  const t = useTranslations("toUser");
  const client = useApi();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const deferred = useDeferredValue(query.trim());
  const recent = useWorkspaceFileStore((s) => s.recent);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);

  const search = useQuery({
    queryKey: ["workspace-file-search", workspaceAgentId, deferred],
    queryFn: ({ signal }) => client.searchWorkspaceFiles(workspaceAgentId, deferred, SEARCH_LIMIT, signal),
    enabled: deferred.length > 0,
    staleTime: 10_000,
  });

  const choices = useMemo<Choice[]>(() => {
    if (!query.trim()) {
      return recent.map((file) => ({ key: `${file.agentId}:${file.path}`, label: fileName(file.path), detail: file.path, target: { ...file, agentId } }));
    }
    const typed = asPath(query);
    const found = (search.data?.results ?? []).map((result) => ({
      key: result.path,
      label: result.name,
      detail: result.relative,
      target: { agentId, path: result.path },
    }));
    return typed ? [{ key: `path:${typed}`, label: t("filesOpenPath", { path: typed }), detail: "", target: { agentId, path: typed } }, ...found] : found;
  }, [agentId, query, recent, search.data, t]);

  const open = (choice: Choice | undefined) => {
    if (!choice) return;
    useWorkspaceFileStore.getState().openFile(choice.target);
    onClose();
  };

  return (
    <div data-testid="quick-open" className="absolute inset-x-2 top-10 z-40 rounded-lg border border-border bg-background shadow-lg">
      <input
        ref={inputRef}
        role="combobox"
        aria-expanded
        aria-controls={listId}
        aria-activedescendant={choices[selected] ? `${listId}-${selected}` : undefined}
        aria-label={t("filesOpen")}
        placeholder={t("filesSearchPlaceholder")}
        value={query}
        onChange={(event) => { setQuery(event.target.value); setSelected(0); }}
        onBlur={(event) => { if (!event.currentTarget.parentElement?.contains(event.relatedTarget)) onClose(); }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (event.key === "Escape") { event.preventDefault(); onClose(); }
          else if (event.key === "ArrowDown") { event.preventDefault(); setSelected((i) => Math.min(i + 1, Math.max(0, choices.length - 1))); }
          else if (event.key === "ArrowUp") { event.preventDefault(); setSelected((i) => Math.max(0, i - 1)); }
          else if (event.key === "Enter") { event.preventDefault(); open(choices[selected]); }
        }}
        className="w-full rounded-t-lg border-b bg-transparent px-3 py-2 text-[13px] outline-none placeholder:text-muted-foreground/60"
      />
      <div className="px-3 pt-2 pb-1 text-[10px] text-muted-foreground">
        {query.trim() ? null : t("filesRecent")}
      </div>
      <ul id={listId} role="listbox" aria-label={t("filesOpen")} className="max-h-72 overflow-y-auto pb-1">
        {choices.map((choice, index) => (
          <li
            key={choice.key}
            id={`${listId}-${index}`}
            role="option"
            aria-selected={index === selected}
            onMouseDown={(event) => { event.preventDefault(); open(choice); }}
            onMouseEnter={() => setSelected(index)}
            className={cn("mx-1 cursor-pointer items-center gap-2 rounded px-2 py-1 text-[12px]", index === selected && "bg-accent", FLEX)}
          >
            {choice.detail ? <FileText aria-hidden className="size-3.5 shrink-0 opacity-60" /> : <CornerDownLeft aria-hidden className="size-3.5 shrink-0 opacity-60" />}
            <span className="shrink-0">{choice.label}</span>
            {choice.detail ? <span className={cn("truncate text-[11px] text-muted-foreground", MIN_W_0)}>{choice.detail}</span> : null}
          </li>
        ))}
        {query.trim() && search.isFetching && !search.data ? (
          <li className={cn("items-center gap-2 px-3 py-1.5 text-[11px] text-muted-foreground", FLEX)}>
            <Spinner className="size-3" label={t("filesSearching")} />{t("filesSearching")}
          </li>
        ) : null}
        {query.trim() && search.data && choices.length === 0 ? (
          <li className="px-3 py-1.5 text-[11px] text-muted-foreground">{t("filesNoMatches")}</li>
        ) : null}
      </ul>
    </div>
  );
}
