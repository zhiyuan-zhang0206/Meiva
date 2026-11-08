import { RotateCw } from "lucide-react";
import { useTranslations } from "next-intl";

import type { AgentRow } from "@/lib/contracts/types";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";
import type { PendingAction } from "@/lib/agents/use-agents";

export function RowActions({
  agent,
  pending,
  hasReplyIndicator,
  onResurrect,
}: {
  agent: AgentRow;
  pending: PendingAction | undefined;
  hasReplyIndicator: boolean;
  onResurrect: () => void;
}) {
  const t = useTranslations("agentRow");
  const wrapperCls = cn(
    "absolute top-1/2 -translate-y-1/2 items-center gap-0.5",
    FLEX,
    hasReplyIndicator ? "right-9" : "right-3",
  );

  if (agent.status === "terminated") {
    return (
      <div className={wrapperCls}>
        {pending === "resurrecting" ? (
          <ActionSpinner color="text-muted-foreground" />
        ) : (
          <button
            type="button"
            onClick={onResurrect}
            disabled={pending !== undefined}
            className="p-0.5 rounded hover:bg-accent hover:text-foreground text-muted-foreground disabled:opacity-30"
            aria-label={t("resurrectConfirm", { id: agent.agent_id })}
          >
            <RotateCw className="size-3" />
          </button>
        )}
      </div>
    );
  }

  if (pending === "restarting") {
    return (
      <div className={wrapperCls}>
        <ActionSpinner color="text-muted-foreground" />
      </div>
    );
  }
  if (pending === "terminating") {
    return (
      <div className={wrapperCls}>
        <ActionSpinner color="text-destructive" />
      </div>
    );
  }
  if (pending === "compacting") {
    return (
      <div className={wrapperCls}>
        <ActionSpinner color="text-muted-foreground" />
      </div>
    );
  }

  return null;
}

function ActionSpinner({ color }: { color: string }) {
  return (
    <span className="p-0.5 inline-flex items-center justify-center">
      <Spinner className={cn("size-3", color)} />
    </span>
  );
}
