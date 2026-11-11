"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ChatMarkdown } from "@/components/content/markdown";
import { Button } from "@/components/ui/button";
import { SendButton } from "@/components/ui/send-button";
import { errMsg } from "@/lib/contracts/errors";
import type { AnswerQuestions, QuestionAnswer, QuestionItem } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { newOperationKey } from "@/lib/transport/operation-key";
import { useApi } from "@/lib/transport/transport-context";

import { QuestionForm } from "./question-form";

// meiva:note
// One reply for every open question of one agent, in question id order: each question is answered
// or skipped, and nothing is sent until all of them are. The submission names the open set the
// form was built from; when the server's set differs (a question was asked or closed meanwhile)
// nothing is settled, the caller takes the latest set, and every draft and skip of a question that
// is still open stays. Controls are only disabled while a submission is in flight, never after a
// failure, so a retry reuses the same operation key for the same content.
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
  const [answers, setAnswers] = useState<ReadonlyMap<number, QuestionAnswer | null>>(new Map());
  const [skipped, setSkipped] = useState<ReadonlySet<number>>(new Set());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const inFlight = useRef(false);
  const attempt = useRef<{ intent: string; key: string } | null>(null);

  const setAnswer = useCallback((id: number, answer: QuestionAnswer | null) => {
    setAnswers((prev) => {
      if (prev.get(id) === answer) return prev;
      const next = new Map(prev);
      next.set(id, answer);
      return next;
    });
  }, []);

  const toggleSkip = (id: number) => {
    setSkipped((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  };

  const bodyFor = (override?: { id: number; answer: QuestionAnswer }): AnswerQuestions | null => {
    const results: AnswerQuestions["results"] = [];
    for (const q of ordered) {
      if (skipped.has(q.id)) {
        results.push({ id: q.id, skip: true });
        continue;
      }
      const answer = override?.id === q.id ? override.answer : answers.get(q.id);
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
      onSettled(body.expected_ids);
      setPending(false);
    } catch (e: unknown) {
      setError(t("failed", { verb: t("submitAnswers"), msg: errMsg(e) }));
      setPending(false);
    } finally {
      inFlight.current = false;
    }
  };

  const ready = ordered.length > 0 && bodyFor() !== null;

  return (
    <form
      className={cn("text-[13px]", className)}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="divide-y divide-border">
        {ordered.map((q) => (
          <QuestionRow
            key={q.id}
            question={q}
            skipped={skipped.has(q.id)}
            pending={pending}
            onToggleSkip={() => toggleSkip(q.id)}
            onAnswer={setAnswer}
            onEnter={(answer) => void submit({ id: q.id, answer })}
          />
        ))}
      </div>
      <div className={cn("items-center justify-between gap-3 border-t border-border p-3", FLEX)}>
        <div className={cn(MIN_W_0, "space-y-1")}>
          {changed && <p role="status" className="text-[13px] text-muted-foreground">{t("questionsChanged")}</p>}
          {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}
          {!ready && !error && !changed && <p className="text-xs text-muted-foreground">{t("answerAllHint")}</p>}
        </div>
        <SendButton
          type="submit"
          state={pending ? "sending" : "send"}
          disabled={pending || !ready}
          aria-label={t("submitAnswers")}
          title={t("submitAnswers")}
        />
      </div>
    </form>
  );
}

function QuestionRow({
  question,
  skipped,
  pending,
  onToggleSkip,
  onAnswer,
  onEnter,
}: {
  question: QuestionItem;
  skipped: boolean;
  pending: boolean;
  onToggleSkip: () => void;
  onAnswer: (id: number, answer: QuestionAnswer | null) => void;
  onEnter: (answer: QuestionAnswer) => void;
}) {
  const t = useTranslations("questionDetail");
  const report = useCallback((answer: QuestionAnswer | null) => onAnswer(question.id, answer), [onAnswer, question.id]);
  return (
    <article aria-label={question.title} className="space-y-2 p-3">
      <div className={cn("items-start justify-between gap-2", FLEX)}>
        <div className={cn(MIN_W_0, "space-y-1.5")}>
          <h4 className="text-[13px] font-semibold leading-snug break-words"><ChatMarkdown content={question.title} inline /></h4>
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
          onClick={onToggleSkip}
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
          onAnswerChange={report}
          onSubmit={onEnter}
          submitAction={null}
        />
      </div>
    </article>
  );
}
