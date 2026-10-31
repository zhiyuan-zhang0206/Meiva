// meiva:note
// LoginForm tests — submit wiring + error surfacing. The form reads the real AuthProvider, so the
// outcome a test wants is the gateway's answer: the fake transport's `login` resolves, or rejects
// with the ApiError the real client raises (401/403 credentials, 429 rate limit), or rejects with
// anything else (backend down). Verifies: the button stays disabled until a password is typed;
// submit forwards (username defaulting to "admin", trimmed); each failure reads as its own message
// and a down backend never reads as "Invalid password".

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-context";
import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import { ApiError } from "@/lib/transport/api";
import { ApiTestProvider, resetTransport, transport } from "@/test-support/fake-api";

import { LoginForm } from "./login-form";

function renderForm() {
  return render(
    <ApiTestProvider>
      <BrowserProvider browser={fakeBrowser()}>
        <AuthProvider>
          <LoginForm />
        </AuthProvider>
      </BrowserProvider>
    </ApiTestProvider>,
  );
}

beforeEach(() => {
  resetTransport();
  // meiva:note
  // The provider checks the session on mount; this file is about what a submit does.
  transport.checkAuth.mockResolvedValue({ authenticated: false });
});

afterEach(cleanup);

function typePassword(value: string) {
  fireEvent.change(screen.getByLabelText("Password"), { target: { value } });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("LoginForm", () => {
  it("disables submit until a password is entered", () => {
    renderForm();
    const button = screen.getByRole("button", { name: "Sign in" });
    expect(button).toHaveProperty("disabled", true);
    typePassword("secret");
    expect(button).toHaveProperty("disabled", false);
  });

  it("submits with a trimmed password, defaulting username to admin", async () => {
    transport.login.mockResolvedValue(undefined);
    renderForm();
    typePassword("  secret  ");
    submit();
    await waitFor(() => expect(transport.login).toHaveBeenCalledWith("admin", "secret"));
  });

  it("submits with the typed username when provided", async () => {
    transport.login.mockResolvedValue(undefined);
    renderForm();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "  alice  " } });
    typePassword("pw");
    submit();
    await waitFor(() => expect(transport.login).toHaveBeenCalledWith("alice", "pw"));
  });

  // meiva:note
  // A real 401/403: the gateway is up and rejected the credentials.
  it("shows 'Invalid password' when the gateway rejects the credentials", async () => {
    transport.login.mockRejectedValue(new ApiError(401, "bad credentials", {}));
    renderForm();
    typePassword("wrong");
    submit();
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Invalid password");
  });

  // meiva:note
  // Fetch reject, timeout or 5xx: the backend never evaluated the password, so this must not read
  // as "Invalid password" — the bug this guards is a down backend masquerading as a wrong one.
  it("shows a connection error, not 'Invalid password', when the request never gets an answer", async () => {
    transport.login.mockRejectedValue(new Error("network down"));
    renderForm();
    typePassword("secret");
    submit();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Connection failed/);
    expect(alert.textContent).not.toMatch(/Invalid password/);
  });

  // meiva:note
  // A 429 means the gateway's login rate limiter locked this IP; the credential was never
  // evaluated, so this reads as "try again later", never as a wrong password or a connection
  // failure.
  it("shows a rate-limit message, not 'Invalid password', when the gateway is rate limiting", async () => {
    transport.login.mockRejectedValue(new ApiError(429, "too many attempts", {}));
    renderForm();
    typePassword("secret");
    submit();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Too many failed attempts/);
    expect(alert.textContent).not.toMatch(/Invalid password/);
    expect(alert.textContent).not.toMatch(/Connection failed/);
  });

  it("does not call login when the password is only whitespace", () => {
    renderForm();
    typePassword("   ");
    // Button is disabled; submitting the form directly hits the early return.
    fireEvent.submit(screen.getByLabelText("Password").closest("form")!);
    expect(transport.login).not.toHaveBeenCalled();
  });
});
