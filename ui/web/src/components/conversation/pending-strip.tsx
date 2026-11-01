"use client";

import { Clock, FileText } from "lucide-react";
import { useTranslations } from "next-intl";

import { assetUrl } from "@/lib/transport/api";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import type { DeliveredUploadStatus, PendingInbound } from "@/lib/contracts/types";
import { cn, formatFileSize } from "@/lib/format/utils";

export function userPending(items: readonly PendingInbound[]): PendingInbound[] {
  return items.filter((it) => it.sender === "user" || Boolean(it.files?.length));
}

export function PendingStrip({
  items,
  deliveringBatch = null,
  maxWidthCss,
}: {
  items: PendingInbound[];
  deliveringBatch?: DeliveredUploadStatus | null;
  /** Composer column cap — the strip floats above the composer and its
   *  content aligns with the conversation column (user ruling 2026-08-06). */
  maxWidthCss?: string;
}) {
  const t = useTranslations("pendingStrip");
  const queued = userPending(items);
  const showDelivering = deliveringBatch?.state === "pending";
  const totalCount = queued.length + (showDelivering ? 1 : 0);
  if (totalCount === 0) return null;
  return (
    // User ruling 2026-08-06: same divider rhythm as the composer — px-4 on
    // the root, divider on the inner wrapper (never edge to edge).
    <div className="px-4 py-1.5 text-xs text-muted-foreground">
      <div
        style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
        className="mx-auto border-t border-border pt-1.5"
      >
        <div className={cn("items-center gap-1.5 font-medium", FLEX)}>
          <Clock className="size-3 shrink-0" />
          {t("pending", { count: totalCount })}
        </div>
        <ul className="mt-0.5 max-h-24 overflow-y-auto space-y-0.5">
          {showDelivering ? (
            <li
              data-testid="pending-delivering-batch"
              className={cn("items-center gap-1.5 pl-4", FLEX, MIN_W_0)}
            >
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
            </li>
          ) : null}
          {queued.map((it, index) => {
            const prevHasFiles = index > 0 && Boolean(queued[index - 1]?.files?.length);
            const hideText =
              (Boolean(it.images?.length) && it.content === "[image]") ||
              ((Boolean(it.files?.length) || prevHasFiles) && it.content === "[files]") ||
              (it.source === "system:upload" && Boolean(it.files?.length));
            return (
              <li key={it.id} className={cn("items-center gap-1.5 pl-4", FLEX, MIN_W_0)}>
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
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
