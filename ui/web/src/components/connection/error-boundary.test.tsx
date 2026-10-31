// ErrorBoundary resetKey tests — the boundary must be resettable on a
// selection change (e.g. an agent switch) WITHOUT remounting healthy
// children: the inspector's per-agent cache lives in the children's query
// observers, so a `key=` remount would discard it (task #3894).

import { fireEvent, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./error-boundary";

// meiva:note
// The boundary reports a caught error to the sink the shell hands in, so a test states the
// sink and reads what was reported. React's own report of the same error comes from the root,
// not from the boundary: it is stated per render call below (`onCaughtError`), which is also
// what keeps the run's output clean without replacing a global.
function fakeDiagnostics() {
  return {
    warn: vi.fn<(message: string) => void>(),
    error: vi.fn<(message: string, cause: unknown) => void>(),
  };
}

function MountProbe({ onMount }: { onMount: () => void }) {
  useEffect(() => {
    onMount();
  }, [onMount]);
  return <p>probe content</p>;
}

describe("ErrorBoundary resetKey", () => {
  it("changing resetKey while healthy does not remount children", () => {
    let mounts = 0;
    const onMount = () => {
      mounts += 1;
    };
    const view = render(
      <ErrorBoundary diagnostics={fakeDiagnostics()} resetKey={1}>
        <MountProbe onMount={onMount} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("probe content")).toBeTruthy();
    expect(mounts).toBe(1);

    view.rerender(
      <ErrorBoundary diagnostics={fakeDiagnostics()} resetKey={2}>
        <MountProbe onMount={onMount} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("probe content")).toBeTruthy();
    expect(mounts).toBe(1);
  });

  it("changing resetKey clears an error so the incoming subtree renders", async () => {
    let shouldThrow = true;
    function MaybeThrow() {
      if (shouldThrow) {
        throw new Error("boom");
      }
      return <p>recovered</p>;
    }
    const diagnostics = fakeDiagnostics();

    const view = render(
      <ErrorBoundary diagnostics={diagnostics} resetKey={1}>
        <MaybeThrow />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    );
    expect(await screen.findByText("Something went wrong")).toBeTruthy();
    expect(screen.getByText("boom")).toBeTruthy();
    expect(diagnostics.error).toHaveBeenCalledWith(
      expect.stringContaining("[ErrorBoundary] Caught render error: boom"),
      expect.any(Error),
    );

    shouldThrow = false;
    view.rerender(
      <ErrorBoundary diagnostics={diagnostics} resetKey={2}>
        <MaybeThrow />
      </ErrorBoundary>,
    );
    expect(await screen.findByText("recovered")).toBeTruthy();
    expect(screen.queryByText("Something went wrong")).toBeNull();
  });

  it("keeps the fallback without a resetKey change; Retry recovers once the child stops throwing", async () => {
    let shouldThrow = true;
    function MaybeThrow() {
      if (shouldThrow) {
        throw new Error("boom");
      }
      return <p>recovered</p>;
    }
    const diagnostics = fakeDiagnostics();

    render(
      <ErrorBoundary diagnostics={diagnostics}>
        <MaybeThrow />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    );
    expect(await screen.findByText("Something went wrong")).toBeTruthy();

    // Same props, same failure: the boundary stays on its fallback.
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Something went wrong")).toBeTruthy();

    // The child recovers; Retry now gets past it.
    shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("recovered")).toBeTruthy();
    expect(screen.queryByText("Something went wrong")).toBeNull();
    expect(diagnostics.error).toHaveBeenCalledTimes(2);
  });
});
