"use client";

// Login page — full-screen centered form.

import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useCallback, useEffect } from "react";

import { LoginForm } from "@/components/auth/login-form";
import { useAuth } from "@/lib/auth/auth-context";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

// meiva:note
// `redirectHome` sends an already-authenticated session back to the conversation; the route builds
// it from the router.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export interface LoginPageViewProps {
  status: ReturnType<typeof useAuth>["status"];
  redirectHome: () => void;
}

// meiva:note
// This route is the login page's composition root: it reads the session and the router and hands
// the shell the two things the shell acts on, so a test renders the shell with a status and a spy
// instead of replacing the auth module and `next/navigation`.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
export default function LoginPage() {
  const { status } = useAuth();
  const router = useRouter();
  const redirectHome = useCallback(() => { router.replace("/"); }, [router]);
  return <LoginPageView status={status} redirectHome={redirectHome} />;
}

export function LoginPageView({ status, redirectHome }: LoginPageViewProps) {
  const t = useTranslations("login");

  // meiva:note
  // Redirect to home if already authenticated.
  // meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
  useEffect(() => {
    if (status === "authenticated") {
      redirectHome();
    }
  }, [status, redirectHome]);

  if (status === "loading") {
    return (
      <div className={cn("min-h-screen items-center justify-center", FLEX)}>
        <p className="text-muted-foreground">{t("checkingSession")}</p>
      </div>
    );
  }

  return (
    <div className={cn("min-h-screen items-center justify-center", FLEX)}>
      <div className="w-full max-w-sm space-y-6 rounded-lg border bg-card p-8 shadow-sm">
        <div className="space-y-2 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Meiva</h1>
          <p className="text-sm text-muted-foreground">
            {t("enterPassword")}
          </p>
        </div>
        <LoginForm />
      </div>
    </div>
  );
}
