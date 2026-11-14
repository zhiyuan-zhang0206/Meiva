"use client";

import { ChevronLeft, ChevronRight, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ChatMarkdown } from "@/components/content/markdown";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { errMsg } from "@/lib/contracts/errors";
import type { AnswerQuestions, QuestionItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_H_0, OVERFLOW_HIDDEN, QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
import { newOperationKey } from "@/lib/transport/operation-key";
import { useApi } from "@/lib/transport/transport-context";

import { EMPTY_DRAFT, type QuestionDraft } from "@/lib/state/question-drafts-plan";
import { useStore } from "@/lib/state/store";

import { QuestionForm, answerFromDraft } from "./question-form";

// meiva:note
// Submit on the last page resolves the whole open set in question ID order.
// Empty drafts are sent as ID-only results. If the set changes, the server
// returns the latest questions and the caller keeps drafts that are still open.
// A retry of the same batch reuses its operation key.
// meiva:link meiva_builtins/plugins/meiva_fleet/docs/questions.md
export function QuestionsForm({
  agentId,
  questions,
  expanded = true,
  onSettled,
  onChanged,
  className,
}: {
  agentId: number;
  questions: readonly QuestionItem[];
  expanded?: boolean;
  onSettled: (ids: number[]) => void;
  onChanged: (open: QuestionItem[]) => void;
  className?: string;
}) {
  const client = useApi();
  const t = useTranslations("questionDetail");
  const ordered = useMemo(() => [...questions].sort((a, b) => a.id - b.id), [questions]);
  const drafts = useStore((st) => st.questionDrafts[agentId]);
  const setQuestionDraft = useStore((st) => st.setQuestionDraft);
  const clearQuestionDrafts = useStore((st) => st.clearQuestionDrafts);
  const pruneQuestionDrafts = useStore((st) => st.pruneQuestionDrafts);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const inFlight = useRef(false);
  const attempt = useRef<{ intent: string; key: string } | null>(null);
  const draftOf = (id: number): QuestionDraft => drafts?.[id] ?? EMPTY_DRAFT;

  // meiva:note
  // A question that is no longer open leaves no draft behind.
  const openIds = useMemo(() => ordered.map((q) => q.id), [ordered]);
  useEffect(() => {
    pruneQuestionDrafts(agentId, openIds);
  }, [agentId, openIds, pruneQuestionDrafts]);

  const index = Math.max(0, ordered.findIndex((q) => q.id === selectedId));
  const question = ordered.at(index);
  if (!question) return null;
  const nextQuestion = ordered.at(index + 1);
  const draft = draftOf(question.id);

  const submit = async () => {
    if (nextQuestion || inFlight.current) return;
    const body: AnswerQuestions = {
      expected_ids: openIds,
      results: ordered.map((q) => {
        const answer = answerFromDraft(q.question, draftOf(q.id));
        return answer ? { id: q.id, answer } : { id: q.id };
      }),
    };
    inFlight.current = true;
    setPending(true);
    setError(null);
    setChanged(false);
    try {
      const intent = JSON.stringify({ agentId, body });
      if (attempt.current?.intent !== intent) attempt.current = { intent, key: newOperationKey() };
      const result = await client.answerQuestions(agentId, body, attempt.current.key);
      if (result.kind === "changed") {
        setChanged(true);
        setPending(false);
        onChanged(result.open);
        return;
      }
      clearQuestionDrafts(agentId, body.expected_ids);
      onSettled(body.expected_ids);
      setPending(false);
    } catch (e: unknown) {
      setError(t("failed", { verb: t("submit"), msg: errMsg(e) }));
      setPending(false);
    } finally {
      inFlight.current = false;
    }
  };

  return (
    <div className={cn("text-[13px]", className)}>
      <div
        inert={!expanded}
        aria-hidden={!expanded}
        className={cn(
          "grid motion-safe:transition-[grid-template-rows] motion-safe:duration-150 motion-safe:ease-out",
          expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className={cn(MIN_H_0, OVERFLOW_HIDDEN)}>
          <ScrollArea key={question.id} viewportClassName={cn(QUESTION_REGION_MAX_HEIGHT_CLASS, "overscroll-y-contain")}>
            <QuestionRow
              question={question}
              draft={draft}
              pending={pending}
              onDraftChange={(nextDraft) => setQuestionDraft(agentId, question.id, nextDraft)}
            />
          </ScrollArea>
          {changed && <p role="status" className="px-4 pb-2 text-xs text-muted-foreground">{t("questionsChanged")}</p>}
          {error && <p role="alert" className="px-4 pb-2 text-xs text-destructive">{error}</p>}
        </div>
      </div>
      <div className="px-4 pb-3 pt-1">
        <div
          inert={!expanded}
          aria-hidden={!expanded}
          className={cn(
            "min-h-8 items-center justify-end gap-0.5 pl-8",
            !expanded && "invisible",
            FLEX,
          )}
        >
          {ordered.length > 1 && (
            <nav aria-label={t("questionNavigation")} className={cn("items-center", FLEX)}>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={t("previousQuestion")}
                disabled={pending || index === 0}
                onClick={() => setSelectedId(ordered[index - 1].id)}
              >
                <ChevronLeft aria-hidden="true" />
              </Button>
              <span className="grid min-w-8 text-center text-xs tabular-nums text-muted-foreground">
                <span
                  aria-hidden="true"
                  className="invisible col-start-1 row-start-1 before:content-[attr(data-width-label)]"
                  data-width-label={t("questionPosition", { current: ordered.length, total: ordered.length })}
                />
                <span aria-live="polite" className="col-start-1 row-start-1">
                  {t("questionPosition", { current: index + 1, total: ordered.length })}
                </span>
              </span>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={t("nextQuestion")}
                disabled={pending || !nextQuestion}
                onClick={() => setSelectedId(nextQuestion?.id ?? question.id)}
              >
                <ChevronRight aria-hidden="true" />
              </Button>
            </nav>
          )}
          <Button
            size="sm"
            className="relative px-1.5 disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100"
            disabled={pending || Boolean(nextQuestion)}
            aria-busy={pending}
            aria-label={t("submit")}
            onClick={() => void submit()}
          >
            <span className={pending ? "invisible" : undefined}>{t("submit")}</span>
            {pending && <LoaderCircle aria-hidden="true" className="absolute size-3.5 animate-spin" />}
          </Button>
        </div>
      </div>
    </div>
  );
}

function QuestionRow({
  question,
  draft,
  pending,
  onDraftChange,
}: {
  question: QuestionItem;
  draft: QuestionDraft;
  pending: boolean;
  onDraftChange: (draft: QuestionDraft) => void;
}) {
  return (
    <article aria-label={question.title} className="space-y-3 px-4 pb-3 pt-3.5">
      <div className="space-y-1.5">
        <h4 className="text-[15px] font-medium leading-snug break-words"><ChatMarkdown content={question.title} inline /></h4>
        {question.content && (
          <div className="text-[13px] break-words text-muted-foreground">
            <ChatMarkdown content={question.content} />
          </div>
        )}
      </div>
      <QuestionForm
        question={question.question}
        pending={pending}
        draft={draft}
        onDraftChange={onDraftChange}
      />
    </article>
  );
}
