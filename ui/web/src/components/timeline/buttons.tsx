"use client";

// meiva:note
// Copy is the only hover-revealed action in a block's chrome band, which `TimelineRow` places in the gap under the block (see CARD_FOOTER_CLS in ./card): it attaches to every expanded, non-partial card. The hover-reveal / opacity transition lives on that band — this component renders only the button, so it stays a simple inline-light control (font-mono, muted, hover-tint). `compact` drops the padding so the control fits the 12px band and keeps its hit area as an invisible pseudo-element.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md

import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/format/utils";
import { copyToClipboard } from "@/lib/format/clipboard";

const ACTION_BTN_CLS = cn(
  "inline-flex items-center p-0.5 rounded",
  "text-muted-foreground/60 hover:text-foreground transition-colors",
  "hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed",
);

// Copy a card's content to the clipboard. After click, the icon flips to the
// check icon for 1s as feedback, then reverts. The timer ref lets "rapid
// repeat clicks" reset (a new click refreshes the 1s timer). Copy itself
// (incl. the execCommand fallback for non-secure contexts) lives in the
// shared copyToClipboard helper.
export function CopyButton({ text, compact = false }: { text: string; compact?: boolean }) {
  const t = useTranslations("timeline");
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  const onClick = useCallback(async () => {
    const ok = await copyToClipboard(text);
    if (!ok) {
      return;
    }
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      setCopied(false);
      timerRef.current = null;
    }, 1000);
  }, [text]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        ACTION_BTN_CLS,
        // meiva:note
        // The band under a block is 12px tall, so the card-chrome Copy drops its
        // padding and grows an invisible hit area instead of a painted one — a
        // 16px button would overlap the card's bottom edge it now sits below.
        // meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
        compact && "relative p-0 after:absolute after:-inset-1 after:content-['']",
        copied && "text-emerald-500",
      )}
      aria-label={copied ? t("copied") : t("copyMessage")}
    >
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
    </button>
  );
}
