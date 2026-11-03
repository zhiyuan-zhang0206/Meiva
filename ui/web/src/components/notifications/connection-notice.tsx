"use client";

// meiva:note
// The banner floats over the conversation immediately below HeaderBar (top-[52px] z-30)
// so the floating header never covers it. Transient disconnects (agent switch, momentary
// network hiccup, quick cluster restart) auto-retry silently without any banner or
// jitter; only a sustained disconnect outlasting CONNECTION_ERROR_DELAY_MS (15s) escalates
// to a floating red error banner with a manual Retry button.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { useTranslations } from "next-intl";

import { InlineError } from "@/components/ui/inline-error";
import { cn } from "@/lib/format/utils";
import { BAR_CLEAR_TOP_OFFSET_CLASS, FLEX } from "@/lib/layout/layout";
import { useStore } from "@/lib/state/store";

export const CONNECTION_ERROR_DELAY_MS = 15_000;

export function ConnectionNotice() {
  const connState = useStore((s) => s.connState);
  const bumpReconnect = useStore((s) => s.bumpReconnect);
  const t = useTranslations("common");

  if (connState !== "closed") return null;

  return (
    <div
      className={cn(
        "pointer-events-none absolute inset-x-0 z-30 justify-center px-3",
        BAR_CLEAR_TOP_OFFSET_CLASS,
        FLEX,
      )}
    >
      <div className="pointer-events-auto max-w-[min(100%,42rem)]">
        <InlineError
          surface="floating"
          role="alert"
          onRetry={bumpReconnect}
          retryLabel={t("retry")}
        >
          {t("liveUpdatesDisconnected")}
        </InlineError>
      </div>
    </div>
  );
}
