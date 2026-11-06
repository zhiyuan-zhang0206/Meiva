"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Circle } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import type { ResolveUserMessage, UserMessage } from "@/lib/contracts/types";
import { useApi } from "@/lib/transport/transport-context";
import { useDocumentVisible } from "@/lib/layout/use-document-visible";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";
import { errMsg } from "@/lib/contracts/errors";

export function MessageActions({ message }: { message: UserMessage }) {
  const t = useTranslations("toUser");
  const api = useApi();
  const cache = useQueryClient();
  const visible = useDocumentVisible();
  const anchor = useRef<HTMLDivElement>(null);
  const operations = useRef(new Map<string, string>());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  return <div ref={anchor} className="text-xs text-muted-foreground" data-publication-id={message.id}>
    <div className={cn(FLEX, "items-center gap-2")}>
      {message.read_at === null && <button
        className="relative inline-flex items-center justify-center after:absolute after:-inset-1 after:content-['']"
        type="button"
        disabled={busy}
        title={t("deliveryUnread")}
        aria-label={t("deliveryUnread")}
        onClick={() => void resolve({ action: "read" })}
      ><Circle className="size-2 fill-current" aria-hidden="true" /></button>}
    </div>
    {error && <p role="alert" className="absolute bottom-full right-0 mb-1 max-w-xs rounded border bg-background px-2 py-1">{error}</p>}
  </div>;
}
