"use client";

import React, { useCallback } from "react";
import { Textarea } from "@/components/ui/textarea";
import { isSendEnter } from "@/lib/input/enter-to-send";
import { useCoarsePointer } from "@/lib/input/use-coarse-pointer";
import { cn } from "@/lib/format/utils";

interface AutoExpandTextareaProps
  extends Omit<React.ComponentProps<"textarea">, "onKeyDown"> {
  focusVisible?: boolean;
  coarsePointer?: boolean;
  onSend: () => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
}

// meiva:note
// A textarea that auto-grows via CSS field-sizing-content and sends on Enter; the
// rule itself lives in lib/input/enter-to-send.ts. `focusVisible` reaches Textarea.
// `coarsePointer` is whether the primary pointer is a finger: omitted, this field
// reads it, and a caller that already knows it passes it in.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
export const AutoExpandTextarea = React.forwardRef<
  HTMLTextAreaElement,
  AutoExpandTextareaProps
>(function AutoExpandTextarea(
  { focusVisible, coarsePointer, onSend, onKeyDown, className, ...props },
  ref,
) {
  const primaryPointerIsCoarse = useCoarsePointer();
  const touchKeyboard = coarsePointer ?? primaryPointerIsCoarse;
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (isSendEnter(e, touchKeyboard)) {
        e.preventDefault();
        onSend();
        return;
      }
      onKeyDown?.(e);
    },
    [onSend, onKeyDown, touchKeyboard],
  );

  return (
    <Textarea
      ref={ref}
      focusVisible={focusVisible}
      onKeyDown={handleKeyDown}
      className={cn("resize-none", className)}
      {...props}
    />
  );
});
