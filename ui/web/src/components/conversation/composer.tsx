"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ComposerAttachmentStrip,
  ComposerMetaRow,
  handleComposerKeyDown,
  type PendingImage,
} from "@/components/conversation/composer-helpers";
import {
  draftKey,
  isAttempt,
  readSendAttempt,
  readSession,
  removeSession,
  resolveSendAttempt,
  sendAttemptKey,
  writeSession,
  type SendAttempt,
} from "@/components/conversation/composer-session";
import {
  parseSlash,
  SlashAutocomplete,
  type SlashAutocompleteHandle,
} from "@/components/conversation/slash-autocomplete";
import { SendButton } from "@/components/ui/send-button";
import { Textarea } from "@/components/ui/textarea";
import { MessageDeliveryUnknownError } from "@/lib/transport/api";
import { useApi, useWallNow } from "@/lib/transport/transport-context";
import type { Diagnostics } from "@/lib/transport/transport";
import { errMsg } from "@/lib/contracts/errors";
import { clearMessageSent, markMessageSent } from "@/lib/format/interaction-timing";
import { track } from "@/lib/telemetry/telemetry";
import type { CommandItem } from "@/lib/contracts/types";
import { useBrowser } from "@/lib/browser/browser-provider";
import { cn } from "@/lib/format/utils";
import { useCoarsePointer } from "@/lib/input/use-coarse-pointer";
import {
  FLEX,
  FLEX_1,
  FLEX_COL,
  MIN_W_0,
} from "@/lib/layout/layout";

// meiva:note
// The composer's own bottom band: a 1px rule, 12px (`pt-3`), the control row,
// 24px (`pb-6`) of viewport clearance (user ruling 2026-10-31) — 73px at rest.
// The control row's minimum is the composer's own 36px controls (the `size-9`
// upload button and the `min-h-9` textarea). The band belongs to the composer
// alone: the sidebar footer draws a rule of its own, and the two are never
// aligned by design (user ruling 2026-11-01).
// meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/bottom-rules.meiva.okf.md
const COMPOSER_BAND_CLASS = "border-t border-border pt-3 pb-6";
const COMPOSER_BAND_ROW_CLASS = "min-h-9";

export type ComposerMode = "disabled" | "idle" | "busy";

interface Props {
  // meiva:note
  // Where a failed command-catalog fetch is reported: the page's sink, the same one a render
  // crash and a stream failure reach. The shell hands it in; a test hands in its own instead
  // of replacing the console.
  diagnostics: Diagnostics;
  mode: ComposerMode;
  children?: React.ReactNode;
  onSend: (content: string, imageUrls: string[], clientMessageId: string) => Promise<boolean>;
  onStop: () => void;
  onUploadFiles?: (files: File[]) => void;
  onAttachImage?: (file: File) => Promise<string>;
  filesUploading?: boolean;
  focusToken?: number;
  contextTokens: number;
  contextPending?: boolean;
  maxContextTokens?: number;
  softCompactTokens?: number;
  hardCompactTokens?: number;
  showContextMeter?: boolean;
  agentId?: number | null;
  agentTerminated?: boolean;
  maxWidthCss?: string;
  suggestions?: readonly string[] | null;
}

