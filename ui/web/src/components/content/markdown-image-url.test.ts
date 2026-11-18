import { describe, expect, it } from "vitest";
import {
  markdownImageUrl,
  resolveWorkspaceFile,
  resolveWorkspaceReference,
} from "./markdown-image-url";

describe("workspace Markdown image references", () => {
  it("routes an explicit workspace to its owner, independent of the selected agent", () => {
    expect(markdownImageUrl("/Users/person/.meiva/workspaces/7632/accent/image.png", 405, "http://gateway"))
      .toBe("http://gateway/api/agents/7632/files/raw?path=accent%2Fimage.png");
    expect(markdownImageUrl("file:///home/person/.meiva/workspaces/9/image.png", null, ""))
      .toBe("/api/agents/9/files/raw?path=image.png");
  });
  it("uses the selected agent for relative and cwd files, encoding the request path", () => {
    expect(markdownImageUrl("./plot with space.png", 405, ""))
      .toBe("/api/agents/405/files/raw?path=.%2Fplot%20with%20space.png");
    expect(markdownImageUrl("/tmp/image.png", 405, ""))
      .toBe("/api/agents/405/files/raw?path=%2Ftmp%2Fimage.png");
  });
  it("preserves remote, uploaded and explicit document resource URLs", () => {
    for (const src of ["https://example.com/a.png", "//example.com/a.png", "/api/agents/1/uploads/key/0", "/api/agents/1/to-user/resource?file=a.png", "blob:image", "#anchor"]) {
      expect(markdownImageUrl(src, 405, "http://gateway")).toBe(src);
    }
    expect(markdownImageUrl("./a.png", null, "")).toBe("./a.png");
  });
  it("retains traversal for the server's workspace boundary to reject", () => {
    expect(markdownImageUrl("/home/x/.meiva/workspaces/9/../10/private.png", 405, ""))
      .toBe("/api/agents/9/files/raw?path=..%2F10%2Fprivate.png");
  });

  it("extracts explicit workspace owner and subpath for the file viewer", () => {
    expect(
      resolveWorkspaceFile(
        "/Users/zhiyuan-output/.meiva/workspaces/7765/deliver/user-positions-before-after.png",
        7516,
      ),
    ).toEqual({
      ownerAgentId: 7765,
      requestPath: "deliver/user-positions-before-after.png",
    });
    expect(resolveWorkspaceFile("src/main.py", 7516)).toEqual({
      ownerAgentId: 7516,
      requestPath: "src/main.py",
    });
  });

  it("resolves relative references inside a viewed Markdown file without crossing workspace boundaries", () => {
    const crossDoc = "/Users/zhiyuan-output/.meiva/workspaces/7765/deliver/REPORT.md";
    expect(resolveWorkspaceReference(crossDoc, "user-positions-before-after.png")).toBe(
      "/Users/zhiyuan-output/.meiva/workspaces/7765/deliver/user-positions-before-after.png",
    );
    expect(resolveWorkspaceReference(crossDoc, "../notes.md")).toBe(
      "/Users/zhiyuan-output/.meiva/workspaces/7765/notes.md",
    );
    expect(resolveWorkspaceReference(crossDoc, "../../7766/secret.md")).toBe(
      "/Users/zhiyuan-output/.meiva/workspaces/7765/../7766/secret.md",
    );
    expect(
      resolveWorkspaceReference(crossDoc, "/Users/other/.meiva/workspaces/8888/other.md"),
    ).toBe("/Users/other/.meiva/workspaces/8888/other.md");
    expect(resolveWorkspaceReference("docs/guides/spec.md", "../intro.md")).toBe("docs/intro.md");
  });
});
