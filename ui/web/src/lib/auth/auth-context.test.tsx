// AuthProvider / useAuth tests.
//
// Pins the auth state machine: mount -> checkAuth resolves the initial status;
// login() flips to authenticated on success and reports "invalid-credentials"
// vs. "network-error" on failure depending on what api.login threw; logout()
// always lands on unauthenticated. api is mocked so no network runs.

// meiva:note
// @vitest-environment happy-dom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserProvider } from "../browser/browser-provider";
import { fakeBrowser, type FakeBrowser } from "../browser/testing";
import { ApiError, type Api } from "../transport/api";
import { cacheOnlyApi } from "../transport/tests/api/api-provider";
import { ApiProvider } from "../transport/transport-context";
import { AuthProvider, useAuth } from "./auth-context";
import { setTicketFetchForTest } from "./login-ticket";

const checkAuth = vi.fn<Api["checkAuth"]>();
const login = vi.fn<Api["login"]>();
const logout = vi.fn<Api["logout"]>();
const testApi: Api = {
  ...cacheOnlyApi(),
  checkAuth,
  login,
  logout,
};
// The one-time-link redemption is a plain POST made by the lazily loaded login-ticket
// module; its only collaborator is `fetch`.
const redeemFetch = vi.fn<(input: string | URL | Request, init?: RequestInit) => Promise<Response>>();
const verdict = (status: number) => new Response("{}", { status });
const redeemedTicket = (): unknown => {
  const body = redeemFetch.mock.calls[0][1]?.body;
  return (JSON.parse(typeof body === "string" ? body : "{}") as { ticket?: unknown }).ticket;
};

// meiva:note
// The provider reads the page's address through the browser port, so every test mounts it with a
// fake page: the ticket tests set that page's address instead of arranging a global.
let browser: FakeBrowser;

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BrowserProvider browser={browser}>
      <ApiProvider api={testApi}>
        <AuthProvider>{children}</AuthProvider>
      </ApiProvider>
    </BrowserProvider>
  );
}

beforeEach(() => {
  browser = fakeBrowser();
  checkAuth.mockReset();
  login.mockReset();
  logout.mockReset();
  redeemFetch.mockReset();
  setTicketFetchForTest(redeemFetch);
});

afterEach(() => {
  setTicketFetchForTest(undefined);
  cleanup();
});

describe("AuthProvider mount check", () => {
  it("resolves to authenticated when checkAuth says so", async () => {
    checkAuth.mockResolvedValue({ authenticated: true });
    const { result } = renderHook(() => useAuth(), { wrapper });
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("authenticated"));
  });

  it("resolves to unauthenticated when checkAuth says so", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));
  });

  it("resolves to unauthenticated when checkAuth rejects", async () => {
    checkAuth.mockRejectedValue(new Error("network"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));
  });
});

