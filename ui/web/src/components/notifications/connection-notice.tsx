"use client";

// meiva:note
// The banner floats over the conversation instead of sitting in it: a status row
// inside the log takes 30px of height, so a reconnect pushed every card down and
// back under the reader. How long a drop must last before it is worth saying is
// decided where the socket state enters the store (AppConnectionBanner), not here;
// a paused or restarting cluster shows as the connection state it produces, and a
// reload while the app is down is served the Gate's full-screen unavailable page.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { useTranslations } from "next-intl";

import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";
import { useStore } from "@/lib/state/store";

// meiva:note
// An agent switch re-opens the merged stream, so the socket drops for a few hundred
// milliseconds by design. Below this threshold the banner stays away; a drop that
// outlasts it is real and says so.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
export const NOTICE_DELAY_MS = 800;

export function ConnectionNotice() {
  const connState = useStore((s) => s.connState);
  const t = useTranslations("common");

  if (connState === "open") return null;

  const isClosed = connState === "closed";
  const tone = isClosed
    ? "bg-destructive/15 text-destructive border-destructive/40"
    : "bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/30";
  const message = isClosed ? t("liveUpdatesDisconnected") : t("liveUpdatesReconnecting");

  return (
    <div className={cn("pointer-events-none absolute inset-x-0 top-1 z-20 justify-center px-3", FLEX)}>
      <div
        role="status"
        aria-live="polite"
        className={cn("pointer-events-auto max-w-[min(100%,42rem)] rounded-md border px-3 py-1.5 text-xs shadow-sm", tone)}
      >
        {message}
      </div>
    </div>
  );
}
