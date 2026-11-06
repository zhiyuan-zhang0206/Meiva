"use client";

import { useQueryClient } from "@tanstack/react-query";
import { OpenNoticeDetail } from "@/components/notifications/open-notice-detail";
import { NoticeQuestionSummary } from "@/components/notifications/notice-question-form";
import { ChatMarkdown } from "@/components/content/markdown";
import type { NoticeItem } from "@/lib/contracts/types";

export function AskCard({ question }: { question: NoticeItem }) {
  const cache = useQueryClient();
  if (question.resolved_at === null) return <OpenNoticeDetail
    agentId={question.agent_id} notice={question}
    onResolved={() => { void cache.invalidateQueries({ queryKey: ["notices"] }); void cache.invalidateQueries({ queryKey: ["notices-resolved"] }); }}
  />;
  return <article className="space-y-2">
    <ChatMarkdown content={question.title} />
    {question.content && <ChatMarkdown content={question.content} />}
    {question.question && <NoticeQuestionSummary question={question.question} answer={question.answer} />}
  </article>;
}
