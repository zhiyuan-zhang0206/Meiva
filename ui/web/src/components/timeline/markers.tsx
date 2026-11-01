"use client";

import { GitFork, Info, NotebookText, PowerOff, RotateCw, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";

import { ChatMarkdown } from "@/components/content/markdown";
import { cn } from "@/lib/format/utils";

import { isSilentEphemeralPayload, type LifecycleTag, type MarkerClass } from "./model/marker-classify";


export interface MarkerVisual {
  readonly icon: LucideIcon;
  readonly title: string;
  readonly titleKey?: string;
}

const LIFECYCLE_META: Record<
  LifecycleTag,
  { icon: LucideIcon; title: string; titleKey: string }
> = {
  lifecycle_terminate: { icon: PowerOff, title: "Terminated", titleKey: "terminated" },
  lifecycle_restart: { icon: RotateCw, title: "Restarted", titleKey: "restarted" },
  lifecycle_resurrect: { icon: RotateCw, title: "Resurrected", titleKey: "resurrected" },
  lifecycle_fork: { icon: GitFork, title: "Forked", titleKey: "forked" },
};

const MEMORY_META: MarkerVisual = { icon: NotebookText, title: "Memory", titleKey: "memory" };
const NOTE_META: MarkerVisual = { icon: Info, title: "Note", titleKey: "note" };

export function markerVisual(cls: MarkerClass): MarkerVisual {
  switch (cls.kind) {
    case "lifecycle":
      return LIFECYCLE_META[cls.tag];
    case "memory":
      return MEMORY_META;
    case "note":
      return NOTE_META;
    case "ephemeral":
      throw new Error("markerVisual called on an ephemeral marker");
  }
}

export function MarkerBody({ payload, markdown = false }: { payload: string; markdown?: boolean }) {
  if (markdown) {
    return (
      <div className="text-[12px] leading-relaxed">
        <ChatMarkdown content={payload} />
      </div>
    );
  }
  return (
    <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed m-0">{payload}</pre>
  );
}

function UnknownMarkerChip({ source, payload }: { source: string | null; payload: string }) {
  const t = useTranslations("markers");
  console.warn("[timeline] unrecognized system_marker, frontend not adapted", { source, payload });
  return (
    <div
      data-testid="marker-unrecognized"
      className="border-l-2 border-destructive/60 bg-destructive/10 text-destructive rounded-r-sm px-3 py-2"
    >
      <div className="text-[11px] font-mono uppercase tracking-widest mb-1">
        {t("unrecognized")}
      </div>
      <div className="text-[11px] font-mono opacity-70 mb-1">
        source = {source === null ? "null" : JSON.stringify(source)}
      </div>
      <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed m-0">{payload}</pre>
    </div>
  );
}

function EphemeralMarker({ label, isError = false }: { label: string; isError?: boolean }) {
  return (
    <div
      data-testid={isError ? "marker-error" : undefined}
      className={cn("font-mono text-xs", isError ? "text-destructive" : "text-muted-foreground")}
    >
      {label}
    </div>
  );
}

// meiva:link ui/web/src/docs/frontend-components/timeline-view/markers.meiva.okf.md
export function EphemeralSystemMarker({
  source,
  payload,
}: {
  source: string | null;
  payload: string;
}) {
  const t = useTranslations("markers");
  if (isSilentEphemeralPayload(payload)) return null;
  if (payload.startsWith("error:")) {
    const content = payload.slice("error:".length);
    return <EphemeralMarker label={t("errorPrefix", { content })} isError />;
  }
  return <UnknownMarkerChip source={source} payload={payload} />;
}
