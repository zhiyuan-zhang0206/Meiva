"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Textarea } from "@/components/ui/textarea";
import type { NoticeAnswer, NoticeQuestion } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";

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
  submitAction,
}: {
  submitAction: ReactNode;
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

  const inputClass = cn("min-h-9 resize-none py-2 font-mono text-[13px] leading-snug md:text-[13px] pointer-coarse:text-base md:pointer-coarse:text-base", FLEX_1);
  if (question.type === "text") {
    return (
      <div className={cn("items-end gap-2", FLEX)}>
        <Textarea
          id={fieldId}
          aria-label={t("openQuestion")}
          ref={textRef}
          rows={1}
          focusVisible={false}
          value={draft.text}
          onChange={(e) => setDraft({ ...draft, text: e.target.value })}
          placeholder={t("openPlaceholder")}
          disabled={pending}
          className={inputClass}
        />
        {submitAction}
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
  const optionClass = cn(FLEX, "-mx-2 cursor-pointer items-start gap-2 rounded px-2 py-1.5 text-[13px] leading-snug hover:bg-muted/40 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring");

  return (
    <div className="space-y-3">
      <fieldset disabled={pending} className="disabled:opacity-60">
        <legend className="sr-only">{single ? t("singleSelect") : t("multiSelect")}</legend>
        {question.options.map((option, index) => {
          const selected = draft.optionIds.includes(option.id);
          return (
            <label key={option.id} className={optionClass}>
              <input
                type={single ? "radio" : "checkbox"}
                name={fieldId}
                checked={selected}
                onChange={(e) => choose(option.id, e.target.checked)}
                className="mt-0.5 size-3.5 shrink-0 accent-primary"
              />
              <span className={cn("break-words", MIN_W_0)}>
                <span className={selected ? "font-medium" : undefined}><span className="text-muted-foreground">{index + 1}.</span> {option.label}</span>
                {option.description && <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">{option.description}</span>}
              </span>
            </label>
          );
        })}

      </fieldset>
      <div className={cn("items-end gap-2", FLEX)}>
        <Textarea
          id={`${fieldId}-comment`}
          aria-label={t("comment")}
          rows={1}
          focusVisible={false}
          value={draft.comment}
          onChange={(e) => setDraft({ ...draft, comment: e.target.value })}
          disabled={pending}
          placeholder={t("comment")}
          className={inputClass}
        />
        {submitAction}
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
