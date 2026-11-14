"use client";

import { useId, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";

import { ChatMarkdown } from "@/components/content/markdown";
import { Textarea } from "@/components/ui/textarea";
import type { QuestionAnswer, QuestionSpec } from "@/lib/contracts/types";
import type { QuestionDraft } from "@/lib/state/question-drafts-plan";
import { isImeComposition } from "@/lib/input/enter-to-send";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";

export function answerFromDraft(question: QuestionSpec, draft: QuestionDraft): QuestionAnswer | null {
  const comment = draft.comment.trim();
  if (question.type === "text") {
    return draft.text.trim() ? { text: draft.text.trim() } : null;
  }
  // meiva:note
  // Choosing nothing is allowed in both kinds: a comment alone is a whole answer
  // (user ruling 2026-10-21). Submitting still needs one of the two, which is
  // what the disabled button expresses.
  // meiva:link meiva_builtins/plugins/meiva_fleet/docs/questions.md
  const count = draft.optionIds.length;
  if (count > 1 && question.type === "single_select") return null;
  if (count === 0 && !comment) return null;
  return {
    option_ids: question.options.filter((o) => draft.optionIds.includes(o.id)).map((o) => o.id),
    ...(comment ? { comment } : {}),
  };
}

// meiva:note
// Numbered choices share their frozen order with server replies and history.
// Draft fields stay editable until the user activates the shared Submit button.
// meiva:link ui/web/src/docs/frontend-state/shared-context/questions.md
export function QuestionForm({
  question,
  pending,
  draft,
  onDraftChange,
}: {
  question: QuestionSpec;
  pending: boolean;
  draft: QuestionDraft;
  onDraftChange: (draft: QuestionDraft) => void;
}) {
  const t = useTranslations("questionDetail");
  const fieldId = useId();

  const inputClass = cn("min-h-9 text-foreground placeholder:text-muted-foreground/60 resize-none py-2 text-[13px] leading-snug md:text-[13px] pointer-coarse:text-base md:pointer-coarse:text-base", FLEX_1);
  const single = question.type === "single_select";
  const choose = (id: string, checked: boolean) => {
    onDraftChange({
      ...draft,
      optionIds: single
        ? (checked ? [id] : [])
        : checked ? [...draft.optionIds, id] : draft.optionIds.filter((v) => v !== id),
    });
  };
  const optionClass = cn(FLEX, "-mx-2 cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-[13px] leading-snug hover:bg-muted/40 has-[:checked]:bg-muted/50 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring");

  const applySuggestion = (field: "text" | "comment", event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!draft[field] && question.note_suggestion && !pending && !isImeComposition(event)
      && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey
      && (event.key === "Tab" || event.key === "ArrowRight")) {
      event.preventDefault();
      onDraftChange({ ...draft, [field]: question.note_suggestion });
    }
  };

  if (question.type === "text") {
    return (
      <Textarea
        id={fieldId} aria-label={t("openQuestion")} rows={1}
        focusVisible={false} value={draft.text}
        onChange={(e) => onDraftChange({ ...draft, text: e.target.value })}
        onKeyDown={(event) => applySuggestion("text", event)}
        aria-description={question.note_suggestion && !draft.text ? t("suggestionKeys") : undefined}
        placeholder={question.note_suggestion ?? t("openPlaceholder")}
        disabled={pending} className={inputClass}
      />
    );
  }

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
                onClick={() => { if (single && selected) choose(option.id, false); }}
                onKeyDown={(e) => {
                  if (single && selected && (e.key === " " || e.key === "Enter")) {
                    e.preventDefault();
                    choose(option.id, false);
                  }
                }}
                className="mt-0.5 size-3.5 shrink-0 accent-primary"
              />
              <span className={cn("break-words", MIN_W_0)}>
                <span className={selected ? "font-medium" : undefined}><span className="text-muted-foreground">{index + 1}.</span> <ChatMarkdown content={option.label} inline /></span>
                {option.description && <span className="mt-0.5 block text-xs leading-snug text-muted-foreground"><ChatMarkdown content={option.description} inline /></span>}
              </span>
            </label>
          );
        })}
      </fieldset>
      <Textarea
        id={`${fieldId}-comment`} aria-label={t("comment")} rows={1} focusVisible={false}
        value={draft.comment} onChange={(e) => onDraftChange({ ...draft, comment: e.target.value })}
        onKeyDown={(event) => applySuggestion("comment", event)}
        disabled={pending}
        placeholder={question.note_suggestion ?? t("comment")}
        aria-description={question.note_suggestion && !draft.comment ? t("suggestionKeys") : undefined}
        className={inputClass}
      />
    </div>
  );
}