describe("one-time login link (#ticket=...)", () => {
  it("strips the fragment before redeeming, then trusts the session check", async () => {
    browser = fakeBrowser({ href: "/?tab=1#ticket=abc.def" });
    let hashWhenRedeemed: string | undefined;
    redeemFetch.mockImplementation(() => {
      hashWhenRedeemed = new URL(browser.href()).hash;
      return Promise.resolve(verdict(200));
    });
    checkAuth.mockResolvedValue({ authenticated: true });

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    expect(redeemFetch).toHaveBeenCalledTimes(1);
    expect(redeemFetch.mock.calls[0][0]).toMatch(/\/api\/auth\/login-ticket\/redeem$/);
    expect(redeemedTicket()).toBe("abc.def");
    expect(hashWhenRedeemed).toBe("");
    expect(new URL(browser.href()).hash).toBe("");
    expect(new URL(browser.href()).search).toBe("?tab=1");
  });

  it("keeps unrelated fragment parameters", async () => {
    browser = fakeBrowser({ href: "/#section=x&ticket=t1" });
    redeemFetch.mockResolvedValue(verdict(200));
    checkAuth.mockResolvedValue({ authenticated: true });

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    expect(new URL(browser.href()).hash).toBe("#section=x");
    expect(redeemedTicket()).toBe("t1");
  });

  it("a spent or expired ticket lands on the login page", async () => {
    browser = fakeBrowser({ href: "/#ticket=old" });
    redeemFetch.mockResolvedValue(verdict(401));
    checkAuth.mockResolvedValue({ authenticated: false });

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    expect(new URL(browser.href()).hash).toBe("");
  });

  it("a gateway that never answers leaves the browser on the login page", async () => {
    browser = fakeBrowser({ href: "/#ticket=t" });
    redeemFetch.mockRejectedValue(new TypeError("Failed to fetch"));
    checkAuth.mockResolvedValue({ authenticated: false });

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));
  });

  it("a rejected ticket does not sign out a browser that already has a session", async () => {
    browser = fakeBrowser({ href: "/#ticket=old" });
    redeemFetch.mockResolvedValue(verdict(401));
    checkAuth.mockResolvedValue({ authenticated: true });

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("authenticated"));
  });

  it("without a ticket nothing is redeemed and the check decides as before", async () => {
    browser = fakeBrowser({ href: "/#other=1" });
    checkAuth.mockResolvedValue({ authenticated: false });

    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    expect(redeemFetch).not.toHaveBeenCalled();
    expect(browser.href()).toContain("#other=1");
  });
});

describe("login / logout", () => {
  it("login success flips status to authenticated and returns 'ok'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "secret");
    });
    expect(outcome).toBe("ok");
    expect(login).toHaveBeenCalledWith("admin", "secret");
    expect(result.current.status).toBe("authenticated");
  });

  it("a real 401 rejection returns 'invalid-credentials' and leaves status unauthenticated", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new ApiError(401, "HTTP 401: bad password"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "wrong");
    });
    expect(outcome).toBe("invalid-credentials");
    expect(result.current.status).toBe("unauthenticated");
  });

  it("a 403 rejection also returns 'invalid-credentials'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new ApiError(403, "HTTP 403: forbidden"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "wrong");
    });
    expect(outcome).toBe("invalid-credentials");
  });

  it("a 429 rejection (login rate-limited) returns 'rate-limited'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(
      new ApiError(429, "HTTP 429: too many failed login attempts"),
    );
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "wrong");
    });
    expect(outcome).toBe("rate-limited");
    expect(result.current.status).toBe("unauthenticated");
  });

  it("a network-level failure (fetch reject, no HTTP response) returns 'network-error'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new TypeError("Failed to fetch"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "secret");
    });
    expect(outcome).toBe("network-error");
    expect(result.current.status).toBe("unauthenticated");
  });

  it("a 5xx ApiError also returns 'network-error', not 'invalid-credentials'", async () => {
    checkAuth.mockResolvedValue({ authenticated: false });
    login.mockRejectedValue(new ApiError(503, "HTTP 503: Service Unavailable"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("unauthenticated"));

    let outcome!: string;
    await act(async () => {
      outcome = await result.current.login("admin", "secret");
    });
    expect(outcome).toBe("network-error");
  });

  it("logout flips status to unauthenticated", async () => {
    checkAuth.mockResolvedValue({ authenticated: true });
    logout.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    await act(async () => {
      await result.current.logout();
    });
    expect(logout).toHaveBeenCalled();
    expect(result.current.status).toBe("unauthenticated");
  });

  it("logout lands on unauthenticated even when api.logout throws", async () => {
    checkAuth.mockResolvedValue({ authenticated: true });
    logout.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useAuth(), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("authenticated"));

    // logout()'s finally clears status, but the rejection still propagates.
    await act(async () => {
      await expect(result.current.logout()).rejects.toThrow("boom");
    });
    expect(result.current.status).toBe("unauthenticated");
  });
});

describe("useAuth outside provider", () => {
  it("throws a clear error", () => {
    // Silence the React error-boundary console noise for the expected throw.
    const origError = console.error;
    console.error = () => undefined;
    try {
      expect(() => renderHook(() => useAuth())).toThrow(
        /useAuth must be used inside/,
      );
    } finally {
      console.error = origError;
    }
  });
});
