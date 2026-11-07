import { describe, expect, it } from "vitest";
import {
  isAudioExtension,
  isFilePath,
  isImageExtension,
  isPdfExtension,
  isVideoExtension,
  parseLineRange,
} from "./file-path-utils";

describe("extension predicates", () => {
  it("classifies media extensions correctly", () => {
    expect(isImageExtension(".png")).toBe(true);
    expect(isImageExtension(".svg")).toBe(true);
    expect(isImageExtension(".py")).toBe(false);

    expect(isVideoExtension(".mp4")).toBe(true);
    expect(isVideoExtension(".webm")).toBe(true);
    expect(isVideoExtension(".mp3")).toBe(false);

    expect(isAudioExtension(".mp3")).toBe(true);
    expect(isAudioExtension(".wav")).toBe(true);
    expect(isAudioExtension(".mp4")).toBe(false);

    expect(isPdfExtension(".pdf")).toBe(true);
    expect(isPdfExtension(".txt")).toBe(false);
  });
});

describe("isFilePath", () => {
  it("identifies media and document files (video, audio, pdf, images)", () => {
    expect(isFilePath("demo.mp4").isPath).toBe(true);
    expect(isFilePath("recording.webm").isPath).toBe(true);
    expect(isFilePath("audio.mp3").isPath).toBe(true);
    expect(isFilePath("sound.wav").isPath).toBe(true);
    expect(isFilePath("report.pdf").isPath).toBe(true);
    expect(isFilePath("docs/spec.pdf#L12").isPath).toBe(true);
    expect(isFilePath("icon.ico").isPath).toBe(true);
    expect(isFilePath("image.bmp").isPath).toBe(true);
  });
  it("identifies relative code files", () => {
    const res = isFilePath("src/foo.py");
    expect(res.isPath).toBe(true);
    expect(res.path).toBe("src/foo.py");
    expect(res.filename).toBe("foo.py");
    expect(res.extension).toBe(".py");
    expect(res.lineRange).toBeUndefined();
  });

  it("extracts line range anchors (#L10, #L10-L20, #L10-20)", () => {
    const res1 = isFilePath("src/foo.py#L42");
    expect(res1.isPath).toBe(true);
    expect(res1.path).toBe("src/foo.py");
    expect(res1.lineRange).toBe("42");

    const res2 = isFilePath("gateway/agents/router.py#L10-L20");
    expect(res2.isPath).toBe(true);
    expect(res2.path).toBe("gateway/agents/router.py");
    expect(res2.lineRange).toBe("10-20");

    const res3 = isFilePath("ui/web/src/index.ts#L10-20");
    expect(res3.isPath).toBe(true);
    expect(res3.lineRange).toBe("10-20");
  });

  it("identifies file:// URLs", () => {
    const res = isFilePath("file:///Users/dev/repo/main.py#L5");
    expect(res.isPath).toBe(true);
    expect(res.path).toBe("file:///Users/dev/repo/main.py");
    expect(res.lineRange).toBe("5");
    expect(res.filename).toBe("main.py");
  });

  it("identifies single files with known extensions", () => {
    expect(isFilePath("to_user.md").isPath).toBe(true);
    expect(isFilePath("schema.sql").isPath).toBe(true);
    expect(isFilePath("package.json").isPath).toBe(true);
    expect(isFilePath("test.sh").isPath).toBe(true);
  });

  it("identifies root, relative and home prefixed paths", () => {
    expect(isFilePath("./scripts/build.sh").isPath).toBe(true);
    expect(isFilePath("../config.toml").isPath).toBe(true);
    expect(isFilePath("/var/log/app.log").isPath).toBe(true);
    expect(isFilePath("~/Meiva/AGENTS.md").isPath).toBe(true);
  });

  it("rejects web URLs and dangerous schemes", () => {
    expect(isFilePath("https://example.com/foo.py").isPath).toBe(false);
    expect(isFilePath("http://localhost:3000/app.ts").isPath).toBe(false);
    expect(isFilePath("javascript:alert(1)").isPath).toBe(false);
    expect(isFilePath("mailto:test@example.com").isPath).toBe(false);
  });

  it("rejects plain text and code snippets", () => {
    expect(isFilePath("hello world").isPath).toBe(false);
    expect(isFilePath("const x = 1;").isPath).toBe(false);
    expect(isFilePath("def func():").isPath).toBe(false);
    expect(isFilePath("a + b").isPath).toBe(false);
    expect(isFilePath("").isPath).toBe(false);
    expect(isFilePath("not_a_path").isPath).toBe(false);
  });
});

describe("parseLineRange", () => {
  it("parses single line and range", () => {
    expect(parseLineRange("10")).toEqual({ start: 10, end: 10 });
    expect(parseLineRange("10-20")).toEqual({ start: 10, end: 20 });
    expect(parseLineRange(undefined)).toBeNull();
    expect(parseLineRange("invalid")).toBeNull();
  });
});
