"use client";

import { ArrowUp, Loader2, Square } from "lucide-react";

import { cn } from "@/lib/format/utils";
import { FLEX, ICON_SIZE_CLASS } from "@/lib/layout/layout";

// The circular send affordance shared by every reply surface (the home
// composer and the fleet "Waiting on you" answer box), so they never drift
// apart visually. State picks the glyph:
//  - "send"    → ArrowUp (submit)
//  - "sending" → spinner (in-flight, button disabled by the caller)
//  - "stop"    → filled square (cancel the current turn; composer only)
export type SendButtonState = "send" | "sending" | "stop";

interface SendButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  state: SendButtonState;
}

// meiva:note
// The disc is 32px — one step below the 36px control row it sits in: the filled
// circle carries more visual weight than the bare upload glyph beside it, so at
// equal size it read as the larger control (user ruling 2026-10-29). The button
// keeps the row's 36px box (a 36x36 click target, well above the 24x24 floor) and
// shrinks only its paint: p-0.5 clips the background to the 32px content box,
// which also keeps the glyph centred on the upload icon's axis instead of hanging
// lower on a bottom-aligned smaller button.
// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
export function SendButton({ state, className, ...props }: SendButtonProps) {
  return (
    <button
      type="button"
      className={cn(
        "size-9 p-0.5 bg-clip-content shrink-0 rounded-full items-center justify-center transition",
        // disabled:pointer-events-none is more reliable than relying on
        // disabled:hover: variant source-order — it eats hover directly so
        // hover:bg-primary/90 can't apply while disabled.
        "bg-primary text-primary-foreground hover:bg-primary/90 active:scale-95",
        "disabled:opacity-40 disabled:cursor-not-allowed disabled:pointer-events-none",
        className,
        FLEX
      )}
      {...props}
    >
      {state === "sending" ? (
        <Loader2 className={cn(ICON_SIZE_CLASS, "animate-spin")} strokeWidth={2.5} />
      ) : state === "stop" ? (
        // lucide Square defaults to a stroked square; we want filled.
        <Square className="size-3" fill="currentColor" />
      ) : (
        <ArrowUp className={ICON_SIZE_CLASS} strokeWidth={2.5} />
      )}
    </button>
  );
}
