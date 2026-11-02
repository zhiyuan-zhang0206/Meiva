"use client";

// ToastHost — the ONE renderer for the store's toast slot, mounted at the
// app root (Providers) so an error toast from ANY route (/control, /fleet,
// /insights, …) is actually visible. Previously the renderer lived only in
// app/page.tsx, so every non-Home route's showToast call vanished silently.
// role="alert" makes the toast reach a screen reader immediately (errors
// visible to every perception channel — R4 invariant 3).

import { useStore } from "@/lib/state/store";

export function ToastHost() {
  const toast = useStore((s) => s.toast);
  if (!toast) return null;
  return (
    <div
      role="alert"
      className="fixed right-4 bottom-24 z-50 max-w-[calc(100vw-2rem)] break-words rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground shadow-lg"
    >
      {toast}
    </div>
  );
}
