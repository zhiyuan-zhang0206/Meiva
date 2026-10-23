"use client";

import { useTranslations } from "next-intl";

import { type SlashAutocompleteHandle } from "@/components/conversation/slash-autocomplete";
import { ContextButton } from "@/components/inspector/context-breakdown";
import { ContextMeterGhost } from "@/components/inspector/context-meter";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { newOperationKey as newClientMessageId } from "@/lib/transport/operation-key";

export interface PendingImage {
  key: string;
  name: string;
  previewUrl: string;
  url?: string;
  status: "uploading" | "ready" | "error";
}

export const draftKey = (id: number) => `composer-draft-${id}`;
export const sendAttemptKey = (id: number) => `composer-send-attempt-${id}`;

export interface SendAttempt {
  signature: string;
  clientMessageId: string;
  agentId: number | null;
  content: string;
  imageUrls: string[];
}

export function isAttempt(attempt: SendAttempt | null, clientMessageId: string): boolean {
  return attempt?.clientMessageId === clientMessageId;
}

const memorySession = new Map<string, string | null>();

export function readSession(key: string): string | null {
  if (memorySession.has(key)) return memorySession.get(key) ?? null;
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeSession(key: string, value: string): void {
  memorySession.set(key, value);
  try {
    sessionStorage.setItem(key, value);
  } catch {
    return;
  }
  memorySession.delete(key);
}

export function removeSession(key: string): void {
  memorySession.set(key, null);
  try {
    sessionStorage.removeItem(key);
  } catch {
    return;
  }
  memorySession.delete(key);
}

export function readSendAttempt(agentId: number): SendAttempt | null {
  try {
    const parsed = JSON.parse(readSession(sendAttemptKey(agentId)) ?? "null") as unknown;
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("signature" in parsed) ||
      typeof parsed.signature !== "string" ||
      !("clientMessageId" in parsed) ||
      typeof parsed.clientMessageId !== "string" ||
      !("content" in parsed) ||
      typeof parsed.content !== "string" ||
      !("imageUrls" in parsed) ||
      !Array.isArray(parsed.imageUrls) ||
      !parsed.imageUrls.every((url) => typeof url === "string")
    ) {
      return null;
    }
    return parsed as SendAttempt;
  } catch {
    return null;
  }
}

export function resolveSendAttempt(
  current: SendAttempt | null,
  sendingAgentId: number | null,
  content: string,
  imageUrls: string[],
): SendAttempt {
  const signature = JSON.stringify([sendingAgentId, content, imageUrls]);
  let candidate = current;
  if (candidate?.signature !== signature && sendingAgentId != null) {
    const persisted = readSendAttempt(sendingAgentId);
    candidate = persisted?.signature === signature ? persisted : null;
  }
  return candidate?.signature === signature
    ? candidate
    : {
        signature,
        clientMessageId: newClientMessageId(),
        agentId: sendingAgentId,
        content,
        imageUrls: [...imageUrls],
      };
}

export function ComposerMetaRow({
  commandHint,
  showContextMeter,
  contextTokens,
  contextPending,
  agentId,
  breakdownOpen,
  onBreakdownOpenChange,
  maxContextTokens,
  softCompactTokens,
  hardCompactTokens,
}: {
  commandHint: string | null;
  showContextMeter: boolean;
  contextTokens: number;
  contextPending: boolean;
  agentId: number | null;
  breakdownOpen: boolean;
  onBreakdownOpenChange: (open: boolean) => void;
  maxContextTokens: number;
  softCompactTokens: number;
  hardCompactTokens: number;
}) {
  if (commandHint === null && !(showContextMeter && contextTokens > 0) && !(showContextMeter && contextPending)) {
    return null;
  }
  return (
    <div className={cn("relative items-center gap-2 px-0.5 text-xs text-muted-foreground h-4 leading-4", FLEX, MIN_W_0)}>
      <span data-testid="composer-meta" className={cn("truncate", commandHint && "italic", MIN_W_0)}>
        {commandHint ??
          (showContextMeter ? (
            contextTokens > 0 ? (
              <ContextButton
                agentId={agentId}
                open={breakdownOpen}
                onOpenChange={onBreakdownOpenChange}
                contextTokens={contextTokens}
                maxContextTokens={maxContextTokens}
                softCompactTokens={softCompactTokens}
                hardCompactTokens={hardCompactTokens}
                showText={false}
              />
            ) : contextPending ? (
              <ContextMeterGhost />
            ) : null
          ) : null)}
      </span>
    </div>
  );
}

export function ComposerAttachmentStrip({
  images,
  composerLocked,
  onRemove,
}: {
  images: PendingImage[];
  composerLocked: boolean;
  onRemove: (key: string) => void;
}) {
  const t = useTranslations("common");
  if (images.length === 0) return null;
  return (
    <div className={cn("flex-wrap gap-2 px-0.5 pb-1", FLEX)} data-testid="composer-attachments">
      {images.map((img) => (
        <div key={img.key} className="relative">
          <picture>
            <img
              src={img.previewUrl}
              alt={img.name}
              className={cn(
                "h-14 w-14 rounded border border-border object-cover",
                img.status === "uploading" && "opacity-50",
                img.status === "error" && "ring-1 ring-destructive",
              )}
            />
          </picture>
          {img.status !== "ready" ? (
            <span
              className={cn(
                "absolute inset-0 items-center justify-center text-[10px]",
                img.status === "error" ? "text-destructive" : "text-muted-foreground",
                FLEX,
              )}
            >
              {img.status === "error" ? t("failed") : "…"}
            </span>
          ) : null}
          <button
            type="button"
            aria-label={t("removeImage", { name: img.name })}
            onClick={() => onRemove(img.key)}
            disabled={composerLocked}
            className={cn("absolute -right-1.5 -top-1.5 h-4 w-4 items-center justify-center rounded-full bg-foreground/80 text-background text-[10px] leading-none", FLEX)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export function handleComposerKeyDown(
  e: React.KeyboardEvent<HTMLTextAreaElement>,
  opts: {
    ac: SlashAutocompleteHandle | null;
    offeredSuggestion: string | null;
    composerLocked: boolean;
    suggestions: readonly string[] | null;
    replyIndex: number;
    agentId: number | null;
    mode: "disabled" | "idle" | "busy";
    acceptSuggestion: (text: string) => void;
    onCycleSuggestion: (nextIndex: number) => void;
    submit: () => void;
  },
): void {
  const composing = e.nativeEvent.isComposing || e.key === "Process";
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
    if (e.key === "Enter" && !e.shiftKey) {
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
  if (e.key === "Enter" && !e.shiftKey && !composing) {
    e.preventDefault();
    if (opts.mode === "disabled") return;
    opts.submit();
  }
}
