"use client";

// meiva:note
// ToastHost — the ONE renderer for the store's toast slot, mounted at the
// app root (Providers) so an error toast from ANY route (/control, /fleet,
// /insights, …) is actually visible. Stacking context z-[100] ensures open
// Radix Dialog / Popover overlays (z-50 portals at end of document.body) never
// cover or dim error toasts.
// role="alert" makes the toast reach a screen reader immediately (errors
// visible to every perception channel — R4 invariant 3).

import { InlineError } from "@/components/ui/inline-error";
import { useStore } from "@/lib/state/store";

export function ToastHost() {
  const toast = useStore((s) => s.toast);
  if (!toast) return null;
  return (
    <div className="fixed right-4 bottom-24 z-[100] max-w-[min(28rem,calc(100vw-2rem))]">
      <InlineError surface="floating" role="alert">
        {toast}
      </InlineError>
    </div>
  );
}