export function Composer({ diagnostics, mode, onSend, onStop, onUploadFiles, onAttachImage, filesUploading = false, focusToken, contextTokens, contextPending = false, maxContextTokens = 0, softCompactTokens = 0, hardCompactTokens = 0, agentId = null, agentTerminated = false, showContextMeter = false, maxWidthCss, suggestions = null, children }: Props) {
  const client = useApi();
  const wallNow = useWallNow();
  const t = useTranslations("common");
  const browser = useBrowser();
  const session = browser.session;
  const objectUrls = browser.objectUrls;
  const prevAgentIdRef = useRef(agentId);
  const [value, setValue] = useState(() => {
    if (agentId == null) return '';
    return readSession(session, draftKey(agentId)) ?? '';
  });
  const [replySelection, setReplySelection] = useState<{
    options: readonly string[];
    agentId: number | null;
    index: number;
    consumed: boolean;
  } | null>(null);
  const currentReplySelection =
    replySelection !== null && replySelection.options === suggestions && replySelection.agentId === agentId
      ? replySelection : null;
  const replyIndex = currentReplySelection?.index ?? 0;
  const [caret, setCaret] = useState(() => value.length);
  const [dragOver, setDragOver] = useState(false);
  const [images, setImages] = useState<PendingImage[]>([]);
  const imagesEnabled = onAttachImage !== undefined;
  const [imageScope, setImageScope] = useState({ agentId, enabled: imagesEnabled });
  // meiva:link ui/web/src/docs/frontend-data-flow/unavailable-model-capabilities.meiva.okf.md
  if (imageScope.agentId !== agentId || imageScope.enabled !== imagesEnabled) {
    setImageScope({ agentId, enabled: imagesEnabled });
    setImages([]);
  }
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [initialSendAttempt] = useState<SendAttempt | null>(() =>
    agentId == null ? null : readSendAttempt(session, agentId),
  );
  const sendAttemptRef = useRef<SendAttempt | null>(initialSendAttempt);
  // meiva:note
  // The in-flight lock gates submission and attachments — never the textarea's
  // readOnly/disabled state: on Android that tears down the IME connection and
  // the next voice/paste commit is lost silently.
  // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
  const composerLocked = sending;
  const { data: commands = [] } = useQuery({
    queryKey: ["agent-commands", agentId],
    queryFn: async () => {
      try {
        return await (agentId == null ? client.getCommands() : client.getCommands(agentId));
      } catch (e) {
        diagnostics.warn(`[composer] getCommands failed: ${errMsg(e)}`);
        throw new Error("command catalog unavailable", { cause: e });
      }
    },
    staleTime: Infinity,
    retry: false,
  });
  const taRef = useRef<HTMLTextAreaElement>(null);
  const acRef = useRef<SlashAutocompleteHandle>(null);
  const [breakdownOpen, setBreakdownOpen] = useState(false);
  const handleBreakdownOpenChange = useCallback((open: boolean) => {
    setBreakdownOpen(open);
    if (open) acRef.current?.close();
  }, []);
  const handleAutocompleteOpenChange = useCallback((open: boolean) => {
    if (open) setBreakdownOpen(false);
  }, []);
  const pendingCaret = useRef<number | null>(null);
  const syncCaret = (ta: HTMLTextAreaElement) => setCaret(ta.selectionStart);

  const query = parseSlash(value, caret)?.query ?? null;

  const commandHint = useMemo(() => {
    const hintOf = (token: string) => {
      const hint = commands.find((c) => c.name === token.slice(1))?.instruction_hint;
      if (!hint) return null;
      return hint;
    };
    if (parseSlash(value, caret)) return null;
    const firstToken = /\S+/.exec(value);
    if (firstToken && firstToken[0].startsWith("/") && firstToken.index < caret) {
      return hintOf(firstToken[0]);
    }
    return null;
  }, [value, caret, commands]);

  useEffect(() => {
    if (focusToken === undefined) return;
    taRef.current?.focus();
  }, [focusToken]);

  useEffect(() => {
    const pos = pendingCaret.current;
    if (pos === null) return;
    pendingCaret.current = null;
    const ta = taRef.current;
    if (!ta) return;
    ta.focus();
    ta.setSelectionRange(pos, pos);
  }, [value, caret]);

  const clearImages = () => setImages([]);
  const imagePreviewsRef = useRef(new Map<string, string>());
  useEffect(() => {
    for (const [key, previewUrl] of imagePreviewsRef.current) {
      if (!images.some((image) => image.key === key)) {
        objectUrls.revoke(previewUrl);
        imagePreviewsRef.current.delete(key);
      }
    }
  }, [images, objectUrls]);
  useEffect(() => {
    const previews = imagePreviewsRef.current;
    return () => {
      for (const previewUrl of previews.values()) objectUrls.revoke(previewUrl);
      previews.clear();
    };
  }, [objectUrls]);

  const valueRef = useRef(value);
  useEffect(() => {
    valueRef.current = value;
  }, [value]);
  useEffect(() => {
    const prevId = prevAgentIdRef.current;
    if (prevId != null && prevId !== agentId) {
      const snapshot = valueRef.current;
      const trimmed = snapshot.trim();
      if (trimmed) {
        writeSession(session, draftKey(prevId), snapshot);
      } else {
        removeSession(session, draftKey(prevId));
      }
    }
    if (agentId != null && agentId !== prevId) {
      const draft = readSession(session, draftKey(agentId)) ?? "";
      setValue(draft);
      setCaret(draft.length);
      const attempt = readSendAttempt(session, agentId);
      sendAttemptRef.current = attempt;
    }
    prevAgentIdRef.current = agentId;
  }, [agentId, session]);

  useEffect(() => {
    if (agentId == null) return;
    const trimmed = value.trim();
    if (trimmed) {
      writeSession(session, draftKey(agentId), value);
    } else {
      removeSession(session, draftKey(agentId));
    }
  }, [value, agentId, session]);

  const removeImage = (key: string) => {
    if (composerLocked) return;
    setImages((prev) => prev.filter((image) => image.key !== key));
  };

  const attachImages = (files: File[]) => {
    if (composerLocked || !onAttachImage) return;
    for (const file of files) {
      // meiva:note
      // A pending attachment previews from an object URL, so the page image policy must allow blob:.
      // The URL is the attachment's identity while it is pending: it is unique per paste, and the
      // preview and the key that releases it are the same value.
      // meiva:link ui/web/src/lib/auth/docs/content-security-policy.meiva.okf.md
      const previewUrl = objectUrls.create(file);
      const key = previewUrl;
      imagePreviewsRef.current.set(key, previewUrl);
      setImages((prev) => [...prev, { key, name: file.name, previewUrl, status: "uploading" }]);
      onAttachImage(file)
        .then((u) =>
          setImages((prev) =>
            prev.map((i) => (i.key === key ? { ...i, url: u, status: "ready" as const } : i)),
          ),
        )
        .catch(() =>
          setImages((prev) =>
            prev.map((i) => (i.key === key ? { ...i, status: "error" as const } : i)),
          ),
        );
    }
  };

  const send = async (
    content: string,
    imageUrls: string[],
  ) => {
    if (sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    const sendingAgentId = agentId;
    let activeAttempt: SendAttempt | null = null;
    try {
      const attempt = resolveSendAttempt(
        session,
        sendAttemptRef.current,
        sendingAgentId,
        content,
        imageUrls,
        client.newOperationKey,
      );
      activeAttempt = attempt;
      sendAttemptRef.current = attempt;
      if (sendingAgentId != null) {
        writeSession(session, sendAttemptKey(sendingAgentId), JSON.stringify(attempt));
        markMessageSent(sendingAgentId, wallNow());
      }
      const ok = await onSend(content, imageUrls, attempt.clientMessageId);
      if (ok) {
        track("composer-send");
        if (prevAgentIdRef.current === sendingAgentId) {
          if (suggestions) {
            setReplySelection({
              options: suggestions, agentId: sendingAgentId, index: replyIndex, consumed: true,
            });
          }
          setValue((current) => (current.trim() === content ? "" : current));
          setCaret(0);
          clearImages();
        }
        if (sendingAgentId != null) {
          removeSession(session, draftKey(sendingAgentId));
          removeSession(session, sendAttemptKey(sendingAgentId));
        }
        if (isAttempt(sendAttemptRef.current, attempt.clientMessageId)) {
          sendAttemptRef.current = null;
        }
      } else if (sendingAgentId != null) {
        clearMessageSent(sendingAgentId);
      }
    } catch (error: unknown) {
      if (!(error instanceof MessageDeliveryUnknownError)) {
        if (activeAttempt?.agentId != null) clearMessageSent(activeAttempt.agentId);
        throw error;
      }
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };

  const readyImageUrls = images.flatMap((i) => (i.status === "ready" && i.url ? [i.url] : []));
  const uploadingImages = images.some((i) => i.status === "uploading");

  const submit = () => {
    if (composerLocked) return;
    const content = value.trim();
    if (uploadingImages) return;
    if (!content && readyImageUrls.length === 0) return;
    void send(content, readyImageUrls);
  };

  const runCommand = (cmd: CommandItem) => {
    if (composerLocked) return;
    const token = parseSlash(value, caret);
    if (!token) return;
    const head = `${value.slice(0, token.start)}/${cmd.name}`;
    const tail = value.slice(token.end);
    const pos = head.length + 1;
    pendingCaret.current = pos;
    setCaret(pos);
    setValue(`${head}${/^\s/.test(tail) ? "" : " "}${tail}`);
  };

  const offeredSuggestion =
    value === "" && !currentReplySelection?.consumed && suggestions?.length
      ? suggestions[replyIndex] : null;
  const suggestionPlaceholder =
    offeredSuggestion !== null && suggestions && suggestions.length > 1
      ? `[${replyIndex + 1}/${suggestions.length}] ${offeredSuggestion}` : offeredSuggestion;
  const acceptSuggestion = (text: string) => {
    pendingCaret.current = text.length;
    setCaret(text.length);
    setValue(text);
  };

  const routeFiles = (files: File[]) => {
    if (composerLocked) return;
    const imgs = files.filter((f) => f.type.startsWith("image/"));
    const others = files.filter((f) => !f.type.startsWith("image/"));
    if (imgs.length > 0) {
      if (onAttachImage) attachImages(imgs);
      else onUploadFiles?.(imgs);
    }
    if (others.length > 0) onUploadFiles?.(others);
  };
  const canReceiveFiles = !composerLocked && (!!onUploadFiles || !!onAttachImage);
  const handleDrop = (e: React.DragEvent) => {
    if (!canReceiveFiles) return;
    e.preventDefault();
    setDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) routeFiles(files);
  };
  const handleDragOver = (e: React.DragEvent) => {
    if (!canReceiveFiles || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    setDragOver(true);
  };
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (!canReceiveFiles) return;
    const files = Array.from(e.clipboardData.files);
    if (files.length > 0) {
      e.preventDefault();
      routeFiles(files);
    }
  };

  const trimmed = value.trim();
  const hasContent = !!trimmed || readyImageUrls.length > 0;
  const coarsePointer = useCoarsePointer();
  const showStop = mode === "busy" && !hasContent && !composerLocked;
  const buttonDisabled =
    composerLocked || mode === "disabled" || uploadingImages || (!showStop && !hasContent);

  return (
    <div
      data-testid="composer"
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={() => setDragOver(false)}
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
      // meiva:note
      // pt-0.5 is this side's share of the gap between the message list and the
      // band's rule; the timeline supplies the rest (its pb-1, 6px together).
      // The 2026-10-29 ruling set 10px; the user read that line as too airy the
      // same day. Everything below the rule is the band, and
      // the band is content-sized: it grows upward with the draft, so its rule
      // moves. No other column follows it: the sidebar footer's rule is its
      // own and never aligned with this one (user rulings 2026-10-29, 2026-11-01).
      // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
      className={cn(
        "mx-auto w-full px-4 pt-0.5 transition-colors",
        MIN_W_0,
        dragOver && "bg-primary/5 ring-1 ring-inset ring-primary/40",
      )}
    >
      <div
        data-testid="composer-band"
        className={cn("gap-2", COMPOSER_BAND_CLASS, FLEX, FLEX_COL)}
      >
      <ComposerMetaRow
        commandHint={commandHint}
        showContextMeter={showContextMeter}
        contextTokens={contextTokens}
        contextPending={contextPending}
        agentId={agentId}
        breakdownOpen={breakdownOpen}
        onBreakdownOpenChange={handleBreakdownOpenChange}
        maxContextTokens={maxContextTokens}
        softCompactTokens={softCompactTokens}
        hardCompactTokens={hardCompactTokens}
      />
      {filesUploading ? (
        <div
          role="status"
          data-testid="composer-upload-hint"
          className="px-0.5 text-xs leading-4 text-muted-foreground"
        >
          {t("filesUploadingHint")}
        </div>
      ) : null}
      <ComposerAttachmentStrip
        images={images}
        composerLocked={composerLocked}
        onRemove={removeImage}
      />
      <div className="relative">
        <SlashAutocomplete
          ref={acRef}
          commands={commands}
          query={query}
          onSelect={runCommand}
          onOpenChange={handleAutocompleteOpenChange}
        />
        <div className={cn("gap-2 items-end", COMPOSER_BAND_ROW_CLASS, FLEX)}>
        {children}
        <Textarea
          ref={taRef}
          data-testid="composer-input"
          aria-label={t("composerInput")}
          aria-description={offeredSuggestion !== null ? t("replySuggestionKeys") : undefined}
          placeholder={
            suggestionPlaceholder ??
            (agentTerminated ? t("composerPlaceholderResurrect") : t("composerPlaceholder"))
          }
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            syncCaret(e.target);
          }}
          onKeyUp={(e) => syncCaret(e.currentTarget)}
          onClick={(e) => syncCaret(e.currentTarget)}
          onSelect={(e) => syncCaret(e.currentTarget)}
          onPaste={handlePaste}
          disabled={mode === "disabled"}
          focusVisible={false}
          className={cn("font-mono text-base resize-none max-h-[50vh] overflow-y-auto min-h-9 py-1.5 leading-5", FLEX_1)}
          suppressHydrationWarning
          onKeyDown={(e) =>
            handleComposerKeyDown(e, {
              ac: acRef.current,
              offeredSuggestion,
              composerLocked,
              suggestions,
              replyIndex,
              agentId,
              mode,
              coarsePointer,
              acceptSuggestion,
              onCycleSuggestion: (nextIndex) => {
                if (!suggestions) return;
                setReplySelection({
                  options: suggestions,
                  agentId,
                  index: nextIndex,
                  consumed: false,
                });
              },
              submit,
            })
          }
        />
        <SendButton
          data-testid="composer-send"
          state={sending ? "sending" : showStop ? "stop" : "send"}
          aria-label={sending ? "Sending" : showStop ? "Stop current turn" : "Send message"}
          disabled={buttonDisabled}
          onClick={() => { if (showStop) { track("composer-stop"); onStop(); } else { submit(); } }}
        />
        </div>
      </div>
      </div>
    </div>
  );
}
