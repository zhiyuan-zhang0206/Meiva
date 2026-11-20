"use client";

import { Clock, FileText } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";

import { assetUrl } from "@/lib/transport/api";
import { FLEX, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import type { DeliveredUploadStatus, PendingInbound, Quote } from "@/lib/contracts/types";
import { cn, formatFileSize } from "@/lib/format/utils";

export function userPending(items: readonly PendingInbound[]): PendingInbound[] {
  return items.filter((it) => it.sender === "user" || Boolean(it.files?.length));
}

function PendingQuoteLine({ quote }: { quote: Quote }) {
  const t = useTranslations("pendingStrip");
  return (
    <div data-testid="pending-quote" className="truncate text-[11px] text-muted-foreground">
      {t("quoteSource", { author: quote.author })} <span className="text-foreground/60">{quote.text}</span>
    </div>
  );
}

// meiva:note
// A quoted message is one unit: a left rule groups its one-line quote preview, text and attachments;
// a message without a quote keeps the compact single line.
function PendingRow({
  quote,
  testId,
  children,
}: {
  quote: Quote | null | undefined;
  testId?: string;
  children: ReactNode;
}) {
  if (!quote) {
    return (
      <li data-testid={testId} className={cn("items-center gap-1.5 pl-4", FLEX, MIN_W_0)}>
        {children}
      </li>
    );
  }
  return (
    <li data-testid={testId} className={cn("ml-4 gap-0.5 border-l-2 border-border pl-2", FLEX, FLEX_COL, MIN_W_0)}>
      <PendingQuoteLine quote={quote} />
      <div className={cn("items-center gap-1.5", FLEX, MIN_W_0)}>{children}</div>
    </li>
  );
}

export function PendingStrip({
  items,
  deliveringBatch = null,
  maxWidthCss,
}: {
  items: PendingInbound[];
  deliveringBatch?: DeliveredUploadStatus | null;
  maxWidthCss?: string;
}) {
  const t = useTranslations("pendingStrip");
  const queued = userPending(items);
  const showDelivering = deliveringBatch?.state === "pending";
  const totalCount = queued.length + (showDelivering ? 1 : 0);
  if (totalCount === 0) return null;
  return (
    <div
      data-testid="pending-strip"
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
      className="mx-auto w-full px-4 py-1.5 text-xs text-muted-foreground"
    >
      <div className="border-t border-border pt-1.5">
        <div className={cn("items-center gap-1.5 font-medium", FLEX)}>
          <Clock className="size-3 shrink-0" />
          {t("pending", { count: totalCount })}
        </div>
        <ul className="mt-0.5 max-h-36 overflow-y-auto space-y-1.5">
          {showDelivering ? (
            <PendingRow testId="pending-delivering-batch" quote={deliveringBatch.quote}>
              <span className="truncate">
                {deliveringBatch.message_text ||
                  t("delivering", { count: deliveringBatch.files.length })}
              </span>
              <span className={cn("shrink-0 gap-1", FLEX)}>
                {deliveringBatch.files.map((f) => (
                  <span
                    key={f.staged_id}
                    className={cn(
                      "items-center gap-1 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px]",
                      FLEX,
                    )}
                  >
                    <FileText className="size-3 shrink-0" />
                    <span className="max-w-28 truncate">{f.name}</span>
                  </span>
                ))}
              </span>
            </PendingRow>
          ) : null}
          {queued.map((it, index) => {
            const prevHasFiles = index > 0 && Boolean(queued[index - 1]?.files?.length);
            const hideText =
              (Boolean(it.images?.length) && it.content === "[image]") ||
              ((Boolean(it.files?.length) || prevHasFiles) && it.content === "[files]") ||
              (it.source === "system:upload" && Boolean(it.files?.length));
            return (
              <PendingRow key={it.id} quote={it.quote}>
                {hideText ? null : <span className="truncate">{it.content}</span>}
                {it.files?.length ? (
                  <span className={cn("shrink-0 flex-wrap gap-1", FLEX)}>
                    {it.files.map((f, idx) => (
                      <span
                        key={`${idx}:${f.url}`}
                        data-testid="pending-file-chip"
                        className={cn(
                          "items-center gap-1 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px]",
                          FLEX,
                        )}
                      >
                        <FileText className="size-3 shrink-0" />
                        <span className="max-w-32 truncate">{f.name}</span>
                        <span className="text-[10px] text-muted-foreground">
                          {formatFileSize(f.size)}
                        </span>
                      </span>
                    ))}
                  </span>
                ) : null}
                {it.images?.length ? (
                  <span className={cn("shrink-0 gap-1", FLEX)}>
                    {it.images.map((src, idx) => (
                      <picture key={`${idx}:${src}`}>
                        <img
                          src={assetUrl(src)}
                          alt={t("imageAlt")}
                          crossOrigin="use-credentials"
                          loading="lazy"
                          className="h-10 w-10 rounded border border-border object-cover"
                        />
                      </picture>
                    ))}
                  </span>
                ) : null}
              </PendingRow>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
