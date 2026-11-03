"use client";

import { AlertCircle } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";

export interface InlineErrorProps {
  children: ReactNode;
  detail?: ReactNode;
  onRetry?: () => void;
  retryLabel?: ReactNode;
  retryDisabled?: boolean;
  role?: "alert" | "status";
  surface?: "plain" | "floating";
  className?: string;
  "data-testid"?: string;
  retryTestId?: string;
}

export function InlineError({
  children,
  detail,
  onRetry,
  retryLabel = "Retry",
  retryDisabled = false,
  role = "alert",
  surface = "plain",
  className,
  "data-testid": testId,
  retryTestId,
}: InlineErrorProps) {
  const content = (
    <>
      <AlertCircle aria-hidden className="size-3.5 shrink-0 text-destructive" />
      <span className="break-words">{children}</span>
      {detail ? <span className="text-destructive/80 break-words">{detail}</span> : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={retryDisabled}
          data-testid={retryTestId}
          className="shrink-0 rounded border border-destructive/35 px-2 py-0.5 text-xs font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50"
        >
          {retryLabel}
        </button>
      ) : null}
    </>
  );

  if (surface === "floating") {
    return (
      <div
        role={role}
        data-testid={testId}
        className={cn(
          "rounded-md border border-destructive/40 bg-popover px-3 py-2 text-xs text-destructive shadow-md",
          FLEX,
          "flex-wrap items-center gap-2",
          className,
        )}
      >
        {content}
      </div>
    );
  }

  return (
    <div
      role={role}
      data-testid={testId}
      className={cn(
        FLEX,
        "flex-wrap items-center gap-2 text-xs text-destructive",
        className,
      )}
    >
      {content}
    </div>
  );
}
