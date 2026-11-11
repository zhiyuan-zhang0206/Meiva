import { ThemeProvider, useTheme } from "next-themes";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, expect, it } from "vitest";
import { renderWithBrowser } from "@/test-support/render";
import { createLazyChunk } from "./lazy-chunk";
import { ChatMarkdown, preloadChatMarkdown } from "./markdown";
import { MermaidRendererProvider, type MermaidRenderer } from "./mermaid-code";

beforeAll(preloadChatMarkdown);
afterEach(cleanup);
const source = "graph TD; A-->B";
const fence = (code: string) => `\`\`\`mermaid\n${code}\n\`\`\``;
function chunk(importModule: () => Promise<MermaidRenderer>) {
  return createLazyChunk({ name: "test-mermaid", importModule, report: () => undefined });
}

it("routes a fence to SVG and keeps its source copy control", async () => {
  let received = "";
  const renderer = chunk(async () => { await Promise.resolve(); return async (_id, code) => {
    await Promise.resolve();
    received = code;
    return '<svg aria-label="diagram"><text>A to B</text></svg>';
  }; });
  const { container } = renderWithBrowser(
    <MermaidRendererProvider value={renderer}><ChatMarkdown content={fence(source)} /></MermaidRendererProvider>,
  );
  await waitFor(() => expect(container.querySelector(".overflow-x-auto svg")).not.toBeNull());
  expect(received).toBe(source);
  expect(container.querySelector("pre")).toBeNull();
  expect(screen.getByRole("button", { name: /copy mermaid code/i })).toBeTruthy();
});

it("shows copyable source while loading", () => {
  const renderer = chunk(() => new Promise(() => undefined));
  const { container } = renderWithBrowser(
    <MermaidRendererProvider value={renderer}><ChatMarkdown content={fence(source)} /></MermaidRendererProvider>,
  );
  expect(container.querySelector("pre code.language-mermaid")?.textContent).toBe(source);
  expect(screen.getByRole("button", { name: /copy mermaid code/i })).toBeTruthy();
});

it.each(["import", "render"])("preserves source after %s rejection", async (failure) => {
  let attempted = false;
  const renderer = chunk(async () => {
    await Promise.resolve();
    if (failure === "import") { attempted = true; throw Error("offline"); }
    return async () => { await Promise.resolve(); attempted = true; throw Error("incomplete diagram"); };
  });
  const { container } = renderWithBrowser(
    <MermaidRendererProvider value={renderer}><ChatMarkdown content={fence("graph TD; A-->")} /></MermaidRendererProvider>,
  );
  await waitFor(() => expect(attempted).toBe(true));
  expect(container.querySelector("pre code")?.textContent).toBe("graph TD; A-->");
});

it("does not load Mermaid for inline code", () => {
  let loaded = false;
  const renderer = chunk(() => { loaded = true; return Promise.resolve(() => Promise.resolve("<svg/>")); });
  renderWithBrowser(
    <MermaidRendererProvider value={renderer}><ChatMarkdown content="`mermaid`" /></MermaidRendererProvider>,
  );
  expect(screen.getByText("mermaid").tagName).toBe("CODE");
  expect(loaded).toBe(false);
});

it("discards a stale render after a streaming edit", async () => {
  let complete: (svg: string) => void = () => undefined;
  const renderer = chunk(() => Promise.resolve<MermaidRenderer>((_id, code) => code === source
    ? new Promise((resolve) => { complete = resolve; })
    : Promise.reject(Error("incomplete"))));
  const view = (code: string) => <MermaidRendererProvider value={renderer}><ChatMarkdown content={fence(code)} /></MermaidRendererProvider>;
  const { container, rerender } = renderWithBrowser(view(source));
  await waitFor(() => expect(renderer.peek()).not.toBeNull());
  rerender(view("graph TD; A-->"));
  complete("<svg/>");
  await waitFor(() => expect(container.querySelector("pre code")?.textContent).toBe("graph TD; A-->"));
  expect(container.querySelector(".overflow-x-auto svg")).toBeNull();
});

it("rerenders diagrams when the resolved theme changes", async () => {
  const themes: boolean[] = [];
  const renderer = chunk(() => Promise.resolve((_id, _code, dark) => {
    themes.push(dark);
    return Promise.resolve(`<svg data-theme="${dark ? "dark" : "light"}"/>`);
  }));
  function ThemeSwitch() {
    const { setTheme } = useTheme();
    return <button onClick={() => setTheme("dark")}>Dark theme</button>;
  }
  const { container } = renderWithBrowser(<ThemeProvider defaultTheme="light" enableSystem={false}>
    <ThemeSwitch />
    <MermaidRendererProvider value={renderer}><ChatMarkdown content={fence(source)} /></MermaidRendererProvider>
  </ThemeProvider>);
  await waitFor(() => expect(container.querySelector('svg[data-theme="light"]')).not.toBeNull());
  fireEvent.click(screen.getByText("Dark theme"));
  await waitFor(() => expect(container.querySelector('svg[data-theme="dark"]')).not.toBeNull());
  expect(themes).toEqual([false, true]);
});
