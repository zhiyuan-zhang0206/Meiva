"use client";

import { type ReactNode } from "react";

import { useDeferredMount } from "@/lib/layout/use-client-ready";

// meiva:note
// Children mount in their own idle slice, in document order with every other
// DeferredMount on the page - so one long form fills in over frames instead of
// blocking the click that opened it.
// meiva:link ui/web/src/docs/frontend-components/control-page-load.meiva.okf.md
export function DeferredMount({ children }: { children: ReactNode }) {
  const mounted = useDeferredMount();
  return mounted ? <>{children}</> : null;
}
