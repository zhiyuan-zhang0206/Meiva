import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, within } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeAll, describe, expect, it, vi, beforeEach } from "vitest";
import type { FakeBrowser } from "@/lib/browser/testing";
import { fakeBrowser } from "@/lib/browser/testing";
import type { ToUserDocument } from "@/lib/contracts/types";
import { ApiError } from "@/lib/transport/api";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
import { preloadChatMarkdown } from "@/components/content/markdown";
import { BAR_DIVIDER_CLASS, BAR_MIN_HEIGHT_CLASS } from "@/lib/layout/layout";
import { ToUserDocumentView } from "./to-user-document";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});

beforeAll(async () => {
  await preloadChatMarkdown();
});

const id = "a".repeat(64);
const linkId = "b".repeat(64);
function document(content: string, modifiedAt: string | null = null): ToUserDocument {
  return { content, version: "1", modified_at: modifiedAt, legacy_html: false, resources: {
    "/tmp/plot.png": { id, name: "plot.png", markdown: false, version: "asset-v1" },
    "details.md": { id: linkId, name: "details.md", markdown: true, version: "doc-v1" },
    "file:///tmp/file.pdf": { id: "c".repeat(64), name: "file.pdf", markdown: false, version: null },
  } };
}
const HOME = "http://fake.local/?agent_id=7";

function show(
  value: ToUserDocument,
  { mobile = false, linked, onInspect, onCollapse, onNavigate, inspecting = false, browser = fakeBrowser({ href: HOME }) }: { mobile?: boolean; linked?: ToUserDocument | Error; onInspect?: () => void; onCollapse?: () => void; onNavigate?: () => void; inspecting?: boolean; browser?: FakeBrowser } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  if (linked !== undefined) {
    if (!(linked instanceof Error)) qc.setQueryData(["to-user", 7, linkId], linked);
    transport.getToUser.mockImplementation(() =>
      linked instanceof Error ? Promise.reject(linked) : Promise.resolve(linked),
    );
  } else {
    transport.getToUser.mockImplementation(() => new Promise(() => undefined));
  }
  return renderWithBrowser(
    <QueryClientProvider client={qc}>
      <ToUserDocumentView
        agentId={7}
        document={value}
        failure={null}
        pending={false}
        refresh={vi.fn()}
        mobile={mobile}
        inspecting={inspecting}
        onInspect={onInspect}
        onCollapse={onCollapse}
        onNavigate={onNavigate}
      />
    </QueryClientProvider>,
    { browser },
  );
}

function chain(browser: FakeBrowser): string[] {
  const where = browser.navigation.location();
  if (where === null) throw new Error("the fake page always has an address");
  return new URL(where.href).searchParams.getAll("reference");
}

