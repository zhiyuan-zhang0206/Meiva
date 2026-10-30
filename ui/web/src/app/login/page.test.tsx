// meiva:note
// LoginPageView tests — session-aware shell around LoginForm: loading shows the "Checking session"
// placeholder, an already-authenticated session redirects home, otherwise the page renders the
// form. The shell takes the session status and the redirect as required props; the form is real.
// meiva:link ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md
import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-context";
import { renderWithBrowser } from "@/test-support/render";

import { LoginPageView } from "./page";

// meiva:note
// The form authenticates through the real `AuthProvider` reading the fake transport the test
// door mounts, so the shell's status and the form's login are both real values here.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md
function withAuth({ children }: { children: React.ReactNode }) {
  return <AuthProvider>{children}</AuthProvider>;
}

function renderPage(status: "loading" | "authenticated" | "unauthenticated") {
  return renderWithBrowser(<LoginPageView status={status} redirectHome={redirectHome} />, {
    wrapper: withAuth,
  });
}

const redirectHome = vi.fn();

beforeEach(() => {
  redirectHome.mockReset();
});

afterEach(cleanup);

describe("LoginPage", () => {
  it("shows a checking-session placeholder while loading", () => {
    renderPage("loading");
    expect(screen.getByText(/Checking session/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("renders the login form when unauthenticated", () => {
    renderPage("unauthenticated");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(redirectHome).not.toHaveBeenCalled();
  });

  it("redirects home when already authenticated", async () => {
    renderPage("authenticated");
    await waitFor(() => expect(redirectHome).toHaveBeenCalled());
  });
});
