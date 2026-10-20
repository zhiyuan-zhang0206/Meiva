"use client";

// meiva:note
// Copy is the only hover-revealed action rendered inside a MessageCard's bottom-right corner (see MessageCard's `actions` prop in ./card): it attaches to every expanded, non-partial card. The shared backdrop / hover-reveal / opacity transition lives on MessageCard — this component renders only the button content, so it stays a simple inline-light control (font-mono, muted, hover-tint).
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
export function CopyButton({ text }: { text: string }) {
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
      className={cn(ACTION_BTN_CLS, copied && "text-emerald-500")}
      aria-label={copied ? t("copied") : t("copyMessage")}
    >
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
    </button>
  );
}
