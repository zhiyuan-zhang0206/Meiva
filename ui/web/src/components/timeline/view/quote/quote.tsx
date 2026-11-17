"use client";

import { createContext, useContext, useEffect, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";
import { messageCardConfig } from "../../card";
import { useBrowser } from "@/lib/browser/browser-provider";
import type { Quote } from "@/lib/contracts/types";
import type { BackendTimelineItem } from "@/lib/contracts/types";

export const QuoteTarget = createContext<{ itemId: string; token: number } | null>(null);
export const QuoteNavigation = createContext<((quote: Quote) => void) | null>(null);

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
  return {
    source_agent_id: agentId, source_item_id: item.item_id,
    author: item.kind === "inbound_chat" ? (messageCardConfig(item)?.title ?? item.source ?? "User") : `Agent #${agentId}`,
    created_at: item.created_at ?? "", text,
    source_message_id: item.source_message_id, source_inbound_id: item.source_inbound_id ?? item.inbound_id,
    source_block_idx: item.source_block_idx,
  };
}

export function QuoteSelection({ scope, items, agentId, onQuote }: {
  scope: RefObject<HTMLDivElement | null>; items: readonly BackendTimelineItem[];
  agentId: number; onQuote?: (quote: Quote) => void;
}) {
  const browser = useBrowser();
  const t = useTranslations("timeline");
  const [candidate, setCandidate] = useState<{ quote: Quote; top: number; left: number } | null>(null);
  useEffect(() => {
    const root = scope.current;
    if (!root || !onQuote) return;
    const changed = () => {
      const range = browser.selection.read(root);
      const quote = range ? quoteFromRange(range, root, items, agentId) : null;
      if (!range || !quote) { setCandidate(null); return; }
      const rect = range.getBoundingClientRect();
      const outer = root.getBoundingClientRect();
      setCandidate({ quote, top: Math.max(4, Math.min(rect.bottom - outer.top + 8, outer.height - 48)), left: Math.max(8, Math.min(rect.left - outer.left, outer.width - 180)) });
    };
    return browser.selection.subscribe(root, changed);
  }, [browser, scope, items, agentId, onQuote]);
  if (!candidate) return null;
  const tooLong = Array.from(candidate.quote.text).length > 4000;
  return (
    <div className="absolute z-40 rounded-md border border-border bg-background px-3 py-2 text-sm shadow-md" style={{ top: candidate.top, left: candidate.left }}>
      {tooLong ? <span role="status">{t("quoteTooLong")}</span> : <button type="button" onPointerDown={(event) => event.preventDefault()} onClick={() => { onQuote?.(candidate.quote); setCandidate(null); }}>{t("quoteSelection")}</button>}
    </div>
  );
}
