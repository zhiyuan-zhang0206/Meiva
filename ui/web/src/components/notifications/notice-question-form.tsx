"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Textarea } from "@/components/ui/textarea";
import type { NoticeAnswer, NoticeQuestion } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";

interface Draft {
  optionIds: string[];
  text: string;
  comment: string;
}

function answerFromDraft(question: NoticeQuestion, draft: Draft): NoticeAnswer | null {
  const comment = draft.comment.trim();
  if (question.type === "text") {
    return draft.text.trim() ? { text: draft.text.trim() } : null;
  }
  const count = draft.optionIds.length;
  if (!count || (question.type === "single_select" && count !== 1)) return null;
  return {
    option_ids: question.options.filter((o) => draft.optionIds.includes(o.id)).map((o) => o.id),
    ...(comment ? { comment } : {}),
  };
}

/** Numbered choices share their frozen order with server replies and history. */
export function NoticeQuestionForm({
  question,
  pending,
  focusAnswer,
  onAnswerChange,
}: {
  question: NoticeQuestion;
  pending: boolean;
  focusAnswer: boolean;
  onAnswerChange: (answer: NoticeAnswer | null) => void;
}) {
  const t = useTranslations("noticeDetail");
  const fieldId = useId();
  const textRef = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState<Draft>({
    optionIds: [], text: "", comment: "",
  });

  useEffect(() => {
    onAnswerChange(answerFromDraft(question, draft));
  }, [question, draft, onAnswerChange]);

  useEffect(() => {
    if (focusAnswer) textRef.current?.focus();
  }, [focusAnswer]);

  if (question.type === "text") {
    return (
      <div className="space-y-1.5">
        <label htmlFor={fieldId} className="text-xs font-medium">{t("openQuestion")}</label>
        <Textarea
          id={fieldId}
          ref={textRef}
          value={draft.text}
          onChange={(e) => setDraft({ ...draft, text: e.target.value })}
          placeholder={t("openPlaceholder")}
          disabled={pending}
          className="min-h-20 rounded-md py-1.5 text-[13px] md:text-[13px] pointer-coarse:text-base md:pointer-coarse:text-base"
        />
      </div>
    );
  }

  const single = question.type === "single_select";
  const choose = (id: string, checked: boolean) => {
    setDraft({
      ...draft,
      optionIds: single ? [id] : checked ? [...draft.optionIds, id] : draft.optionIds.filter((v) => v !== id),
    });
  };
  const optionClass = cn(FLEX, "cursor-pointer items-start gap-2 rounded-md border px-2.5 py-2 text-[13px] leading-snug transition-colors has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring");

  return (
    <div className="space-y-2.5">
      <fieldset disabled={pending} className="space-y-1.5 disabled:opacity-60">
        <legend className="mb-1.5 text-xs text-muted-foreground">{single ? t("singleSelect") : t("multiSelect")}</legend>
        {question.options.map((option, index) => {
          const selected = draft.optionIds.includes(option.id);
          return (
            <label key={option.id} className={cn(optionClass, selected ? "border-primary bg-primary/5 ring-1 ring-primary/20" : "border-border hover:border-muted-foreground/40 hover:bg-muted/40")}>
              <input
                type={single ? "radio" : "checkbox"}
                name={fieldId}
                checked={selected}
                onChange={(e) => choose(option.id, e.target.checked)}
                className="mt-0.5 size-3.5 shrink-0 accent-primary"
              />
              <span className={cn("break-words", MIN_W_0)}>
                <span className="font-medium">{index + 1}. {option.label}</span>
                {option.description && <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">{option.description}</span>}
              </span>
            </label>
          );
        })}

      </fieldset>
      <div className="space-y-1.5">
        <label htmlFor={`${fieldId}-comment`} className="text-xs font-medium">{t("comment")}</label>
        <Textarea
          id={`${fieldId}-comment`}
          value={draft.comment}
          onChange={(e) => setDraft({ ...draft, comment: e.target.value })}
          disabled={pending}
          placeholder={t("commentPlaceholder")}
          className="min-h-14 rounded-md py-1.5 text-[13px] md:text-[13px] pointer-coarse:text-base md:pointer-coarse:text-base"
        />
      </div>
    </div>
  );
}

/** Read-only answer history keeps the complete numbering context for Other. */
export function NoticeQuestionSummary({ question, answer }: { question: NoticeQuestion; answer: NoticeAnswer | null | undefined }) {
  const t = useTranslations("noticeDetail");
  if (question.type === "text") {
    return (
      <div className="space-y-3 text-sm">
        {answer?.text && <p className="whitespace-pre-wrap break-words">{answer.text}</p>}
        {answer?.comment && <p className="whitespace-pre-wrap break-words">{t("comment")}: {answer.comment}</p>}
      </div>
    );
  }
  return (
    <div className="space-y-3 text-sm">
      <ol className="space-y-2" aria-label={t("originalOptions")}>
        {question.options.map((option, index) => (
          <li key={option.id} className={cn("break-words", answer?.option_ids?.includes(option.id) ? "font-medium text-foreground" : "text-muted-foreground")}>
            {index + 1}. {option.label}
            {option.description && <span className="mt-1 block font-normal text-muted-foreground">{option.description}</span>}
            {answer?.option_ids?.includes(option.id) && <span className="ml-2 text-xs">{t("selected")}</span>}
          </li>
        ))}
        {question.allow_other && <li>{question.options.length + 1}. {t("other")}{answer?.other_text && <span className="ml-2 text-xs">{t("selected")}</span>}</li>}
      </ol>
      {answer?.other_text && <p className="whitespace-pre-wrap break-words">{t("other")}: {answer.other_text}</p>}
      {answer?.comment && <p className="whitespace-pre-wrap break-words"><span className="text-muted-foreground">{t("comment")}: </span>{answer.comment}</p>}
    </div>
  );
}
