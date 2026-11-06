"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { ResolveUserMessage, UserMessage } from "@/lib/contracts/types";
import { useApi } from "@/lib/transport/transport-context";
import { useDocumentVisible } from "@/lib/layout/use-document-visible";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { errMsg } from "@/lib/contracts/errors";

export function MessageActions({ message }: { message: UserMessage }) {
  const t = useTranslations("toUser");
  const api = useApi();
  const cache = useQueryClient();
  const visible = useDocumentVisible();
  const { settings } = useUserSettings();
  const anchor = useRef<HTMLDivElement>(null);
  const operations = useRef(new Map<string, string>());
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = message.response === "required" && message.resolved_at === null;
  const canReply = message.response !== "none" && message.resolved_at === null;

  async function resolve(body: ResolveUserMessage) {
    const intent = JSON.stringify(body);
    let key = operations.current.get(intent);
    if (!key) { key = api.newOperationKey(); operations.current.set(intent, key); }
    setBusy(true);
    setError(null);
    try {
      await api.resolveUserMessage(message.agent_id, message.id, body, key);
      await cache.invalidateQueries({ queryKey: ["user-messages", message.agent_id] });
      await cache.invalidateQueries({ queryKey: ["agents"] });
    } catch (failure) { setError(errMsg(failure)); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    if (!visible || message.read_at !== null || !anchor.current || typeof IntersectionObserver === "undefined") return;
    let sent = false;
    const observer = new IntersectionObserver((entries) => {
      if (sent || !entries.some((entry) => entry.isIntersecting)) return;
      sent = true;
      observer.disconnect();
      const body = { action: "read" as const };
      const intent = JSON.stringify(body);
      let key = operations.current.get(intent);
      if (!key) { key = api.newOperationKey(); operations.current.set(intent, key); }
      void api.resolveUserMessage(message.agent_id, message.id, body, key)
        .then(() => cache.invalidateQueries({ queryKey: ["user-messages", message.agent_id] }))
        .catch((failure: unknown) => setError(errMsg(failure)));
    }, { threshold: 1 });
    observer.observe(anchor.current);
    return () => observer.disconnect();
  }, [api, cache, visible, message.id, message.agent_id, message.read_at]);

  return <div ref={anchor} className="mt-2 space-y-2 text-xs text-muted-foreground" data-publication-id={message.id}>
    <div className={cn(FLEX, "items-center gap-2")}>
      {message.read_at === null && <button disabled={busy} onClick={() => void resolve({ action: "read" })}>{t("deliveryUnread")}</button>}
      {pending && <span>{t("deliveryAwaiting")}</span>}
      {message.resolved_at && <span>{t("deliveryResolved")}</span>}
    </div>
    {canReply && <>
      <div className={cn(FLEX, "flex-wrap gap-2")}>{(settings["behavior.replySuggestions"] === true ? message.suggestions : []).map((suggestion) =>
        <button key={suggestion} className="rounded border px-2 py-1 text-left hover:bg-accent" onClick={() => setDraft(suggestion)}>{suggestion}</button>,
      )}</div>
      <textarea aria-label={t("deliveryReply")} placeholder={t("deliveryReply")} value={draft} onChange={(event) => setDraft(event.target.value)} className="w-full rounded border bg-background p-2 text-sm text-foreground" />
      <div className={cn(FLEX, "gap-3")}>
        <button disabled={busy || !draft.trim()} onClick={() => void resolve({ action: "answer", reply: draft })}>{t("deliverySend")}</button>
        <button disabled={busy} onClick={() => void resolve({ action: "dismiss" })}>{t("deliveryDismiss")}</button>
      </div>
    </>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
