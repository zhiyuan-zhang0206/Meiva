// meiva:note
// AuthGuard tests — the redirect gate, one case per auth status x address branch: loading shows the
// spinner, the login page renders its children whatever the auth state, an authenticated surface
// renders, and an unauthenticated one off the login page replaces the address with /login. useAuth
// is answered by a mock and the address comes from a fake navigation value, so each branch is
// driven directly and the record shows whether the guard pushed or replaced.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuth } from "@/lib/auth/auth-context";
import { NavigationProvider } from "@/lib/browser/navigation/navigation-provider";
import { fakeNavigation } from "@/lib/browser/navigation/testing";

vi.mock("@/lib/auth/auth-context", () => ({
  useAuth: vi.fn(),
}));

import { AuthGuard } from "./auth-guard";

const mockAuth = vi.mocked(useAuth);
const navigation = fakeNavigation();

function setStatus(status: "loading" | "authenticated" | "unauthenticated") {
  mockAuth.mockReturnValue({
    status,
    login: vi.fn(),
    logout: vi.fn(),
  });
}

// meiva:note
// The guard reads its address from the navigation value, not from the router module: this wrapper is
// the only thing between the test and the component.
function renderGuard(ui: React.ReactElement) {
  return render(<NavigationProvider navigation={navigation}>{ui}</NavigationProvider>);
}

beforeEach(() => {
  navigation.moves.length = 0;
  navigation.moveTo("/");
});

afterEach(cleanup);

describe("AuthGuard", () => {
  it("shows a spinner while loading and does not redirect", () => {
    setStatus("loading");
    const { container } = renderGuard(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.queryByTestId("child")).toBeNull();
    expect(container.querySelector(".animate-spin")).toBeTruthy();
    expect(navigation.moves).toEqual([]);
  });

  it("renders children when authenticated", () => {
    setStatus("authenticated");
    renderGuard(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
    expect(navigation.moves).toEqual([]);
  });

  it("redirects to /login when unauthenticated off the login page", () => {
    navigation.moveTo("/");
    setStatus("unauthenticated");
    const { container } = renderGuard(
      <AuthGuard>
        <span data-testid="child">app</span>
      </AuthGuard>,
    );
    expect(screen.queryByTestId("child")).toBeNull();
    expect(container.firstChild).toBeNull();
    // meiva:note
    // A replace, not a push: the refused page must not come back with the back button.
    expect(navigation.moves).toEqual([{ kind: "replace", href: "/login" }]);
  });

  it("renders children on the login page without redirecting (unauthenticated)", () => {
    navigation.moveTo("/login");
    setStatus("unauthenticated");
    renderGuard(
      <AuthGuard>
        <span data-testid="child">login</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
    expect(navigation.moves).toEqual([]);
  });

  it("renders children on the login page when authenticated", () => {
    navigation.moveTo("/login");
    setStatus("authenticated");
    renderGuard(
      <AuthGuard>
        <span data-testid="child">login</span>
      </AuthGuard>,
    );
    expect(screen.getByTestId("child")).toBeTruthy();
  });
});
