// meiva:note
// AuthGuard tests — the redirect gate, one case per auth status x address branch: loading shows the
// spinner, the login page renders its children whatever the auth state, an authenticated surface
// renders, and an unauthenticated one off the login page replaces the address with /login. The
// guard reads the real AuthProvider, which asks the fake transport's `checkAuth`: a test answers
// that one call with the status it wants, so every branch runs through the provider the app mounts
// instead of a replaced module, and the address comes from the navigation port.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-context";
import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import { NavigationProvider } from "@/lib/browser/navigation/navigation-provider";
import { fakeNavigation } from "@/lib/browser/navigation/testing";
import { ApiTestProvider, resetTransport, transport } from "@/test-support/fake-api";

import { AuthGuard } from "./auth-guard";

const navigation = fakeNavigation();

// meiva:note
// The guard reads its address from the navigation value, not from the router module: this wrapper is
// the only thing between the test and the component.
function renderGuard(ui: React.ReactElement) {
  return render(
    <ApiTestProvider>
      <BrowserProvider browser={fakeBrowser()}>
        <AuthProvider>
          <NavigationProvider navigation={navigation}>{ui}</NavigationProvider>
        </AuthProvider>
      </BrowserProvider>
    </ApiTestProvider>,
  );
}

function guard(child: string) {
  return (
    <AuthGuard>
      <span data-testid="child">{child}</span>
    </AuthGuard>
  );
}

beforeEach(() => {
  resetTransport();
  navigation.moves.length = 0;
  navigation.moveTo("/");
});

afterEach(cleanup);

describe("AuthGuard", () => {
  it("shows a spinner while loading and does not redirect", () => {
    transport.checkAuth.mockReturnValue(new Promise(() => undefined));
    const { container } = renderGuard(guard("app"));
    expect(screen.queryByTestId("child")).toBeNull();
    expect(container.querySelector(".animate-spin")).toBeTruthy();
    expect(navigation.moves).toEqual([]);
  });

  it("renders children when authenticated", async () => {
    transport.checkAuth.mockResolvedValue({ authenticated: true });
    renderGuard(guard("app"));
    expect(await screen.findByTestId("child")).toBeTruthy();
    expect(navigation.moves).toEqual([]);
  });

  it("redirects to /login when unauthenticated off the login page", async () => {
    transport.checkAuth.mockResolvedValue({ authenticated: false });
    const { container } = renderGuard(guard("app"));
    await waitFor(() =>
      // meiva:note
      // A replace, not a push: the refused page must not come back with the back button.
      expect(navigation.moves).toEqual([{ kind: "replace", href: "/login" }]),
    );
    expect(screen.queryByTestId("child")).toBeNull();
    expect(container.firstChild).toBeNull();
  });

  it("renders children on the login page without redirecting (unauthenticated)", async () => {
    navigation.moveTo("/login");
    transport.checkAuth.mockResolvedValue({ authenticated: false });
    renderGuard(guard("login"));
    expect(await screen.findByTestId("child")).toBeTruthy();
    expect(navigation.moves).toEqual([]);
  });

  it("renders children on the login page when authenticated", async () => {
    navigation.moveTo("/login");
    transport.checkAuth.mockResolvedValue({ authenticated: true });
    renderGuard(guard("login"));
    expect(await screen.findByTestId("child")).toBeTruthy();
  });
});
