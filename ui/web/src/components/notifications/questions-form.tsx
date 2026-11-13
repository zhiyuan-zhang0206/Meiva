"use client";

import { ChevronLeft, ChevronRight, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ChatMarkdown } from "@/components/content/markdown";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { errMsg } from "@/lib/contracts/errors";
import type { AnswerQuestions, QuestionAnswer, QuestionItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0, QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
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

  const bodyFor = (override?: { id: number; answer: QuestionAnswer }): AnswerQuestions | null => {
    const results: AnswerQuestions["results"] = [];
    for (const q of ordered) {
      const draft = draftOf(q.id);
      if (draft.skipped) {
        results.push({ id: q.id, skip: true });
        continue;
      }
      const answer = override?.id === q.id ? override.answer : answerFromDraft(q.question, draft);
      if (!answer) return null;
      results.push({ id: q.id, answer, skip: false });
    }
    return { expected_ids: ordered.map((q) => q.id), results };
  };

  const submit = async (override?: { id: number; answer: QuestionAnswer }) => {
    const body = bodyFor(override);
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
      setError(t("failed", { verb: t("submitAnswers"), msg: errMsg(e) }));
      setPending(false);
    } finally {
      inFlight.current = false;
    }
  };

  const index = Math.max(0, ordered.findIndex((q) => q.id === selectedId));
  const question = ordered.at(index);
  if (!question) return null;
  const ready = bodyFor() !== null;

  return (
    <form
      className={cn("text-[13px]", className)}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <ScrollArea key={question.id} viewportClassName={cn(QUESTION_REGION_MAX_HEIGHT_CLASS, "overscroll-y-contain")}>
        <QuestionRow
          question={question}
          draft={draftOf(question.id)}
          pending={pending}
          onDraftChange={(draft) => setQuestionDraft(agentId, question.id, draft)}
          onEnter={(answer) => void submit({ id: question.id, answer })}
        />
      </ScrollArea>
      <div className="border-t border-border/60 px-4 py-2">
        {changed && <p role="status" className="mb-2 text-xs text-muted-foreground">{t("questionsChanged")}</p>}
        {error && <p role="alert" className="mb-2 text-xs text-destructive">{error}</p>}
        <div className={cn("items-center justify-between gap-2", FLEX)}>
          {ordered.length > 1 ? (
            <nav aria-label={t("questionNavigation")} className={cn("items-center gap-1", FLEX)}>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("previousQuestion")}
                disabled={pending || index === 0}
                onClick={() => setSelectedId(ordered[index - 1].id)}
              >
                <ChevronLeft aria-hidden="true" />
              </Button>
              <span aria-live="polite" className="min-w-10 text-center text-xs tabular-nums text-muted-foreground">
                {t("questionPosition", { current: index + 1, total: ordered.length })}
              </span>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("nextQuestion")}
                disabled={pending || index === ordered.length - 1}
                onClick={() => setSelectedId(ordered[index + 1].id)}
              >
                <ChevronRight aria-hidden="true" />
              </Button>
            </nav>
          ) : <span />}
          <Button
            type="submit"
            disabled={pending || !ready}
            title={ready ? undefined : t("answerAllHint")}
            aria-busy={pending}
          >
            {pending && <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />}
            {t("submitAnswers")}
          </Button>
        </div>
      </div>
    </form>
  );
}

function QuestionRow({
  question,
  draft,
  pending,
  onDraftChange,
  onEnter,
}: {
  question: QuestionItem;
  draft: QuestionDraft;
  pending: boolean;
  onDraftChange: (draft: QuestionDraft) => void;
  onEnter: (answer: QuestionAnswer) => void;
}) {
  const t = useTranslations("questionDetail");
  const skipped = draft.skipped;
  return (
    <article aria-label={question.title} className="space-y-3 p-4">
      <div className={cn("items-start justify-between gap-2", FLEX)}>
        <div className={cn(MIN_W_0, "space-y-1.5")}>
          <h4 className="text-sm font-semibold leading-snug break-words"><ChatMarkdown content={question.title} inline /></h4>
          {question.content && (
            <div className="text-[13px] break-words text-foreground/85">
              <ChatMarkdown content={question.content} />
            </div>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          aria-pressed={skipped}
          onClick={() => onDraftChange({ ...draft, skipped: !skipped })}
          disabled={pending}
          className="shrink-0 text-muted-foreground"
        >
          {skipped ? t("undoSkip") : t("skip")}
        </Button>
      </div>
      {skipped && <p className="text-xs text-muted-foreground">{t("skipped")}</p>}
      <div hidden={skipped}>
        <QuestionForm
          question={question.question}
          pending={pending}
          focusAnswer={false}
          draft={draft}
          onDraftChange={onDraftChange}
          onSubmit={onEnter}
          submitAction={null}
        />
      </div>
    </article>
  );
}
