import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeAll, describe, expect, it, vi, beforeEach } from "vitest";
import { fakeBrowser } from "@/lib/browser/testing";
import type { ToUserDocument } from "@/lib/contracts/types";
import { ApiError } from "@/lib/transport/api";
import { WallClockProvider } from "@/lib/transport/transport-context";

import { preloadChatMarkdown } from "@/components/content/markdown";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { ToUserDocumentView } from "./to-user-document";

import { resetTransport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});

beforeAll(async () => {
  await preloadChatMarkdown();
});

// meiva:note
// The files the document links to: a workspace-relative Markdown document and image, and a file
// outside the workspace by its absolute path. Each opens as a tab of the panel.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
const plot = "reports/plot.png";
const details = "reports/details.md";
const report = "/tmp/file.pdf";
function document(content: string, modifiedAt: string | null = null): ToUserDocument {
  return { content, version: "1", modified_at: modifiedAt, legacy_html: false, resources: {
    "/tmp/plot.png": { file: plot, name: "plot.png", markdown: false, version: "asset-v1" },
    "details.md": { file: details, name: "details.md", markdown: true, version: "doc-v1" },
    "file:///tmp/file.pdf": { file: report, name: "file.pdf", markdown: false, version: null },
  } };
}
const HOME = "http://fake.local/?agent_id=7";

function show(value: ToUserDocument | undefined, { failure = null }: { failure?: unknown } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  return renderWithBrowser(
    <WallClockProvider wallNow={() => Date.now()}>
      <QueryClientProvider client={qc}>
        <ToUserDocumentView agentId={7} document={value} failure={failure} pending={false} />
      </QueryClientProvider>
    </WallClockProvider>,
    { browser: fakeBrowser({ href: HOME }) },
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useWorkspaceFileStore.setState({ activeFile: null, tabs: {}, recent: [], navigator: null });
});

describe("To user Markdown", () => {
  it("renders local images and file URIs through authenticated resource URLs", () => {
    show(document("![plot](/tmp/plot.png)\n\n[report](file:///tmp/file.pdf)"));
    expect(screen.getByRole("img").getAttribute("src")).toContain(`/api/agents/7/to-user/resource?file=${plot}&v=asset-v1`);
    expect(screen.getByRole("link", { name: "report" }).getAttribute("href")).toContain("file=/tmp/file.pdf");
  });
  it("keeps the shared theme, strips hostile HTML and unregistered filesystem links", () => {
    const { container } = show(document('# Status\n\n<style>body{color:red}</style>\n\n<script>alert(1)</script>\n\n[secret](/private/file)\n\n[site](https://example.com)'));
    expect(screen.getByRole("heading", { name: "Status" })).toBeTruthy();
    expect(container.querySelector(".chat-md.font-sans")).toBeTruthy();
    expect(container.querySelector(".chat-md")!.querySelector("iframe,style,script")).toBeNull();
    expect(screen.queryByRole("link", { name: "secret" })).toBeNull();
    expect(screen.getByText("secret")).toBeTruthy();
    expect(screen.getByRole("link", { name: "site" }).getAttribute("target")).toBe("_blank");
  });
  it("renders the safe raw-HTML subset, including registered local images", () => {
    const { container } = show(document('<details><summary>More</summary>body</details>\n\n<img src="/tmp/plot.png" alt="raw plot">'));
    expect(container.querySelector("details > summary")?.textContent).toBe("More");
    expect(container.querySelector("details")?.textContent).toContain("body");
    expect(screen.getByRole("img", { name: "raw plot" }).getAttribute("src")).toContain(`/api/agents/7/to-user/resource?file=${plot}&v=asset-v1`);
  });
  it("keeps modifier clicks as ordinary resource downloads", () => {
    show(document("[Details](details.md)"));
    const link = screen.getByRole("link", { name: "Details" });
    link.addEventListener("click", (event) => event.preventDefault());
    fireEvent.click(link, { ctrlKey: true });
    expect(useWorkspaceFileStore.getState().activeFile).toBeNull();
  });
  // meiva:note
  // Every linked file opens as a tab, a Markdown document included (user ruling 2026-11-21: the
  // panel's home is its first tab, so the document keeps no chain of its own).
  it("opens a linked Markdown document and any other linked file as a tab", () => {
    show(document("[Details](details.md)\n\n[report](file:///tmp/file.pdf)"));
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    expect(useWorkspaceFileStore.getState().activeFile).toEqual({ agentId: 7, path: details, lineRange: undefined });
    fireEvent.click(screen.getByRole("link", { name: "report" }));
    expect(useWorkspaceFileStore.getState().activeFile).toEqual({ agentId: 7, path: report, lineRange: undefined });
    expect(useWorkspaceFileStore.getState().tabs[7].map((tab) => tab.path)).toEqual([details, report]);
  });
  it("is only the reading area: no bar, home, back or refresh of its own", () => {
    show(document("body"));
    expect(screen.queryByTestId("document-bar")).toBeNull();
    for (const name of ["Home", "Back", "Refresh"]) expect(screen.queryByRole("button", { name })).toBeNull();
  });
  it("shows migration guidance when only a legacy HTML document remains", () => {
    show({ content: null, version: null, modified_at: null, legacy_html: true });
    expect(screen.getByText("Awaiting Markdown.")).toBeTruthy();
  });
});

it("opens a document image at full size and closes it on Escape", async () => {
  show(document("# Status\n\n![shot](https://example.com/shot.png)"));
  const thumb = await screen.findByTestId("zoomable-image");
  fireEvent.click(thumb);
  const lightbox = await screen.findByTestId("image-lightbox");
  expect(lightbox.getAttribute("role")).toBe("dialog");
  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByTestId("image-lightbox")).toBeNull();
});

describe("To user refusals", () => {
  // meiva:note
  // A refusal the server called permanent reads differently from one it called temporary: the copy
  // follows the response's code, never the status (user 2026-10-28).
  // meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
  it("tells a permanent refusal from one worth retrying", () => {
    show(undefined, {
      failure: new ApiError(413, "too large", { code: "to_user_document_too_large", retryable: false }),
    });
    expect(screen.getByTestId("document-failure").textContent).toContain("too large to show here");
    cleanup();
    show(undefined, {
      failure: new ApiError(503, "unavailable", { code: "http_503", retryable: true }),
    });
    const retryable = screen.getByTestId("document-failure").textContent;
    expect(retryable).toContain("unavailable");
    expect(retryable).not.toContain("too large to show here");
  });
});
