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
import { FLEX, QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
import { newOperationKey } from "@/lib/transport/operation-key";
import { useApi } from "@/lib/transport/transport-context";

import { EMPTY_DRAFT, type QuestionDraft } from "@/lib/state/question-drafts-plan";
import { useStore } from "@/lib/state/store";

import { QuestionForm, answerFromDraft } from "./question-form";

// meiva:note
// One reply for every open question of one agent, in question id order: each question is answered
// or skipped, and nothing is sent until all of them are. The submission names the open set the
// form was built from; when the server's set differs (a question was asked or closed meanwhile)
// nothing is settled, the caller takes the latest set, and every draft and skip of a question that
// is still open stays. Drafts and skips live in the UI store (memory only), so they also stay when the
// user goes to another agent and comes back; they leave it when the question is answered or is no
// longer open. Controls are only disabled while a submission is in flight, never after a failure, so a
// retry reuses the same operation key for the same content.
// meiva:link meiva_builtins/plugins/meiva_fleet/docs/questions.md
export function QuestionsForm({
  agentId,
  questions,
  onSettled,
  onChanged,
  className,
}: {
  agentId: number;
  questions: readonly QuestionItem[];
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

  const bodyFor = (): AnswerQuestions | null => {
    const results: AnswerQuestions["results"] = [];
    for (const q of ordered) {
      const draft = draftOf(q.id);
      if (draft.skipped) {
        results.push({ id: q.id, skip: true });
        continue;
      }
      const answer = answerFromDraft(q.question, draft);
      if (!answer) return null;
      results.push({ id: q.id, answer, skip: false });
    }
    return { expected_ids: ordered.map((q) => q.id), results };
  };

  const submit = async () => {
    const body = bodyFor();
    if (!body || pending || inFlight.current) return;
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

  const index = Math.max(0, ordered.findIndex((q) => q.id === selectedId));
  const question = ordered.at(index);
  if (!question) return null;
  const nextQuestion = ordered.at(index + 1);
  const draft = draftOf(question.id);
  const ready = bodyFor() !== null;

  return (
    <div className={cn("text-[13px]", className)}>
      <ScrollArea key={question.id} viewportClassName={cn(QUESTION_REGION_MAX_HEIGHT_CLASS, "overscroll-y-contain")}>
        <QuestionRow
          question={question}
          draft={draft}
          pending={pending}
          onDraftChange={(nextDraft) => setQuestionDraft(agentId, question.id, nextDraft)}
        />
      </ScrollArea>
      <div className="px-4 pb-3 pt-1">
        {changed && <p role="status" className="mb-2 text-xs text-muted-foreground">{t("questionsChanged")}</p>}
        {error && <p role="alert" className="mb-2 text-xs text-destructive">{error}</p>}
        <div className={cn("min-h-8 items-center justify-end gap-1 pl-8", FLEX)}>
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
              <span className="grid min-w-9 text-center text-xs tabular-nums text-muted-foreground">
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
            variant="ghost"
            size="sm"
            aria-pressed={draft.skipped}
            onClick={() => {
              setQuestionDraft(agentId, question.id, { ...draft, skipped: !draft.skipped });
              if (!draft.skipped && nextQuestion) setSelectedId(nextQuestion.id);
            }}
            disabled={pending}
            className="text-muted-foreground"
          >
            <span className="grid place-items-center">
              <span
                aria-hidden="true"
                className="invisible col-start-1 row-start-1 before:content-[attr(data-width-label)]"
                data-width-label={t("undoSkip")}
              />
              <span className="col-start-1 row-start-1">{draft.skipped ? t("undoSkip") : t("skip")}</span>
            </span>
          </Button>
          <Button
            size="sm"
            className="relative disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100"
            disabled={pending || !ready}
            title={ready ? undefined : t("answerAllHint")}
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
  const t = useTranslations("questionDetail");
  const skipped = draft.skipped;
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
      {skipped ? (
        <p className="text-xs text-muted-foreground">{t("skipped")}</p>
      ) : (
        <QuestionForm
          question={question.question}
          pending={pending}
          draft={draft}
          onDraftChange={onDraftChange}
        />
      )}
    </article>
  );
}
