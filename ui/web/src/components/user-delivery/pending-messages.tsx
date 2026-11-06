"use client";

import { useTranslations } from "next-intl";
import { useUserMessages } from "@/lib/context/use-user-messages";
import { ChatMarkdown } from "@/components/content/markdown";
import { MessageActions } from "./message-actions";

export function PendingUserMessages({ agentId }: { agentId: number }) {
  const t = useTranslations("toUser");
  const query = useUserMessages(agentId);
  const pending = query.data?.items.filter((item) => item.read_at === null) ?? [];
  if (query.error) return <p role="status">{t("deliveryUnavailable")}</p>;
  if (!pending.length) return null;
  return <section className="max-h-72 shrink-0 overflow-y-auto border-t p-4" aria-label={t("deliveryPending")}>
    <p className="mb-3 text-xs font-medium">{t("deliveryPending")}</p>
    {pending.map((message) => <article key={message.id} className="mb-4">
      <ChatMarkdown content={message.content ?? ""} />
      <MessageActions message={message} />
    </article>)}
  </section>;
}
