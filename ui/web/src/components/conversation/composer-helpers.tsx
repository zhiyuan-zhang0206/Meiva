"use client";

import { FileText, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";

import { type SlashAutocompleteHandle } from "@/components/conversation/slash-autocomplete";
import type { DeliveredUploadStatus } from "@/lib/contracts/types";
import { cn, formatFileSize } from "@/lib/format/utils";
import { isImeComposition, isSendEnter, type KeyStroke } from "@/lib/input/enter-to-send";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

export interface StagedAttachment {
  readonly key: string;
  readonly agentId: number | null;
  readonly file: File | null;
  readonly name: string;
  readonly size: number;
  readonly contentType: string;
  readonly previewUrl: string | null;
  readonly operationKey: string;
  readonly status: "uploading" | "ready" | "error";
  readonly progress: number;
  readonly fileId: string | null;
  readonly error: string | null;
}

export function ComposerMetaRow({
  commandHint,
}: {
  commandHint: string | null;
}) {
  if (commandHint === null) {
    return null;
  }
  return (
    <div className={cn("relative items-center gap-2 px-0.5 text-xs text-muted-foreground h-4 leading-4", FLEX, MIN_W_0)}>
      <span data-testid="composer-meta" className={cn("truncate italic", MIN_W_0)}>
        {commandHint}
      </span>
    </div>
  );
}

export function ComposerHeldBatchBanner({
  heldBatch,
  composerLocked,
  onRestore,
  onRetry,
}: {
  heldBatch: DeliveredUploadStatus | null;
  composerLocked: boolean;
  onRestore: () => void;
  onRetry: () => void;
}) {
  const t = useTranslations("common");
  if (heldBatch?.state !== "hold") return null;
  return (
    <div
      role="alert"
      data-testid="composer-held-batch"
      className={cn(
        "items-center justify-between gap-2 rounded-md border border-destructive/40 bg-destructive/5 px-2.5 py-1.5 text-xs",
        FLEX,
      )}
    >
      <span className={cn("truncate text-destructive", MIN_W_0)}>
        {t("heldUploadNotice", { reason: heldBatch.reason ?? "delivery failed" })}
      </span>
      <div className={cn("shrink-0 items-center gap-1.5", FLEX)}>
        <button
          type="button"
          data-testid="composer-held-restore"
          onClick={onRestore}
          disabled={composerLocked}
          className="rounded border border-border bg-background px-2 py-0.5 text-[11px] font-medium text-foreground hover:bg-accent disabled:opacity-50"
        >
          {t("heldUploadRestore")}
        </button>
        <button
          type="button"
          data-testid="composer-held-retry"
          onClick={onRetry}
          disabled={composerLocked}
          className="rounded border border-border bg-background px-2 py-0.5 text-[11px] font-medium text-foreground hover:bg-accent disabled:opacity-50"
        >
          {t("heldUploadRetry")}
        </button>
      </div>
    </div>
  );
}

export function ComposerAttachmentStrip({
  attachments,
  composerLocked,
  onRemove,
  onRetry,
}: {
  attachments: readonly StagedAttachment[];
  composerLocked: boolean;
  onRemove: (key: string) => void;
  onRetry: (key: string) => void;
}) {
  const t = useTranslations("common");
  if (attachments.length === 0) return null;
  return (
    <div className={cn("flex-wrap gap-2 px-0.5 pb-1", FLEX)} data-testid="composer-attachments">
      {attachments.map((att) => (
        <div
          key={att.key}
          data-testid="composer-attachment-card"
          data-status={att.status}
          className="relative"
        >
          {att.previewUrl ? (
            <picture>
              <img
                src={att.previewUrl}
                alt={att.name}
                className={cn(
                  "h-14 w-14 rounded border border-border object-cover",
                  att.status === "uploading" && "opacity-50",
                  att.status === "error" && "ring-1 ring-destructive",
                )}
              />
            </picture>
          ) : (
            <div
              className={cn(
                "h-14 max-w-48 items-center gap-2 rounded border border-border bg-muted/40 px-2.5 py-1.5",
                att.status === "uploading" && "opacity-60",
                att.status === "error" && "border-destructive ring-1 ring-destructive",
                FLEX,
              )}
            >
              <FileText className="size-4 shrink-0 text-muted-foreground" />
              <div className={MIN_W_0}>
                <div className="truncate font-mono text-xs text-foreground">{att.name}</div>
                <div className="text-[10px] text-muted-foreground">{formatFileSize(att.size)}</div>
              </div>
            </div>
          )}
          {att.status !== "ready" ? (
            <span
              className={cn(
                "absolute inset-0 items-center justify-center rounded bg-background/60 text-[10px] font-medium",
                att.status === "error" ? "text-destructive" : "text-muted-foreground",
                FLEX,
              )}
            >
              {att.status === "error" ? t("failed") : `${att.progress}%`}
            </span>
          ) : null}
          {att.status === "error" && att.file !== null ? (
            <button
              type="button"
              aria-label={t("retryUpload", { name: att.name })}
              title={att.error ?? undefined}
              onClick={() => onRetry(att.key)}
              disabled={composerLocked}
              className={cn(
                "absolute -bottom-1.5 -right-1.5 h-5 w-5 items-center justify-center rounded-full border border-border bg-background text-foreground shadow-sm hover:bg-accent",
                FLEX,
              )}
            >
              <RotateCcw className="size-2.5" />
            </button>
          ) : null}
          <button
            type="button"
            aria-label={t("removeImage", { name: att.name })}
            onClick={() => onRemove(att.key)}
            disabled={composerLocked}
            className={cn(
              "absolute -right-1.5 -top-1.5 h-4 w-4 items-center justify-center rounded-full bg-foreground/80 text-background text-[10px] leading-none",
              FLEX,
            )}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

// meiva:note
// The composer reads only these keydown fields, so the handler takes a plain
// value: a React keydown satisfies it, and a test supplies one without a cast.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
export interface ComposerKeyDown extends KeyStroke {
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  preventDefault: () => void;
}

export function handleComposerKeyDown(
  e: ComposerKeyDown,
  opts: {
    ac: SlashAutocompleteHandle | null;
    offeredSuggestion: string | null;
    composerLocked: boolean;
    suggestions: readonly string[] | null;
    replyIndex: number;
    agentId: number | null;
    mode: "disabled" | "idle" | "busy";
    coarsePointer: boolean;
    acceptSuggestion: (text: string) => void;
    onCycleSuggestion: (nextIndex: number) => void;
    submit: () => void;
  },
): void {
  const composing = isImeComposition(e);
  const sendEnter = isSendEnter(e, opts.coarsePointer);
  const { ac } = opts;
  if (ac?.isOpen() && !composing) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      ac.moveDown();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      ac.moveUp();
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      ac.close();
      return;
    }
    if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      ac.selectActive();
      return;
    }
    if (sendEnter) {
      e.preventDefault();
      if (ac.selectActive()) return;
    }
  }
  if (
    opts.offeredSuggestion !== null &&
    !composing &&
    !opts.composerLocked &&
    !ac?.isOpen() &&
    !e.shiftKey &&
    !e.ctrlKey &&
    !e.altKey &&
    !e.metaKey
  ) {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      opts.acceptSuggestion(opts.offeredSuggestion);
      return;
    }
    if (
      opts.suggestions &&
      opts.suggestions.length > 1 &&
      (e.key === "ArrowUp" || e.key === "ArrowDown")
    ) {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      opts.onCycleSuggestion(
        Math.min(Math.max(opts.replyIndex + step, 0), opts.suggestions.length - 1),
      );
      return;
    }
  }
  if (sendEnter) {
    e.preventDefault();
    if (opts.mode === "disabled") return;
    opts.submit();
  }
}
