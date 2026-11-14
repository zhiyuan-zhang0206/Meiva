"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronUp } from "lucide-react";
import { useId } from "react";
import { useTranslations } from "next-intl";

import { QuestionsForm } from "@/components/notifications/questions-form";
import type { QuestionItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";
import { dropOpenQuestions, replaceAgentQuestions } from "@/lib/notifications/use-questions";
import { useStore } from "@/lib/state/store";

// meiva:note
// The panel reserves a single row above the composer so opening it can
// reveal the current question without resizing the timeline.
// meiva:link ui/web/src/docs/frontend-state/shared-context/questions.md
export function PendingQuestions({ agentId, questions, failed, maxWidthCss }: {
  agentId: number;
  questions: readonly QuestionItem[];
  failed: boolean;
  maxWidthCss?: string;
}) {
  const t = useTranslations("questionDetail");
  const cache = useQueryClient();
  const expanded = useStore((s) => !s.questionPanelCollapsed[agentId]);
  const toggleQuestionPanel = useStore((s) => s.toggleQuestionPanel);
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
        {failed && <p role="status" className="px-4 py-2.5 text-xs text-muted-foreground">{t("questionsUnavailable")}</p>}
        {questions.length > 0 && (
          <>
            <div id={contentId}>
              <QuestionsForm
                agentId={agentId}
                questions={questions}
                expanded={expanded}
                onSettled={(ids) => dropOpenQuestions(cache, ids)}
                onChanged={(open) => replaceAgentQuestions(cache, agentId, open)}
              />
            </div>
            <button
              type="button"
              aria-label={label}
              aria-expanded={expanded}
              aria-controls={contentId}
              onClick={() => toggleQuestionPanel(agentId)}
              className={cn(
                "absolute bottom-1 left-4 h-8 items-center gap-2 rounded-lg px-2 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2",
                FLEX,
              )}
            >
              <ChevronUp
                aria-hidden
                className={cn("size-4", expanded && "rotate-180")}
              />
              {!expanded && <span>{label}</span>}
            </button>
          </>
        )}
      </div>
    </section>
  );
}
