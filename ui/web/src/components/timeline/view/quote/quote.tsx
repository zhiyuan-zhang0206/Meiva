"use client";

import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";
import { messageCardConfig } from "../../card";
import { useBrowser } from "@/lib/browser/browser-provider";
import type { BackendTimelineItem, Quote } from "@/lib/contracts/types";

export const QuoteTarget = createContext<{ itemId: string; token: number } | null>(null);
export const QuoteNavigation = createContext<((quote: Quote) => void) | null>(null);

export const QUOTE_POPOVER_ESTIMATED_WIDTH = 148;
export const QUOTE_POPOVER_ESTIMATED_HEIGHT = 40;

export function QuoteCard({ quote }: { quote: Quote }) {
  const navigate = useContext(QuoteNavigation);
  const t = useTranslations("timeline");
  return (
    <div data-quote-exclude className="mb-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
      <button type="button" className="text-left text-xs underline-offset-2 hover:underline" onClick={() => navigate?.(quote)}>{t("quoteSource", { author: quote.author })}</button>
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{quote.text}</p>
    </div>
  );
}

function parentElement(node: Node): Element | null {
  return node.nodeType === 1 ? node as Element : node.parentElement;
}

// meiva:note
// Endpoints must share one rendered body; chrome, inputs and multi-row ranges never qualify.
export function quoteFromRange(range: Range, scope: Element, items: readonly BackendTimelineItem[], agentId: number): Quote | null {
  const start = parentElement(range.startContainer);
  const end = parentElement(range.endContainer);
  if (!start || !end) return null;
  const body = start.closest("[data-quote-body]");
  if (!body || !scope.contains(body) || body !== end.closest("[data-quote-body]")) return null;
  if (start.closest("input,textarea,button,[role=button],[contenteditable],[data-quote-exclude]") || end.closest("input,textarea,button,[role=button],[contenteditable],[data-quote-exclude]")) return null;
  const row = body.closest(".timeline-item[data-item-id]");
  const item = items.find((candidate) => candidate.item_id === row?.getAttribute("data-item-id"));
  const text = range.toString();
  if (!item || item.partial || !text.trim()) return null;
  if (!["inbound_chat", "agent_chat", "agent_code", "agent_reasoning", "code_output", "system_marker"].includes(item.kind)) return null;
  // meiva:note
  // The conversation's own agent is named without its number: a quote can only come from the
  // conversation it is sent in, so the number says nothing (user report 2026-11-21).
  return {
    source_agent_id: agentId, source_item_id: item.item_id,
    author: item.kind === "inbound_chat" ? (messageCardConfig(item)?.title ?? item.source ?? "User") : "Agent",
    created_at: item.created_at ?? "", text,
    source_message_id: item.source_message_id, source_inbound_id: item.source_inbound_id ?? item.inbound_id,
    source_block_idx: item.source_block_idx,
  };
}

export type MeasurableRange = Pick<Range, "getBoundingClientRect"> &
  Partial<Pick<Range, "getClientRects">>;

// meiva:note
// Uses the last non-empty client rect so both single-line and multi-line selections anchor at the bottom-right corner of the last selected character, falling back to getBoundingClientRect when client rects are unavailable.
export function quoteAnchorRect(range: MeasurableRange): DOMRect {
  const rects = range.getClientRects
    ? Array.from(range.getClientRects()).filter((rect) => rect.width > 0 || rect.height > 0)
    : [];
  return rects.at(-1) ?? range.getBoundingClientRect();
}

export function clampQuotePopoverPosition({
  anchorX,
  anchorY,
  outerWidth,
  outerHeight,
  popWidth = QUOTE_POPOVER_ESTIMATED_WIDTH,
  popHeight = QUOTE_POPOVER_ESTIMATED_HEIGHT,
}: {
  anchorX: number;
  anchorY: number;
  outerWidth: number;
  outerHeight: number;
  popWidth?: number;
  popHeight?: number;
}): { left: number; top: number } {
  return {
    left: Math.max(8, Math.min(anchorX + 4, outerWidth - popWidth - 8)),
    top: Math.max(4, Math.min(anchorY + 6, outerHeight - popHeight - 6)),
  };
}

interface QuoteCandidate {
  quote: Quote;
  anchorX: number;
  anchorY: number;
  outerWidth: number;
  outerHeight: number;
}

export function QuoteSelection({ scope, items, agentId, onQuote }: {
  scope: RefObject<HTMLDivElement | null>; items: readonly BackendTimelineItem[];
  agentId: number; onQuote?: (quote: Quote) => void;
}) {
  const browser = useBrowser();
  const t = useTranslations("timeline");
  const popoverRef = useRef<HTMLDivElement>(null);
  const [candidate, setCandidate] = useState<QuoteCandidate | null>(null);
  useEffect(() => {
    const root = scope.current;
    if (!root || !onQuote) return;
    const changed = () => {
      const range = browser.selection.read(root);
      const quote = range ? quoteFromRange(range, root, items, agentId) : null;
      if (!range || !quote) { setCandidate(null); return; }
      const endRect = quoteAnchorRect(range);
      const outer = root.getBoundingClientRect();
      setCandidate({
        quote,
        anchorX: endRect.right - outer.left,
        anchorY: endRect.bottom - outer.top,
        outerWidth: outer.width,
        outerHeight: outer.height,
      });
    };
    return browser.selection.subscribe(root, changed);
  }, [browser, scope, items, agentId, onQuote]);
  useLayoutEffect(() => {
    const popover = popoverRef.current;
    const root = scope.current;
    if (!candidate || !popover || !root) return;
    if (popover.offsetWidth <= 0 && popover.offsetHeight <= 0) return;
    const outer = root.getBoundingClientRect();
    const clamped = clampQuotePopoverPosition({
      anchorX: candidate.anchorX,
      anchorY: candidate.anchorY,
      outerWidth: outer.width > 0 ? outer.width : candidate.outerWidth,
      outerHeight: outer.height > 0 ? outer.height : candidate.outerHeight,
      popWidth: popover.offsetWidth > 0 ? popover.offsetWidth : QUOTE_POPOVER_ESTIMATED_WIDTH,
      popHeight: popover.offsetHeight > 0 ? popover.offsetHeight : QUOTE_POPOVER_ESTIMATED_HEIGHT,
    });
    popover.style.left = `${clamped.left}px`;
    popover.style.top = `${clamped.top}px`;
  }, [candidate, scope]);
  if (!candidate || !onQuote) return null;
  const initialPos = clampQuotePopoverPosition({
    anchorX: candidate.anchorX,
    anchorY: candidate.anchorY,
    outerWidth: candidate.outerWidth,
    outerHeight: candidate.outerHeight,
  });
  const tooLong = Array.from(candidate.quote.text).length > 4000;
  return (
    <div
      ref={popoverRef}
      data-testid="quote-selection-popover"
      className="absolute z-40 rounded-md border border-border bg-background px-3 py-2 text-sm shadow-md"
      style={{ top: initialPos.top, left: initialPos.left }}
    >
      {tooLong ? <span role="status">{t("quoteTooLong")}</span> : <button type="button" onPointerDown={(event) => event.preventDefault()} onClick={() => { onQuote(candidate.quote); setCandidate(null); }}>{t("quoteSelection")}</button>}
    </div>
  );
}
