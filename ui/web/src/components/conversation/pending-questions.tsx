"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, MessageCircleQuestion } from "lucide-react";
import { useId, useState } from "react";
import { useTranslations } from "next-intl";

import { OpenNoticeDetail } from "@/components/notifications/open-notice-detail";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { NoticeItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, OVERFLOW_HIDDEN, QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
import { dropOpenNotices } from "@/lib/notifications/use-notices";

// meiva:note
// The collapsed bar has one stable row above the composer. Answers expand over
// the timeline without resizing it; hidden mounted forms retain every draft.
// meiva:link ui/web/src/docs/frontend-state/shared-context/questions.meiva.okf.md
export function PendingQuestions({ questions, failed, maxWidthCss }: {
  questions: readonly NoticeItem[];
  failed: boolean;
  maxWidthCss?: string;
}) {
  const t = useTranslations("noticeDetail");
  const cache = useQueryClient();
  const [expanded, setExpanded] = useState(false);
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
            <div className="divide-y divide-border">
              {questions.map((question) => <article key={question.id} aria-label={question.title} className="p-3">
                <OpenNoticeDetail agentId={question.agent_id} notice={question} onResolved={() => {
                  dropOpenNotices(cache, [question.id]);
                  void cache.invalidateQueries({ queryKey: ["notices-resolved"] });
                }} />
              </article>)}
            </div>
          </ScrollArea>
        </div>
      </>}
    </div>
  </section>;
}
