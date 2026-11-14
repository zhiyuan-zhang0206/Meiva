"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { useId, useState } from "react";
import { useTranslations } from "next-intl";

import { QuestionsForm } from "@/components/notifications/questions-form";
import type { QuestionItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";
import { dropOpenQuestions, replaceAgentQuestions } from "@/lib/notifications/use-questions";

// meiva:note
// The panel reserves a single header row above the composer so opening it can
// reveal the current question without resizing the timeline.
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
  const label = t("pendingQuestions", { count: questions.length });
  return (
    <section
      data-testid="pending-questions"
      aria-label={label}
      className="relative z-30 mx-auto h-11 w-full"
      style={maxWidthCss ? { maxWidth: maxWidthCss } : undefined}
    >
      <div className="absolute inset-x-4 bottom-1 rounded-xl border border-border/80 bg-background">
        {questions.length > 0 && (
          <>
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={contentId}
              onClick={() => setExpanded(!expanded)}
              className={cn(
                "h-[38px] w-full items-center justify-between gap-2 rounded-t-xl px-4 text-left text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2",
                !expanded && "rounded-b-xl",
                FLEX,
              )}
            >
              {label}
              <ChevronDown aria-hidden className={cn("size-3.5 shrink-0", !expanded && "-rotate-90")} />
            </button>
            <div id={contentId} hidden={!expanded} className="border-t border-border/60">
              <QuestionsForm
                agentId={questions[0].agent_id}
                questions={questions}
                onSettled={(ids) => dropOpenQuestions(cache, ids)}
                onChanged={(open) => replaceAgentQuestions(cache, questions[0].agent_id, open)}
              />
            </div>
          </>
        )}
        {failed && <p role="status" className="px-4 py-2.5 text-xs text-muted-foreground">{t("questionsUnavailable")}</p>}
      </div>
    </section>
  );
}
