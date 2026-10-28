"use client";

import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/format/utils";
import { useBrowser } from "@/lib/browser/browser-provider";

interface Props {
  text: string;
  /** Label for accessibility — what's being copied, e.g. "code" or "command output". */
  label?: string;
  /** When true, the code block is still streaming — pin button to top-right
   * so it stays accessible while the tail scrolls. When false (default),
   * pin to bottom-right — consistent with completed blocks. */
  streaming?: boolean;
}

// meiva:note
// A small copy-to-clipboard button for code and command blocks. The copy goes through the browser
// port (`browser.clipboard`: the Clipboard API, with the `execCommand` fallback for non-secure
// contexts behind it), so a test asserts on the answer the page gave rather than stubbing
// `navigator.clipboard`. On success the button briefly shows a Check icon and a "Copied!" tooltip
// before resetting. It is positioned inside a `group` container that reveals it on hover.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
export function CopyButton({ text, label = "code", streaming = false }: Props) {
  const t = useTranslations("common");
  const browser = useBrowser();
  const [copied, setCopied] = useState(false);

  const timerRef = useRef<number | null>(null);
  const handleCopy = useCallback(async () => {
    const ok = await browser.clipboard.write(text);
    if (ok) {
      setCopied(true);
      // One reset timer at a time, cleared on unmount (same pattern as
      // timeline/buttons.tsx — Task #1051).
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => {
        setCopied(false);
        timerRef.current = null;
      }, 2000);
    }
  }, [browser, text]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  return (
    <Button
      variant="ghost"
      size="icon-sm"
      onClick={handleCopy}
      aria-label={copied ? t("copied") : `${t("copy")} ${label}`}
      className={cn(
        "absolute right-1.5 z-10",
        streaming ? "top-1.5" : "bottom-1.5",
        "bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground",
        "opacity-0 group-hover:opacity-100 transition-opacity",
        "focus-visible:opacity-100",
        "border border-border/50"
      )}
    >
      {copied ? (
        <Check className="size-3.5 text-emerald-500" />
      ) : (
        <Copy className="size-3.5" />
      )}
    </Button>
  );
}
