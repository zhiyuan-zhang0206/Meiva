"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { ChatMarkdown } from "@/components/content/markdown";
import { Button } from "@/components/ui/button";
import { AutoExpandTextarea } from "@/components/ui/auto-expand-textarea";
import { SendButton } from "@/components/ui/send-button";
import { newOperationKey } from "@/lib/transport/operation-key";
import { errMsg } from "@/lib/contracts/errors";
import type { NoticeAnswer, OpenNotice, ResolveNoticeIn } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1 } from "@/lib/layout/layout";

import { NoticeQuestionForm } from "./notice-question-form";
import { useApi } from "@/lib/transport/transport-context";

export function OpenNoticeDetail({
  agentId,
  notice,
  onResolved,
  autoFocus = false,
  className,
}: {
  agentId: number;
  notice: OpenNotice;
  onResolved?: () => void;
  autoFocus?: boolean;
  className?: string;
}) {
  const client = useApi();
  const t = useTranslations("noticeDetail");
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
      await client.resolveNotice(agentId, notice.id, body, attempt.current.key);
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

  // meiva:note
  // One answer path for a question, reached from the Submit button and from Enter
  // in the answer or choice Note field. NoticeQuestionForm builds the answer from its
  // own draft, so Enter does not wait for the round trip through structuredAnswer.
  // meiva:link ui/web/src/docs/frontend-components/enter-to-send.md
  const submitAnswer = (answer: NoticeAnswer) =>
    void run({ action: "answer", answer }, t("noLongerAvailable"), t("submitAnswer"));

  const answer = () => {
    if (notice.question) {
      if (structuredAnswer) submitAnswer(structuredAnswer);
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
        <h4 className="text-[13px] font-semibold leading-snug break-words"><ChatMarkdown content={notice.title} inline /></h4>
        {notice.content && (
          <div className="text-[13px] break-words text-foreground/85">
            <ChatMarkdown content={notice.content} />
          </div>
        )}
      </div>

      <div className="space-y-1.5">
        {notice.question ? (
          <NoticeQuestionForm
            question={notice.question}
            pending={pending || unavailable}
            focusAnswer={autoFocus}
            onAnswerChange={setStructuredAnswer}
            onSubmit={submitAnswer}
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
        <div>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={dismiss}
            disabled={pending || unavailable}
            aria-label={t("closeQuestion")}
            title={t("closeQuestion")}
            className="-ml-2 text-muted-foreground"
          >
            {t("dismiss")}
          </Button>
        </div>
      </div>

      {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}
    </div>
  );
}
