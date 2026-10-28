"use client";

import { useMediaQuery } from "@/lib/layout/use-media-query";

// meiva:note
// True when the primary pointer is a finger rather than a mouse or trackpad, so
// the keyboard in front of a field has no Shift key to reach "the other" Enter.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
export function useCoarsePointer(): boolean {
  return useMediaQuery("(pointer: coarse)");
}
