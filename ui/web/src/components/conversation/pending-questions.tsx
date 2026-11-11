"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, MessageCircleQuestion } from "lucide-react";
import { useId, useState } from "react";
import { useTranslations } from "next-intl";

import { QuestionsForm } from "@/components/notifications/questions-form";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { QuestionItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, OVERFLOW_HIDDEN, QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
import { dropOpenQuestions, replaceAgentQuestions } from "@/lib/notifications/use-questions";

// meiva:note
// The queue opens expanded by default when questions are waiting, while the
// collapsed bar occupies one stable row above the composer. Answers expand over
// the timeline without resizing it; hidden mounted forms retain every draft.
// meiva:link ui/web/src/docs/frontend-state/shared-context/questions.md
export function PendingQuestions({ questions, failed, maxWidthCss }: {
  questions: readonly QuestionItem[];
  failed: boolean;
  maxWidthCss?: string;
}) {
  const t = useTranslations("questionDetail");
  const cache = useQueryClient();
  const [expanded, setExpanded] = useState(true);
  const contentId = useId();
  if (!questions.length && !failed) return null;
  return <section data-testid="pending-questions" aria-label={t("pendingQuestions", { count: questions.length })}
    className="relative z-30 mx-auto w-full px-4 pb-1"
    style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}>
    <div className={cn("relative border border-border bg-background shadow-lg", expanded ? "rounded-b-xl" : "rounded-xl")}>
      {failed && <p role="status" className="px-3 py-2 text-xs text-muted-foreground">{t("questionsUnavailable")}</p>}
      {questions.length > 0 && <>
        <button type="button" aria-expanded={expanded} aria-controls={contentId}
          onClick={() => setExpanded(!expanded)} className={cn(FLEX, "w-full items-center gap-2 px-3 py-2.5 text-left text-sm font-medium hover:bg-muted/40")}>
          <MessageCircleQuestion aria-hidden className="size-4 shrink-0 text-muted-foreground" />
          <span className="grow">{t("pendingQuestions", { count: questions.length })}</span>
          {expanded ? <ChevronDown aria-hidden className="size-4" /> : <ChevronUp aria-hidden className="size-4" />}
        </button>
        <div id={contentId} hidden={!expanded} className={cn(OVERFLOW_HIDDEN, "absolute -inset-x-px bottom-full rounded-t-xl border border-b-0 border-border bg-background shadow-lg")}>
          <ScrollArea viewportClassName={cn(QUESTION_REGION_MAX_HEIGHT_CLASS, "overscroll-y-contain")}>
            <QuestionsForm
              agentId={questions[0].agent_id}
              questions={questions}
              onSettled={(ids) => dropOpenQuestions(cache, ids)}
              onChanged={(open) => replaceAgentQuestions(cache, questions[0].agent_id, open)}
            />
          </ScrollArea>
        </div>
      </>}
    </div>
  </section>;
}