function travel(browser: FakeBrowser, to: string): void {
  act(() => browser.traverse(to));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("To user Markdown", () => {
  it("renders local images and file URIs through authenticated resource URLs", () => {
    show(document("![plot](/tmp/plot.png)\n\n[report](file:///tmp/file.pdf)"));
    expect(screen.getByRole("img").getAttribute("src")).toContain(`/api/agents/7/to-user/resource?reference=${id}&v=asset-v1`);
    expect(screen.getByRole("link", { name: "report" }).getAttribute("href")).toContain("reference=" + "c".repeat(64));
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
    expect(screen.getByRole("img", { name: "raw plot" }).getAttribute("src")).toContain(`/api/agents/7/to-user/resource?reference=${id}&v=asset-v1`);
  });
  it("keeps modifier clicks as ordinary resource downloads", () => {
    const { browser } = show(document("[Details](details.md)"));
    const link = screen.getByRole("link", { name: "Details" });
    link.addEventListener("click", (event) => event.preventDefault());
    fireEvent.click(link, { ctrlKey: true });
    expect(chain(browser)).toEqual([]);
    expect(transport.getToUser).not.toHaveBeenCalled();
  });
  it("draws the panel title rule on the shared column title bar", () => {
    show(document("body"));
    const tokens = screen.getByTestId("document-bar").className.split(/\s+/);
    expect(tokens).toEqual(expect.arrayContaining(["relative", BAR_MIN_HEIGHT_CLASS]));
    expect(tokens).toEqual(expect.arrayContaining(BAR_DIVIDER_CLASS.split(" ")));
    expect(tokens).not.toContain("border-b");
  });
  it("shows migration guidance when only a legacy HTML document remains", () => {
    show({ content: null, version: null, modified_at: null, legacy_html: true });
    expect(screen.getByText("legacyHtml")).toBeTruthy();
  });
});

describe("To user navigation history", () => {
  const root = () => document("[Details](details.md)");
  const nested = () => document("# Nested document");

  it("puts the opened document in the URL and follows Back and Forward to it", () => {
    const onNavigate = vi.fn();
    const { browser } = show(root(), { linked: nested(), onNavigate });
    expect(onNavigate).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    expect(onNavigate).toHaveBeenCalledTimes(2);
    expect(chain(browser)).toEqual([linkId]);
    expect(screen.getByRole("heading", { name: "Nested document" })).toBeTruthy();

    travel(browser, "http://fake.local/?agent_id=7");
    expect(onNavigate).toHaveBeenCalledTimes(3);
    expect(chain(browser)).toEqual([]);
    expect(screen.getByRole("link", { name: "Details" })).toBeTruthy();

    travel(browser, `http://fake.local/?agent_id=7&reference=${linkId}`);
    expect(onNavigate).toHaveBeenCalledTimes(4);
    expect(chain(browser)).toEqual([linkId]);
    expect(screen.getByRole("heading", { name: "Nested document" })).toBeTruthy();
  });

  it("restores a shared link's chain but not a chain left by another agent", () => {
    const shared = fakeBrowser({ href: `http://fake.local/?agent_id=7&reference=${linkId}` });
    show(root(), { linked: nested(), browser: shared });
    expect(screen.getByRole("heading", { name: "Nested document" })).toBeTruthy();

    travel(shared, `http://fake.local/?agent_id=8&reference=${linkId}`);
    expect(screen.queryByRole("heading", { name: "Nested document" })).toBeNull();
  });

  it("walks browser history from the panel's back control, falling back to the parent", () => {
    const { browser } = show(root(), { linked: nested() });
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    fireEvent.click(screen.getByRole("button", { name: "back" }));
    expect(browser.actions.backs).toBe(1);

    cleanup();
    const deep = fakeBrowser({ href: `http://fake.local/?agent_id=7&reference=${linkId}` });
    show(root(), { linked: nested(), browser: deep });
    fireEvent.click(screen.getByRole("button", { name: "back" }));
    expect(deep.actions.backs).toBe(0);
    expect(chain(deep)).toEqual([]);
    expect(screen.getByRole("link", { name: "Details" })).toBeTruthy();
  });
});

describe("To user document bar", () => {
  it("orders the bar as shared context, then the update time with refresh, with the inspector alone at the right", () => {
    show(document("# Status", "2026-10-17T06:00:00Z"), { onInspect: vi.fn() });
    const status = screen.getByTestId("document-status");
    const bar = screen.getByTestId("document-bar");
    expect(bar.firstElementChild?.getAttribute("aria-label")).toBe("home");
    expect(screen.getByText("sharedContext").nextElementSibling).toBe(status);
    expect(within(status).getByText("updated")).toBeTruthy();
    expect(within(status).getByRole("button", { name: "refresh" })).toBeTruthy();
    const trigger = screen.getByTestId("open-inspector");
    expect(trigger.parentElement?.className).toContain("ml-auto");
    expect(bar.lastElementChild).toBe(trigger.parentElement);
  });

  it("names the mode in the bar and turns the magnifier into its exit", () => {
    // meiva:note
    // Inspector is a mode: while it is on, the bar says what the panel is
    // showing and the same button that entered it is the way out (user
    // 2026-10-20), instead of a second close control inside the panel.
    // meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
    show(document("# Status"), { onInspect: vi.fn(), inspecting: true });
    expect(screen.getByText("title")).toBeTruthy();
    expect(screen.queryByText("sharedContext")).toBeNull();
    const trigger = screen.getByTestId("open-inspector");
    expect(trigger.getAttribute("aria-pressed")).toBe("true");
    expect(trigger.getAttribute("aria-label")).toBe("closeInspector");
  });

  it("leaves only the mode's own controls in the bar while the inspector is on", () => {
    // meiva:note
    // The bar is the inspector mode's one control surface: the document's own
    // navigation (home, back) and its refresh read the document body the mode
    // hides, so they leave with it (user 2026-10-20).
    // meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
    show(document("# Status", "2026-10-17T06:00:00Z"), {
      onInspect: vi.fn(),
      onCollapse: vi.fn(),
      inspecting: true,
    });
    expect(screen.queryByRole("button", { name: "home" })).toBeNull();
    expect(screen.queryByRole("button", { name: "refresh" })).toBeNull();
    expect(screen.queryByTestId("document-status")).toBeNull();
    expect(screen.getByTestId("open-inspector")).toBeTruthy();
    expect(screen.getByTestId("context-panel-collapse")).toBeTruthy();
  });

  it("marks the inspector toggle with a selected background and gives it no hover wash", () => {
    // meiva:note
    // bg-accent is 97% lightness in the light theme — a pressed state painted
    // with it reads as unpressed (user 2026-10-20). Hovering the toggle changes
    // nothing at all: the pointer cursor is the only hover feedback (user
    // 2026-10-21).
    // meiva:link ui/web/src/components/context/to-user-document.tsx
    show(document("hello"), { onInspect: vi.fn(), inspecting: true });
    const classes = screen.getByTestId("open-inspector").className.split(/\s+/);
    expect(classes).toContain("bg-foreground/20");
    expect(classes.filter((name) => name.startsWith("hover:bg-"))).toEqual([]);
  });

  it("leaves the idle inspector toggle without a hover wash too", () => {
    show(document("hello"), { onInspect: vi.fn() });
    const classes = screen.getByTestId("open-inspector").className.split(/\s+/);
    expect(classes.filter((name) => name.startsWith("hover:bg-"))).toEqual([]);
  });

  it("labels the document section in the mobile reading view", () => {
    show(document("# Status"), { mobile: true });
    expect(within(screen.getByTestId("document-bar")).getByText("sharedContext")).toBeTruthy();
    expect(screen.getByTestId("document-status")).toBeTruthy();
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

it("offers the inspector trigger only when the panel wires it, and reports presses", () => {
  const onInspect = vi.fn();
  const view = show(document("hello"));
  expect(screen.queryByTestId("open-inspector")).toBeNull();
  view.unmount();

  show(document("hello"), { onInspect });
  fireEvent.click(screen.getByTestId("open-inspector"));
  expect(onInspect).toHaveBeenCalledOnce();
});

describe("shared-context column collapse", () => {
  it("closes the column from its own bar when the host offers it", () => {
    const onCollapse = vi.fn();
    show(document("hello"), { onCollapse });
    fireEvent.click(screen.getByTestId("context-panel-collapse"));
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it("renders no collapse control without the host callback or on mobile", () => {
    show(document("hello"));
    expect(screen.queryByTestId("context-panel-collapse")).toBeNull();
    cleanup();
    show(document("hello"), { onCollapse: vi.fn(), mobile: true });
    expect(screen.queryByTestId("context-panel-collapse")).toBeNull();
  });
});

describe("To user refusals", () => {
  // meiva:note
  // One test, two classes: a refusal the server called permanent reads differently from
  // one it called temporary, and only the permanent one hands over the file itself — the
  // copy follows the response's code, never the status (user 2026-10-28).
  // meiva:link ui/web/src/docs/frontend-state/shared-context.meiva.okf.md
  it("tells a permanent refusal from one worth retrying, and hands over the file", async () => {
    show(document("[Details](details.md)"), {
      linked: new ApiError(413, "too large", { code: "to_user_document_too_large", retryable: false }),
    });
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    const refused = (await screen.findByTestId("document-failure")).textContent;
    expect(refused).toContain("tooLarge");
    expect(screen.getByRole("link", { name: "openRaw" }).getAttribute("href")).toContain(
      `/api/agents/7/to-user/resource?reference=${linkId}`,
    );

    cleanup();
    window.history.replaceState(null, "", "/?agent_id=7");
    show(document("[Details](details.md)"), {
      linked: new ApiError(503, "unavailable", { code: "http_503", retryable: true }),
    });
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    const retryable = (await screen.findByTestId("document-failure")).textContent;
    expect(retryable).toContain("unavailable");
    expect(retryable).not.toContain("tooLarge");
    expect(screen.queryByRole("link", { name: "openRaw" })).toBeNull();
  });
});
