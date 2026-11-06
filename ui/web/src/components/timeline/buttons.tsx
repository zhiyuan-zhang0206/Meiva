"use client";

import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/format/utils";
import { useBrowser } from "@/lib/browser/browser-provider";

const ACTION_BTN_CLS = cn(
  "relative inline-flex items-center rounded after:absolute after:-inset-1 after:content-['']",
  "text-muted-foreground/60 hover:text-foreground transition-colors",
  "hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed",
);

// meiva:note
// Copy a card's content to the clipboard. After the click the icon flips to the check icon for 1s
// as feedback, then reverts; the timer ref lets "rapid repeat clicks" reset (a new click refreshes
// the 1s timer). The copy itself is the browser port's clipboard, which is where the execCommand
// fallback for non-secure contexts lives.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
export function CopyButton({ text }: { text: string }) {
  const t = useTranslations("timeline");
  const browser = useBrowser();
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  const onClick = useCallback(async () => {
    const ok = await browser.clipboard.write(text);
    if (!ok) {
      return;
    }
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      setCopied(false);
      timerRef.current = null;
    }, 1000);
  }, [browser, text]);

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
        copied && "text-emerald-500",
      )}
      aria-label={copied ? t("copied") : t("copyMessage")}
    >
      {copied ? <Check className="size-2.5" /> : <Copy className="size-2.5" />}
    </button>
  );
}
