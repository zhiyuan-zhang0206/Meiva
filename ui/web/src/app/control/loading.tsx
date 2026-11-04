"use client";

import { useTranslations } from "next-intl";

import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export default function Loading() {
  const t = useTranslations("control");
  const common = useTranslations("common");

  return (
    <div className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <header className="shrink-0 border-b border-border px-4 py-2">
        <h1 className="text-sm font-semibold">{t("title")}</h1>
      </header>
      <p role="status" className="px-6 py-6 text-sm text-muted-foreground">
        {common("loading")}
      </p>
    </div>
  );
}
