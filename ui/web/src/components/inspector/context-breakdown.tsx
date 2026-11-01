"use client";

import { X } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";

import { ContextMeter } from "@/components/inspector/context-meter";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

// The live meter stays in the initial bundle; breakdown content loads on expand.
const ContextBreakdownBody = dynamic(
  () => import("./context-breakdown-body").then((module) => module.ContextBreakdownBody),
  { loading: () => <div className="h-4" aria-busy="true" /> },
);

export interface ContextButtonProps {
  /** The active agent, or null. Without one there is nothing to break down, so
   * the readout renders non-interactively. */
  agentId: number | null;
  // meiva:note
  // Controlled expansion state owned by InspectorPanel.
  // meiva:link ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md
  open: boolean;
  onOpenChange: (open: boolean) => void;
  contextTokens: number;
  maxContextTokens: number;
  softCompactTokens: number;
  hardCompactTokens: number;
  showText?: boolean;
}

const READOUT_TEXT_CLASS = "hidden sm:inline";

// meiva:note
// The inspector's context readout as a button: the inline `ContextMeter` gauge
// that, when clicked, expands the breakdown popover in place (lazy-loaded on
// expand). Escape, an outside click, the close button, or clicking the meter
// again collapses it.
// meiva:link ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md
export function ContextButton(props: ContextButtonProps) {
  const { agentId, open, onOpenChange, ...meter } = props;
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const t = useTranslations("contextBreakdown");

  // meiva:note
  // Escape / outside pointer-down collapse the panel. Document-level listeners
  // exist only while open; the closing outside click still lands on its target
  // (non-modal — nothing is swallowed).
  // meiva:link ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) onOpenChange(false);
    };
    const onPointerDown = (e: PointerEvent) => {
      // meiva:note
      // The listener sits on the document, so the target is whatever node was pressed: a target that
      // is not a Node belongs to no element and cannot be inside either box.
      // meiva:link ui/web/src/docs/frontend-components/context-breakdown.meiva.okf.md
      const target = e.target;
      if (!(target instanceof Node)) return;
      if (panelRef.current?.contains(target) || buttonRef.current?.contains(target)) return;
      onOpenChange(false);
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open, onOpenChange]);

  if (agentId == null) {
    return <ContextMeter {...meter} textClassName={READOUT_TEXT_CLASS} />;
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        data-testid="context-meter-button"
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        className={cn("rounded-sm text-left hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring", MIN_W_0)}
      >
        <ContextMeter {...meter} textClassName={READOUT_TEXT_CLASS} />
      </button>
      {open ? (
        // bottom-full anchors the panel's bottom to the meta row's top, so it
        // can only occupy space *above* the composer (over the timeline). The
        // max-height + internal scroll keep a tall breakdown inside the
        // viewport instead of growing past it. Width is shrink-to-fit
        // (no `w-*`): the panel sizes to its content — min-w-80 is a floor so
        // a sparse breakdown isn't a sliver, max-w-[...] a ceiling so one
        // pathologically long category/section name can't push the panel
        // past the viewport. Inside that range nothing truncates; only past
        // the ceiling do the `truncate` rows below ellipsis.
        <div
          ref={panelRef}
          role="region"
          aria-label={t("title")}
          data-testid="context-breakdown-panel"
          // overflow-x-hidden is load-bearing, not decorative: per the CSS
          // overflow spec, an element with overflow-y other than "visible"
          // has its overflow-x computed value forced from "visible" to
          // "auto" too — the two axes can't be set independently that way —
          // so overflow-y-auto alone silently made this panel horizontally
          // scrollable once content ran past the max-width ceiling, instead
          // of the intended pure vertical scroll with only the ceiling-
          // exceeding rows truncating in place.
          //
          // whitespace-normal resets an inherited `white-space: nowrap`: the
          // composer's meta row wraps this panel in a `truncate` span (for
          // the *collapsed* meter's own text) — nowrap is an inherited
          // property, so without this reset every paragraph in here that
          // isn't itself `truncate` (the summary line, the description)
          // would refuse to wrap and get silently clipped by overflow-x-
          // hidden instead of flowing across lines like the rest of the
          // panel.
          className={cn(
            "absolute left-0 z-50 min-w-80 max-w-[min(28rem,90vw)] max-h-[50vh] overflow-x-hidden overflow-y-auto whitespace-normal rounded-md border border-border bg-popover p-2.5 text-popover-foreground dark:text-[#ece9e2] shadow-md",
            "bottom-full mb-2",
          )}
        >
          <div className={cn("mb-2 items-start justify-between gap-2", FLEX)}>
            <p className="text-[12.5px] font-semibold text-foreground dark:text-[#f2efe9]">{t("title")}</p>
            <button
              type="button"
              data-testid="context-breakdown-close"
              aria-label={t("close")}
              onClick={() => onOpenChange(false)}
              className="shrink-0 rounded-sm p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <X className="size-3.5" />
            </button>
          </div>
          <ContextBreakdownBody agentId={agentId} />
        </div>
      ) : null}
    </>
  );
}
