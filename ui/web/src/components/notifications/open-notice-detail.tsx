"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { ChatMarkdown } from "@/components/content/markdown";
import { Button } from "@/components/ui/button";
import { AutoExpandTextarea } from "@/components/ui/auto-expand-textarea";
import { SendButton } from "@/components/ui/send-button";
import { api } from "@/lib/transport/api";
import { newOperationKey } from "@/lib/transport/operation-key";
import { errMsg } from "@/lib/contracts/errors";
import { PRIORITY_BG } from "@/lib/notifications/notices";
import { formatRelative } from "@/lib/format/time";
import { useTimeFormat } from "@/lib/format/use-time-format";
import type { NoticeAnswer, OpenNotice, ResolveNoticeIn } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1 } from "@/lib/layout/layout";

import { NoticeQuestionForm } from "./notice-question-form";

export function OpenNoticeDetail({
  agentId,
  notice,
  onResolved,
  onCycle,
  autoFocus = false,
  showTimestamp = false,
  className,
}: {
  agentId: number;
  notice: OpenNotice;
  onResolved?: () => void;
  onCycle?: (direction: -1 | 1) => void;
  autoFocus?: boolean;
  showTimestamp?: boolean;
  className?: string;
}) {
  const t = useTranslations("noticeDetail");
  const { formatAbsolute } = useTimeFormat();
  const [reply, setReply] = useState("");
  const [structuredAnswer, setStructuredAnswer] = useState<NoticeAnswer | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const inFlight = useRef(false);
  const attempt = useRef<{ intent: string; key: string } | null>(null);

  // Focus the reply box on mount when asked. The parent keys this component by
  // notice id, so a new notice replacing a resolved one re-focuses too.
  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus();
  }, [autoFocus]);

  // On success we do NOT clear `pending`: the resolve removes the open notice,
  // so the caller (via onResolved) refetches and this component unmounts — a
  // sticky disabled state avoids a flash of re-enabled controls before it goes.
  const run = async (body: ResolveNoticeIn, alreadyMsg: string, failVerb: string) => {
    if (pending || unavailable || inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      const intent = JSON.stringify({ agentId, noticeId: notice.id, body });
      if (attempt.current?.intent !== intent) {
        attempt.current = { intent, key: newOperationKey() };
      }
      // Retain this intent's key after failure so a manual retry recovers its
      // original reply. A changed action/body/target starts a distinct intent.
      await api.resolveNotice(agentId, notice.id, body, attempt.current.key);
      setReply("");
      onResolved?.();
    } catch (e: unknown) {
      const msg = errMsg(e);
      const conflict = msg.includes("409");
      setError(conflict ? alreadyMsg : t("failed", { verb: failVerb, msg }));
      if (conflict && notice.question) setUnavailable(true);
      setPending(false);
    } finally {
      inFlight.current = false;
    }
  };

  const answer = () => {
    if (notice.question) {
      if (structuredAnswer) void run({ action: "answer", answer: structuredAnswer }, t("noLongerAvailable"), t("submitAnswer"));
      return;
    }
    const text = reply.trim();
    if (!text) return;
    void run({ action: "answer", reply: text }, t("alreadyAnswered"), t("send"));
  };
  const dismiss = () =>
    void run({ action: "dismiss" }, t(notice.question ? "noLongerAvailable" : "alreadyDismissed"), t("dismiss"));

  return (
    <div className={cn("space-y-3 text-[13px]", className)}>
      <div className="space-y-1.5">
        <div className={cn("flex-wrap items-center gap-1.5", FLEX)}>
          <span
            className={cn(
              "shrink-0 rounded px-1 text-xs font-semibold text-white",
              PRIORITY_BG[notice.priority],
            )}
          >
            {notice.priority}
          </span>
          {notice.blocking && (
            <span className="shrink-0 rounded bg-destructive/10 px-1 text-xs font-medium tracking-wide text-destructive uppercase">
              {t("blocking")}
            </span>
          )}
          {showTimestamp ? (
            <>
              <span aria-hidden className="text-[10px] text-muted-foreground">·</span>
              <span className="text-[10px] text-muted-foreground">
                {formatRelative(notice.created_at, undefined, { capitalize: true })}, {formatAbsolute(notice.created_at)}
              </span>
            </>
          ) : null}
          {notice.expire_at ? (
            <>
              <span aria-hidden className="text-[10px] text-muted-foreground">·</span>
              <span className="text-[10px] text-muted-foreground">
                {t("expires", { time: formatRelative(notice.expire_at) })}
              </span>
            </>
          ) : null}
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={dismiss}
            disabled={pending || unavailable}
            aria-label={t("closeQuestion")}
            title={t("closeQuestion")}
            className="ml-auto text-muted-foreground"
          >
            {t("dismiss")}
          </Button>
        </div>
        <h4 className="text-[13px] font-semibold leading-snug break-words"><ChatMarkdown content={notice.title} inline /></h4>
        {notice.content && (
          <div className="text-[13px] break-words text-foreground/85">
            <ChatMarkdown content={notice.content} />
          </div>
        )}
      </div>

      {notice.question ? (
        <NoticeQuestionForm
          question={notice.question}
          pending={pending || unavailable}
          focusAnswer={autoFocus}
          onAnswerChange={setStructuredAnswer}
          submitAction={
            <SendButton
              state={pending ? "sending" : "send"}
              onClick={answer}
              disabled={pending || unavailable || !structuredAnswer}
              aria-label={t("submitAnswer")}
              title={t("submitAnswer")}
            />
          }
        />
      ) : (
        <div className={cn("items-end gap-2", FLEX)}>
          <AutoExpandTextarea
            ref={textareaRef}
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            onSend={answer}
            onKeyDown={(e) => {
              // With an empty reply box, Up/Down cycles the caller's open list to
              // the previous/next notice (the inbox's arrow-key navigation). A
              // non-empty box owns those keys for cursor movement.
              if (onCycle && !reply.trim() && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
                e.preventDefault();
                onCycle(e.key === "ArrowUp" ? -1 : 1);
              }
            }}
            placeholder={t("answerPlaceholder")}
            disabled={pending}
            aria-label={t("replyTo", { title: notice.title })}
            className={cn("min-h-[3.25rem] rounded border border-input bg-background px-2 py-1.5 font-mono text-xs focus:border-primary disabled:opacity-50", FLEX_1)}
          />
          <SendButton
            state={pending ? "sending" : "send"}
            onClick={answer}
            disabled={pending || !reply.trim()}
            aria-label={t("sendAnswer")}
          />
        </div>
      )}

      {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}
    </div>
  );
}
